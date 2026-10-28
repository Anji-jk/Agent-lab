"""Run one planned task from plan to a verified local commit.

For the task it creates a worktree and then runs three agents in turn:

    builder  -> makes the change
    tester   -> writes tests for each acceptance criterion (only under tests/)
    reviewer -> reads the diff and judges each criterion (cannot write)

Between agents the orchestrator checks the facts for itself (``task_checks``):
which files changed, whether the full suite and the tester's named tests pass.
``task_agents.decide`` turns agent reports plus those facts into the status.
Only a task that is ``done`` gets a commit, made by the orchestrator, on the
task's own branch in the task's own worktree, and then merged into the run's
branch (M7.1, ``integration``). Nothing is pushed.
"""

from __future__ import annotations

from collections.abc import Callable
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.execution import runner_adapter

from orchestrator.execution import agents as ta
from orchestrator.execution.diagnose import failure_reason
from orchestrator.execution.integration import MergeProblem, integration_head, merge_task

from orchestrator.execution import checks as tc
from orchestrator.execution.budget import (
    BudgetExhausted, budget_problem, release_agent, reserve_agent,
)
from orchestrator.errors import ExecutionRefused
from orchestrator.planning.profiles import RoleSettings, profile_for, role_settings
from orchestrator.execution.roles import BUILDER, REVIEWER, ROLE_NAMES, TESTER, TEST_DIR, role
from orchestrator.records.run_registry import RunRegistryError, locked, run_directory, utc_now
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.records.task_events import NullLog, TaskLog
from orchestrator.records.task_registry import (
    ACTIVE_STATUSES, list_tasks, load_task, save_task, update_task,
)
from orchestrator.execution.worktrees import WorktreeError, create_worktree
from orchestrator.monitoring.progress import agent_run_id as attempt_agent_run_id

TEST_DIRS = (f"{TEST_DIR}/",)
# The full suite took 496 s in a worktree outside OneDrive on Windows (M2.8).
TEST_TIMEOUT_SECONDS = 1200
# Set this to give the tests longer on a slow machine (M4.6): seconds, 60 to 86400.
TEST_TIMEOUT_VARIABLE = "AGENT_LAB_TEST_TIMEOUT"
CREDENTIAL_VARIABLES = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
)
AGENT_METRIC_FIELDS = (
    "status", "duration_seconds", "total_cost_usd", "input_tokens",
    "output_tokens", "num_turns", "model", "session_id",
)
MAX_RUN_ID_FOR_TASK = 52  # the worktree run ID adds "-<task id>" and a retry's "-r2", and must stay <= 64
# (task ID, its record) -> (is its process still running, why we think so)
IsAlive = Callable[[str, dict[str, Any]], tuple[bool, str]]


class TaskNotReady(ExecutionRefused):
    """The task cannot start yet (a dependency is not done, no common base)."""


@dataclass(frozen=True, slots=True)
class TaskContext:
    plan: dict[str, Any]
    task: dict[str, Any]
    base_commit: str
    settings: dict[str, RoleSettings]


def test_timeout_seconds() -> int:
    """How long the orchestrator's own test runs may take: AGENT_LAB_TEST_TIMEOUT, or 1200."""
    raw = os.environ.get(TEST_TIMEOUT_VARIABLE, "").strip()
    if not raw:
        return TEST_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        raise ExecutionRefused(
            f"{TEST_TIMEOUT_VARIABLE} must be a whole number of seconds, not {raw!r}."
        ) from None
    if not 60 <= value <= 86400:
        raise ExecutionRefused(f"{TEST_TIMEOUT_VARIABLE} must be between 60 and 86400 seconds.")
    return value


