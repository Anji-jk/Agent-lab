"""run-plan runs each task as its own process and watches it (M7.2).

Before M7.2, run-plan ran a task's pipeline inside its own process, so it could
do nothing else until the task ended, and a crash in the task was a crash of
run-plan. Now run-plan starts ``python -m orchestrator run-task <run> <task>``
(with ``--retry`` or ``--continue`` when that is the step) as a child process,
waits for it, and reads what happened from the task's record, as before.

* The child is started in the same console, so Ctrl+C reaches it as well: both
  stop, and the next run-plan continues the task, as before.
* What the child prints goes to ``runs/<run>/run-plan-logs/<task>-<n>.log``, not
  to run-plan's window, which keeps printing one line per step. The log is kept
  outside the task's folder because a retry moves that folder's files.
* A child that exits without leaving a final record crashed: run-plan stops and
  names its exit code and its log, instead of going on blindly.
* A child that refuses to start (the run budget, a task that is not ready) exits
  with code 4 and the reason on its last ``Error:`` line; run-plan reports that,
  as it reported the refusal before.

``cancel <run> <task>`` on a task run-plan started now stops just that task's
process (its record holds the child's process ID), and run-plan, seeing the
task cancelled, stops with the usual message.

``Launcher`` is what run_plan uses to start a task. ``ProcessLauncher`` starts
real processes; ``InProcessLauncher`` runs the task in the same process and is
what tests use with stand-in agents (a stand-in cannot cross into a child).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from orchestrator.errors import ExecutionRefused
from orchestrator.execution.background import run_task_command
from orchestrator.execution.runner_adapter import PROJECT_ROOT
from orchestrator.records.run_registry import run_directory

EXIT_REFUSED = 4           # the CLI's code for a refusal (cli/commands.py)
LOG_FOLDER = "run-plan-logs"
POLL_SECONDS = 1.0
STOP_GRACE_SECONDS = 30.0  # after Ctrl+C, how long the child may take to stop by itself
FLAGS = {"run": [], "retry": ["--retry"], "continue": ["--continue"]}


@dataclass
class Ended:
    """How a task's process ended."""
    exit_code: int | None
    log: Path | None = None
    error: str | None = None   # the refusal or error it printed, if any
    refused: bool = False      # it refused to start; nothing was created or spent


class Handle(Protocol):
    task_id: str
    action: str

    def poll(self) -> Ended | None: ...
    def stop(self) -> None: ...


Launcher = Callable[[str, str], Handle]  # (action, task) -> the started task


def last_error(log: Path) -> str | None:
    """The last line the CLI printed as ``Error: ...`` (its refusals and failures)."""
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("Error: "):
            # A refusal may run over several lines; keep them with it.
            return "\n".join([lines[index][len("Error: "):], *lines[index + 1:]]).strip()
    return None


def log_tail(log: Path, lines: int = 15) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


# --- real processes -----------------------------------------------------------------------------


@dataclass
class TaskProcess:
    task_id: str
    action: str
    process: Any
    log: Path
    _ended: Ended | None = field(default=None, repr=False)

    def poll(self) -> Ended | None:
        if self._ended is None:
            code = self.process.poll()
            if code is not None:
                error = last_error(self.log)
                self._ended = Ended(code, self.log, error, refused=code == EXIT_REFUSED)
        return self._ended

    def stop(self, grace: float = STOP_GRACE_SECONDS,
             sleep: Callable[[float], None] = time.sleep) -> None:
        """After Ctrl+C: the child got it too; give it time, then make sure it is gone."""
        deadline = time.monotonic() + grace
        while self.process.poll() is None and time.monotonic() < deadline:
            sleep(0.2)
        if self.process.poll() is None:
            from orchestrator.execution.cancel import kill_tree
            kill_tree(self.process.pid)


@dataclass
class ProcessLauncher:
    run_id: str
    plans_dir: Path
    runs_dir: Path
    repo_path: Path
    worktree_root: Path | None = None
    popen: Callable[..., Any] = subprocess.Popen

    def log_path(self, task_id: str) -> Path:
        folder = run_directory(Path(self.runs_dir), self.run_id) / LOG_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        number = 1 + sum(1 for _ in folder.glob(f"{task_id}-*.log"))
        return folder / f"{task_id}-{number}.log"

    def command(self, action: str, task_id: str) -> list[str]:
        command = run_task_command(self.run_id, task_id, plans_dir=self.plans_dir,
                                   runs_dir=self.runs_dir, repo_path=self.repo_path,
                                   worktree_root=self.worktree_root)
        return command + FLAGS[action]

    def __call__(self, action: str, task_id: str) -> TaskProcess:
        log = self.log_path(task_id)
        command = self.command(action, task_id)
        with open(log, "ab") as out:  # the child keeps its own handle
            process = self.popen(command, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT)
        return TaskProcess(task_id, action, process, log)


# --- in this process (tests) ------------------------------------------------------------------


@dataclass
class Done:
    """A task already run to its end in this process."""
    task_id: str
    action: str
    ended: Ended

    def poll(self) -> Ended:
        return self.ended

    def stop(self) -> None:
        return None


@dataclass
class InProcessLauncher:
    """Runs the step here, to its end, like run-plan before M7.2."""
    do: Callable[[str, str], Any]  # (action, task) -> the task's record

    def __call__(self, action: str, task_id: str) -> Done:
        try:
            self.do(action, task_id)
        except ExecutionRefused as exc:
            return Done(task_id, action, Ended(EXIT_REFUSED, None, str(exc), refused=True))
        return Done(task_id, action, Ended(0))


def wait(handle: Handle, poll_seconds: float = POLL_SECONDS,
         sleep: Callable[[float], None] = time.sleep) -> Ended:
    """Wait for one task to end; on Ctrl+C make sure its process stops too."""
    try:
        while True:
            ended = handle.poll()
            if ended is not None:
                return ended
            sleep(poll_seconds)
    except BaseException:
        handle.stop()
        raise
