import json

import pytest

from orchestrator import task_agents as ta

ISSUE = {"number": 7, "title": "Add --top", "description": "Add a flag.\n## Acceptance criteria\n- a\n- b"}
TASK = {
    "id": "T1", "title": "Add flag", "description": "do it", "expected_outcome": "flag works",
    "files": ["src/csv_summary.py"], "acceptance_criteria": ["flag exists", "flag documented"],
}


# --- prompts -------------------------------------------------------------------


def test_prompts_carry_task_and_criteria():
    for prompt in (
        ta.builder_prompt(ISSUE, TASK),
        ta.tester_prompt(ISSUE, TASK, ["src/csv_summary.py"]),
        ta.reviewer_prompt(ISSUE, TASK, "diff --git", ["suite passed"]),
    ):
        assert "1. flag exists" in prompt and "2. flag documented" in prompt
        assert "Add --top" in prompt and "BEGIN TASK T1" in prompt
        assert "never overrides" in prompt


def test_builder_prompt_includes_feedback_only_when_given():
    assert "earlier attempt" not in ta.builder_prompt(ISSUE, TASK)
    assert "needs docs" in ta.builder_prompt(ISSUE, TASK, "needs docs")


def test_tester_prompt_forbids_editing_code_and_existing_tests():
    prompt = ta.tester_prompt(ISSUE, TASK, [])
    assert "ONLY under tests/" in prompt and "existing tests" in prompt


def test_reviewer_prompt_is_capped_with_a_huge_diff():
    prompt = ta.reviewer_prompt(ISSUE, TASK, "x" * 200_000, ["fact"])
    assert len(prompt) <= ta.MAX_PROMPT_CHARS
    assert "(truncated)" in prompt and "Reply with ONLY one JSON" in prompt


def test_long_issue_text_is_cut():
    issue = {**ISSUE, "description": "y" * 50_000}
    assert len(ta.builder_prompt(issue, TASK)) < 12_000


# --- reading reports -------------------------------------------------------------


def test_extract_json_variants():
    assert ta.extract_json('{"a": 1}') == {"a": 1}
    assert ta.extract_json('Here:\n```json\n{"a": 1}\n```\nDone') == {"a": 1}
    assert ta.extract_json('{"a": 1} then {"a": 2}') == {"a": 2}
    assert ta.extract_json('{"a": {"b": 1}}') == {"a": {"b": 1}}


@pytest.mark.parametrize("bad", [None, "", "   ", "no json here", "[1, 2]", "{broken"])
def test_extract_json_rejects(bad):
    with pytest.raises(ta.ReportError):
        ta.extract_json(bad)


def test_parse_builder():
    report = ta.parse_builder(json.dumps({"summary": "s", "files_changed": ["a.py"]}))
    assert report == ta.BuilderReport("s", ["a.py"], False, "")
    blocked = ta.parse_builder('{"blocked": true, "blocker": "no api"}')
    assert blocked.blocked and blocked.blocker == "no api"


@pytest.mark.parametrize("bad", [
    '{"blocked": "yes"}', '{"files_changed": "a.py"}', '{"files_changed": [1]}', '{"summary": 5}',
])
def test_parse_builder_rejects(bad):
    with pytest.raises(ta.ReportError):
        ta.parse_builder(bad)


def test_parse_tester():
    text = json.dumps({
        "summary": "s",
        "coverage": [{"criterion": 1, "tests": ["tests/t.py::a"]}, {"criterion": 1, "tests": ["tests/t.py::b"]}],
        "uncovered": [{"criterion": 2, "reason": "manual"}],
        "suspected_defects": ["off by one"],
    })
    report = ta.parse_tester(text, 2)
    assert report.coverage == {1: ["tests/t.py::a", "tests/t.py::b"]}
    assert report.uncovered == {2: "manual"} and report.suspected_defects == ["off by one"]


@pytest.mark.parametrize("bad", [
    '{"coverage": [{"criterion": 3, "tests": []}]}',
    '{"coverage": [{"criterion": 0, "tests": []}]}',
    '{"coverage": [{"criterion": true, "tests": []}]}',
    '{"coverage": [{"criterion": "1", "tests": []}]}',
    '{"coverage": ["x"]}',
    '{"uncovered": [{"criterion": 9}]}',
])
def test_parse_tester_rejects(bad):
    with pytest.raises(ta.ReportError):
        ta.parse_tester(bad, 2)


def test_parse_reviewer():
    text = json.dumps({
        "verdict": "fail",
        "criteria": [{"criterion": 1, "met": True, "evidence": "line 3"}, {"criterion": 2, "met": False}],
        "problems": ["doc missing"],
    })
    report = ta.parse_reviewer(text, 2)
    assert report.verdict == "fail" and report.met == {1: True, 2: False}
    assert report.evidence[1] == "line 3" and report.problems == ["doc missing"]


