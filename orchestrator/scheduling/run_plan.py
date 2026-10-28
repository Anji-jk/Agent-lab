"""Work through a stored plan: one task at a time, or several side by side (M4.7, M7.3).

``run-plan <run>`` does what a person would otherwise do by hand: look at where
the plan stands, start the next task, and repeat. It keeps no memory of its
own. Before every step it reads the plan's progress from disk (``plan_progress``),
so stopping it at any point (Ctrl+C, a closed window, a reboot) loses nothing,
and running it again goes on from where the records say the plan is.

What it does when it looks, in this order:

1. Every task done: the plan is complete.
2. A task is running that this run-plan did not start (another process): start
   nothing, and stop when its own tasks are finished.
3. A task a person must look at (``needs_review``, ``cancelled``, or ``failed``
   with no retry left): stop the whole plan. Independent tasks are not started
   around it: a plan that stops at the first problem is easy to reason about.
4. An interrupted task (its process went away part-way), or one stopped by the
   run budget or (after its reset) by Claude's usage limit: continue it in its own worktree (M4.5), before anything new,
   because its worktree already holds work that was paid for.
5. A failed task with its retry left: retry it once, with the findings (M4.4).
6. Otherwise, the ready tasks in plan order: run them.

Side by side (M7.3): with room for more than one task (``max_parallel``), run-plan
starts every task it may start together. Two tasks run together only when the plan
marks both parallel-safe (the planner allows that only for tasks that are not
ordered and share no file); a task that is not parallel-safe runs alone, and the
tasks after it in plan order wait for it. Tasks are taken in plan order, so a
sequential task is never passed over. When one task ends, run-plan looks again: the
next ready task starts, or a task that depended on it is now ready. The tasks'
work is merged one at a time into the run's branch (M7.1), and git work that could
collide (making a worktree, committing) takes turns.

When a task stops the plan (a person must look, the run budget, Claude's usage
limit, a process that died) no new task starts; the tasks already running finish
first, and then the plan stops with the first reason.

A task stopped by the run budget stops the whole plan: every later agent would
meet the same limit. Raise the budget and run ``run-plan`` again; the stopped task
continues from the stage it reached.

So does a task stopped by Claude's usage limit (M5.5: the five-hour or weekly
limit refused an agent). Every later agent would be refused too. Run
``run-plan`` again once the limit has reset (the stop message names the time);
before then it stops at once, and after it the task continues from its stage.

Only one ``run-plan`` may work on a run at a time. It holds
``runs/<run>/run-plan.json`` (its process ID) while it works; a lock left by a
process that has gone is taken over, one held by a live ``run-plan`` refuses.

Nothing is pushed or merged outside the run's own branch: each finished task is a
local commit, as with ``run-task``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import runner_adapter
from orchestrator.execution import task_runner as tr
from orchestrator.execution.cancel import command_line, is_plan_process
from orchestrator.execution.continuing import continue_task, limit_not_reset
from orchestrator.execution.diagnose import when_text
from orchestrator.execution.retry import retry_problem, retry_task
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.records.run_registry import run_directory, utc_now
from orchestrator.records.run_registry import RunRegistryError
from orchestrator.records.task_registry import FINAL_STATUSES, list_tasks, load_task
from orchestrator.scheduling.task_process import (
    POLL_SECONDS, Ended, Handle, InProcessLauncher, Launcher,
)
from orchestrator.scheduling.plan_progress import (
    CommandLine,
    PlanProgress,
    plan_progress,
    process_liveness,
)

LOCK_FILE_NAME = "run-plan.json"
SETTLE_SECONDS = 15.0      # how long a task's final status may take to appear (cancel)
SETTLE_POLL_SECONDS = 0.5
ACTIONS = ("run", "retry", "continue")
DEFAULT_MAX_PARALLEL = 3
MAX_PARALLEL_LIMIT = 8       # more tasks at once would mostly wait on each other's merges
MAX_PARALLEL_VARIABLE = "AGENT_LAB_MAX_PARALLEL"


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
    limit_stop: str | None = None  # Claude's usage limit stopped an agent: when it resets ("" unknown)


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


def _running_elsewhere(task: Any) -> str:
    return (f"{task.task_id} is running in another process (at {task.stage}). A task that "
            "run-plan did not start is running; wait for it to finish, or cancel it.")


def _choices(progress: PlanProgress,
             records: dict[str, dict[str, Any]]) -> tuple[list[tuple[str, str]], str | None]:
    """What could be started, in order, or the reason a person must look first.

    Continues and retries come first (their worktrees hold paid-for work), then the
    ready tasks in plan order.
    """
    can_go_on: list[tuple[str, str]] = []
    for task in progress.tasks:
        record = records.get(task.task_id) or {}
        if task.state == "interrupted":
            can_go_on.append(("continue", task.task_id))
        elif task.state == "needs_review" and record.get("budget_stop"):
            can_go_on.append(("continue", task.task_id))
        elif task.state == "needs_review" and record.get("limit_stop"):
            resets = limit_not_reset(record)
            if resets:
                return [], (f"{task.task_id} was stopped by Claude's usage limit, which resets at "
                            f"{when_text(resets)}. Run run-plan again after that; {task.task_id} continues "
                            "from where it stopped.")
            can_go_on.append(("continue", task.task_id))
        elif task.state == "failed" and retry_problem(record) is None:
            can_go_on.append(("retry", task.task_id))
        elif task.state in ("failed", "needs_review", "cancelled"):
            # A person must look; nothing else runs until they have.
            return [], _stop_reason(progress.run_id, task.task_id, task.state, record, task.reason)
    return can_go_on + [("run", task_id) for task_id in progress.ready], None


def next_action(progress: PlanProgress,
                records: dict[str, dict[str, Any]]) -> tuple[str, str] | str | None:
    """(action, task) to do next, a reason to stop, or None when the plan is complete."""
    if progress.state == "complete":
        return None
    for task in progress.with_state("running"):
        return _running_elsewhere(task)
    choices, stop = _choices(progress, records)
    if stop:
        return stop
    if choices:
        return choices[0]
    return "No task can start, and none is running."  # not expected: waiting needs a cause


@dataclass(frozen=True, slots=True)
class Launches:
    """What run-plan does now: tasks to start, or why it starts nothing more."""
    start: tuple[tuple[str, str], ...] = ()
    stop: str | None = None
    complete: bool = False


def next_actions(progress: PlanProgress, records: dict[str, dict[str, Any]], active: set[str],
                 safe: Mapping[str, bool], slots: int) -> Launches:
    """The steps to start now, given the tasks (``active``) this run-plan already runs.

    ``safe`` is each task's parallel-safe flag from the plan and ``slots`` the most tasks
    that may run together. Tasks are taken in the order of ``_choices``; at the first one
    that cannot start now, the rest wait, so a task that has to run alone is never
    passed over by later ones.
    """
    if progress.state == "complete":
        return Launches(complete=True)
    for task in progress.with_state("running"):
        if task.task_id not in active:
            return Launches(stop=_running_elsewhere(task))
    choices, stop = _choices(progress, records)
    if stop:
        return Launches(stop=stop)
    alone = any(not safe.get(task_id, False) for task_id in active)  # a task running by itself
    start: list[tuple[str, str]] = []
    for action, task_id in choices:
        if task_id in active:  # started by this run-plan; its record may not exist yet
            continue
        if alone or len(active) + len(start) >= slots:
            break
        if not safe.get(task_id, False):
            if not active and not start:
                start.append((action, task_id))  # it runs by itself
            break
        start.append((action, task_id))
    if not start and not active:
        return Launches(stop="No task can start, and none is running.")
    return Launches(tuple(start))


def max_parallel_setting(one_at_a_time: bool = False,
                         environ: Mapping[str, str] | None = None) -> int:
    """How many tasks run-plan runs together: 1 with --one-at-a-time, else the setting or 3."""
    if one_at_a_time:
        return 1
    environ = os.environ if environ is None else environ
    raw = (environ.get(MAX_PARALLEL_VARIABLE) or "").strip()
    if not raw:
        return DEFAULT_MAX_PARALLEL
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_PARALLEL_LIMIT:
        raise ExecutionRefused(
            f"{MAX_PARALLEL_VARIABLE} is {raw!r}; it must be a whole number from 1 to "
            f"{MAX_PARALLEL_LIMIT} (how many tasks run together). Fix it in .env or the window, "
            "or use --one-at-a-time.")
    return int(raw)


def describe_next(decision: tuple[str, str] | str | None) -> str:
    """``next_action``'s answer for the plan line: "continue T2", or "" when it would stop."""
    if not isinstance(decision, tuple):
        return ""
    action, task_id = decision
    verb = {"run": "start", "retry": "retry", "continue": "continue"}[action]
    return f"{verb} {task_id}"


