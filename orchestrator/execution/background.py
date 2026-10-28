"""Start a task's pipeline as its own process, so the terminal is free.

``run-task --background`` checks here what it can check at once (the plan, its
dependencies, that the task has not run before), then starts the same
``python -m orchestrator run-task <run> <task>`` as a separate process and returns.

* The process is detached from this terminal: closing the window or pressing
  Ctrl+C here does not stop it. On Windows it is started with no window, so the
  agents, tests and git commands it starts do not pop up windows either.
* Everything it prints goes to ``run-task.log`` in the task's folder.
* ``launch.json`` beside it records the process ID and the command, so later
  steps (cancel, pause) know what to stop.
* The parent waits until the child has written the task record, up to
  ``STARTUP_WAIT_SECONDS``. A child that dies before that (a bad setting, an
  import error) is reported now, with the end of its log, instead of failing
  silently in the background.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.execution.checks import project_python
from orchestrator.execution.runner_adapter import PROJECT_ROOT
from orchestrator.execution.task_runner import prepare
from orchestrator.records.run_registry import RunRegistryError, utc_now, write_json_atomic
from orchestrator.records.task_registry import (  # noqa: F401
    LAUNCH_FILE_NAME, LOG_FILE_NAME, launch_path, load_task, log_path,
)

STARTUP_WAIT_SECONDS = 20.0
POLL_SECONDS = 0.25
LOG_TAIL_LINES = 20

Popen = Callable[[list[str], Path, Path], Any]


def detached_options() -> dict[str, Any]:
    """How to start a process that outlives this terminal."""
    if sys.platform == "win32":
        # A new process group: Ctrl+C here is not passed on. No window: the child gets
        # a hidden console of its own, which survives this window closing and which
        # every program it starts shares, so none of them opens a window.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def start_detached(command: list[str], log_file: Path, cwd: Path) -> subprocess.Popen:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as out:  # the child keeps its own handle
        return subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            close_fds=True, **detached_options(),
        )


def run_task_command(run_id: str, task_id: str, *, plans_dir: Path, runs_dir: Path,
                     repo_path: Path, worktree_root: Path | None) -> list[str]:
    """The foreground run-task command the background process runs."""
    command = [
        project_python(), "-m", "orchestrator", "run-task", run_id, task_id,
        "--plans-dir", str(Path(plans_dir).resolve()),
        "--runs-dir", str(Path(runs_dir).resolve()),
        "--repo-path", str(Path(repo_path).resolve()),
    ]
    if worktree_root is not None:
        command += ["--worktree-root", str(Path(worktree_root).resolve())]
    return command


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log was written)"
    return "\n".join(text.splitlines()[-lines:]) or "(the log is empty)"


def _has_record(runs_dir: Path, run_id: str, task_id: str) -> bool:
    try:
        load_task(runs_dir, run_id, task_id)
    except (RunRegistryError, ValueError):
        return False
    return True


@dataclass(frozen=True, slots=True)
class Started:
    pid: int
    log: Path
    launch: Path
    record_seen: bool  # False: still starting after the wait; check the log


def start_task_in_background(
    run_id: str,
    task_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    worktree_root: Path | None = None,
    popen: Popen = start_detached,
    wait_seconds: float = STARTUP_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Started:
    # Everything that can be refused now is refused now, not in a log file later.
    prepare(run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path)
    try:
        record = load_task(runs_dir, run_id, task_id)
    except RunRegistryError:
        record = None
    if record is not None:
        raise ExecutionRefused(
            f"{task_id} of run {run_id} has already run (it is {record['status']}). "
            "A task runs once; plan again for a new run."
        )

    command = run_task_command(run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir,
                               repo_path=repo_path, worktree_root=worktree_root)
    log = log_path(runs_dir, run_id, task_id)
    process = popen(command, log, PROJECT_ROOT)
    launch = launch_path(runs_dir, run_id, task_id)
    launch.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(launch, {
        "pid": process.pid,
        "started_at": utc_now(),
        "command": command,
        "log": str(log),
    })

    deadline = clock() + wait_seconds
    while clock() < deadline:
        if _has_record(runs_dir, run_id, task_id):
            return Started(process.pid, log, launch, record_seen=True)
        code = process.poll()
        if code is not None:
            if _has_record(runs_dir, run_id, task_id):  # finished very fast
                return Started(process.pid, log, launch, record_seen=True)
            raise ExecutionRefused(
                f"The background run of {task_id} stopped at once (exit code {code}). "
                f"The end of its log ({log}):\n{_tail(log)}"
            )
        sleep(POLL_SECONDS)
    return Started(process.pid, log, launch, record_seen=False)