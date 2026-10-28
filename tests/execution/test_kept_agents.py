"""M7.8: a continued task shows the agents it kept from the stopped attempt as finished."""

from datetime import datetime, timezone

from orchestrator.monitoring import dashboard as dash
from orchestrator.monitoring import progress
from orchestrator.records.task_registry import load_task
from task_runner_support import RUN_ID, resume, stop_at  # pyright: ignore[reportMissingImports]

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def watch_while(agents, role, world, seen):
    """Look at the task as the screens do while ``role`` of the continued attempt runs."""
    real = agents.behaviour[role]

    def look(cwd):
        record = load_task(world["runs"], RUN_ID, "T1")
        seen["record"] = record
        seen["views"] = {name: progress.task_agent_view(RUN_ID, "T1", name, record,
                                                        world["script"], NOW)
                         for name in ("builder", "tester", "reviewer")}
        data = dash.DashboardData(world["runs"], world["plans"], runner_script=world["script"],
                                  get_command_line=lambda pid: None)
        seen["dashboard"] = data.run(RUN_ID)
        return real(cwd)

    agents.behaviour[role] = look


def test_while_the_reviewer_runs_again_the_builder_and_tester_show_as_kept(world, agents):
    stop_at(world, agents, "reviewer")
    seen = {}
    watch_while(agents, "reviewer", world, seen)
    record = resume(world, agents)
    assert record["status"] == "done"

    during = seen["record"]
    assert during["attempt"] == 2 and set(during["reused_agents"]) == {"builder", "tester"}
    assert during["reused_agents"]["builder"]["attempt"] == 1
    assert during["checks"]["suite"]  # the kept tests' results are there to read meanwhile
    views = seen["views"]
    for name in ("builder", "tester"):
        assert views[name].state == "finished" and views[name].kept_from == 1, name
        assert views[name].describe().startswith(f"{name}: succeeded in attempt 1, result kept")
    assert views["reviewer"].kept_from is None

    task = seen["dashboard"]["tasks"][0]
    assert task["agents"]["builder"]["kept_from"] == 1
    assert task["agents"]["tester"]["kept_from"] == 1
    assert task["cost_usd"] == 0  # the kept agents were paid for in attempt 1


def test_while_the_tester_runs_again_only_the_builder_is_kept(world, agents):
    stop_at(world, agents, "tester")
    seen = {}
    watch_while(agents, "tester", world, seen)
    resume(world, agents)
    views = seen["views"]
    assert views["builder"].kept_from == 1
    assert views["tester"].kept_from is None and views["reviewer"].state == "not started"


def test_after_the_task_ends_the_kept_builder_still_shows_its_attempt(world, agents):
    stop_at(world, agents, "tester")
    record = resume(world, agents)
    view = progress.task_agent_view(RUN_ID, "T1", "builder", record, world["script"], NOW)
    assert view.kept_from == 1 and view.state == "finished"
    assert progress.agent_attempt(RUN_ID, "T1", "builder", record, world["script"]) == 1
    assert progress.agent_attempt(RUN_ID, "T1", "tester", record, world["script"]) == 2


def test_an_older_record_without_the_attempt_falls_back_to_the_one_before():
    record = {"attempt": 3, "reused_agents": {"builder": {"outcome": "success"}}}
    from pathlib import Path
    script = Path("no-such-runner.py")
    assert progress.kept_attempt(RUN_ID, "T1", "builder", record, script) == 2
    assert progress.kept_attempt(RUN_ID, "T1", "tester", record, script) is None
