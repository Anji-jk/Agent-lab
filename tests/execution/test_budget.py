"""M4.6: the run budget."""

import pytest

from orchestrator.errors import ExecutionRefused
from orchestrator.records.task_registry import list_tasks
from task_runner_support import (
    RUN_ID,
    costly,
    resume,
    retry,
    run,
    wrong_double,
)


def test_a_budget_too_small_for_the_first_agent_refuses_before_anything(world, agents):
    from orchestrator.execution.budget import BudgetExhausted, set_budget
    set_budget(world["runs"], RUN_ID, 0.50)
    with pytest.raises(BudgetExhausted, match=r"\$0.0000 of \$0.50 is spent, and the builder may spend up to \$0.60"):
        run(world, agents)
    assert agents.calls == [] and list_tasks(world["runs"], RUN_ID) == {}  # it stays ready


def test_a_budget_that_runs_out_mid_task_stops_before_the_next_agent(world, agents):
    from orchestrator.execution.budget import run_spent, set_budget
    set_budget(world["runs"], RUN_ID, 0.80)
    record = run(world, agents, invoke_runner=costly(agents, {"builder": 0.60}))
    # The builder fitted (0 + 0.60 <= 0.80); the tester's 0.25 would not (0.60 + 0.25 > 0.80).
    assert agents.roles == ["builder"]
    assert record["status"] == "needs_review" and record["budget_stop"] is True
    assert record["reasons"][0].startswith("Run budget: $0.6000 of $0.80 is spent, and the tester")
    assert record["checkpoint"]["stage"] == "testing"
    assert run_spent(world["runs"], RUN_ID, world["script"]) == pytest.approx(0.60)


def test_a_task_stopped_by_the_budget_continues_once_it_is_raised(world, agents):
    from orchestrator.execution.budget import run_spent, set_budget
    from orchestrator.execution import retry as rt
    set_budget(world["runs"], RUN_ID, 0.80)
    stopped = run(world, agents, invoke_runner=costly(agents, {"builder": 0.60}))
    assert "only a failed task is retried" in rt.retry_problem(stopped)  # needs_review
    set_budget(world["runs"], RUN_ID, 2.00)
    agents.calls.clear()
    record = resume(world, agents, invoke_runner=costly(agents, {}))
    assert record["status"] == "done" and agents.roles == ["tester", "reviewer"]
    assert record["previous_attempts"][0]["status"] == "stopped by the run budget"
    # 0.60 (builder, attempt 1) + 0.1 + 0.1 (tester and reviewer, attempt 2); nothing twice.
    assert run_spent(world["runs"], RUN_ID, world["script"]) == pytest.approx(0.80)


def test_spending_counts_every_attempt(world, agents):
    from orchestrator.execution.budget import run_spent
    agents.behaviour["builder"] = wrong_double
    run(world, agents)  # builder and tester, then the suite fails: 0.2
    agents.behaviour["builder"] = agents.build_double
    retry(world, agents)  # three agents: 0.3
    assert run_spent(world["runs"], RUN_ID, world["script"]) == pytest.approx(0.5)


def test_no_budget_means_no_run_limit(world, agents):
    from orchestrator.execution.budget import budget_problem, format_budget
    assert budget_problem(world["runs"], RUN_ID, "builder", 999) is None
    assert format_budget(world["runs"], RUN_ID) is None
    assert run(world, agents)["status"] == "done"


def test_a_damaged_budget_file_stops_the_task_rather_than_allowing_anything(world, agents):
    from orchestrator.execution.budget import budget_path, set_budget
    set_budget(world["runs"], RUN_ID, 5)
    budget_path(world["runs"], RUN_ID).write_text("{not json")
    with pytest.raises(ExecutionRefused, match="damaged"):
        run(world, agents)
    assert agents.calls == []


@pytest.mark.parametrize("amount", [0, -1, 1001, True])
def test_a_budget_must_be_a_sensible_amount(world, amount):
    from orchestrator.execution.budget import set_budget
    with pytest.raises(ValueError, match="more than \\$0 and at most \\$1000"):
        set_budget(world["runs"], RUN_ID, amount)


def test_the_budget_command_and_status_show_what_is_left(world, agents, capsys):
    import orchestrator.cli.commands as cli
    run(world, agents)  # 0.3 spent
    base = ["--runs-dir", str(world["runs"])]
    assert cli.main(["budget", RUN_ID, *base]) == cli.EXIT_OK
    assert "has no budget" in capsys.readouterr().out
    assert cli.main(["budget", RUN_ID, "--set", "1.5", *base]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Set the budget of run run1 to $1.50." in out
    assert "Budget: $0.3000 of $1.50 spent on tasks, $1.2000 left" in out
    assert cli.main(["status", RUN_ID, *base, "--plans-dir", str(world["plans"])]) == cli.EXIT_OK
    assert "Budget: $0.3000 of $1.50 spent on tasks" in capsys.readouterr().out
    assert cli.main(["budget", RUN_ID, "--remove", *base]) == cli.EXIT_OK
    assert "Removed the budget" in capsys.readouterr().out
