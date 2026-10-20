"""Structured task-level plans: parse, validate, store, load.

A plan is stored at ``<plans_dir>/<run_id>/plan.json`` next to (but never as)
the run's ``orchestration.json``: saving a plan creates no execution-run
record. Everything read back from disk or from the AI is re-validated, since
neither is trusted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from orchestrator.run_registry import (
    RunRegistryError,
    run_directory,
    utc_now,
    write_json_atomic,
)

SCHEMA_VERSION = 1
PLAN_FILE_NAME = "plan.json"

TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
MAX_TASKS = 30
MAX_TITLE = 200
MAX_TEXT = 4000
MAX_LIST_ITEMS = 30
MAX_ITEM = 500


class PlanValidationError(ValueError):
    """The AI's output (or a stored plan) does not match the schema."""


def _text(value: object, field: str, *, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(f"{field} must be text.")
    value = value.strip()
    if required and not value:
        raise PlanValidationError(f"{field} must not be empty.")
    if len(value) > limit:
        raise PlanValidationError(f"{field} is longer than {limit} characters.")
    return value


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise PlanValidationError(
            f"{field} must be a list of at most {MAX_LIST_ITEMS} items."
        )
    return [_text(item, f"{field} item", limit=MAX_ITEM) for item in value]


def _relative_path(value: str) -> str:
    """Files are advisory hints; refuse anything that points outside the repo."""
    if (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or value.startswith(("~", "\\", "/"))
        or ".." in re.split(r"[\\/]", value)
    ):
        raise PlanValidationError(f"files entry must be repository-relative: {value!r}")
    return value


def validate_tasks(raw_tasks: object) -> list[dict[str, Any]]:
    """Validate and normalize the task list. Unknown fields are dropped."""
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanValidationError("tasks must be a non-empty list.")
    if len(raw_tasks) > MAX_TASKS:
        raise PlanValidationError(f"A plan may have at most {MAX_TASKS} tasks.")

    tasks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_tasks, start=1):
        if not isinstance(raw, dict):
            raise PlanValidationError(f"Task {index} must be an object.")
        task_id = raw.get("id")
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise PlanValidationError(f"Task {index} has an invalid id: {task_id!r}.")
        label = f"Task {task_id}"
        tasks.append(
            {
                "id": task_id,
                "title": _text(raw.get("title"), f"{label} title", limit=MAX_TITLE),
                "description": _text(
                    raw.get("description"), f"{label} description", limit=MAX_TEXT
                ),
                "depends_on": _string_list(raw.get("depends_on"), f"{label} depends_on"),
                "expected_outcome": _text(
                    raw.get("expected_outcome"),
                    f"{label} expected_outcome",
                    limit=MAX_TEXT,
                ),
                "files": [
                    _relative_path(item)
                    for item in _string_list(raw.get("files"), f"{label} files")
                ],
                "verification": _string_list(
                    raw.get("verification"), f"{label} verification"
                ),
            }
        )

    ids = [task["id"] for task in tasks]
    if len(set(ids)) != len(ids):
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        raise PlanValidationError(f"Duplicate task ids: {', '.join(duplicated)}.")

    known = set(ids)
    for task in tasks:
        for dependency in task["depends_on"]:
            if dependency == task["id"]:
                raise PlanValidationError(f"Task {task['id']} depends on itself.")
            if dependency not in known:
                raise PlanValidationError(
                    f"Task {task['id']} depends on missing task {dependency!r}."
                )
        if len(set(task["depends_on"])) != len(task["depends_on"]):
            raise PlanValidationError(f"Task {task['id']} lists a dependency twice.")

    _reject_cycles(tasks)
    return tasks


def _reject_cycles(tasks: list[dict[str, Any]]) -> None:
    """Kahn's algorithm: whatever cannot be ordered is part of a cycle."""
    remaining = {task["id"]: set(task["depends_on"]) for task in tasks}
    while remaining:
        ready = [task_id for task_id, deps in remaining.items() if not deps]
        if not ready:
            raise PlanValidationError(
                "Task dependencies contain a cycle: "
                + ", ".join(sorted(remaining))
                + "."
            )
        for task_id in ready:
            del remaining[task_id]
        for deps in remaining.values():
            deps.difference_update(ready)


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _candidate_documents(text: str) -> list[str]:
    candidates = [text.strip()]
    candidates += [match.strip() for match in _FENCE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    return candidates


def parse_plan_output(text: object) -> dict[str, Any]:
    """Turn the agent's final message into ``{"summary": str, "tasks": [...]}``.

    Accepts bare JSON, a fenced block, or JSON surrounded by prose.
    """
    if not isinstance(text, str) or not text.strip():
        raise PlanValidationError("The planner returned no output.")

    document: Any = None
    for candidate in _candidate_documents(text):
        try:
            document = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict):
            break
        document = None
    if document is None:
        raise PlanValidationError("The planner output did not contain a JSON object.")

    summary = document.get("summary", "")
    return {
        "summary": _text(summary, "summary", limit=MAX_TEXT, required=False),
        "tasks": validate_tasks(document.get("tasks")),
    }


def plan_path(plans_dir: Path, run_id: str) -> Path:
    return run_directory(plans_dir, run_id) / PLAN_FILE_NAME


def save_task_plan(plans_dir: Path, run_id: str, document: dict[str, Any]) -> Path:
    """Persist a plan. Never overwrites, and never writes a run record."""
    path = plan_path(plans_dir, run_id)
    if path.exists():
        raise FileExistsError(f"A plan already exists for run {run_id}: {path}")

    record = {
        "schema_version": SCHEMA_VERSION,
        **document,
        "run_id": run_id,
        "created_at": utc_now(),
    }
    # Re-validate what we are about to write.
    record["tasks"] = validate_tasks(record.get("tasks"))

    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, record)
    return path


def load_task_plan(plans_dir: Path, run_id: str) -> dict[str, Any]:
    """Read and re-validate a stored plan."""
    path = plan_path(plans_dir, run_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RunRegistryError(f"No plan found for run {run_id}.") from None
    except (OSError, json.JSONDecodeError):
        raise RunRegistryError(f"Plan file is unreadable or malformed: {run_id}") from None

    if not isinstance(record, dict) or record.get("run_id") != run_id:
        raise RunRegistryError(f"Plan file does not match its run ID: {run_id}")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise RunRegistryError(
            f"Unsupported plan schema version: {record.get('schema_version')!r}"
        )
    try:
        record["tasks"] = validate_tasks(record.get("tasks"))
    except PlanValidationError as exc:
        raise RunRegistryError(f"Stored plan is invalid: {exc}") from None
    return record
