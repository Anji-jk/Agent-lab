"""M5.4: what happened is posted on the task Issues, and the run report on the parent."""

from typing import Any

import pytest

from orchestrator.github import comments as cm
from orchestrator.github import publish as pub
from orchestrator.github import sync as sy
from orchestrator.records.task_registry import save_task, update_task
from github_fakes import PARENT, RUN, FakeIssues, FakeProject, make_lab  # pyright: ignore[reportMissingImports]

SECRET = "ghp_" + "Z" * 36


def done_record(**extra):
    return {"status": "done", "cost_usd": 0.163, "created_at": "2026-10-24T17:00:00+00:00",
            "updated_at": "2026-10-24T17:12:00+00:00", "commit": "abcdef1234567890",
            "branch": "agent/issue-4-run-t1",
            "agents": {"builder": {"status": "succeeded", "num_turns": 8, "total_cost_usd": 0.0577},
                       "tester": {"status": "succeeded", "num_turns": 8, "total_cost_usd": 0.0705},
                       "reviewer": {"status": "succeeded", "num_turns": 1, "total_cost_usd": 0.0346}},
            "findings": {"suite_passed": True, "named_passed": True, "reviewer_verdict": "pass",
                         "reviewer_notes": ["Mean is rounded as asked."],
                         "tester_notes": [f"The suite is slow; token={SECRET}"]},
            "reasons": ["Every check passed."], **extra}


# --- what a comment says ---------------------------------------------------------------------


def test_a_done_task_comment():
    text = cm.task_comment(RUN, "T1", done_record())
    assert text.startswith("**T1: done** (attempt 1) · Cost $0.1630 · 12 min")
    assert "| builder | succeeded | 8 | $0.0577 |" in text
    assert "**Tests:** the full suite passed; the tester's tests passed." in text
    assert "**Reviewer:** pass." in text and "- Mean is rounded as asked." in text
    assert "**Tester's notes:**" in text
    assert "Committed `abcdef123456` on `agent/issue-4-run-t1` (local only; nothing was pushed)." in text
    assert "**Why:**" not in text
    assert text.rstrip().endswith(f"<!-- agent-lab comment: run={RUN} task=T1 attempt-1:done -->")


def test_secrets_are_masked():
    text = cm.task_comment(RUN, "T1", done_record())
    assert SECRET not in text and "[masked]" in text


def test_a_task_that_did_not_pass_says_why():
    record = done_record(status="needs_review", commit=None,
                         reasons=["The tester failed: boom", "The full test suite fails."])
    text = cm.task_comment(RUN, "T1", record)
    assert "**T1: needs review**" in text
    assert "**Why:**\n- The tester failed: boom\n- The full test suite fails." in text
    assert "Committed" not in text


def test_a_continued_attempt_shows_what_was_kept():
    record = done_record(attempt=2, continued_at="testing", reused_agents={"builder": {}})
    record["agents"].pop("builder")
    text = cm.task_comment(RUN, "T2", record)
    assert "(attempt 2, continued from testing)" in text
    assert "| builder | kept from the stopped attempt | - | - |" in text


def test_a_retry_says_so():
    assert "(attempt 2, a retry, told what went wrong)" in cm.task_comment(RUN, "T1", done_record(attempt=2))


def test_a_timeout_is_called_a_timeout():
    record = done_record(status="failed", findings={"suite_passed": False, "suite_timed_out": True})
    assert "**Tests:** the full suite timed out." in cm.task_comment(RUN, "T1", record)


def test_long_lists_are_cut():
    record = done_record(findings={"reviewer_verdict": "pass", "reviewer_notes": [f"n{i}" for i in range(9)]})
    text = cm.task_comment(RUN, "T1", record)
    assert "- n4" in text and "- n5" not in text and "- … and 4 more" in text


@pytest.mark.parametrize("status, key", [
    ("done", "attempt-1:done"), ("failed", "attempt-1:failed"), ("needs_review", "attempt-1:needs_review"),
    ("cancelled", "attempt-1:cancelled"), ("testing", None), ("pending", None),
])
def test_only_an_attempt_that_ended_gets_a_comment(status, key):
    assert cm.comment_key({"status": status}) == key


