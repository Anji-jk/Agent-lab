"""Where a stored plan stands right now (M4.1).

The planner decides once what the tasks are and which depends on which. This
module answers the question the plan cannot: which of them are done, failed or
running *now*, and which can start next. The scheduler asks it before every
task and after every restart, because it keeps no memory of its own; ``status``
shows the same answer.

It only reads (the plan, the task records, process command lines and progress
files), so it can never disturb a run.

Each task gets one state:

* ``done``, ``failed``, ``needs_review``, ``cancelled``: from its record.
* ``running``: its record is at a stage and its process is still this task's.
* ``interrupted``: its record is at a stage but its process has gone (a closed
  window, a crash, a reboot). Nobody needs to run ``cancel`` for this to be seen.
* ``ready``: not started, and every dependency is done.
* ``waiting``: not started, and a dependency is not done yet but may still be.
* ``blocked``: not started, and a dependency, directly or through a chain, ended
  failed, needs review or cancelled. The tasks that block it are named.

``interrupted`` and ``blocked`` are worked out every time, never saved: a saved
"blocked" would be wrong as soon as the task blocking it is retried and passes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.execution import runner_adapter
from orchestrator.execution.background import launch_path
from orchestrator.execution.cancel import command_line, is_task_process
from orchestrator.monitoring.progress import (
    STAGE_ROLE,
    agent_run_id,
    attempt_of,
    agent_view,
    progress_path,
    read_progress,
)
from orchestrator.records.task_registry import list_tasks

RECORDED_STATES = frozenset({"done", "failed", "needs_review", "cancelled"})
BLOCKING_STATES = frozenset({"failed", "needs_review", "cancelled"})
REASON_LIMIT = 120

CommandLine = Callable[[int], "str | None"]
# (task_id, record) -> (still running?, why we think so)
Liveness = Callable[[str, dict[str, Any]], "tuple[bool, str]"]


@dataclass(frozen=True, slots=True)
class TaskProgress:
    task_id: str
    title: str
    state: str
    depends_on: tuple[str, ...] = ()
    stage: str | None = None              # the record's stage while running or interrupted
    blocked_by: tuple[str, ...] = ()      # the failed/cancelled tasks that block this one
    waiting_for: tuple[str, ...] = ()     # dependencies not done yet
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlanProgress:
    run_id: str
    tasks: tuple[TaskProgress, ...]
    unknown_records: tuple[str, ...] = ()  # records for task IDs the plan does not have

    def task(self, task_id: str) -> TaskProgress:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(task_id)

    def with_state(self, *states: str) -> list[TaskProgress]:
        return [t for t in self.tasks if t.state in states]

    @property
    def ready(self) -> list[str]:
        """Tasks that can start now, in plan order."""
        return [t.task_id for t in self.with_state("ready")]

    @property
    def next_task(self) -> str | None:
        ready = self.ready
        return ready[0] if ready else None

    @property
    def state(self) -> str:
        """``complete``, ``running``, ``ready`` (something can start) or ``stopped``."""
        if all(t.state == "done" for t in self.tasks):
            return "complete"
        if self.with_state("running"):
            return "running"
        if self.ready:
            return "ready"
        return "stopped"

    @property
    def stopped_by(self) -> list[TaskProgress]:
        """What keeps a stopped plan from going on: interrupted and unfinished tasks."""
        return self.with_state("interrupted", *sorted(BLOCKING_STATES))


def _first_reason(record: Mapping[str, Any]) -> str | None:
    reasons = record.get("reasons")
    if isinstance(reasons, list) and reasons and isinstance(reasons[0], str):
        text = " ".join(reasons[0].split())
        return text if len(text) <= REASON_LIMIT else text[: REASON_LIMIT - 1] + "…"
    return None


def compute_progress(
    run_id: str,
    plan: Mapping[str, Any],
    records: Mapping[str, dict[str, Any]],
    is_alive: Liveness,
) -> PlanProgress:
    """Each plan task's state from its record and its dependencies' states."""
    tasks = {task["id"]: task for task in plan["tasks"]}
    states: dict[str, TaskProgress] = {}

    def state_of(task_id: str) -> TaskProgress:
        if task_id in states:
            return states[task_id]
        task = tasks[task_id]
        common = {"task_id": task_id, "title": str(task.get("title", "")),
                  "depends_on": tuple(task["depends_on"])}
        record = records.get(task_id)
        if record is not None:
            status = record["status"]
            if status in RECORDED_STATES:
                result = TaskProgress(state=status, reason=_first_reason(record), **common)
            else:  # pending, building, testing or reviewing: is anything still running it?
                alive, why = is_alive(task_id, record)
                result = TaskProgress(state="running" if alive else "interrupted",
                                      stage=status, reason=None if alive else why, **common)
        else:
            blockers: list[str] = []
            waiting: list[str] = []
            for dependency in task["depends_on"]:
                found = state_of(dependency)
                if found.state in BLOCKING_STATES:
                    blockers.append(dependency)
                elif found.state == "blocked":
                    blockers.extend(found.blocked_by)
                elif found.state != "done":
                    waiting.append(dependency)
            blockers = list(dict.fromkeys(blockers))
            if blockers:  # it can't finish, even if other dependencies still might
                result = TaskProgress(state="blocked", blocked_by=tuple(blockers), **common)
            elif waiting:
                result = TaskProgress(state="waiting", waiting_for=tuple(waiting), **common)
            else:
                result = TaskProgress(state="ready", **common)
        states[task_id] = result
        return result

    ordered = tuple(state_of(task_id) for task_id in tasks)
    unknown = tuple(sorted(set(records) - set(tasks)))
    return PlanProgress(run_id, ordered, unknown)


# --- is a task's process still running? ---------------------------------------------------


def _launch_pid(runs_dir: Path, run_id: str, task_id: str) -> int | None:
    try:
        data = json.loads(launch_path(runs_dir, run_id, task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def process_liveness(
    runs_dir: Path,
    run_id: str,
    *,
    get_command_line: CommandLine | None = None,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    now: datetime | None = None,
) -> Liveness:
    """Checks a task's own process; for older records with no process ID, its heartbeat.

    A process ID counts only while its command line is still this task's
    ``run-task``: Windows and Linux reuse IDs, so an ID alone proves nothing.
    """
    get_command_line = get_command_line or command_line

    def is_alive(task_id: str, record: dict[str, Any]) -> tuple[bool, str]:
        pids = []
        for pid in (_launch_pid(runs_dir, run_id, task_id), record.get("pid")):
            if isinstance(pid, int) and not isinstance(pid, bool) and pid not in pids:
                pids.append(pid)
        if pids:
            for pid in pids:
                line = get_command_line(pid)
                if line is not None and is_task_process(line, run_id, task_id):
                    return True, f"process {pid} is running it"
            listed = ", ".join(map(str, pids))
            return False, f"its process ({listed}) is no longer running"
        role = STAGE_ROLE.get(record["status"])
        if role is not None:
            progress = read_progress(
                progress_path(agent_run_id(run_id, task_id, role, attempt_of(record)), runner_script))
            if progress is not None:
                view = agent_view(role, progress, now or datetime.now(timezone.utc))
                if view.state in ("starting", "running") and view.heartbeat_age is not None and not view.stale:
                    return True, f"the {role}'s heartbeat is recent"
        return False, "no process ID was recorded and there is no recent heartbeat"

    return is_alive


def plan_progress(
    plan: Mapping[str, Any],
    runs_dir: Path,
    run_id: str,
    *,
    get_command_line: CommandLine | None = None,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    now: datetime | None = None,
) -> PlanProgress:
    """Where the stored plan of ``run_id`` stands, read from disk."""
    records = list_tasks(runs_dir, run_id)
    alive = process_liveness(runs_dir, run_id, get_command_line=get_command_line,
                             runner_script=runner_script, now=now)
    return compute_progress(run_id, plan, records, alive)


# --- showing it -------------------------------------------------------------------------------


def _detail(task: TaskProgress, states: Mapping[str, str]) -> str:
    if task.state == "running":
        return str(task.stage)
    if task.state == "interrupted":
        return f"stopped while {task.stage}: {task.reason}"
    if task.state == "blocked":
        return "by " + ", ".join(f"{b} ({states.get(b, '?')})" for b in task.blocked_by)
    if task.state == "waiting":
        return "for " + ", ".join(task.waiting_for)
    if task.state == "ready":
        return "can start now"
    return task.reason or ""


def format_plan_progress(progress: PlanProgress) -> str:
    done = len(progress.with_state("done"))
    head = f"Plan: {done} of {len(progress.tasks)} done"
    running = [t.task_id for t in progress.with_state("running")]
    if progress.state == "complete":
        head += " · complete"
    elif running:
        head += f" · running {', '.join(running)}"
    if progress.next_task is not None:
        head += f" · next {progress.next_task}"
    if progress.state == "stopped":
        head += " · stopped: " + ", ".join(f"{t.task_id} {t.state}" for t in progress.stopped_by)
    states = {t.task_id: t.state for t in progress.tasks}
    lines = [head]
    for task in progress.tasks:
        lines.append(f"  {task.task_id:<4} {task.state:<12} {_detail(task, states)}".rstrip())
    if progress.unknown_records:
        lines.append(f"  Warning: records for tasks not in the plan: {', '.join(progress.unknown_records)}")
    return "\n".join(lines)