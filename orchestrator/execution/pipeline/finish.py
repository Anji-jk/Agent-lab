"""Finishing a task: what it is committed as."""

from __future__ import annotations

from typing import Any


def commit_message(plan: dict[str, Any], task: dict[str, Any], run_id: str) -> str:
    issue = plan["issue"]
    criteria = "\n".join(f"- {c}" for c in task["acceptance_criteria"])
    return (
        f"{task['id']}: {task['title']}\n\n"
        f"Issue #{issue['number']}, run {run_id}.\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        "Built, tested and reviewed by separate agents; verified by the orchestrator."
    )