# --- working through the plan -----------------------------------------------------------------


OnStep = Callable[[str, str, "Step | None"], None]  # (action, task, finished step or None)


@dataclass
class _Running:
    """A task this run-plan has started and is waiting for."""
    handle: Handle
    action: str
    before: tuple[Any, ...] | None   # what the record looked like before, to see it change


def _wait_for_any(running: dict[str, _Running],
                  poll_seconds: float = POLL_SECONDS) -> list[tuple[str, Ended]]:
    """Wait until at least one running task has ended; return those that have."""
    while True:
        ended = [(task_id, done) for task_id, task in running.items()
                 if (done := task.handle.poll()) is not None]
        if ended:
            return ended
        time.sleep(poll_seconds)


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
    launch: Launcher | None = None,
    max_parallel: int = 1,
) -> PlanRun:
    """Run the plan's tasks until it is complete or something stops it.

    ``launch`` starts each step: the CLI passes a ``ProcessLauncher`` (M7.2), so each
    task runs as its own process; without one the step runs here, in this process.
    ``max_parallel`` is how many tasks may run together (M7.3): 1 runs them one at a
    time, as before; the CLI passes 3 unless told otherwise.
    """
    plan = load_task_plan(plans_dir, run_id)
    lock = acquire_lock(runs_dir, run_id, get_command_line)
    result = PlanRun(run_id)
    common = dict(plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path,
                  runner_script=runner_script, invoke_runner=invoke_runner,
                  test_command=test_command, env=env)
    # Each task can be run, continued and retried; more steps than that is a bug, not a plan.
    limit = 4 * len(plan["tasks"]) + 2
    safe = {task["id"]: bool(task.get("parallel_safe")) for task in plan["tasks"]}
    launch = launch or InProcessLauncher(
        lambda action, task_id: _do(action, run_id, task_id, worktree_root, get_command_line,
                                    common))
    running: dict[str, _Running] = {}
    halt: str | None = None      # why nothing more is started; the plan stops once nothing runs
    refused = False

    def progress_now() -> PlanProgress:
        return plan_progress(plan, runs_dir, run_id, get_command_line=get_command_line,
                             runner_script=Path(runner_script))

    try:
        while True:
            result.progress = progress_now()
            if halt is None:
                decision = next_actions(result.progress, list_tasks(runs_dir, run_id),
                                        set(running), safe, max_parallel)
                if decision.complete:
                    return result
                halt = decision.stop
                for action, task_id in decision.start:
                    if len(result.steps) + len(running) >= limit:
                        halt = (f"Stopped after {limit} steps; this should not happen, "
                                "please report it.")
                        break
                    if on_step:
                        on_step(action, task_id, None)
                    before = _seen(runs_dir, run_id, task_id)
                    running[task_id] = _Running(launch(action, task_id), action, before)
            if not running:
                result.stopped = halt or "No task can start, and none is running."
                result.refused = refused
                return result

            for task_id, ended in _wait_for_any(running):
                task = running.pop(task_id)
                record = _settled(runs_dir, run_id, task_id, _record(runs_dir, run_id, task_id))
                problem = _not_finished(task.action, task_id, ended, task.before, record)
                if problem is not None:
                    if halt is None:  # the first reason is the one given
                        halt, refused = problem, ended.refused
                    continue
                assert record is not None
                reasons = record.get("reasons") or []
                step = Step(task_id, task.action, record["status"], int(record.get("attempt") or 1),
                            record.get("cost_usd"),
                            _budget_reason(reasons) if record.get("budget_stop") else
                            (str(reasons[0]) if reasons else None),
                            bool(record.get("budget_stop")),
                            _limit_resets(record))
                result.steps.append(step)
                if on_step:
                    on_step(task.action, task_id, step)
                if halt is None and step.limit_stop is not None:
                    when = f", which resets at {when_text(step.limit_stop)}" if step.limit_stop else ""
                    halt = (f"{task_id} was stopped by Claude's usage limit{when}. Every "
                            "later agent would be refused too, so the plan stops here. Run "
                            f"run-plan again after the reset: {task_id} continues from "
                            "where it stopped.")
                elif halt is None and step.budget_stop:
                    halt = (f"{task_id} was stopped by the run budget. {step.reason} "
                            f"Then run-plan again: {task_id} continues from where it stopped.")
    except BaseException:
        for task in running.values():  # Ctrl+C or a bug: no process is left running
            task.handle.stop()
        raise
    finally:
        release_lock(lock)


