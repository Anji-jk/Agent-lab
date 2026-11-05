"""The Jira half of a story plan and validation of the planner's answer."""

import pytest

from orchestrator.planning import story_plan as sp
from orchestrator.planning.plan_file import PlanValidationError, validate_tasks
from story_samples import ANSWER, answer, entry, story, work_item


def merge(the_answer=ANSWER, the_story=None, docs_dir="docs"):
    return sp.merge_planner_output(the_story or story(), the_answer, docs_dir)


def test_jira_half_is_copied_in_jira_order():
    tasks = sp.jira_tasks(story())
    assert [(task["id"], task["jira_key"]) for task in tasks] == [
        ("T1", "KAN-11"), ("T2", "KAN-12"), ("T3", "KAN-13")]
    assert tasks[0]["title"] == "Write CSV formatter for entries"
    assert tasks[0]["acceptance_criteria"] == [
        "Date, account and amount columns.", "Commas in names are quoted."]
    assert "Acceptance Criteria" in tasks[0]["description"]


def test_subtasks_need_criteria_and_at_least_one_open_item():
    with pytest.raises(PlanValidationError, match="No acceptance criteria in KAN-12"):
        sp.jira_tasks(story(work_item("KAN-11", "a"), work_item("KAN-12", "b", criteria="")))
    with pytest.raises(PlanValidationError, match="no open sub-tasks"):
        sp.jira_tasks(story(work_item("KAN-11", "a", done=True)))


def test_a_good_answer_becomes_the_plan_and_records_only_new_jira_links():
    result = merge()
    t1, t2, t3 = result["tasks"]
    assert result["summary"].startswith("Formatter first")
    assert t1["files"] == ["ledger/csv_export.py", "tests/test_csv_export.py"]
    assert t1["doc_target"] == "docs/export.md"
    assert t1["context"] == {"read_first": ["CLAUDE.md", "ledger/models.py"],
                             "notes": "Use the csv module."}
    assert t2["depends_on"] == ["T1"]
    assert t2["dependency_reasons"] == {"T1": "Jira: KAN-12 is blocked by KAN-11"}
    assert t3["depends_on"] == ["T2"]
    assert t3["dependency_reasons"] == {"T2": "It documents the command KAN-12 adds."}
    assert result["jira_links"] == [{"subtask": "KAN-13", "blocked_by": "KAN-12",
                                     "reason": "It documents the command KAN-12 adds."}]
    assert result["notes"] == []


def test_planner_cannot_change_jira_owned_fields():
    changed = answer(KAN_11={"title": "Changed", "acceptance_criteria": ["weaker"],
                             "jira_key": "KAN-99", "description": "changed"})
    task = merge(changed)["tasks"][0]
    assert task["title"] == "Write CSV formatter for entries"
    assert task["jira_key"] == "KAN-11"
    assert task["acceptance_criteria"] == ["Date, account and amount columns.",
                                           "Commas in names are quoted."]


def test_a_jira_dependency_omitted_by_planner_is_put_back():
    result = merge(answer(KAN_12={"depends_on": []}))
    assert result["tasks"][1]["depends_on"] == ["T1"]
    assert "a Jira link the planner left out, put back" in result["notes"][0]
    assert not any(link["subtask"] == "KAN-12" and link["blocked_by"] == "KAN-11"
                   for link in result["jira_links"])


def test_all_missing_duplicate_and_unknown_entries_are_reported():
    bad = {"tasks": [entry("KAN-11"), entry("KAN-11"), entry("KAN-77"),
                     entry("KAN-12", depends_on=["KAN-13"])]}
    with pytest.raises(PlanValidationError) as caught:
        merge(bad)
    text = str(caught.value)
    for part in ("KAN-11 is planned twice", "KAN-77 is not an open sub-task of KAN-10",
                 "KAN-13 has no entry", "KAN-12 depends on KAN-13 without a reason"):
        assert part in text


def test_done_subtasks_are_omitted_with_notes():
    with_done = {"tasks": [*ANSWER["tasks"], entry("KAN-14")]}
    assert "KAN-14 is done in Jira" in merge(with_done)["notes"][0]
    on_done = answer(KAN_13={"depends_on": ["KAN-14"], "dependency_reasons": {"KAN-14": "x"}})
    result = merge(on_done)
    assert result["tasks"][2]["depends_on"] == []
    assert "already done in Jira" in result["notes"][0]


