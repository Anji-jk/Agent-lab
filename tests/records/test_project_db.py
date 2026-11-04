"""The project database, built from run files written by hand."""

import json
import sqlite3

import pytest

from orchestrator.records import project_db as pdb

RUN = "run-20261104-000000-abc123"


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if isinstance(data, dict) else data, encoding="utf-8")


def task(task_id, **more):
    return {"id": task_id, "title": f"title {task_id}", "description": "d", "depends_on": [],
            "expected_outcome": "o", "files": ["a.py"], "verification": [],
            "acceptance_criteria": ["c"], "complexity": "small", "parallel_safe": False, **more}


@pytest.fixture
def lab(tmp_path):
    """A project .agent-lab: one story run with done, retried-failed, and planned tasks."""
    runs, agents = tmp_path / "runs", tmp_path / "agents"
    write(runs / RUN / "plan.json", {
        "source": {"kind": "jira", "key": "KAN-10", "url": "https://x/browse/KAN-10",
                   "epic": {"key": "KAN-9", "summary": "Reports"}},
        "issue": {"repository": None, "number": None, "key": "KAN-10", "title": "Export",
                  "url": "https://x/browse/KAN-10", "description": "d"},
        "config": {"repository": None, "base_ref": "main", "base_commit": "b" * 40,
                   "run_id": RUN, "branch": f"agent/KAN-10-{RUN}"},
        "tasks": [task("T1", jira_key="KAN-11"),
                  task("T2", jira_key="KAN-12", depends_on=["T1"]), task("T3")],
        "planner": {"run_id": f"{RUN}-plan", "total_cost_usd": 0.5, "model_used": "m"},
        "created_at": "2026-11-04T10:00:00+00:00",
    })
    write(runs / RUN / "tasks" / "T1" / "task.json", {
        "run_id": RUN, "task_id": "T1", "status": "done", "commit": "c" * 40, "cost_usd": 1.25,
        "reasons": [], "created_at": "2026-11-04T10:01:00+00:00",
        "updated_at": "2026-11-04T10:10:00+00:00",
        "agents": {"builder": {"outcome": "success", "attempt": 1, "total_cost_usd": 1.0},
                   "tester": {"outcome": "success", "attempt": 1, "total_cost_usd": 0.25}}})
    write(runs / RUN / "tasks" / "T2" / "attempt-1" / "task.json", {
        "run_id": RUN, "task_id": "T2", "status": "failed", "cost_usd": 0.5,
        "reasons": ["tests failed"],
        "agents": {"builder": {"outcome": "success", "attempt": 1}}})
    write(runs / RUN / "tasks" / "T2" / "task.json", {
        "run_id": RUN, "task_id": "T2", "status": "failed", "attempt": 2, "cost_usd": 0.75,
        "reasons": ["The builder timed out: no answer in 600s"],
        "previous_attempts": [{"attempt": 1, "status": "failed", "cost_usd": 0.5}],
        "updated_at": "2026-11-04T10:30:00+00:00",
        "agents": {"builder": {"outcome": "timeout", "attempt": 2, "total_cost_usd": 0.75,
                               "error": "timed out after 600s"}}})
    write(runs / RUN / "run-plan-logs" / "T2-1.log", "old\n")
    write(runs / RUN / "run-plan-logs" / "T2-2.log",
          "".join(f"line {number}\n" for number in range(100)))
    write(runs / RUN / "integration.json", {
        "branch": f"agent/KAN-10-{RUN}", "head": "h" * 40,
        "merges": [{"task": "T1", "commit": "c" * 40, "kind": "fast-forward", "head": "h" * 40,
                    "at": "2026-11-04T10:11:00+00:00"}]})
    write(runs / RUN / "report.md", "# report\n")
    write(agents / f"{RUN}-T2-r2-builder" / "result.json", {
        "status": "timed_out", "exit_code": None, "timed_out": True, "error": "Timed out.",
        "event_count": 42, "stderr_file": str(agents / f"{RUN}-T2-r2-builder" / "stderr.log"),
        "started_at": "2026-11-04T10:20:00+00:00"})
    write(agents / f"{RUN}-T2-r2-builder" / "stderr.log", "warning\nfatal: something broke\n")
    write(agents / f"{RUN}-plan" / "result.json", {"status": "succeeded", "num_turns": 9})
    return runs, agents


def rows(path, sql, *parameters):
    return [dict(row) for row in pdb.query(path, sql, parameters)]


def test_rebuild_fills_every_table(lab):
    runs, agents = lab
    found = pdb.rebuild(runs, agents)
    assert found == {"runs": 1, "tasks": 3, "attempts": 3, "agent_runs": 5, "merges": 1}
    path = pdb.db_path(runs)
    assert path == runs.parent / "agentlab.db"
    (run,) = rows(path, "SELECT * FROM runs")
    assert (run["source_kind"], run["item"], run["epic"]) == ("jira", "KAN-10", "KAN-9")
    assert run["status"] == "stopped"
    assert (run["tasks_total"], run["tasks_done"], run["tasks_failed"]) == (3, 1, 1)
    assert run["cost_usd"] == 0.5 + 1.25 + 0.5 + 0.75
    assert run["head_commit"] == "h" * 40 and run["report_path"].endswith("report.md")


