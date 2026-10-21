"""One record per planned task, kept next to its run.

``<runs_dir>/<run_id>/tasks/<task_id>/task.json`` says where a task stands:
which agent is working on it, what each agent cost, what the orchestrator
found, and why it ended in the state it did. Like the run registry, writes
are atomic and serialized with an OS lock, and a status may only move along
the arrows below.

pending -> building -> testing -> reviewing -> done

From building, testing or reviewing a task can also end as failed,
needs_review or cancelled (pending can go to failed or cancelled).

``failed`` means the work is wrong and trying again may help. ``needs_review``
means the orchestrator cannot tell, or an agent broke a rule, so a person
should look. Only the orchestrator moves a task to ``done``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.run_registry import (
    RunRegistryError,
    locked,
    run_directory,
    utc_now,
    write_json_atomic,
)
from orchestrator.task_plan import TASK_ID_PATTERN

TASK_RECORD_NAME = "task.json"

TASK_STATUSES = frozenset(
    {
        "pending",
        "building",
        "testing",
        "reviewing",
        "done",
        "failed",
        "needs_review",
        "cancelled",
    }
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"building", "failed", "cancelled"}),
    "building": frozenset({"testing", "failed", "needs_review", "cancelled"}),
    "testing": frozenset({"reviewing", "failed", "needs_review", "cancelled"}),
    "reviewing": frozenset({"done", "failed", "needs_review", "cancelled"}),
    "done": frozenset(),
    "failed": frozenset({"needs_review"}),
    "needs_review": frozenset(),
    "cancelled": frozenset(),
}

ACTIVE_STATUSES = frozenset({"building", "testing", "reviewing"})
FINAL_STATUSES = frozenset({"done", "failed", "needs_review", "cancelled"})

IMMUTABLE_FIELDS = frozenset({"run_id", "task_id", "created_at", "history"})


def validate_task_id(task_id: object) -> str:
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("Invalid task ID.")
    return task_id


def validate_task_transition(current: str, new: str) -> None:
    for name in (current, new):
        if name not in TASK_STATUSES:
            raise ValueError(f"Invalid task status: {name!r}")
    if new != current and new not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Illegal task status transition: {current} -> {new}")


def task_directory(runs_dir: Path, run_id: str, task_id: str) -> Path:
    validate_task_id(task_id)
    return run_directory(runs_dir, run_id) / "tasks" / task_id


def _record_path(runs_dir: Path, run_id: str, task_id: str) -> Path:
    return task_directory(runs_dir, run_id, task_id) / TASK_RECORD_NAME


def _validate_record(record: object, run_id: str, task_id: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RunRegistryError("Task record must be a JSON object.")
    if record.get("run_id") != run_id or record.get("task_id") != task_id:
        raise RunRegistryError("Task record does not match its run and task IDs.")
    if record.get("status") not in TASK_STATUSES:
        raise RunRegistryError(f"Task record has invalid status: {record.get('status')!r}")
    for field in ("created_at", "updated_at"):
        try:
            datetime.fromisoformat(record[field])
        except (KeyError, TypeError, ValueError):
            raise RunRegistryError(f"Task record field {field!r} is not a timestamp.") from None
    return record


def save_task(
    runs_dir: Path, run_id: str, task_id: str, metadata: dict[str, Any]
) -> Path:
    """Create a task record in ``pending`` (or the status given). Never overwrites."""
    directory = task_directory(runs_dir, run_id, task_id)
    status = metadata.get("status", "pending")
    if status not in TASK_STATUSES:
        raise ValueError(f"Invalid task status: {status!r}")

    with locked(directory):
        path = directory / TASK_RECORD_NAME
        if path.exists():
            raise FileExistsError(f"Task {task_id} of run {run_id} already has a record.")
        now = utc_now()
        record = {
            **metadata,
            "run_id": run_id,
            "task_id": task_id,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "history": [{"status": status, "at": now}],
        }
        write_json_atomic(path, record)
    return path


def load_task(runs_dir: Path, run_id: str, task_id: str) -> dict[str, Any]:
    path = _record_path(runs_dir, run_id, task_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RunRegistryError(f"No record for task {task_id} of run {run_id}.") from None
    except OSError as exc:
        raise RunRegistryError(f"Could not read task {task_id}: {type(exc).__name__}") from None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        raise RunRegistryError(f"Task record is malformed JSON: {task_id}") from None
    return _validate_record(record, run_id, task_id)


def update_task(
    runs_dir: Path, run_id: str, task_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Merge ``changes`` into the record. A status change must be legal and is
    added to ``history``. Nothing is written if validation fails."""
    forbidden = IMMUTABLE_FIELDS & changes.keys()
    if forbidden:
        raise ValueError(f"Cannot modify: {', '.join(sorted(forbidden))}")
    if "updated_at" in changes:
        raise ValueError("updated_at is managed by the registry.")

    directory = task_directory(runs_dir, run_id, task_id)
    if not (directory / TASK_RECORD_NAME).exists():
        raise RunRegistryError(f"No record for task {task_id} of run {run_id}.")

    with locked(directory):
        record = load_task(runs_dir, run_id, task_id)  # re-read under the lock
        now = utc_now()
        history = list(record.get("history", []))
        if "status" in changes and changes["status"] != record["status"]:
            validate_task_transition(record["status"], changes["status"])
            history.append({"status": changes["status"], "at": now})
        elif "status" in changes:
            validate_task_transition(record["status"], changes["status"])
        updated = {**record, **changes, "updated_at": now, "history": history}
        write_json_atomic(directory / TASK_RECORD_NAME, updated)
    return updated


def list_tasks(runs_dir: Path, run_id: str) -> dict[str, dict[str, Any]]:
    """Every task record of a run, by task ID."""
    tasks_dir = run_directory(runs_dir, run_id) / "tasks"
    if not tasks_dir.is_dir():
        return {}
    found: dict[str, dict[str, Any]] = {}
    for child in sorted(tasks_dir.iterdir()):
        if child.is_dir() and (child / TASK_RECORD_NAME).is_file():
            found[child.name] = load_task(runs_dir, run_id, child.name)
    return found
