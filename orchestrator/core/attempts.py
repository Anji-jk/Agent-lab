"""Which attempt a task record is, and the runner's ID for one of its agents."""

from __future__ import annotations

from typing import Any


def agent_run_id(run_id: str, task_id: str, role: str, attempt: int = 1) -> str:
    """The runner's ID for one agent of a task; ``task_runner`` names them this way.

    A retry is attempt 2 and gets its own IDs, so the first attempt's files stay.
    """
    if attempt <= 1:
        return f"{run_id}-{task_id}-{role}"
    return f"{run_id}-{task_id}-r{attempt}-{role}"


def attempt_of(record: dict[str, Any] | None) -> int:
    """Which attempt a task record is: 1 unless it says otherwise (older records say nothing)."""
    value = (record or {}).get("attempt")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 1