def test_tasks_keep_their_plan_and_their_errors(lab):
    runs, agents = lab
    pdb.rebuild(runs, agents)
    tasks = {item["task_id"]: item for item in rows(pdb.db_path(runs), "SELECT * FROM tasks")}
    assert tasks["T1"]["jira_key"] == "KAN-11" and tasks["T1"]["merge_kind"] == "fast-forward"
    assert tasks["T1"]["error"] is None and tasks["T1"]["log_tail"] is None
    retry = tasks["T2"]
    assert (retry["attempt"], retry["attempts"], retry["cost_usd"]) == (2, 2, 1.25)
    assert retry["error"] == "The builder timed out: no answer in 600s"
    assert retry["log_path"].endswith("T2-2.log")
    assert retry["log_tail"].splitlines()[-1] == "line 99"
    assert len(retry["log_tail"].splitlines()) == pdb.TAIL_LINES
    assert json.loads(retry["depends_on"]) == ["T1"]
    assert tasks["T3"]["status"] == "planned" and tasks["T3"]["attempts"] == 0


def test_attempts_and_agents(lab):
    runs, agents = lab
    pdb.rebuild(runs, agents)
    path = pdb.db_path(runs)
    assert rows(path, "SELECT task_id, attempt, status, error FROM attempts WHERE task_id='T2'"
                     " ORDER BY attempt") == [
        {"task_id": "T2", "attempt": 1, "status": "failed", "error": "tests failed"},
        {"task_id": "T2", "attempt": 2, "status": "failed",
         "error": "The builder timed out: no answer in 600s"}]
    (builder,) = rows(path, "SELECT * FROM agent_runs WHERE task_id='T2' AND attempt=2")
    assert builder["agent_run_id"] == f"{RUN}-T2-r2-builder"
    assert builder["outcome"] == "timeout" and builder["timed_out"] == 1
    assert builder["error"] == "Timed out." and builder["diagnosis"] == "timed out after 600s"
    assert builder["stderr_tail"] == "warning\nfatal: something broke"
    assert builder["event_count"] == 42 and builder["folder"].endswith("-T2-r2-builder")
    (planner,) = rows(path, "SELECT * FROM agent_runs WHERE role='planner'")
    assert planner["task_id"] is None and planner["cost_usd"] == 0.5 and planner["num_turns"] == 9
    successful = rows(path, "SELECT stderr_tail, diagnosis FROM agent_runs WHERE task_id='T1'")
    assert all(row == {"stderr_tail": None, "diagnosis": None} for row in successful)


def test_the_files_are_only_read(lab):
    runs, agents = lab
    before = {path: path.read_bytes() for path in [*runs.rglob("*"), *agents.rglob("*")]
              if path.is_file()}
    pdb.rebuild(runs, agents)
    after = {path: path.read_bytes() for path in [*runs.rglob("*"), *agents.rglob("*")]
             if path.is_file()}
    assert after == before


def test_refresh_replaces_one_run(lab):
    runs, agents = lab
    pdb.rebuild(runs, agents)
    record = runs / RUN / "tasks" / "T1" / "task.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(json.dumps({**data, "cost_usd": 2.0}), encoding="utf-8")
    pdb.refresh_run(runs, RUN, agents)
    assert rows(pdb.db_path(runs), "SELECT cost_usd FROM tasks WHERE task_id='T1'") == [
        {"cost_usd": 2.0}]
    assert pdb.counts(pdb.db_path(runs))["tasks"] == 3


def test_a_missing_or_older_database_is_rebuilt(lab):
    runs, agents = lab
    path = pdb.db_path(runs)
    pdb.refresh_run(runs, RUN, agents)
    assert pdb.counts(path)["runs"] == 1
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        connection.execute("DELETE FROM tasks")
    connection.close()
    pdb.refresh_run(runs, RUN, agents)
    assert pdb.counts(path)["tasks"] == 3


def test_try_refresh_never_raises(lab):
    runs, agents = lab
    pdb.db_path(runs).mkdir()
    problem = pdb.try_refresh_run(runs, RUN, agents)
    assert problem and "Error" in problem


def test_an_issue_run_and_odd_files(tmp_path):
    runs = tmp_path / "runs"
    write(runs / "r1" / "plan.json", {
        "issue": {"repository": "o/r", "number": 12, "title": "t"},
        "config": {}, "tasks": [task("T1"), "junk"]})
    write(runs / "r2" / "plan.json", "not json")
    write(runs / "r1" / "tasks" / "T1" / "task.json", "{broken")
    found = pdb.rebuild(runs, tmp_path / "agents")
    assert found["runs"] == 1 and found["tasks"] == 1
    (run,) = rows(pdb.db_path(runs), "SELECT source_kind, item, repository, status FROM runs")
    assert run == {"source_kind": "github", "item": "12", "repository": "o/r",
                   "status": "planned"}


def test_an_empty_project(tmp_path):
    assert pdb.rebuild(tmp_path / "runs", tmp_path / "agents") == dict.fromkeys(pdb.TABLES, 0)


def test_db_commands(lab, capsys):
    import orchestrator.cli.commands as cli

    runs, _ = lab
    assert cli.main(["db", "summary", "--runs-dir", str(runs)]) == cli.EXIT_REFUSED
    assert "Run: db rebuild" in capsys.readouterr().err
    assert cli.main(["db", "rebuild", "--runs-dir", str(runs)]) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert "runs 1, tasks 3, attempts 3" in output and "only read" in output
    assert cli.main(["db", "summary", "--runs-dir", str(runs)]) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert f"{RUN}  KAN-10  stopped  tasks 1/3 done  $3.00  Export" in output
    assert f"{RUN} T2  failed: The builder timed out" in output
