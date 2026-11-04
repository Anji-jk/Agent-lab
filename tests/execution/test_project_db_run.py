"""After a task ends, the project database holds the run as the files describe it."""

from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.records import project_db as pdb
from task_runner_support import RUN_ID, run


def rows(world, sql, *parameters):
    return [dict(row) for row in pdb.query(pdb.db_path(world["runs"]), sql, parameters)]


def test_a_finished_task_is_in_the_database(world, agents):
    record = run(world, agents)
    assert record["status"] == "done", record["reasons"]
    path = pdb.db_path(world["runs"])
    assert path == world["runs"].parent / "agentlab.db" and path.is_file()

    (the_run,) = rows(world, "SELECT * FROM runs")
    assert the_run["run_id"] == RUN_ID and the_run["source_kind"] == "github"
    assert the_run["item"] == "7" and the_run["title"] == "Add helpers"
    assert the_run["status"] == "partly done"
    assert (the_run["tasks_total"], the_run["tasks_done"]) == (2, 1)
    assert the_run["head_commit"] == record["merge"]["head"]

    tasks = {task["task_id"]: task for task in rows(world, "SELECT * FROM tasks")}
    assert tasks["T1"]["status"] == "done" and tasks["T1"]["commit_sha"] == record["commit"]
    assert tasks["T1"]["merge_kind"] == record["merge"]["kind"]
    assert tasks["T1"]["cost_usd"] == record["cost_usd"]
    assert tasks["T1"]["error"] is None and tasks["T1"]["log_tail"] is None
    assert tasks["T2"]["status"] == "planned" and tasks["T2"]["depends_on"] == '["T1"]'

    agents = rows(world, "SELECT role, outcome, cost_usd FROM agent_runs WHERE task_id = 'T1' "
                         "ORDER BY role")
    assert agents == [{"role": "builder", "outcome": "success", "cost_usd": 0.1},
                      {"role": "reviewer", "outcome": "success", "cost_usd": 0.1},
                      {"role": "tester", "outcome": "success", "cost_usd": 0.1}]
    assert rows(world, "SELECT task_id, position FROM merges") == [
        {"task_id": "T1", "position": 1}]
    assert rows(world, "SELECT attempt, status FROM attempts") == [
        {"attempt": 1, "status": "done"}]


def test_a_failed_task_keeps_its_error(world, agents):
    agents.fail["builder"] = RunnerOutcome("timeout", None, "too slow", {})
    record = run(world, agents)
    assert record["status"] != "done"
    (task,) = rows(world, "SELECT * FROM tasks WHERE task_id = 'T1'")
    assert task["status"] == record["status"]
    assert task["error"] == record["reasons"][0]
    (builder,) = rows(world, "SELECT * FROM agent_runs WHERE role = 'builder'")
    assert builder["outcome"] == "timeout" and builder["diagnosis"]


def test_a_broken_database_does_not_stop_the_task(world, agents):
    pdb.db_path(world["runs"]).mkdir(parents=True)
    record = run(world, agents)
    assert record["status"] == "done"
    from task_runner_support import orchestrator_steps
    assert any(step["step"] == "database_not_updated" for step in orchestrator_steps(world))
