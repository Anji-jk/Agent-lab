"""What the builder of a task is told about the tasks it depends on that are done.

All of it comes from the orchestrator's own records of those tasks (``task.json``): the files
they changed as the orchestrator measured them (the builder's and the tester's), the commit
merged into the run's branch, and the builder's one-paragraph summary, kept when it reported.
Only direct dependencies that are done count; a task that depends on nothing gets nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.records.run_registry import RunRegistryError
from orchestrator.records.task_registry import load_task

SUMMARY_CHARS = 600


def finished_dependencies(runs_dir: Path, run_id: str, plan: dict[str, Any],
                          task: dict[str, Any]) -> list[dict[str, Any]]:
    planned = {item["id"]: item for item in plan.get("tasks") or []}
    found = []
    for task_id in task.get("depends_on") or []:
        try:
            record = load_task(runs_dir, run_id, task_id)
        except (RunRegistryError, OSError, ValueError):
            continue
        if record.get("status") != "done":
            continue
        checks = record.get("checks") or {}
        changed = {*(checks.get("builder_changed") or []), *(checks.get("tester_changed") or [])}
        other = planned.get(task_id) or {}
        found.append({
            "id": task_id,
            "jira_key": other.get("jira_key"),
            "title": other.get("title") or record.get("title") or "",
            "commit": record.get("commit"),
            "summary": checks.get("builder_summary"),
            "files": sorted(str(path) for path in changed),
        })
    return found