# --- posting them ------------------------------------------------------------------------------


@pytest.fixture
def lab(tmp_path):
    lab = make_lab(tmp_path)
    fakes: Any = (FakeIssues(), FakeProject())
    pub.publish(RUN, plans_dir=lab["plans"], runs_dir=lab["runs"], issues=fakes[0],
                project=fakes[1], write=True, pause=0)
    return lab


def record(lab, task_id, *statuses, **extra):
    save_task(lab["runs"], RUN, task_id, {"status": "pending", **extra})
    for status in statuses:
        update_task(lab["runs"], RUN, task_id, {"status": status})


def sync(lab, issues=None, **kw):
    issues = issues or FakeIssues()
    fakes: Any = (issues, FakeProject())
    options = {"pause": 0, "get_command_line": lambda pid: None, **kw}
    result = sy.sync_board(RUN, plans_dir=lab["plans"], runs_dir=lab["runs"], issues=fakes[0],
                           project=fakes[1], **options)
    return result, issues


def test_an_ended_attempt_is_posted_once(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    result, issues = sync(lab)
    assert [n for n, _ in issues.comments()] == [101] and result.commented == [101]
    assert "**T1: done** (attempt 1)" in issues.comments()[0][1]
    _, issues = sync(lab)
    assert issues.comments() == []


def test_a_task_still_running_gets_no_comment(lab):
    record(lab, "T1", "building", pid=4242)
    _, issues = sync(lab, get_command_line=lambda pid: f"python -m orchestrator run-plan {RUN}")
    assert issues.comments() == []


def test_a_second_attempt_gets_its_own_comment(lab):
    record(lab, "T1", "building", "testing", "failed")
    _, issues = sync(lab)
    assert "**T1: failed**" in issues.comments()[0][1]
    path = lab["runs"] / RUN / "tasks" / "T1"
    (path / "task.json").unlink()
    record(lab, "T1", "building", "testing", "reviewing", "done", attempt=2)
    _, issues = sync(lab)
    assert len(issues.comments()) == 1 and "**T1: done** (attempt 2" in issues.comments()[0][1]


def test_a_failed_comment_is_tried_again(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    result, issues = sync(lab, issues=FakeIssues(fail={"comment": 1}))
    assert issues.comments() == [] and any("commenting on #101" in p for p in result.problems)
    _, issues = sync(lab)
    assert [n for n, _ in issues.comments()] == [101]


def test_the_run_report_goes_on_the_parent_when_the_plan_completes(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    record(lab, "T2", "building", "testing", "reviewing", "done")
    _, issues = sync(lab)
    on_parent = [text for n, text in issues.comments() if n == PARENT]
    assert len(on_parent) == 1
    assert f"# Run report: {RUN}" in on_parent[0] and "complete, 2 of 2 tasks done" in on_parent[0]
    _, issues = sync(lab)
    assert [n for n, _ in issues.comments()] == []  # not again


def test_a_stopped_plan_reports_and_reports_again_once_it_finishes(lab):
    record(lab, "T1", "building", "needs_review")
    _, issues = sync(lab)
    assert len([n for n, _ in issues.comments() if n == PARENT]) == 1
    _, issues = sync(lab)
    assert issues.comments() == []
    (lab["runs"] / RUN / "tasks" / "T1" / "task.json").unlink()
    record(lab, "T1", "building", "testing", "reviewing", "done", attempt=2)
    record(lab, "T2", "building", "testing", "reviewing", "done")
    _, issues = sync(lab)
    assert len([n for n, _ in issues.comments() if n == PARENT]) == 1


def test_a_plan_still_going_has_no_report(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")  # T2 ready: not finished
    _, issues = sync(lab)
    assert [n for n, _ in issues.comments() if n == PARENT] == []


def test_comments_can_be_left_out(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    _, issues = sync(lab, comments=False)
    assert issues.comments() == []


def test_the_writes_are_spaced_out(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    pauses = []
    sync(lab, pause=1.0, sleep=pauses.append)
    # T1 moved, closed, commented; T2 moved; #4 found (add) and moved: six writes, five pauses
    assert pauses == [1.0] * 5


# --- a cancelled attempt says what it had done (M5.5) ------------------------------------------

from orchestrator.scheduling.left_behind import AgentLeft, FileChange, LastTests, LeftBehind  # noqa: E402


def left(**extra):
    values: dict[str, Any] = dict(
        run_id=RUN, task_id="T1", status="cancelled", stage="testing", base_commit="b" * 40,
        files=[FileChange("calc.py", "modified", 12, 1), FileChange("tests/test_double.py", "new", 30, 0)],
        files_note=None,
        agents=[AgentLeft("builder", "builder: finished, 8 turns, $0.0577", "Added double().",
                          None, 0.0577, finished=True),
                AgentLeft("tester", "tester: cancelled, 3 turns, $0.0201", f"Writing tests {SECRET}",
                          LastTests("python -m pytest tests/test_double.py", "failed", "1 failed, 1 passed"),
                          0.0201),
                AgentLeft("reviewer", "reviewer: not started", None, None, None)],
        spent=0.0778)
    values.update(extra)
    return LeftBehind(**values)


def cancelled_record():
    return {"status": "cancelled", "reasons": ["Cancelled by you at 2026-10-26T09:00:00+00:00."],
            "agents": {}, "findings": {}}


def test_a_cancelled_comment_says_what_it_had_done():
    text = cm.task_comment(RUN, "T1", cancelled_record(), left())
    assert "**What it had done before it was stopped** (not checked or reviewed):" in text
    assert "- Stopped at: testing" in text
    assert "- Files changed since the base commit `bbbbbbbbbbbb` (2):" in text
    assert "  - modified `calc.py` (+12 -1)" in text and "  - new `tests/test_double.py` (+30 -0)" in text
    assert "- builder: finished, 8 turns, $0.0577" in text and "  - last said: Added double()." in text
    assert "  - last tests: `python -m pytest tests/test_double.py` → failed (1 failed, 1 passed)" in text
    assert "reviewer: not started" not in text  # an agent that never started adds nothing
    assert "- Spent before the stop: $0.0778" in text
    assert SECRET not in text
    assert "**Why:**" in text and "Cancelled by you" in text


def test_a_cancelled_comment_whose_files_cannot_be_read_says_so():
    text = cm.task_comment(RUN, "T1", cancelled_record(),
                           left(files=None, files_note="no worktree was created", agents=[]))
    assert "- Files: unknown, no worktree was created" in text


def test_without_what_it_had_done_the_comment_is_as_before():
    assert "What it had done" not in cm.task_comment(RUN, "T1", cancelled_record())


def test_the_sync_adds_what_a_cancelled_task_had_done(lab):
    record(lab, "T1", "building", "cancelled")
    _, issues = sync(lab)
    text = issues.comments()[0][1]
    assert "**T1: cancelled**" in text
    assert "**What it had done before it was stopped**" in text
    assert "- Stopped at: building" in text and "no worktree was created" in text


def test_a_done_task_does_not_get_that_section(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    _, issues = sync(lab)
    assert "What it had done" not in issues.comments()[0][1]


def test_a_limit_stop_comment_gives_the_reason(lab):
    reason = ("The reviewer was stopped by Claude's usage limit: Claude's weekly usage limit was "
              "reached; it resets at 2026-10-26 11:00 UTC.")
    record(lab, "T1", "building", "testing", "reviewing", "needs_review", reasons=[reason])
    _, issues = sync(lab)
    text = issues.comments()[0][1]
    assert "Claude's weekly usage limit was reached; it resets at 2026-10-26 11:00 UTC." in text
    assert "no detail reported" not in text


def test_a_cancelled_comment_costs_what_was_spent_and_has_no_empty_agent_table():
    text = cm.task_comment(RUN, "T1", cancelled_record(), left())
    assert text.startswith("**T1: cancelled** (attempt 1) · Cost $0.0778")
    assert "| Agent |" not in text