def test_cycles_sizes_paths_and_empty_outcomes_are_refused():
    loop = answer(KAN_11={"depends_on": ["KAN-13"],
                          "dependency_reasons": {"KAN-13": "x"}})
    with pytest.raises(PlanValidationError, match="cycle"):
        merge(loop)
    with pytest.raises(PlanValidationError, match="complexity"):
        merge(answer(KAN_11={"complexity": "huge"}))
    with pytest.raises(PlanValidationError, match="repository-relative"):
        merge(answer(KAN_11={"files": ["../outside.py"]}))
    with pytest.raises(PlanValidationError, match="expected_outcome"):
        merge(answer(KAN_11={"expected_outcome": ""}))


def test_tasks_with_overlapping_files_are_made_sequential():
    shared = answer(KAN_11={"parallel_safe": True, "files": ["README.md"]},
                    KAN_13={"parallel_safe": True, "depends_on": [],
                            "dependency_reasons": {}, "files": ["README.md"]})
    result = merge(shared)
    assert not result["tasks"][0]["parallel_safe"]
    assert any("runs one at a time" in note for note in result["notes"])


def test_doc_targets_must_be_in_docs_folder():
    with pytest.raises(PlanValidationError, match="not in the docs folder docs/"):
        merge(answer(KAN_11={"doc_target": "notes/export.md"}))
    result = merge(docs_dir=None)
    assert result["tasks"][0]["doc_target"] is None
    assert "no docs folder" in result["notes"][0]


def test_story_fields_are_validated_and_issue_fields_stay_unchanged():
    tasks = merge()["tasks"]
    assert validate_tasks(tasks) == tasks
    with pytest.raises(PlanValidationError, match="Sub-task in two tasks: KAN-11"):
        validate_tasks([tasks[0], {**tasks[1], "jira_key": "KAN-11"}])
    with pytest.raises(PlanValidationError, match="reason for a task it does not depend on"):
        validate_tasks([{**tasks[0], "dependency_reasons": {"T9": "x"}}])
    plain = {"id": "T1", "title": "t", "description": "d", "expected_outcome": "o",
             "acceptance_criteria": ["c"], "complexity": "small"}
    (task,) = validate_tasks([plain])
    assert not {"jira_key", "dependency_reasons", "doc_target", "context"} & set(task)


def record(status=sp.WAITING):
    result = merge()
    return {"run_id": "run-1", "source": {"kind": "jira", "key": "KAN-10", "url": "u",
                                          "epic": {"key": "KAN-9", "summary": "Reports"}},
            "issue": {"title": "Export ledger entries to CSV"},
            "config": {"run_id": "run-1", "branch": "agent/KAN-10-run-1", "base_ref": "main",
                       "base_commit": "a" * 40, "model": "opus"},
            "summary": result["summary"], "tasks": result["tasks"],
            "skipped_subtasks": [{"key": "KAN-14", "status": "Done"}],
            "orchestrator_notes": ["a note"],
            "approval": {"status": status, "jira_links": result["jira_links"]},
            "planner": {"total_cost_usd": 0.4, "num_turns": 8}}


def test_story_plan_is_formatted_for_review():
    text = sp.format_story_plan(record(), "plan.json")
    assert text.startswith("Plan for story KAN-10 - Export ledger entries to CSV")
    assert "T2  KAN-12  Add the export command" in text
    assert "Needs:   T1 (KAN-11): Jira: KAN-12 is blocked by KAN-11\n" in text
    assert ("Needs:   T2 (KAN-12): It documents the command KAN-12 adds. "
            "[new: added to Jira after approval]") in text
    assert "Accept:  Commas in names are quoted." in text
    assert "Doc:     docs/export.md" in text and "Read:    CLAUDE.md, ledger/models.py" in text
    assert "Left out, done in Jira: KAN-14 (Done)" in text
    assert "Jira changes after approval (1):\n  + KAN-13 is blocked by KAN-12" in text
    assert "Status:      waiting for approval" in text


def test_story_execution_is_refused():
    assert "waiting for approval" in sp.story_plan_refusal(record())
    assert "not supported yet" in sp.story_plan_refusal(record(sp.APPROVED))
    assert sp.story_plan_refusal({"issue": {"number": 4}, "tasks": []}) is None
