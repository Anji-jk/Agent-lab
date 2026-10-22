import json
import threading

import pytest

from orchestrator.records.run_registry import RunRegistryError
from orchestrator.records.task_registry import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    FINAL_STATUSES,
    TASK_STATUSES,
    list_tasks,
    load_task,
    save_task,
    task_directory,
    update_task,
    validate_task_transition,
)

RUN = "run-1"


def make(runs_dir, task_id="T1", **extra):
    save_task(runs_dir, RUN, task_id, {"title": "A task", **extra})


def test_new_task_is_pending_with_history(tmp_path):
    make(tmp_path)

    record = load_task(tmp_path, RUN, "T1")

    assert record["status"] == "pending"
    assert record["title"] == "A task"
    assert [h["status"] for h in record["history"]] == ["pending"]
    assert (task_directory(tmp_path, RUN, "T1") / "task.json").is_file()


def test_a_record_is_never_overwritten(tmp_path):
    make(tmp_path)
    with pytest.raises(FileExistsError):
        make(tmp_path)


def test_a_normal_lifecycle_is_recorded_in_order(tmp_path):
    make(tmp_path)
    for status in ("building", "testing", "reviewing", "done"):
        update_task(tmp_path, RUN, "T1", {"status": status})

    record = load_task(tmp_path, RUN, "T1")

    assert [h["status"] for h in record["history"]] == [
        "pending", "building", "testing", "reviewing", "done",
    ]


@pytest.mark.parametrize(
    "current,new",
    [
        ("pending", "done"),
        ("pending", "testing"),
        ("building", "done"),
        ("testing", "building"),
        ("reviewing", "testing"),
        ("done", "failed"),
        ("done", "building"),
        ("needs_review", "done"),
        ("cancelled", "building"),
        ("failed", "done"),
    ],
)
def test_illegal_transitions_are_refused(current, new):
    with pytest.raises(ValueError, match="Illegal task status transition"):
        validate_task_transition(current, new)


def test_an_illegal_move_writes_nothing(tmp_path):
    make(tmp_path)
    before = load_task(tmp_path, RUN, "T1")

    with pytest.raises(ValueError):
        update_task(tmp_path, RUN, "T1", {"status": "done", "note": "sneaky"})

    assert load_task(tmp_path, RUN, "T1") == before


def test_a_failed_task_can_be_flagged_for_review(tmp_path):
    make(tmp_path)
    update_task(tmp_path, RUN, "T1", {"status": "failed"})
    update_task(tmp_path, RUN, "T1", {"status": "needs_review"})

    assert load_task(tmp_path, RUN, "T1")["status"] == "needs_review"


def test_every_status_has_an_exit_table_and_only_known_targets():
    assert set(ALLOWED_TRANSITIONS) == TASK_STATUSES
    for targets in ALLOWED_TRANSITIONS.values():
        assert targets <= TASK_STATUSES
    assert ACTIVE_STATUSES | FINAL_STATUSES | {"pending"} == TASK_STATUSES


def test_done_is_final():
    assert ALLOWED_TRANSITIONS["done"] == frozenset()


def test_metadata_update_keeps_the_status_and_history(tmp_path):
    make(tmp_path)
    update_task(tmp_path, RUN, "T1", {"status": "building"})
    update_task(tmp_path, RUN, "T1", {"cost_usd": 0.12})
    update_task(tmp_path, RUN, "T1", {"status": "building", "note": "again"})

    record = load_task(tmp_path, RUN, "T1")

    assert record["cost_usd"] == 0.12 and record["note"] == "again"
    assert [h["status"] for h in record["history"]] == ["pending", "building"]


@pytest.mark.parametrize("field", ["run_id", "task_id", "created_at", "history", "updated_at"])
def test_managed_fields_cannot_be_changed(tmp_path, field):
    make(tmp_path)
    with pytest.raises(ValueError):
        update_task(tmp_path, RUN, "T1", {field: "x"})


@pytest.mark.parametrize("task_id", ["", "../x", "a/b", "T 1", "x" * 40, None, 3])
def test_bad_task_ids_are_refused(tmp_path, task_id):
    with pytest.raises(ValueError, match="Invalid task ID"):
        task_directory(tmp_path, RUN, task_id)


def test_missing_and_malformed_records(tmp_path):
    with pytest.raises(RunRegistryError, match="No record"):
        load_task(tmp_path, RUN, "T1")
    with pytest.raises(RunRegistryError, match="No record"):
        update_task(tmp_path, RUN, "T1", {"note": "x"})

    make(tmp_path)
    path = task_directory(tmp_path, RUN, "T1") / "task.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(RunRegistryError, match="malformed"):
        load_task(tmp_path, RUN, "T1")

    path.write_text(json.dumps({"run_id": RUN, "task_id": "T2", "status": "pending"}), encoding="utf-8")
    with pytest.raises(RunRegistryError, match="does not match"):
        load_task(tmp_path, RUN, "T1")


def test_list_tasks(tmp_path):
    assert list_tasks(tmp_path, RUN) == {}
    make(tmp_path, "T2")
    make(tmp_path, "T1")
    update_task(tmp_path, RUN, "T1", {"status": "building"})

    found = list_tasks(tmp_path, RUN)

    assert list(found) == ["T1", "T2"]
    assert found["T1"]["status"] == "building" and found["T2"]["status"] == "pending"


def test_concurrent_updates_are_not_lost(tmp_path):
    make(tmp_path)
    errors = []

    def bump(index):
        try:
            update_task(tmp_path, RUN, "T1", {f"field_{index}": index})
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=bump, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    record = load_task(tmp_path, RUN, "T1")
    assert not errors
    assert all(record[f"field_{i}"] == i for i in range(8))
