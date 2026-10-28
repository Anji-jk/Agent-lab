"""Whether a task's own process is still running."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.core.attempts import agent_run_id, attempt_of
from orchestrator.core.runner_paths import DEFAULT_RUNNER_SCRIPT
from orchestrator.execution.cancel import command_line, is_task_process
from orchestrator.monitoring.progress import STAGE_ROLE, agent_view, progress_path, read_progress
from orchestrator.records.task_registry import launch_path

CommandLine = Callable[[int], "str | None"]
# (task_id, record) -> (still running?, why we think so)
Liveness = Callable[[str, dict[str, Any]], "tuple[bool, str]"]


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
    runner_script: Path = DEFAULT_RUNNER_SCRIPT,
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
