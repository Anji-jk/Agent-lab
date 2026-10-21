"""Persist orchestration run metadata as JSON.

Each run lives in ``<runs_dir>/<run_id>/orchestration.json``. Writes are
atomic (temporary file + ``os.replace``) and serialized per run with an OS
file lock, so concurrent updaters cannot lose each other's changes.

Run identity (``run_id``) is deliberately separate from task identity
(``task_id``, e.g. ``issue-1``): one Issue may be executed many times.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


RECORD_NAME = "orchestration.json"
LOCK_NAME = "orchestration.lock"
RUN_ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")

# The Claude runner writes its own artifacts to ``runs/<task-id>/``. The
# registry lives in ``runs/orchestration/``, so that name can never be a run.
RESERVED_RUN_IDS = frozenset({"orchestration"})

VALID_STATUSES = {
    "queued",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "needs_review",
}

# Legal lifecycle transitions. Re-asserting the current status is always
# allowed (it is a metadata-only update); anything else must appear here.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset(
        {"succeeded", "failed", "timed_out", "cancelled", "needs_review"}
    ),
    "succeeded": frozenset({"needs_review"}),
    "failed": frozenset({"needs_review"}),
    "timed_out": frozenset({"needs_review"}),
    "cancelled": frozenset(),
    "needs_review": frozenset(),
}

# Fields a caller may never change through update_run().
IMMUTABLE_FIELDS = frozenset({"run_id", "created_at"})


class RunRegistryError(RuntimeError):
    """Raised for missing, malformed, or unwritable run records."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """Return a unique run ID such as ``run-20261018-171530-a1b2c3``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:6]}"


def validate_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("Invalid run ID.")
    if run_id.lower() in RESERVED_RUN_IDS:
        raise ValueError(f"Run ID is reserved: {run_id}")
    return run_id


def validate_transition(current: str, new: str) -> None:
    """Raise ValueError unless ``current`` -> ``new`` is a legal move."""
    for name in (current, new):
        if name not in VALID_STATUSES:
            raise ValueError(f"Invalid run status: {name!r}")
    if new != current and new not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Illegal status transition: {current} -> {new}")


def run_directory(runs_dir: Path, run_id: str) -> Path:
    """Return the run's directory, refusing anything outside ``runs_dir``."""
    validate_run_id(run_id)
    base = runs_dir.resolve()
    run_dir = (base / run_id).resolve()
    if run_dir.parent != base:
        raise ValueError("Run directory escapes the runs directory.")
    return run_dir


@contextmanager
def _locked(run_dir: Path, timeout: float = 10.0) -> Generator[None]:
    """Hold an exclusive OS lock on the run (released if the process dies)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(run_dir / LOCK_NAME, "a+b") as lock_file:
        while True:
            try:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RunRegistryError(
                        f"Timed out waiting for the lock on {run_dir.name}."
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


locked = _locked  # for modules that keep their own records beside a run's


def _parse_timestamp(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise RunRegistryError(f"Record field {field!r} must be a timestamp.")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise RunRegistryError(
            f"Record field {field!r} is not an ISO timestamp."
        ) from None


def _validate_record(record: object, run_id: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RunRegistryError("Run record must be a JSON object.")
    if record.get("run_id") != run_id:
        raise RunRegistryError("Run record does not match its run ID.")
    if record.get("status") not in VALID_STATUSES:
        raise RunRegistryError(
            f"Run record has invalid status: {record.get('status')!r}"
        )
    _parse_timestamp(record.get("created_at"), "created_at")
    _parse_timestamp(record.get("updated_at"), "updated_at")
    return record


def write_json_atomic(output_path: Path, record: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent, prefix=".orchestration-", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Windows can briefly refuse the replace while a sync client or
        # virus scanner holds the target open; retry a few times.
        for attempt in range(5):
            try:
                os.replace(temporary_path, output_path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        temporary_path.unlink(missing_ok=True)


def save_run(
    runs_dir: Path,
    run_id: str,
    metadata: dict[str, Any],
) -> Path:
    """Create a new run record. Never overwrites an existing one."""
    run_dir = run_directory(runs_dir, run_id)

    status = metadata.get("status")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid run status: {status!r}")

    with _locked(run_dir):
        output_path = run_dir / RECORD_NAME
        if output_path.exists():
            raise FileExistsError(f"Run metadata already exists: {output_path}")

        now = utc_now()
        record = {
            **metadata,
            "run_id": run_id,
            "created_at": now,
            "updated_at": now,
        }
        write_json_atomic(output_path, record)
    return output_path


def load_run(runs_dir: Path, run_id: str) -> dict[str, Any]:
    """Read and validate one run record."""
    output_path = run_directory(runs_dir, run_id) / RECORD_NAME
    try:
        text = output_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RunRegistryError(f"Run not found: {run_id}") from None
    except OSError as exc:
        raise RunRegistryError(
            f"Could not read run {run_id}: {type(exc).__name__}"
        ) from None

    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        raise RunRegistryError(f"Run record is malformed JSON: {run_id}") from None
    return _validate_record(record, run_id)


def update_run(
    runs_dir: Path,
    run_id: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``changes`` into an existing record and return the new record.

    ``created_at`` and ``run_id`` cannot be changed; ``updated_at`` is
    refreshed on every successful update. A ``status`` change must be a
    legal lifecycle transition. Nothing is written if validation fails.
    """
    forbidden = IMMUTABLE_FIELDS & changes.keys()
    if forbidden:
        raise ValueError(f"Cannot modify: {', '.join(sorted(forbidden))}")
    if "updated_at" in changes:
        raise ValueError("updated_at is managed by the registry.")

    run_dir = run_directory(runs_dir, run_id)
    if not (run_dir / RECORD_NAME).exists():
        raise RunRegistryError(f"Run not found: {run_id}")

    with _locked(run_dir):
        # Re-read under the lock so concurrent updates are never lost.
        record = load_run(runs_dir, run_id)
        if "status" in changes:
            validate_transition(record["status"], changes["status"])

        updated = {**record, **changes, "updated_at": utc_now()}
        write_json_atomic(run_dir / RECORD_NAME, updated)
    return updated
