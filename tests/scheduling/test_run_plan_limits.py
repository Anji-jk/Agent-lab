"""M5.5: Claude's usage limit stops the whole plan; run-plan goes on after the reset."""

from datetime import datetime, timedelta, timezone

from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.records.task_registry import list_tasks
from run_plan_support import per_task, stopped, work
from task_runner_support import RUN_ID  # pyright: ignore[reportMissingImports]


def iso(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def limited(resets_at):
    return RunnerOutcome("failure", 1, "Claude's weekly usage limit was reached.", {
        "status": "failed", "is_error": True, "total_cost_usd": 0.0,
        "rate_limit": {"type": "seven_day", "status": "rejected", "resets_at": resets_at}})


def actions(result):
    return [(s.action, s.task_id, s.status) for s in result.steps]


def roles_of(agents, task_id):
    return [c["role"] for c in agents.calls if c["run_id"].startswith(f"{RUN_ID}-{task_id}-")]


def test_a_limit_stops_the_whole_plan_and_names_the_reset(world, agents):
    resets = iso(2)
    agents.fail["reviewer"] = limited(resets)
    result = work(world, per_task(agents))

    assert actions(result) == [("run", "T1", "needs_review")]
    assert result.steps[0].limit_stop == resets
    when = datetime.fromisoformat(resets).strftime("%Y-%m-%d %H:%M UTC")
    assert stopped(result).startswith(f"T1 was stopped by Claude's usage limit, which resets at {when}.")
    assert "T2" not in list_tasks(world["runs"], RUN_ID)


def test_before_the_reset_run_plan_stops_without_starting_an_agent(world, agents):
    agents.fail["reviewer"] = limited(iso(2))
    work(world, per_task(agents))
    agents.calls.clear()

    again = work(world, per_task(agents))

    assert again.steps == [] and agents.calls == []
    assert "usage limit, which resets at" in stopped(again) and "Run run-plan again after that" in stopped(again)


def test_after_the_reset_the_stopped_task_continues_and_the_plan_goes_on(world, agents):
    agents.fail["reviewer"] = limited(iso(-1))  # by the next run-plan the limit has reset
    work(world, per_task(agents))
    del agents.fail["reviewer"]
    agents.calls.clear()

    again = work(world, per_task(agents))

    assert actions(again) == [("continue", "T1", "done"), ("run", "T2", "done")]
    assert roles_of(agents, "T1") == ["reviewer"]
    assert again.complete


def test_a_limit_without_a_reset_time_still_stops_the_plan(world, agents):
    agents.fail["builder"] = limited(None)
    result = work(world, per_task(agents))
    assert result.steps[0].limit_stop == ""
    assert stopped(result).startswith("T1 was stopped by Claude's usage limit. Every later agent")
