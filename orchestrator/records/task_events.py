"""The orchestrator's own step-by-step log for one task.

The agents' events come from Claude (``runs/<agent run id>/events.jsonl``). What
the orchestrator does between them -- creating the worktree, checking which
files changed, running the full suite, deciding, committing -- is written
here, one JSON object per line, to ``<task directory>/events.jsonl``:

    {"at": "2026-10-22T10:05:44+00:00", "step": "suite_started", "command": [...]}

Logging is best effort: a full disk or a locked file must never stop a task,
so a failed write is skipped. The task record stays the source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.records.run_registry import utc_now
from orchestrator.records.task_registry import task_directory

TASK_EVENTS_FILE_NAME = "events.jsonl"


def task_events_path(runs_dir: Path, run_id: str, task_id: str) -> Path:
    return task_directory(runs_dir, run_id, task_id) / TASK_EVENTS_FILE_NAME


class TaskLog:
    """Appends orchestrator steps for one task."""

    def __init__(self, runs_dir: Path, run_id: str, task_id: str):
        self.path = task_events_path(runs_dir, run_id, task_id)

    def emit(self, step: str, **fields: Any) -> bool:
        entry = {"at": utc_now(), "step": step, **fields}
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except (OSError, TypeError, ValueError):
            return False
        return True


class NullLog:
    """A log that keeps nothing, for callers that do not want one."""

    def emit(self, step: str, **fields: Any) -> bool:
        return False
