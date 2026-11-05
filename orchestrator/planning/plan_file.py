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

from orchestrator.records.task_registry import TASK_ID_PATTERN  # noqa: F401
from orchestrator.records.run_registry import (
    RunRegistryError,
    run_directory,
    utc_now,
    write_json_atomic,
)

SCHEMA_VERSION = 2
COMPLEXITIES = ("small", "medium", "large")
PLAN_FILE_NAME = "plan.json"

MAX_TASKS = 30
MAX_TITLE = 200
MAX_TEXT = 4000
MAX_LIST_ITEMS = 30
MAX_ITEM = 500
# A Jira work item's key, such as KAN-11.
JIRA_ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,9}-[1-9][0-9]{0,9}")


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


def _complexity(value: object, field: str) -> str:
    if not isinstance(value, str) or value.strip().lower() not in COMPLEXITIES:
        raise PlanValidationError(f"{field} must be one of: {', '.join(COMPLEXITIES)}.")
    return value.strip().lower()


def _flag(value: object, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PlanValidationError(f"{field} must be true or false.")
    return value


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
                "acceptance_criteria": _acceptance_criteria(
                    raw.get("acceptance_criteria"), label
                ),
                "complexity": _complexity(raw.get("complexity"), f"{label} complexity"),
                "parallel_safe": _flag(raw.get("parallel_safe"), f"{label} parallel_safe"),
                **_story_fields(raw, label),
            }
        )

    ids = [task["id"] for task in tasks]
    if len(set(ids)) != len(ids):
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        raise PlanValidationError(f"Duplicate task ids: {', '.join(duplicated)}.")

    keys = [task["jira_key"] for task in tasks if "jira_key" in task]
    if len(set(keys)) != len(keys):
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        raise PlanValidationError(f"Sub-task in two tasks: {', '.join(duplicated)}.")

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
        extra = set(task.get("dependency_reasons", {})) - set(task["depends_on"])
        if extra:
            raise PlanValidationError(
                f"Task {task['id']} gives a reason for a task it does not depend on: "
                f"{', '.join(sorted(extra))}.")

    _reject_cycles(tasks)
    _enforce_parallel_safety(tasks)
    return tasks


def _story_fields(raw: dict[str, Any], label: str) -> dict[str, Any]:
    """Validate optional fields added to a Jira story task."""
    found: dict[str, Any] = {}
    if "jira_key" in raw:
        key = raw["jira_key"]
        if not isinstance(key, str) or not JIRA_ISSUE_KEY.fullmatch(key):
            raise PlanValidationError(f"{label} has an invalid jira_key: {key!r}.")
        found["jira_key"] = key
    if "dependency_reasons" in raw:
        reasons = raw["dependency_reasons"]
        if not isinstance(reasons, dict) or len(reasons) > MAX_LIST_ITEMS:
            raise PlanValidationError(f"{label} dependency_reasons must be an object.")
        found["dependency_reasons"] = {
            str(task_id): _text(reason, f"{label} reason for {task_id}", limit=MAX_ITEM)
            for task_id, reason in reasons.items()
        }
    if "doc_target" in raw:
        target = raw["doc_target"]
        if target is not None:
            target = _relative_path(_text(target, f"{label} doc_target", limit=MAX_ITEM))
        found["doc_target"] = target
    if "context" in raw:
        context = raw["context"]
        if not isinstance(context, dict):
            raise PlanValidationError(f"{label} context must be an object.")
        found["context"] = {
            "read_first": [
                _relative_path(item)
                for item in _string_list(context.get("read_first"), f"{label} read_first")
            ],
            "notes": _text(
                context.get("notes") or "", f"{label} notes", limit=MAX_TEXT, required=False
            ),
        }
    return found


def _acceptance_criteria(value: object, label: str) -> list[str]:
    criteria = _string_list(value, f"{label} acceptance_criteria")
    if not criteria:
        raise PlanValidationError(f"{label} needs at least one acceptance criterion.")
    return criteria


def _ancestors(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Every task each task depends on, directly or through others."""
    direct = {task["id"]: set(task["depends_on"]) for task in tasks}
    result: dict[str, set[str]] = {}

    def visit(task_id: str) -> set[str]:
        if task_id not in result:
            found: set[str] = set()
            for parent in direct[task_id]:
                found |= {parent} | visit(parent)
            result[task_id] = found
        return result[task_id]

    for task_id in direct:
        visit(task_id)
    return result


def _normalized_files(task: dict[str, Any]) -> set[str]:
    return {
        PurePosixPath(path.replace("\\", "/")).as_posix().lower().removeprefix("./")
        for path in task["files"]
    }


def _enforce_parallel_safety(tasks: list[dict[str, Any]]) -> None:
    """The AI's parallel_safe claim is trusted only when it can be checked.

    A task keeps ``parallel_safe`` only if it names its files and shares none
    with any task it is not ordered against (neither depends on the other).
    A task that does not list its files makes every unordered task unsafe,
    because overlap cannot be ruled out. Downgrading only reduces
    concurrency, so it can never cause a conflict.
    """
    ancestors = _ancestors(tasks)
    files = {task["id"]: _normalized_files(task) for task in tasks}
    for task in tasks:
        if not task["parallel_safe"]:
            continue
        mine = files[task["id"]]
        safe = bool(mine)
        for other in tasks:
            if not safe:
                break
            if other["id"] == task["id"]:
                continue
            ordered = (
                other["id"] in ancestors[task["id"]]
                or task["id"] in ancestors[other["id"]]
            )
            if ordered:
                continue
            theirs = files[other["id"]]
            safe = bool(theirs) and not (mine & theirs)
        task["parallel_safe"] = safe


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


def save_approval(plans_dir: Path, run_id: str, approval: dict[str, Any]) -> dict[str, Any]:
    """Replace a story plan's ``approval`` block; every other part of plan.json stays as it is.

    Used by jira approve after each link it writes, so a stop half-way keeps what was done.
    """
    path = plan_path(plans_dir, run_id)
    load_task_plan(plans_dir, run_id)  # the same checks as reading it
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record.get("approval"), dict):
        raise RunRegistryError(f"Run {run_id} is not a plan that waits for approval.")
    record["approval"] = approval
    write_json_atomic(path, record)
    return load_task_plan(plans_dir, run_id)


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
            f"Unsupported plan schema version: {record.get('schema_version')!r} "
            f"(this version reads {SCHEMA_VERSION}; plans from older versions "
            "must be generated again with ai-plan)."
        )
    try:
        record["tasks"] = validate_tasks(record.get("tasks"))
    except PlanValidationError as exc:
        raise RunRegistryError(f"Stored plan is invalid: {exc}") from None
    return record