def check_environment(runner_script: Path) -> dict[str, str]:
    """Environment for running tests: the agents' allowlist minus credentials."""
    env = {
        key: value
        for key, value in runner_adapter.load_agent_env(runner_script).items()
        if key.upper() not in CREDENTIAL_VARIABLES
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return tc.with_python_on_path(env, tc.project_python())


def find_task(plan: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in plan["tasks"]:
        if task["id"] == task_id:
            return task
    known = ", ".join(task["id"] for task in plan["tasks"])
    raise ExecutionRefused(f"The plan has no task {task_id!r} (it has: {known}).")


def run_head(repo_path: Path, run_id: str, records: dict[str, dict[str, Any]], plan_base: str,
             runs_dir: Path | None = None) -> str:
    """The commit the run's next task starts from (M4.2).

    Every task of a run starts from the newest finished work, so the run's
    commits form one chain on one branch and need no merge: the head is the
    finished commit that contains all the others. Before anything is finished
    it is the commit the plan was made from. A task that depends on others
    starts from a commit containing theirs, because the head contains every
    finished task's commit.
    """
    if runs_dir is not None:
        head = integration_head(runs_dir, run_id, repo_path)
        if head is not None:  # the run's branch (M7.1) holds every merged task
            return head
    finished = [(task_id, record["commit"]) for task_id, record in sorted(records.items())
                if record.get("status") == "done" and record.get("commit")]
    if not finished:
        return plan_base
    for task_id, commit in finished:
        if not tc.commit_exists(repo_path, commit):
            raise ExecutionRefused(
                f"{task_id}'s commit {commit[:12]} does not exist in {repo_path}; the run's "
                "work cannot be continued from it."
            )
    head = tc.pick_base_commit(repo_path, [commit for _, commit in finished])
    if head is None:
        listed = ", ".join(f"{task_id} at {commit[:12]}" for task_id, commit in finished)
        raise TaskNotReady(
            f"The finished work of run {run_id} is on separate branches ({listed}), "
            "from before a run's tasks shared one branch. Combining them needs a merge, "
            "which a later stage provides; plan again for a new run."
        )
    return head


def check_no_other_task_running(
    task_id: str, records: dict[str, dict[str, Any]], is_alive: IsAlive,
    together: Callable[[str], bool] | None = None,
) -> None:
    """Refuse while another task of the run is still running, unless the two may run together.

    ``together(other)`` says whether this task and ``other`` may run side by side: both
    marked parallel-safe in the plan (M7.3), which the planner only allows for tasks that
    are not ordered and share no file. Without it, a task runs alone.

    A task whose process has gone (a closed window) does not count: it is not running.
    """
    for other, record in sorted(records.items()):
        if other == task_id or record.get("status") not in ACTIVE_STATUSES | {"pending"}:
            continue
        alive, why = is_alive(other, record)
        if alive and not (together and together(other)):
            raise TaskNotReady(
                f"{other} is still running ({why}). {task_id} and {other} are not both marked "
                f"parallel-safe in the plan, so they run one at a time; wait for {other} to "
                "finish or cancel it."
            )


def prepare(
    run_id: str,
    task_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    is_alive: IsAlive | None = None,
) -> TaskContext:
    """Everything that must be true before anything is created."""
    plan = load_task_plan(plans_dir, run_id)
    if not plan["issue"].get("description"):
        raise ExecutionRefused(
            "This plan does not contain the Issue text (it was made by an older "
            "version). Generate it again with ai-plan."
        )
    if len(run_id) > MAX_RUN_ID_FOR_TASK:
        raise ExecutionRefused(f"Run ID is too long to add a task suffix (max {MAX_RUN_ID_FOR_TASK}).")
    task = find_task(plan, task_id)

    for dependency in task["depends_on"]:
        try:
            record = load_task(runs_dir, run_id, dependency)
        except RunRegistryError:
            raise TaskNotReady(f"{dependency} has not been run yet; {task_id} depends on it.") from None
        if record["status"] != "done":
            raise TaskNotReady(
                f"{dependency} is {record['status']}, not done; {task_id} depends on it."
            )

    records = list_tasks(runs_dir, run_id)
    if is_alive is None:
        # Imported here: the scheduling package imports this module's callers.
        from orchestrator.scheduling.plan_progress import process_liveness
        is_alive = process_liveness(runs_dir, run_id)
    safe = {t["id"]: bool(t.get("parallel_safe")) for t in plan["tasks"]}
    check_no_other_task_running(
        task_id, records, is_alive,
        together=lambda other: safe.get(task_id, False) and safe.get(other, False))

    base = plan["config"]["base_commit"]
    if not tc.commit_exists(repo_path, base):
        raise ExecutionRefused(f"Base commit {base[:12]} does not exist in {repo_path}.")
    base = run_head(repo_path, run_id, records, base, runs_dir)

    profile = profile_for(task["complexity"])
    return TaskContext(plan, task, base, {name: role_settings(profile, name) for name in ROLE_NAMES})


def _agent_metrics(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in AGENT_METRIC_FIELDS if key in result}


def _total_cost(agents: dict[str, dict[str, Any]]) -> float:
    return round(
        sum(a["total_cost_usd"] for a in agents.values()
            if isinstance(a.get("total_cost_usd"), (int, float))),
        6,
    )


def limit_reason(name: str, reason: str) -> str:
    """An agent stopped by Claude's usage limit: the reason, and what to do about it."""
    return (f"The {name} was stopped by Claude's usage limit: {reason} Nothing is wrong with "
            "the task: once the limit resets, run run-plan again (or run-task --continue), and "
            "it goes on from the stage it reached.")


GIT_LOCK_FOLDER = "git-lock"   # runs/<run>/git-lock: tasks side by side take turns at git (M7.3)
GIT_LOCK_SECONDS = 300


def git_lock(runs_dir: Path, run_id: str):
    """One task at a time creates its worktree or commits; the work itself runs side by side.

    Git takes its own locks, but two tasks making a worktree or a commit in the same
    instant can still fail on them ("Unable to create ... .lock"). Taking turns costs
    a second or two against tasks that run for many minutes.
    """
    return locked(run_directory(Path(runs_dir), run_id) / GIT_LOCK_FOLDER,
                  timeout=GIT_LOCK_SECONDS)


def _liveness(runs_dir: Path, run_id: str, runner_script: Path) -> IsAlive:
    """Whether another task's process is still running (imported here: scheduling imports this module)."""
    from orchestrator.scheduling.plan_progress import process_liveness
    return process_liveness(runs_dir, run_id, runner_script=Path(runner_script))


class _Pipeline:
    def __init__(self, ctx: TaskContext, run_id: str, runs_dir: Path, worktree: Path,
                 runner_script: Path, invoke_runner, test_command: tuple[str, ...], env,
                 log: TaskLog | NullLog | None = None, attempt: int = 1,
                 feedback: str | None = None, earlier: str | None = None,
                 is_alive: IsAlive | None = None):
        self.ctx, self.run_id, self.runs_dir = ctx, run_id, runs_dir
        self.is_alive = is_alive or _liveness(runs_dir, run_id, runner_script)
        self.attempt, self.feedback, self.earlier = attempt, feedback, earlier
        self.reused: dict[str, dict[str, Any]] = {}  # agents' results kept from the stopped attempt
        self.test_timeout = TEST_TIMEOUT_SECONDS
        self.budget_stopped = False
        self.limit_stop: dict[str, Any] | None = None  # Claude's usage limit refused an agent
        self.log = log or NullLog()
        self.worktree, self.runner_script = worktree, runner_script
        self.invoke_runner, self.test_command, self.env = invoke_runner, test_command, env
        self.task = ctx.task
        self.criteria = ctx.task["acceptance_criteria"]
        self.findings = ta.Findings(criteria_count=len(self.criteria))
        self.agents: dict[str, dict[str, Any]] = {}
        self.checks: dict[str, Any] = {}
        self.after_build: dict[str, str] = {}

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
        # Checked and reserved in one step, counting agents running in other tasks (M7.4).
        problem = reserve_agent(self.runs_dir, self.run_id, self.task["id"], name, self.attempt,
                                settings.max_budget_usd, self.is_alive, self.runner_script)
        if problem:  # not started, so nothing was spent on it (M4.6)
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
                    **role(name).runner_options(),
                )
        finally:  # the agent has ended (or never ran): its cap is free for the others
            release_agent(self.runs_dir, self.run_id, self.task["id"])
        self.agents[name] = {
            "outcome": outcome.outcome,
            **_agent_metrics(outcome.result),
        }
        reason = None
        if outcome.outcome != "success":
            # Always a reason: Claude's own, or one worked out from the events (M5.5).
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

    def suite(self) -> tc.CommandResult:
        self.log.emit("suite_started", command=list(self.test_command),
                      timeout_seconds=self.test_timeout)
        result = tc.run_suite(self.worktree, command=self.test_command,
                              timeout=self.test_timeout, env=self.env)
        self.log.emit("suite_finished", **_command_fields(result))
        return result

    # -- checkpoints (M4.5) --
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

    # -- stages --
    def build(self) -> bool:
        f = self.findings
        self.reused.pop(BUILDER, None)  # this stage's agent is run again, not reused
        self.set_status("building")
        outcome = self.call(BUILDER, ta.builder_prompt(self.ctx.plan["issue"], self.task, self.feedback,
                                                       self.earlier))
        if outcome.outcome != "success":
            return False
        report = self.report(BUILDER, outcome, ta.parse_builder)
        if report is None:
            return False
        f.builder_ok = True
        f.builder_blocked = (report.blocker or "no reason given") if report.blocked else ""
        # The worktree started clean, so every changed file is the builder's.
        self.after_build = tc.snapshot(self.worktree)
        changed = sorted(self.after_build)
        f.nothing_changed = not changed
        f.scope_violations = tc.scope_violations(changed, self.task["files"], TEST_DIRS)
        self.checks["builder_changed"] = changed
        self.log.emit("builder_checked", changed=changed, scope_violations=f.scope_violations,
                      nothing_changed=f.nothing_changed, blocked=f.builder_blocked)
        return self.stopped()

    def run_tests(self) -> bool:
        f = self.findings
        self.reused.pop(TESTER, None)
        f.missing_tests = []  # a continued task may carry the stopped attempt's
        self.set_status("testing")
        changed_by_builder = self.checks["builder_changed"]
        outcome = self.call(
            TESTER, ta.tester_prompt(self.ctx.plan["issue"], self.task, changed_by_builder,
                                     self.earlier)
        )
        if outcome.outcome != "success":
            return False
        report = self.report(
            TESTER, outcome, lambda text: ta.parse_tester(text, len(self.criteria))
        )
        if report is None:
            return False
        f.tester_ok = True

        now = tc.snapshot(self.worktree)
        wrote = tc.changed_between(self.after_build, now)
        f.tester_wrote_outside_tests = tc.outside(wrote, TEST_DIRS)
        self.checks["tester_changed"] = wrote
        f.weakened_tests = tc.removed_test_lines(self.worktree, tc.changed_paths(self.worktree), TEST_DIRS)

        f.uncovered = dict(report.uncovered)
        f.suspected_defects = list(report.suspected_defects)
        f.tester_notes = list(report.notes)
        f.unnamed = [
            n for n in range(1, len(self.criteria) + 1)
            if n not in report.coverage and n not in report.uncovered
        ]
        named = list(dict.fromkeys(t for tests in report.coverage.values() for t in tests))
        f.invalid_test_ids = [t for t in named if not tc.valid_test_id(t, TEST_DIRS)]
        valid = [t for t in named if t not in f.invalid_test_ids]
        f.no_tests = not named
        self.log.emit("tester_checked", changed=wrote, outside_tests=f.tester_wrote_outside_tests,
                      weakened_tests=f.weakened_tests, named_tests=named,
                      invalid_test_ids=f.invalid_test_ids, suspected_defects=f.suspected_defects)

        suite = self.suite()
        f.suite_passed = suite.passed
        f.suite_timed_out = suite.timed_out
        f.test_timeout_seconds = self.test_timeout
        self.checks["suite"] = suite.summary()
        if valid:
            self.log.emit("named_tests_started", tests=valid)
            result = tc.run_named_tests(self.worktree, valid, command=self.test_command,
                                        timeout=self.test_timeout, env=self.env)
            self.log.emit("named_tests_finished", **_command_fields(result))
            self.checks["named_tests"] = result.summary()
            # pytest exit 4 = a named file/test does not exist; 5 = nothing collected.
            if result.exit_code in (4, 5):
                f.missing_tests = valid
            f.named_passed = result.passed
            f.named_timed_out = result.timed_out
        else:
            f.named_passed = False if named else None
        return self.stopped()

    def review(self) -> bool:
        f = self.findings
        self.reused.pop(REVIEWER, None)
        self.set_status("reviewing")
        suite, named = self.checks.get("suite", {}), self.checks.get("named_tests", {})
        facts = [
            f"Files changed: {', '.join(tc.changed_paths(self.worktree)) or '(none)'}",
            f"Full test suite: {'passed' if f.suite_passed else 'FAILED'} "
            f"(exit {suite.get('exit_code')})",
            f"Tests named by the tester: {'passed' if f.named_passed else 'FAILED'} "
            f"(exit {named.get('exit_code')})",
            f"Criteria the tester could not automate: {len(f.uncovered)}",
        ]
        diff = tc.diff_text(self.worktree)
        outcome = self.call(
            REVIEWER, ta.reviewer_prompt(self.ctx.plan["issue"], self.task, diff, facts)
        )
        if outcome.outcome != "success":
            return False
        report = self.report(
            REVIEWER, outcome, lambda text: ta.parse_reviewer(text, len(self.criteria))
        )
        if report is None:
            return False
        f.reviewer_ok = True
        f.reviewer_verdict = report.verdict
        f.unmet = [n for n in range(1, len(self.criteria) + 1) if not report.met.get(n, False)]
        f.reviewer_problems = list(report.problems)
        f.reviewer_notes = list(report.notes)
        self.log.emit("reviewer_checked", verdict=report.verdict, unmet=f.unmet,
                      problems=f.reviewer_problems)
        return True


def _command_fields(result: tc.CommandResult) -> dict[str, Any]:
    return {"exit_code": result.exit_code, "timed_out": result.timed_out,
            "seconds": round(result.seconds, 2), "passed": result.passed,
            "output_tail": result.output_tail}


def _commit_message(plan: dict[str, Any], task: dict[str, Any], run_id: str) -> str:
    issue = plan["issue"]
    criteria = "\n".join(f"- {c}" for c in task["acceptance_criteria"])
    return (
        f"{task['id']}: {task['title']}\n\n"
        f"Issue #{issue['number']}, run {run_id}.\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        "Built, tested and reviewed by separate agents; verified by the orchestrator."
    )


def _merge(run_id: str, task: dict[str, Any], commit: str, *, plan: dict[str, Any],
           ctx: TaskContext, runs_dir: Path, repo_path: Path, worktree_root: Path | None,
           test_command: tuple[str, ...], test_timeout: int, env, log, status: str,
           reasons: list[str]) -> tuple[str, list[str], dict[str, Any]]:
    """Merge a done task into the run's branch (M7.1): (status, reasons, what happened)."""
    log.emit("merge_started", commit=commit)
    try:
        merged = merge_task(run_id, task["id"], commit, issue_number=int(plan["issue"]["number"]),
                            base_commit=ctx.base_commit, runs_dir=runs_dir, repo_path=repo_path,
                            worktree_root=worktree_root, test_command=test_command,
                            test_timeout=test_timeout, env=env, title=task.get("title", ""))
    except MergeProblem as exc:
        log.emit("merge_failed", kind=exc.kind, reason=exc.reason)
        # A conflict or a failing merged suite is the task's to fix: its retry starts from
        # the branch's new head and sees the other work. Anything else needs a person.
        status = ta.FAILED if exc.kind in ("conflict", "suite") else ta.NEEDS_REVIEW
        return status, [exc.reason], {"problem": exc.kind, **exc.detail}
    except (tc.CheckError, WorktreeError, FileExistsError, OSError, RunRegistryError) as exc:
        log.emit("merge_failed", kind="error", reason=str(exc))
        return ta.NEEDS_REVIEW, [f"The work was committed on its own branch, but merging it "
                                 f"into the run's branch failed: {exc}"], {"problem": "error"}
    log.emit("merged", **merged.describe())
    return status, reasons, merged.describe()


def run_task(
    run_id: str,
    task_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    worktree_root: Path | None = None,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    invoke_runner=runner_adapter.invoke_runner,
    test_command: tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
    attempt: int = 1,
    feedback: str | None = None,
    previous_attempts: list[dict[str, Any]] | None = None,
    continue_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one task end to end and return its final record."""
    ctx = prepare(run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path)
    task, plan = ctx.task, ctx.plan
    start = (continue_from or {}).get("stage", "building")
    # With the run's branch (M7.1) a task that started earlier is merged into it, so tasks
    # run side by side may finish in any order; only a run from before that has one chain.
    if (continue_from and continue_from["base_commit"] != ctx.base_commit
            and integration_head(runs_dir, run_id, repo_path) is None):
        raise TaskNotReady(
            f"{task_id} started from {continue_from['base_commit'][:12]}, but the run has "
            f"moved on to {ctx.base_commit[:12]} since (another task finished). Continuing "
            "it would split the run's branch."
        )
    if not Path(runner_script).is_file():
        raise ExecutionRefused(f"Runner script not found: {runner_script}")
    test_timeout = test_timeout_seconds()
    first = {"building": BUILDER, "testing": TESTER, "reviewing": REVIEWER}[start]
    alive = _liveness(runs_dir, run_id, Path(runner_script))
    problem = budget_problem(runs_dir, run_id, first, ctx.settings[first].max_budget_usd,
                             Path(runner_script), task_id, alive)
    if problem:  # refused before anything is created: the task stays as it was
        raise BudgetExhausted(problem)

    env = check_environment(runner_script) if env is None else env
    test_command = tc.DEFAULT_TEST_COMMAND if test_command is None else test_command
    # Before anything is created or paid for: a suite that cannot start fails every task.
    if tuple(test_command[1:3]) == ("-m", "pytest"):
        problem = tc.pytest_problem(test_command[0], env)
        if problem:
            raise ExecutionRefused(
                f"pytest is not available for {test_command[0]}: {problem}. Activate the "
                f"project's .venv or set {tc.PYTHON_VARIABLE} to a Python that has pytest."
            )
    log = TaskLog(runs_dir, run_id, task_id)
    save_task(runs_dir, run_id, task_id, {
        "status": start if continue_from else "pending",
        "title": task["title"],
        "complexity": task["complexity"],
        "depends_on": task["depends_on"],
        # A continued task keeps the base its worktree was made from, not the run's head now.
        "base_commit": (continue_from or {}).get("base_commit", ctx.base_commit),
        "roles": {name: s.describe() for name, s in ctx.settings.items()},
        # The process running this pipeline, for cancel and for spotting one that died.
        "pid": os.getpid(),
        **({"attempt": attempt, "previous_attempts": previous_attempts or []} if attempt > 1 else {}),
        **({"continued_at": start} if continue_from else {}),
    })
    log.emit("task_started", base_commit=ctx.base_commit, title=task["title"], pid=os.getpid(),
             **({"attempt": attempt} if attempt > 1 else {}))

    try:
        if continue_from:  # the stopped attempt's worktree, files and all
            branch, worktree = continue_from["branch"], Path(continue_from["worktree_path"])
            if not worktree.is_dir():
                raise FileNotFoundError(f"The worktree {worktree} no longer exists.")
        else:
            with git_lock(runs_dir, run_id):
                branch, worktree = create_worktree(
                    repo_path, plan["issue"]["number"],
                    f"{run_id}-{task_id.lower()}" + (f"-r{attempt}" if attempt > 1 else ""),
                    base_ref=ctx.base_commit, worktree_root=worktree_root,
                )
    except Exception as exc:
        log.emit("worktree_failed", error=str(exc))
        log.emit("task_finished", status="failed", cost_usd=0)
        return update_task(runs_dir, run_id, task_id, {
            "status": "failed", "reasons": [f"The worktree could not be created: {exc}"],
        })
    update_task(runs_dir, run_id, task_id, {"branch": branch, "worktree_path": str(worktree)})
    log.emit("worktree_created", branch=branch, path=str(worktree))

    merging = False
    pipeline = _Pipeline(ctx, run_id, runs_dir, worktree, Path(runner_script),
                         invoke_runner, test_command, env, log, attempt, feedback,
                         (continue_from or {}).get("earlier"), alive)
    pipeline.test_timeout = test_timeout
    try:
        if start == "building":
            go = (pipeline.build() and pipeline.checkpoint("testing")
                  and pipeline.run_tests() and pipeline.checkpoint("reviewing"))
        else:
            pipeline.restore(continue_from["checkpoint"])  # type: ignore[index]
            go = start == "reviewing" or (pipeline.run_tests() and pipeline.checkpoint("reviewing"))
        if go:
            pipeline.review()
        status, reasons = ta.decide(pipeline.findings)
        log.emit("decided", status=status, reasons=reasons)
        commit, merge = None, None
        if status == ta.DONE:
            with git_lock(runs_dir, run_id):
                commit = tc.commit_all(worktree, _commit_message(plan, task, run_id))
            if tc.changed_paths(worktree):
                status, reasons = ta.NEEDS_REVIEW, ["Files remained uncommitted after the commit."]
                commit = None
                log.emit("commit_incomplete", reasons=reasons)
            else:
                log.emit("committed", commit=commit, branch=branch)
                # status shows "merging" meanwhile (M7.6): a real merge runs the full suite,
                # and a merge may first wait for another task's.
                update_task(runs_dir, run_id, task_id, {"merging": utc_now()})
                merging = True
                status, reasons, merge = _merge(run_id, task, commit, plan=plan, ctx=ctx,
                                                runs_dir=runs_dir, repo_path=repo_path,
                                                worktree_root=worktree_root,
                                                test_command=test_command,
                                                test_timeout=test_timeout, env=env, log=log,
                                                status=status, reasons=reasons)
    except Exception as exc:  # a crash must leave a record, not a stuck "building"
        status, reasons, commit = ta.NEEDS_REVIEW, [f"The pipeline stopped: {type(exc).__name__}: {exc}"], None
        merge = None
        log.emit("pipeline_crashed", error=f"{type(exc).__name__}: {exc}")

    current = load_task(runs_dir, run_id, task_id)["status"]
    if current == "pending":
        status = "failed"
    log.emit("task_finished", status=status, cost_usd=_total_cost(pipeline.agents))
    return update_task(runs_dir, run_id, task_id, {
        "status": status,
        "reasons": reasons,
        "commit": commit,
        "agents": pipeline.agents,
        **({"reused_agents": pipeline.reused} if pipeline.reused else {}),
        **({"budget_stop": True} if pipeline.budget_stopped else {}),
        **({"limit_stop": pipeline.limit_stop} if pipeline.limit_stop else {}),
        **({"merge": merge} if merge else {}),
        **({"merging": None} if merging else {}),
        "checks": pipeline.checks,
        "findings": {k: getattr(pipeline.findings, k) for k in pipeline.findings.__dataclass_fields__},
        "cost_usd": _total_cost(pipeline.agents),
    })
