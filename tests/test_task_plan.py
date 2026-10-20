import json

import pytest

from orchestrator.run_registry import RunRegistryError
from orchestrator.task_plan import (
    PlanValidationError,
    load_task_plan,
    parse_plan_output,
    plan_path,
    save_task_plan,
    validate_tasks,
)


def task(task_id="T1", **overrides):
    value = {
        "id": task_id,
        "title": f"Do {task_id}",
        "description": "Details.",
        "depends_on": [],
        "expected_outcome": "It works.",
        "files": ["src/a.py"],
        "verification": ["python -m pytest"],
    }
    value.update(overrides)
    return value


def plan_json(*tasks, summary="Approach."):
    return json.dumps({"summary": summary, "tasks": list(tasks)})


# --- parsing -----------------------------------------------------------------


def test_parses_bare_json():
    parsed = parse_plan_output(plan_json(task("T1"), task("T2", depends_on=["T1"])))

    assert parsed["summary"] == "Approach."
    assert [t["id"] for t in parsed["tasks"]] == ["T1", "T2"]
    assert parsed["tasks"][1]["depends_on"] == ["T1"]


def test_parses_fenced_json_with_prose():
    text = f"Here is the plan:\n```json\n{plan_json(task())}\n```\nHope it helps."
    assert parse_plan_output(text)["tasks"][0]["id"] == "T1"


def test_parses_json_surrounded_by_prose_without_fence():
    text = f"Sure! {plan_json(task())} Let me know."
    assert parse_plan_output(text)["tasks"][0]["id"] == "T1"


def test_optional_fields_default_to_empty():
    minimal = {"id": "T1", "title": "t", "description": "d", "expected_outcome": "o"}
    parsed = parse_plan_output(json.dumps({"tasks": [minimal]}))

    assert parsed["summary"] == ""
    assert parsed["tasks"][0]["depends_on"] == []
    assert parsed["tasks"][0]["files"] == []
    assert parsed["tasks"][0]["verification"] == []


def test_unknown_fields_are_dropped():
    parsed = parse_plan_output(plan_json(task(evil="rm -rf /", priority=1)))
    assert set(parsed["tasks"][0]) == {
        "id", "title", "description", "depends_on",
        "expected_outcome", "files", "verification",
    }


@pytest.mark.parametrize(
    "text",
    [None, "", "   ", "no json here", "{broken", "[1, 2, 3]", '"just a string"', "{}", '{"tasks": []}', '{"tasks": "x"}'],
)
def test_malformed_output_is_rejected(text):
    with pytest.raises(PlanValidationError):
        parse_plan_output(text)


# --- task validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "bad,message",
    [
        (task(id=""), "invalid id"),
        (task(id="has space"), "invalid id"),
        (task(id=5), "invalid id"),
        (task(title=""), "title"),
        (task(title=7), "title"),
        (task(description="  "), "description"),
        (task(expected_outcome=None), "expected_outcome"),
        (task(depends_on="T1"), "depends_on"),
        (task(depends_on=[1]), "depends_on"),
        (task(files="a.py"), "files"),
        (task(verification={"a": 1}), "verification"),
        (task(title="x" * 201), "longer than"),
        ("not an object", "must be an object"),
    ],
)
def test_invalid_tasks_are_rejected(bad, message):
    with pytest.raises(PlanValidationError, match=message):
        validate_tasks([bad])


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "C:\\Windows\\x", "\\\\server\\share", "../secret", "a/../../b", "~/x"]
)
def test_unsafe_file_hints_are_rejected(path):
    with pytest.raises(PlanValidationError, match="repository-relative"):
        validate_tasks([task(files=[path])])


def test_duplicate_ids_are_rejected():
    with pytest.raises(PlanValidationError, match="Duplicate"):
        validate_tasks([task("T1"), task("T1")])


def test_missing_dependency_is_rejected():
    with pytest.raises(PlanValidationError, match="missing task 'T9'"):
        validate_tasks([task("T1", depends_on=["T9"])])