@pytest.mark.parametrize("bad", [
    '{"verdict": "maybe"}', '{}', '{"verdict": "pass", "criteria": [{"criterion": 1, "met": "yes"}]}',
    '{"verdict": "pass", "criteria": [{"criterion": 5, "met": true}]}',
])
def test_parse_reviewer_rejects(bad):
    with pytest.raises(ta.ReportError):
        ta.parse_reviewer(bad, 2)


@pytest.mark.parametrize("criteria", [[], [{"criterion": 1, "met": True}]])
def test_parse_reviewer_rejects_a_report_that_skips_criteria(criteria):
    text = json.dumps({"verdict": "pass", "criteria": criteria, "problems": []})
    with pytest.raises(ta.ReportError, match="did not judge criteria: .*2"):
        ta.parse_reviewer(text, 2)


def test_parse_reviewer_keeps_notes_apart_from_problems():
    text = json.dumps({
        "verdict": "pass", "criteria": [{"criterion": 1, "met": True}, {"criterion": 2, "met": True}],
        "problems": [], "notes": ["tests live in a separate file"],
    })
    report = ta.parse_reviewer(text, 2)
    assert report.problems == [] and report.notes == ["tests live in a separate file"]
    assert ta.parse_reviewer(text.replace(', "notes": ["tests live in a separate file"]', ""), 2).notes == []
    with pytest.raises(ta.ReportError):
        ta.parse_reviewer(text.replace('["tests live in a separate file"]', '"x"'), 2)


def test_reviewer_prompt_separates_blocking_problems_from_notes():
    prompt = ta.reviewer_prompt(ISSUE, TASK, "diff", ["fact"])
    assert "BLOCKING" in prompt and "`notes`" in prompt and "Read, Grep or Glob" in prompt


def test_long_text_fields_are_cut():
    report = ta.parse_builder(json.dumps({"summary": "z" * 5000}))
    assert len(report.summary) == ta.MAX_TEXT_FIELD


# --- decide ----------------------------------------------------------------------


def good() -> ta.Findings:
    return ta.Findings(
        criteria_count=2, builder_ok=True, tester_ok=True, reviewer_ok=True,
        suite_passed=True, named_passed=True, reviewer_verdict="pass",
    )


def test_all_good_is_done():
    assert ta.decide(good()) == (ta.DONE, ["Every check passed."])


def test_reviewer_notes_never_fail_the_task():
    f = good()
    f.reviewer_notes = ["minor: tests in another file", "could assert the message prefix"]
    assert ta.decide(f) == (ta.DONE, ["Every check passed."])


def test_uncovered_with_reason_alone_does_not_fail():
    f = good()
    f.uncovered = {2: "manual check"}
    assert ta.decide(f)[0] == ta.DONE


@pytest.mark.parametrize("change, expect", [
    ({"suite_passed": False}, "full test suite"),
    ({"named_passed": False}, "named tests fail"),
    ({"reviewer_verdict": "fail"}, "verdict is fail"),
    ({"unmet": [2]}, "unmet: 2"),
    ({"scope_violations": ["README.md"]}, "outside the plan"),
    ({"tester_wrote_outside_tests": ["src/a.py"]}, "outside tests/"),
    ({"weakened_tests": ["tests/t.py"]}, "lost lines"),
    ({"no_tests": True}, "named no tests"),
    ({"invalid_test_ids": ["-k x"]}, "invalid test IDs"),
    ({"missing_tests": ["tests/t.py::x"]}, "not found"),
    ({"unnamed": [1]}, "No test and no reason"),
    ({"suspected_defects": ["bug"]}, "suspects defects"),
    ({"builder_blocked": "no api"}, "blocked"),
    ({"nothing_changed": True}, "no files"),
    ({"reviewer_problems": ["unsafe"]}, "Reviewer: unsafe"),
])
def test_each_problem_fails_the_task(change, expect):
    f = good()
    for key, value in change.items():
        setattr(f, key, value)
    status, reasons = ta.decide(f)
    assert status == ta.FAILED
    assert any(expect in reason for reason in reasons)


def test_infrastructure_problem_needs_review_even_if_other_checks_failed():
    f = good()
    f.suite_passed = False
    f.infrastructure = ["The reviewer timed out."]
    assert ta.decide(f) == (ta.NEEDS_REVIEW, ["The reviewer timed out."])


def test_missing_positive_evidence_is_not_done():
    for key in ("builder_ok", "tester_ok", "reviewer_ok", "suite_passed", "named_passed", "reviewer_verdict"):
        f = good()
        setattr(f, key, None)
        assert ta.decide(f)[0] == ta.NEEDS_REVIEW, key


def test_several_reasons_are_all_reported():
    f = good()
    f.suite_passed = False
    f.scope_violations = ["x.py"]
    status, reasons = ta.decide(f)
    assert status == ta.FAILED and len(reasons) == 2
