"""Stop a running task and everything it started (M3.9).

``python -m orchestrator cancel <run> <task>``:

1. refuses a task that has already finished;
2. stops the task's pipeline process and every process under it -- the agent's
   runner, Claude, and any tests or commands Claude started. The process IDs come
   from the task record (``pid``) and ``launch.json`` (background runs);
3. also stops an agent whose pipeline has already died, using the Claude process
   ID in that agent's ``progress.json``;
4. marks the task ``cancelled``, marks a running agent ``cancelled`` too (so status
   and the dashboard stop showing it as running), and logs the step;
5. keeps the worktree, so you can look at what the agent had done.

A process ID can be reused by Windows or Linux once its process has ended. So a
process is stopped only if its command line shows it is still this task's
(``orchestrator run-task <run> <task>``) or an agent's (``claude``). Anything
else is left alone and reported.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import runner_adapter
from orchestrator.execution.background import launch_path
from orchestrator.execution.roles import ROLE_NAMES
from orchestrator.records.run_registry import utc_now, write_json_atomic
from orchestrator.records.task_events import TaskLog
from orchestrator.records.task_registry import FINAL_STATUSES, load_task, update_task

STOP_WAIT_SECONDS = 10.0

CommandLine = Callable[[int], "str | None"]
Kill = Callable[[int], None]


# --- looking at and stopping processes ----------------------------------------------------


def command_line(pid: int) -> str | None:
    """The process's command line, or None if no such process is running."""
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            return completed.stdout.strip() or None
        proc = Path("/proc") / str(int(pid)) / "cmdline"
        if Path("/proc").is_dir():
            raw = proc.read_bytes()
            return raw.replace(b"\0", b" ").decode(errors="replace").strip() or None  # empty: a zombie
        completed = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                                   capture_output=True, text=True, check=False)
        return completed.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _descendants(pid: int) -> list[int]:
    """Every process under ``pid`` (POSIX), deepest first."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="],
                                 capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    stack = list(children.get(pid, []))
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(children.get(child, []))
    return list(reversed(found))


def kill_tree(pid: int) -> None:
    """Stop ``pid`` and everything under it."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, text=True, check=False)
        return
    targets = [*_descendants(pid), pid]
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(command_line(t) for t in targets):
        time.sleep(0.1)
    for target in targets:
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def is_task_process(line: str, run_id: str, task_id: str) -> bool:
    words = line.split()
    return "orchestrator" in line and "run-task" in words and run_id in words and task_id in words


def is_agent_process(line: str) -> bool:
    return "claude" in line.lower()


# --- cancelling a task ---------------------------------------------------------------------


@dataclass
class Cancelled:
    stopped: list[int] = field(default_factory=list)       # this task's processes, stopped
    already_gone: list[int] = field(default_factory=list)  # had ended before cancel
    not_ours: list[int] = field(default_factory=list)      # ID now used by another program
    agents: list[str] = field(default_factory=list)        # agents that were running
    worktree: str | None = None


def _launch_pid(runs_dir: Path, run_id: str, task_id: str) -> int | None:
    try:
        pid = json.loads(launch_path(runs_dir, run_id, task_id).read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError):
        return None
    return pid if isinstance(pid, int) else None


def _running_agents(run_id: str, task_id: str, runner_script: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    running = []
    for role in ROLE_NAMES:
        path = runner_adapter.runner_run_directory(f"{run_id}-{task_id}-{role}", runner_script) / "progress.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("state") in ("starting", "running"):
            running.append((role, path, data))
    return running


def cancel_task(
    run_id: str,
    task_id: str,
    *,
    runs_dir: Path,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    get_command_line: CommandLine = command_line,
    kill: Kill = kill_tree,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Cancelled:
    record = load_task(runs_dir, run_id, task_id)  # RunRegistryError if it never started
    if record["status"] in FINAL_STATUSES:
        raise ExecutionRefused(
            f"{task_id} of run {run_id} has already finished ({record['status']}); "
            "there is nothing to cancel."
        )
    result = Cancelled(worktree=record.get("worktree_path"))

    # The pipeline first: stopping it stops the agent under it, and it can no longer
    # write a verdict over the cancellation.
    pids: list[int] = []
    for pid in (_launch_pid(runs_dir, run_id, task_id), record.get("pid")):
        if isinstance(pid, int) and pid not in pids:
            pids.append(pid)
    # Look at every ID before stopping any: on Windows the .venv launcher in
    # launch.json holds the real Python, so stopping it ends the other one too.
    seen = {pid: get_command_line(pid) for pid in pids}
    for pid, line in seen.items():
        if line is None:
            result.already_gone.append(pid)
        elif is_task_process(line, run_id, task_id):
            if get_command_line(pid) is not None:  # not already ended with its parent
                kill(pid)
            result.stopped.append(pid)
        else:
            result.not_ours.append(pid)

    # Then any agent still running on its own (its pipeline had already died).
    running = _running_agents(run_id, task_id, Path(runner_script))
    for role, _, data in running:
        result.agents.append(role)
        pid = data.get("pid")
        if not isinstance(pid, int) or pid in result.stopped:
            continue
        line = get_command_line(pid)
        if line is None:
            continue
        if is_agent_process(line):
            kill(pid)
            result.stopped.append(pid)
        else:
            result.not_ours.append(pid)

    # Only call it cancelled once everything stopped is really gone.
    deadline = clock() + STOP_WAIT_SECONDS
    alive = [pid for pid in result.stopped if get_command_line(pid) is not None]
    while alive and clock() < deadline:
        sleep(0.25)
        alive = [pid for pid in alive if get_command_line(pid) is not None]
    if alive:
        raise ExecutionRefused(
            f"Process {', '.join(map(str, alive))} did not stop. Run cancel again, or end "
            "it in Task Manager. The task was not marked cancelled."
        )

    now = utc_now()
    for _, path, data in running:
        write_json_atomic(path, {**data, "state": "finished", "status": "cancelled",
                                 "finished_at": now})
    reason = f"Cancelled by you at {now}."
    try:
        update_task(runs_dir, run_id, task_id, {"status": "cancelled", "reasons": [reason],
                                                "cancelled_at": now})
    except ValueError:
        # The pipeline got to a final status just before it was stopped.
        status = load_task(runs_dir, run_id, task_id)["status"]
        raise ExecutionRefused(
            f"{task_id} of run {run_id} finished ({status}) just before it could be cancelled."
        ) from None
    TaskLog(runs_dir, run_id, task_id).emit(
        "task_cancelled", stopped=result.stopped, already_gone=result.already_gone,
        not_ours=result.not_ours, agents=result.agents)
    return result