def _limit_resets(record: dict[str, Any]) -> str | None:
    limit = record.get("limit_stop")
    if not isinstance(limit, dict):
        return None
    return str(limit.get("resets_at") or "")


def _record(runs_dir: Path, run_id: str, task_id: str) -> dict[str, Any] | None:
    try:
        return load_task(runs_dir, run_id, task_id)
    except RunRegistryError:
        return None


def _settled(runs_dir: Path, run_id: str, task_id: str,
             record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The record once its final status is in, waiting a little when it is not yet.

    ``cancel`` stops a task's process first and writes "cancelled" right after; read
    in between, the record would make a cancel look like a crash.
    """
    deadline = time.monotonic() + SETTLE_SECONDS
    while (record is not None and record["status"] not in FINAL_STATUSES
           and time.monotonic() < deadline):
        time.sleep(SETTLE_POLL_SECONDS)
        record = _record(runs_dir, run_id, task_id)
    return record


def _seen(runs_dir: Path, run_id: str, task_id: str) -> tuple[Any, ...] | None:
    """Enough of a task's record to tell, afterwards, whether a step changed it."""
    record = _record(runs_dir, run_id, task_id)
    return None if record is None else (record.get("attempt"), record.get("updated_at"))


def _not_finished(action: str, task_id: str, ended: Ended, before: tuple[Any, ...] | None,
                  record: dict[str, Any] | None) -> str | None:
    """Why the step left no final record (refused, or its process died), or None."""
    log = f" Its output: {ended.log}" if ended.log else ""
    if ended.refused:
        return f"{task_id} was not {_past(action)}: {ended.error or 'it was refused.'}"
    started = record is not None and (record.get("attempt"), record.get("updated_at")) != before
    if not started:
        why = ended.error or f"its process ended with exit code {ended.exit_code}."
        return f"{task_id} was not {_past(action)}: {why}{log}"
    assert record is not None
    if record["status"] in FINAL_STATUSES:
        return None
    return (f"{task_id}'s process ended (exit code {ended.exit_code}) while the task was "
            f"{record['status']}, without finishing it.{log} Run run-plan again: {task_id} "
            "continues from where it stopped.")


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
    return continue_task(run_id, task_id, is_alive=alive, worktree_root=worktree_root, **common)


# --- showing it ---------------------------------------------------------------------------------


def format_step(action: str, task_id: str, step: Step | None) -> str:
    verb = {"run": "Starting", "retry": "Retrying", "continue": "Continuing"}[action]
    if step is None:
        extra = {"retry": " (it failed; once more, told what was wrong)",
                 "continue": " (from where it stopped, in its own worktree)"}.get(action, "")
        return f"{verb} {task_id}{extra}..."
    cost = f", ${step.cost_usd:.4f}" if isinstance(step.cost_usd, (int, float)) else ""
    attempt = f" (attempt {step.attempt})" if step.attempt > 1 else ""
    why = f": {step.reason[:150]}" if step.reason and step.status != "done" else ""
    return f"{task_id}{attempt}: {step.status}{cost}{why}"
