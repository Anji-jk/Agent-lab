"""An agent refused by Claude's usage limit stops the task with the reason, and the
task continues from its stage once the limit has reset."""

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import continuing
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.records.task_registry import load_task
from task_runner_support import RUN_ID, gone, resume, run  # pyright: ignore[reportMissingImports]


def iso(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def limited(resets_at):
    limit = {"type": "seven_day", "status": "rejected", "resets_at": resets_at}
    error = "Claude's weekly usage limit was reached; it resets at 2026-10-26 11:00 UTC."
    return RunnerOutcome("failure", 1, error, {"status": "failed", "is_error": True,
                                               "rate_limit": limit, "total_cost_usd": 0.0,
                                               "num_turns": 1})


def test_a_limited_reviewer_stops_the_task_with_the_reason(world, agents):
    agents.fail["reviewer"] = limited(iso(2))
    record = run(world, agents)

    assert record["status"] == "needs_review"
    assert record["limit_stop"]["role"] == "reviewer"
    assert record["limit_stop"]["type"] == "seven_day"
    reason = " ".join(record["reasons"])
    assert "The reviewer was stopped by Claude's usage limit: Claude's weekly usage limit" in reason
    assert "no detail reported" not in reason
    assert "once the limit resets" in reason


def test_any_other_failure_still_reads_as_before(world, agents):
    agents.fail["reviewer"] = RunnerOutcome("failure", 1, "boom", {})
    record = run(world, agents)
    assert "limit_stop" not in record
    assert "The reviewer failure: boom." in record["reasons"]


def test_it_is_not_continued_before_the_reset(world, agents):
    agents.fail["reviewer"] = limited(iso(2))
    run(world, agents)
    with pytest.raises(ExecutionRefused, match="usage limit, which resets at .* UTC. Continue it after that"):
        resume(world, agents)


def test_after_the_reset_only_the_stopped_stage_runs_again(world, agents):
    agents.fail["reviewer"] = limited(iso(-1))
    run(world, agents)
    del agents.fail["reviewer"]
    agents.calls.clear()

    record = resume(world, agents)

    assert record["status"] == "done"
    assert agents.roles == ["reviewer"]  # the builder's and tests' results were kept
    assert record["previous_attempts"][-1]["status"] == "stopped by Claude's usage limit"
    assert "limit_stop" not in record


def test_continue_problem_reads_the_reset_time():
    record = {"task_id": "T1", "status": "needs_review", "branch": "b", "worktree_path": "/nowhere",
              "limit_stop": {"resets_at": "2026-10-26T11:00:00+00:00"}}
    before = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    assert "resets at 2026-10-26 11:00 UTC" in continuing.continue_problem(record, gone, before)
    assert "no longer exists" in continuing.continue_problem(record, gone, after)
    record["limit_stop"] = {"resets_at": None}  # unknown: let the person try
    assert "no longer exists" in continuing.continue_problem(record, gone, before)


def test_a_record_shows_the_limit(world, agents):
    agents.fail["tester"] = limited(iso(2))
    run(world, agents)
    assert load_task(world["runs"], RUN_ID, "T1")["limit_stop"]["role"] == "tester"
