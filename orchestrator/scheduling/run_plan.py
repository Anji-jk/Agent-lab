"""Work through a stored plan, one task at a time (M4.7).

``run-plan <run>`` does what a person would otherwise do by hand: look at where
the plan stands, start the next task, and repeat. It keeps no memory of its
own. Before every step it reads the plan's progress from disk (``plan_progress``),
so stopping it at any point (Ctrl+C, a closed window, a reboot) loses nothing,
and running it again goes on from where the records say the plan is.

What it does at each step, in this order:

1. Every task done: the plan is complete.
2. A task is running in another process: stop. A run's tasks share one branch,
   so they run one at a time.
3. A task a person must look at (``needs_review``, ``cancelled``, or ``failed``
   with no retry left): stop the whole plan. Independent tasks are not run
   around it: a plan that stops at the first problem is easy to reason about,
   and its branch never holds work that skipped a step.
4. An interrupted task (its process went away part-way), or one stopped by the
   run budget: continue it in its own worktree (M4.5), before anything new,
   because its worktree already holds work that was paid for.
5. A failed task with its retry left: retry it once, with the findings (M4.4).
6. Otherwise, the next ready task in plan order: run it.

A task stopped by the run budget stops the whole plan at once: every later
agent would meet the same limit. Raise the budget and run ``run-plan`` again;
the stopped task continues from the stage it reached.

Only one ``run-plan`` may work on a run at a time. It holds
``runs/<run>/run-plan.json`` (its process ID) while it works; a lock left by a
process that has gone is taken over, one held by a live ``run-plan`` refuses.

Nothing is pushed or merged: each finished task is a local commit, as with
``run-task``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import runner_adapter
from orchestrator.execution import task_runner as tr
from orchestrator.execution.cancel import command_line, is_plan_process
from orchestrator.execution.continuing import continue_task
from orchestrator.execution.retry import retry_problem, retry_task
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.records.run_registry import run_directory, utc_now
from orchestrator.records.task_registry import list_tasks
from orchestrator.scheduling.plan_progress import (
    CommandLine,
    PlanProgress,
    plan_progress,
    process_liveness,
)

LOCK_FILE_NAME = "run-plan.json"
ACTIONS = ("run", "retry", "continue")


class PlanAlreadyRunning(ExecutionRefused):
    """Another ``run-plan`` is working on this run."""


@dataclass(frozen=True, slots=True)
class Step:
    task_id: str
    action: str             # run, retry or continue
    status: str             # the task's status when the step ended
    attempt: int = 1
    cost_usd: float | None = None
    reason: str | None = None
    budget_stop: bool = False  # an agent was not started because of the run budget


@dataclass
class PlanRun:
    run_id: str
    steps: list[Step] = field(default_factory=list)
    stopped: str | None = None   # why it stopped before the plan was complete
    refused: bool = False        # a step was refused before it started (budget, not ready...)
    progress: PlanProgress | None = None

    @property
    def complete(self) -> bool:
        return self.stopped is None


# --- one run-plan per run --------------------------------------------------------------------


def lock_path(runs_dir: Path, run_id: str) -> Path:
    return run_directory(Path(runs_dir), run_id) / LOCK_FILE_NAME


def _holder(path: Path) -> int | None:
    try:
        pid = json.loads(path.read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError, AttributeError):
        return None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def acquire_lock(runs_dir: Path, run_id: str,
                 get_command_line: CommandLine | None = None) -> Path:
    """Take the run's lock, or refuse when a live ``run-plan`` holds it."""
    get_command_line = get_command_line or command_line
    path = lock_path(runs_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"pid": os.getpid(), "started_at": utc_now()}).encode()
    for _ in range(2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = _holder(path)
            line = get_command_line(pid) if pid is not None else None
            if pid is not None and pid != os.getpid() and line is not None \
                    and is_plan_process(line, run_id):
                raise PlanAlreadyRunning(
                    f"run-plan is already working on run {run_id} (process {pid}). Watch it "
                    f"with status {run_id}, or stop it with Ctrl+C in its window."
                ) from None
            path.unlink(missing_ok=True)  # left by a run-plan that has gone: take it over
            continue
        with os.fdopen(handle, "wb") as out:
            out.write(body)
        return path
    raise PlanAlreadyRunning(f"The lock {path} could not be taken; try again.")


def release_lock(path: Path) -> None:
    if _holder(path) == os.getpid():
        path.unlink(missing_ok=True)


# --- what to do next ----------------------------------------------------------------------------


def _stop_reason(run_id: str, task_id: str, state: str, record: dict[str, Any],
                 reason: str | None) -> str:
    if state == "cancelled":
        return (f"{task_id} was cancelled. To go on with it: python -m orchestrator run-task "
                f"{run_id} {task_id} --continue, then run-plan again.")
    if state == "failed":
        return retry_problem(record) or f"{task_id} failed."
    return (f"{task_id} needs review ({reason or 'no reason recorded'}). A person should look: "
            f"python -m orchestrator show-task {run_id} {task_id}.")


def next_action(progress: PlanProgress,
                records: dict[str, dict[str, Any]]) -> tuple[str, str] | str | None:
    """(action, task) to do next, a reason to stop, or None when the plan is complete."""
    if progress.state == "complete":
        return None
    for task in progress.with_state("running"):
        return (f"{task.task_id} is running in another process (at {task.stage}). A run's tasks "
                "run one at a time; wait for it to finish, or cancel it.")

    can_go_on: list[tuple[str, str]] = []
    for task in progress.tasks:
        record = records.get(task.task_id) or {}
        if task.state == "interrupted":
            can_go_on.append(("continue", task.task_id))
        elif task.state == "needs_review" and record.get("budget_stop"):
            can_go_on.append(("continue", task.task_id))
        elif task.state == "failed" and retry_problem(record) is None:
            can_go_on.append(("retry", task.task_id))
        elif task.state in ("failed", "needs_review", "cancelled"):
            # A person must look; nothing else runs until they have.
            return _stop_reason(progress.run_id, task.task_id, task.state, record, task.reason)
    if can_go_on:
        return can_go_on[0]
    if progress.next_task is not None:
        return ("run", progress.next_task)
    return "No task can start, and none is running."  # not expected: waiting needs a cause


# --- working through the plan -----------------------------------------------------------------


OnStep = Callable[[str, str, "Step | None"], None]  # (action, task, finished step or None)


def run_plan(
    run_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    worktree_root: Path | None = None,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    invoke_runner=runner_adapter.invoke_runner,
    test_command: tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
    get_command_line: CommandLine | None = None,
    on_step: OnStep | None = None,
) -> PlanRun:
    """Run the plan's tasks one at a time until it is complete or something stops it."""
    plan = load_task_plan(plans_dir, run_id)
    lock = acquire_lock(runs_dir, run_id, get_command_line)
    result = PlanRun(run_id)
    common = dict(plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path,
                  runner_script=runner_script, invoke_runner=invoke_runner,
                  test_command=test_command, env=env)
    # Each task can be run, continued and retried; more steps than that is a bug, not a plan.
    limit = 4 * len(plan["tasks"]) + 2
    try:
        while True:
            progress = plan_progress(plan, runs_dir, run_id, get_command_line=get_command_line,
                                     runner_script=Path(runner_script))
            result.progress = progress
            last = result.steps[-1] if result.steps else None
            if last is not None and last.budget_stop:
                result.stopped = (f"{last.task_id} was stopped by the run budget. {last.reason} "
                                  f"Then run-plan again: {last.task_id} continues from where it stopped.")
                return result
            decision = next_action(progress, list_tasks(runs_dir, run_id))
            if decision is None:
                return result
            if isinstance(decision, str):
                result.stopped = decision
                return result
            if len(result.steps) >= limit:
                result.stopped = f"Stopped after {limit} steps; this should not happen, please report it."
                return result

            action, task_id = decision
            if on_step:
                on_step(action, task_id, None)
            try:
                record = _do(action, run_id, task_id, worktree_root, get_command_line, common)
            except ExecutionRefused as exc:
                result.stopped = f"{task_id} was not {_past(action)}: {exc}"
                result.refused = True
                result.progress = plan_progress(plan, runs_dir, run_id,
                                                get_command_line=get_command_line,
                                                runner_script=Path(runner_script))
                return result
            reasons = record.get("reasons") or []
            step = Step(task_id, action, record["status"], int(record.get("attempt") or 1),
                        record.get("cost_usd"),
                        _budget_reason(reasons) if record.get("budget_stop") else
                        (str(reasons[0]) if reasons else None),
                        bool(record.get("budget_stop")))
            result.steps.append(step)
            if on_step:
                on_step(action, task_id, step)
    finally:
        release_lock(lock)


def _budget_reason(reasons: list[Any]) -> str:
    for reason in reasons:
        if str(reason).startswith("Run budget:"):
            return str(reason)
    return "Run budget: an agent's cap did not fit in what is left of it."


def _past(action: str) -> str:
    return {"run": "started", "retry": "retried", "continue": "continued"}[action]


def _do(action: str, run_id: str, task_id: str, worktree_root: Path | None,
        get_command_line: CommandLine | None, common: dict[str, Any]) -> dict[str, Any]:
    if action == "run":
        return tr.run_task(run_id, task_id, worktree_root=worktree_root, **common)
    if action == "retry":
        return retry_task(run_id, task_id, worktree_root=worktree_root, **common)
    alive = process_liveness(common["runs_dir"], run_id, get_command_line=get_command_line,
                             runner_script=Path(common["runner_script"]))
    return continue_task(run_id, task_id, is_alive=alive, **common)


# --- showing it ---------------------------------------------------------------------------------


def format_step(action: str, task_id: str, step: Step | None) -> str:
    verb = {"run": "Running", "retry": "Retrying", "continue": "Continuing"}[action]
    if step is None:
        extra = {"retry": " (it failed; once more, told what was wrong)",
                 "continue": " (from where it stopped, in its own worktree)"}.get(action, "")
        return f"{verb} {task_id}{extra}..."
    cost = f", ${step.cost_usd:.4f}" if isinstance(step.cost_usd, (int, float)) else ""
    attempt = f" (attempt {step.attempt})" if step.attempt > 1 else ""
    why = f": {step.reason[:150]}" if step.reason and step.status != "done" else ""
    return f"{task_id}{attempt}: {step.status}{cost}{why}"