def test_self_dependency_is_rejected():
    with pytest.raises(PlanValidationError, match="depends on itself"):
        validate_tasks([task("T1", depends_on=["T1"])])


def test_repeated_dependency_is_rejected():
    with pytest.raises(PlanValidationError, match="twice"):
        validate_tasks([task("T1"), task("T2", depends_on=["T1", "T1"])])


def test_dependency_cycle_is_rejected():
    tasks = [
        task("T1", depends_on=["T3"]),
        task("T2", depends_on=["T1"]),
        task("T3", depends_on=["T2"]),
        task("T4"),
    ]
    with pytest.raises(PlanValidationError, match="cycle: T1, T2, T3"):
        validate_tasks(tasks)


def test_diamond_dependencies_are_fine():
    validate_tasks(
        [
            task("T1"),
            task("T2", depends_on=["T1"]),
            task("T3", depends_on=["T1"]),
            task("T4", depends_on=["T2", "T3"]),
        ]
    )


def test_too_many_tasks_are_rejected():
    with pytest.raises(PlanValidationError, match="at most"):
        validate_tasks([task(f"T{i}") for i in range(1, 32)])


# --- storage -----------------------------------------------------------------


def stored_document():
    return {
        "issue": {"number": 1},
        "config": {"run_id": "run-1"},
        "summary": "s",
        "tasks": [task("T1")],
        "planner": {},
    }


def test_save_and_load_round_trip(tmp_path):
    path = save_task_plan(tmp_path, "run-1", stored_document())

    assert path == plan_path(tmp_path, "run-1")
    record = load_task_plan(tmp_path, "run-1")
    assert record["run_id"] == "run-1"
    assert record["schema_version"] == 1
    assert record["tasks"][0]["id"] == "T1"
    assert "created_at" in record


def test_save_writes_no_run_record(tmp_path):
    save_task_plan(tmp_path, "run-1", stored_document())

    names = sorted(p.name for p in (tmp_path / "run-1").iterdir())
    assert names == ["plan.json"]  # no orchestration.json, no temp files


def test_save_never_overwrites(tmp_path):
    save_task_plan(tmp_path, "run-1", stored_document())
    with pytest.raises(FileExistsError):
        save_task_plan(tmp_path, "run-1", stored_document())


def test_save_rejects_invalid_tasks_and_writes_nothing(tmp_path):
    document = stored_document()
    document["tasks"] = [task("T1", depends_on=["nope"])]

    with pytest.raises(PlanValidationError):
        save_task_plan(tmp_path, "run-1", document)

    assert not (tmp_path / "run-1").exists()


@pytest.mark.parametrize("run_id", ["../x", "", "orchestration"])
def test_invalid_run_ids_are_rejected(tmp_path, run_id):
    with pytest.raises(ValueError):
        save_task_plan(tmp_path, run_id, stored_document())


def test_load_missing_plan(tmp_path):
    with pytest.raises(RunRegistryError, match="No plan found"):
        load_task_plan(tmp_path, "run-1")


@pytest.mark.parametrize(
    "content,message",
    [
        ("{nope", "unreadable or malformed"),
        ("[]", "does not match"),
        (json.dumps({"run_id": "other", "schema_version": 1}), "does not match"),
        (json.dumps({"run_id": "run-1", "schema_version": 99}), "schema version"),
        (
            json.dumps({"run_id": "run-1", "schema_version": 1, "tasks": [task("T1", depends_on=["T2"])]}),
            "Stored plan is invalid",
        ),
    ],
)
def test_corrupt_stored_plans_are_rejected(tmp_path, content, message):
    (tmp_path / "run-1").mkdir()
    (tmp_path / "run-1" / "plan.json").write_text(content, encoding="utf-8")

    with pytest.raises(RunRegistryError, match=message):
        load_task_plan(tmp_path, "run-1")
