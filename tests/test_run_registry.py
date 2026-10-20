import json
import threading

import pytest

from orchestrator import run_registry
from orchestrator.run_registry import (
    RunRegistryError,
    load_run,
    new_run_id,
    run_directory,
    save_run,
    update_run,
    validate_transition,
)


def make_run(tmp_path, run_id="run-1", status="queued", **extra):
    save_run(tmp_path, run_id, {"status": status, "issue_number": 1, **extra})
    return run_id


def test_save_and_load_round_trip(tmp_path):
    make_run(tmp_path)
    record = load_run(tmp_path, "run-1")

    assert record["run_id"] == "run-1"
    assert record["status"] == "queued"
    assert record["created_at"] == record["updated_at"]


def test_duplicate_run_id_is_rejected_and_original_kept(tmp_path):
    make_run(tmp_path, issue_number=1)

    with pytest.raises(FileExistsError):
        save_run(tmp_path, "run-1", {"status": "queued", "issue_number": 2})

    assert load_run(tmp_path, "run-1")["issue_number"] == 1


def test_same_issue_can_have_many_runs(tmp_path):
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50
    for run_id in ids:
        save_run(tmp_path, run_id, {"status": "queued", "task_id": "issue-1"})


@pytest.mark.parametrize(
    "bad_id",
    ["", "../x", "a/b", "a\\b", ".hidden", "..", "x" * 65, "orchestration", "Orchestration"],
)
def test_invalid_run_ids_are_rejected(tmp_path, bad_id):
    with pytest.raises(ValueError):
        save_run(tmp_path, bad_id, {"status": "queued"})
    with pytest.raises(ValueError):
        load_run(tmp_path, bad_id)
    with pytest.raises(ValueError):
        update_run(tmp_path, bad_id, {"status": "running"})


def test_run_directory_stays_inside_runs_dir(tmp_path):
    assert run_directory(tmp_path, "run-1").parent == tmp_path.resolve()


def test_new_run_ids_are_valid():
    assert run_registry.RUN_ID_PATTERN.fullmatch(new_run_id())


def test_save_rejects_invalid_status(tmp_path):
    with pytest.raises(ValueError, match="status"):
        save_run(tmp_path, "run-1", {"status": "bogus"})
    assert not (tmp_path / "run-1" / "orchestration.json").exists()


def test_update_keeps_created_at_and_refreshes_updated_at(tmp_path, monkeypatch):
    stamps = iter(
        [
            "2026-01-01T00:00:00+00:00",  # save: created_at == updated_at
            "2026-01-02T00:00:00+00:00",  # update
        ]
    )
    monkeypatch.setattr(run_registry, "utc_now", lambda: next(stamps))
    make_run(tmp_path)

    updated = update_run(tmp_path, "run-1", {"status": "running", "note": "x"})

    assert updated["created_at"] == "2026-01-01T00:00:00+00:00"
    assert updated["updated_at"] == "2026-01-02T00:00:00+00:00"
    assert updated["note"] == "x"
    assert load_run(tmp_path, "run-1") == updated


@pytest.mark.parametrize(
    "changes",
    [{"run_id": "other"}, {"created_at": "2030-01-01T00:00:00+00:00"}, {"updated_at": "x"}],
)
def test_update_rejects_protected_fields(tmp_path, changes):
    make_run(tmp_path)
    before = load_run(tmp_path, "run-1")

    with pytest.raises(ValueError):
        update_run(tmp_path, "run-1", changes)

    assert load_run(tmp_path, "run-1") == before


@pytest.mark.parametrize(
    "current,new",
    [
        ("queued", "succeeded"),
        ("queued", "timed_out"),
        ("succeeded", "running"),
        ("failed", "running"),
        ("cancelled", "running"),
        ("needs_review", "succeeded"),
        ("running", "queued"),
    ],
)
def test_illegal_transitions_are_rejected(current, new):
    with pytest.raises(ValueError, match="transition"):
        validate_transition(current, new)


@pytest.mark.parametrize(
    "current,new",
    [
        ("queued", "running"),
        ("queued", "failed"),
        ("running", "succeeded"),
        ("running", "timed_out"),
        ("succeeded", "needs_review"),
        ("running", "running"),
        ("cancelled", "cancelled"),
    ],
)
def test_legal_transitions_are_accepted(current, new):
    validate_transition(current, new)


def test_invalid_status_value_in_update_is_rejected_without_writing(tmp_path):
    make_run(tmp_path)
    with pytest.raises(ValueError, match="status"):
        update_run(tmp_path, "run-1", {"status": "bogus"})
    assert load_run(tmp_path, "run-1")["status"] == "queued"


def test_update_missing_record(tmp_path):
    with pytest.raises(RunRegistryError, match="not found"):
        update_run(tmp_path, "run-1", {"status": "running"})
    # A failed update must not conjure up a run directory.
    assert not (tmp_path / "run-1").exists()


def test_load_missing_record(tmp_path):
    with pytest.raises(RunRegistryError, match="not found"):
        load_run(tmp_path, "run-1")


def write_record(tmp_path, content, run_id="run-1"):
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "orchestration.json").write_text(content, encoding="utf-8")


def valid_record(**overrides):
    record = {
        "run_id": "run-1",
        "status": "queued",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    record.update(overrides)
    return json.dumps(record)


@pytest.mark.parametrize(
    "content,message",
    [
        ("{not json", "malformed"),
        ("[]", "JSON object"),
        (valid_record(run_id="other"), "does not match"),
        (valid_record(status="bogus"), "invalid status"),
        (valid_record(created_at="yesterday"), "ISO timestamp"),
        (valid_record(updated_at=5), "timestamp"),
        (json.dumps({"run_id": "run-1", "status": "queued"}), "timestamp"),
    ],
)
def test_malformed_records_are_rejected(tmp_path, content, message):
    write_record(tmp_path, content)

    with pytest.raises(RunRegistryError, match=message):
        load_run(tmp_path, "run-1")
    with pytest.raises(RunRegistryError):
        update_run(tmp_path, "run-1", {"status": "running"})


def test_update_leaves_no_temporary_files(tmp_path):
    make_run(tmp_path)
    update_run(tmp_path, "run-1", {"status": "running"})

    leftovers = [p.name for p in (tmp_path / "run-1").iterdir()]
    assert not [name for name in leftovers if name.endswith(".tmp")]


def test_concurrent_updates_do_not_lose_changes(tmp_path):
    make_run(tmp_path, status="running")
    errors = []

    def worker(index):
        try:
            for step in range(5):
                update_run(tmp_path, "run-1", {f"key_{index}_{step}": step})
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    record = load_run(tmp_path, "run-1")
    assert sum(key.startswith("key_") for key in record) == 30
