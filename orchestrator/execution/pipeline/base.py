"""What every stage uses: the task's status, one agent call, and the checkpoint a stopped task resumes from."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from orchestrator.execution import runner_adapter
from orchestrator.execution import agents as ta
from orchestrator.execution.diagnose import failure_reason
from orchestrator.execution import checks as tc
from orchestrator.execution.budget import (
    release_agent, reserve_agent,
)
from orchestrator.execution.roles import TEST_DIR, role
from orchestrator.records.run_registry import RunRegistryError
from orchestrator.records.task_registry import (
    update_task,
)
from orchestrator.core.attempts import agent_run_id as attempt_agent_run_id


AGENT_METRIC_FIELDS = (
    "status", "duration_seconds", "total_cost_usd", "input_tokens",
    "output_tokens", "num_turns", "model", "session_id",
)
TEST_DIRS = (f"{TEST_DIR}/",)


def _agent_metrics(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in AGENT_METRIC_FIELDS if key in result}


def limit_reason(name: str, reason: str) -> str:
    """An agent stopped by Claude's usage limit: the reason, and what to do about it."""
    return (f"The {name} was stopped by Claude's usage limit: {reason} Nothing is wrong with "
            "the task: once the limit resets, run run-plan again (or run-task --continue), and "
            "it goes on from the stage it reached.")


def _command_fields(result: tc.CommandResult) -> dict[str, Any]:
    return {"exit_code": result.exit_code, "timed_out": result.timed_out,
            "seconds": round(result.seconds, 2), "passed": result.passed,
            "output_tail": result.output_tail}



class PipelineBase:
    """What every stage uses: the task's status, one agent call, and the checkpoint a stopped task resumes from."""

    # -- helpers --
    def set_status(self, status: str, **extra: Any) -> None:
        update_task(self.runs_dir, self.run_id, self.task["id"], {"status": status, **extra})
        self.log.emit("stage", status=status)

    def stopped(self) -> bool:
        """Log why a stage stops the task, if it does; True when it may go on."""
        reasons = ta.problems(self.findings)
        if reasons:
            self.log.emit("stage_stopped", reasons=reasons)
        return not reasons

    def call(self, name: str, prompt: str) -> runner_adapter.RunnerOutcome:
        settings = self.ctx.settings[name]
        # Checked and reserved in one step, counting agents running in other tasks.
        problem = reserve_agent(self.runs_dir, self.run_id, self.task["id"], name, self.attempt,
                                settings.max_budget_usd, self.is_alive, self.runner_script)
        if problem:  # not started, so nothing was spent on it
            self.budget_stopped = True
            self.findings.infrastructure.append(problem)
            self.log.emit("budget_stop", role=name, reason=problem)
            return runner_adapter.RunnerOutcome("not_started", None, problem, {})
        agent_run_id = attempt_agent_run_id(self.run_id, self.task["id"], name, self.attempt)
        self.log.emit("agent_started", role=name, agent_run_id=agent_run_id,
                      settings=settings.describe())
        try:
            with tempfile.TemporaryDirectory(prefix="agent-lab-task-") as scratch:
                prompt_file = Path(scratch) / f"{name}-prompt.txt"
                prompt_file.write_text(prompt, encoding="utf-8")
                outcome = self.invoke_runner(
                    run_id=agent_run_id,
                    worktree_path=self.worktree,
                    prompt_file=prompt_file,
                    timeout_seconds=settings.timeout_seconds,
                    model=settings.model,
                    max_turns=settings.max_turns,
                    effort=settings.effort,
                    max_budget_usd=settings.max_budget_usd,
                    keep_result_text=True,
                    runner_script=self.runner_script,
                    role=name,
                    python=self.python,
                    **role(name).runner_options(),
                )
        finally:  # the agent has ended (or never ran): its cap is free for the others
            release_agent(self.runs_dir, self.run_id, self.task["id"])
        self.agents[name] = {
            "outcome": outcome.outcome,
            "attempt": self.attempt,  # a continue may keep this result; the screens say from when
            **_agent_metrics(outcome.result),
        }
        reason = None
        if outcome.outcome != "success":
            # Always a reason: Claude's own, or one worked out from the events.
            reason = failure_reason(outcome.outcome, outcome.error, outcome.result,
                                    outcome.process_exit_code)
            self.agents[name]["error"] = reason
        self.log.emit("agent_finished", role=name, outcome=outcome.outcome,
                      cost_usd=outcome.result.get("total_cost_usd"),
                      seconds=outcome.result.get("duration_seconds"), error=reason)
        limit = outcome.result.get("rate_limit")
        if reason and isinstance(limit, dict):
            self.limit_stop = {**limit, "role": name}
            self.log.emit("limit_stop", role=name, limit=limit)
            self.findings.infrastructure.append(limit_reason(name, reason))
        elif reason:
            self.findings.infrastructure.append(
                f"The {name} {outcome.outcome.replace('_', ' ')}: {reason}")
        return outcome

    def report(self, name: str, outcome, parser):
        try:
            return parser(outcome.result.get("result_text"))
        except ta.ReportError as exc:
            self.findings.infrastructure.append(f"The {name}'s report was unusable: {exc}")
            self.log.emit("report_unusable", role=name, error=str(exc))
            return None

    # -- checkpoints --
    def checkpoint(self, next_stage: str) -> bool:
        """Save what a continued task needs to start at ``next_stage``. Always True.

        The builder's and tester's results, and the orchestrator's findings, live
        only in this process until the task ends; a checkpoint puts them in the
        record, so a stopped task can be continued without paying for them again.
        """
        data: dict[str, Any] = {
            "stage": next_stage,
            "after_build": self.after_build,
            "checks": self.checks,
            "findings": {k: getattr(self.findings, k) for k in self.findings.__dataclass_fields__},
            "agents": self.agents,  # this attempt's own agents (what it has spent)
            "reused": self.reused,  # results kept from an earlier, stopped attempt
        }
        if next_stage == "reviewing":
            data["after_tests"] = tc.snapshot(self.worktree)
        try:
            update_task(self.runs_dir, self.run_id, self.task["id"], {"checkpoint": data})
        except (OSError, RunRegistryError, ValueError):
            pass  # a missing checkpoint only means a continue starts earlier
        return True

    def restore(self, checkpoint: dict[str, Any]) -> None:
        """Take up the stopped attempt's results from its checkpoint."""
        self.after_build = dict(checkpoint.get("after_build") or {})
        self.checks = dict(checkpoint.get("checks") or {})
        saved = dict(checkpoint.get("findings") or {})
        # JSON turned the criterion numbers into strings.
        saved["uncovered"] = {int(k): v for k, v in (saved.get("uncovered") or {}).items()}
        fields = ta.Findings.__dataclass_fields__
        self.findings = ta.Findings(**{k: v for k, v in saved.items() if k in fields})
        # A 4.5 checkpoint had no "reused": its "agents" then held both.
        self.reused = {**(checkpoint.get("reused") or {}), **(checkpoint.get("agents") or {})}

    def show_kept(self) -> None:
        """Put the kept results in the record now, not only when the task ends.

        Without this, while a continued task's reviewer runs, status and the dashboard show
        the builder and tester as "not started": their results came from the stopped attempt.
        """
        try:
            update_task(self.runs_dir, self.run_id, self.task["id"], {
                "reused_agents": self.reused,
                "checks": self.checks,
                "findings": {k: getattr(self.findings, k) for k in self.findings.__dataclass_fields__},
            })
        except (OSError, RunRegistryError, ValueError):
            pass  # only the display is behind; the task itself goes on
