"""The status command reads task records and agent progress files."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.execution.runner_adapter import RUNS_DIR_VARIABLE

import orchestrator.cli.commands as cli
from orchestrator.monitoring import progress as pg
from orchestrator.records.task_registry import save_task, update_task

NOW = datetime(2026, 10, 22, 8, 0, 0, tzinfo=timezone.utc)
RUN = "run-7"


def stamp(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def progress(state="running", ago=2, **extra):
    data = {"state": state, "turns": 3, "input_tokens": 4, "cache_read_input_tokens": 9000,
            "cache_creation_input_tokens": 1000, "output_tokens": 500, "last_tool": "Edit",
            "heartbeat_at": stamp(ago), "status": None, "total_cost_usd": None}
    data.update(extra)
    return data


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A runs dir for task records and a fake runner script whose runs/ holds progress."""
    script = tmp_path / "lab" / "scripts" / "claude_runner.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    monkeypatch.setenv(RUNS_DIR_VARIABLE, str(tmp_path / "lab" / "runs"))  # where agents' files are
    return {"runs": tmp_path / "records", "script": script, "agents": tmp_path / "lab" / "runs"}


def write_progress(layout, agent_run, data):
    folder = layout["agents"] / agent_run
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "progress.json").write_text(json.dumps(data), encoding="utf-8")


def start_task(layout, task_id, *statuses, title="A task"):
    save_task(layout["runs"], RUN, task_id, {"title": title})
    for status in statuses:
        update_task(layout["runs"], RUN, task_id, {"status": status})


# --- one agent ---------------------------------------------------------------------


def test_tokens_read_include_cached_input():
    view = pg.agent_view("builder", progress(), NOW)
    assert view.tokens_read == 10004 and view.tokens_written == 500
    assert "10.0k tokens read, 500 written" in view.describe()


def test_a_running_agent_shows_its_heartbeat_age_and_last_tool():
    line = pg.agent_view("tester", progress(ago=4), NOW).describe()
    assert line.startswith("tester: running, 3 turns")
    assert "last tool Edit" in line and "heartbeat 4s ago" in line and "stopped?" not in line


def test_a_silent_heartbeat_is_flagged():
    view = pg.agent_view("builder", progress(ago=pg.STALE_SECONDS + 5), NOW)
    assert view.stale and "no heartbeat: stopped?" in view.describe()


def test_a_finished_agent_is_never_stale_and_shows_its_cost():
    view = pg.agent_view("reviewer", progress("finished", ago=3600, status="succeeded",
                                              total_cost_usd=0.0337), NOW)
    assert not view.stale
    line = view.describe()
    assert line.startswith("reviewer: succeeded") and "$0.0337" in line and "heartbeat" not in line


def test_missing_or_broken_progress_files_are_handled(tmp_path):
    assert pg.read_progress(tmp_path / "missing.json") is None
    (tmp_path / "half.json").write_text('{"state": "runn', encoding="utf-8")
    assert pg.read_progress(tmp_path / "half.json") is None
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
    assert pg.read_progress(tmp_path / "list.json") is None
    assert pg.agent_view("builder", None, NOW).describe() == "builder: not started"


def test_odd_values_in_a_progress_file_do_not_break_the_view():
    view = pg.agent_view("builder", {"state": "weird", "turns": "many", "heartbeat_at": "soon",
                                     "total_cost_usd": True, "last_tool": 7}, NOW)
    assert view.state == "running" and view.turns == 0 and view.heartbeat_age is None
    assert view.cost_usd is None and view.last_tool is None


# --- a whole run -------------------------------------------------------------------------


def test_run_status_shows_each_task_and_marks_the_active_agent(layout):
    start_task(layout, "T1", "building", "testing", "reviewing", "done", title="Done task")
    start_task(layout, "T2", "building", "testing", title="Busy task")
    for role in ("builder", "tester", "reviewer"):
        write_progress(layout, f"{RUN}-T1-{role}",
                       progress("finished", status="succeeded", total_cost_usd=0.05))
    write_progress(layout, f"{RUN}-T2-builder", progress("finished", status="succeeded", total_cost_usd=0.09))
    write_progress(layout, f"{RUN}-T2-tester", progress(ago=3))

    tasks = pg.run_status(layout["runs"], RUN, runner_script=layout["script"], now=NOW)
    assert [t.task_id for t in tasks] == ["T1", "T2"]
    t1, t2 = tasks
    assert t1.active_role is None and t1.spent_usd == pytest.approx(0.15)
    assert t2.active_role == "tester"
    assert [a.state for a in t2.agents] == ["finished", "running", "not started"]

    text = pg.format_status(RUN, tasks)
    assert "T2  testing" in text and "-> tester: running" in text
    assert "   reviewer: not started" in text and "spent so far: $0.0900" in text
    assert pg.stale_agents(tasks) == []


def test_a_dead_agent_is_listed(layout):
    start_task(layout, "T1", "building")
    write_progress(layout, f"{RUN}-T1-builder", progress(ago=120))
    tasks = pg.run_status(layout["runs"], RUN, runner_script=layout["script"], now=NOW)
    assert pg.stale_agents(tasks) == ["T1 builder"]


def test_a_run_from_before_progress_files_uses_the_task_record(layout):
    start_task(layout, "T1", "building", "testing", "reviewing", "done")
    update_task(layout["runs"], RUN, "T1", {"agents": {
        "builder": {"status": "succeeded", "total_cost_usd": 0.0918607, "num_turns": 1,
                    "input_tokens": 2},
        "tester": {"status": "succeeded", "total_cost_usd": 0.0699342, "num_turns": 6},
    }})
    (t1,) = pg.run_status(layout["runs"], RUN, runner_script=layout["script"], now=NOW)
    builder, tester, reviewer = (a.describe() for a in t1.agents)
    assert builder == "builder: succeeded, 1 turns, $0.0919"  # no misleading token count
    assert tester.startswith("tester: succeeded, 6 turns")
    assert reviewer == "reviewer: not started"
    assert t1.spent_usd == pytest.approx(0.1617949)


def test_a_progress_file_wins_over_the_task_record(layout):
    start_task(layout, "T1", "building")
    update_task(layout["runs"], RUN, "T1", {"agents": {"builder": {"status": "failed"}}})
    write_progress(layout, f"{RUN}-T1-builder", progress(ago=1))
    (t1,) = pg.run_status(layout["runs"], RUN, runner_script=layout["script"], now=NOW)
    assert t1.agents[0].describe().startswith("builder: running")


def test_a_run_with_no_tasks_yet(layout):
    assert pg.run_status(layout["runs"], RUN, runner_script=layout["script"], now=NOW) == []
    assert "No task has been started yet." in pg.format_status(RUN, [])


def test_the_planner_is_shown_when_it_left_progress(layout):
    assert pg.planner_view(RUN, runner_script=layout["script"], now=NOW) is None
    write_progress(layout, f"{RUN}-plan", progress("finished", status="succeeded", total_cost_usd=0.1))
    planner = pg.planner_view(RUN, runner_script=layout["script"], now=NOW)
    assert planner.role == "planner" and "planner: succeeded" in pg.format_status(RUN, [], planner)


# --- the command ---------------------------------------------------------------------------


@pytest.fixture
def cli_layout(layout, monkeypatch):
    original = pg.runner_paths.runner_run_directory
    monkeypatch.setattr(pg.runner_paths, "runner_run_directory",
                        lambda run_id, script=None: original(run_id, layout["script"]))
    return layout


def test_status_command_prints_the_run(cli_layout, capsys):
    start_task(cli_layout, "T1", "building")
    write_progress(cli_layout, f"{RUN}-T1-builder", progress(ago=1))
    code = cli.main(["status", RUN, "--runs-dir", str(cli_layout["runs"])])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert f"Run {RUN}" in out and "T1  building" in out and "-> builder: running" in out


def test_status_warns_about_a_dead_agent(cli_layout, capsys):
    start_task(cli_layout, "T1", "building")
    write_progress(cli_layout, f"{RUN}-T1-builder", {"state": "running", "heartbeat_at": "2000-01-01T00:00:00+00:00"})
    cli.main(["status", RUN, "--runs-dir", str(cli_layout["runs"])])
    assert "Warning: no heartbeat from T1 builder" in capsys.readouterr().out


def test_status_of_an_unknown_run_is_an_error(cli_layout, tmp_path, capsys):
    code = cli.main(["status", "no-such-run", "--runs-dir", str(cli_layout["runs"]),
                     "--plans-dir", str(tmp_path / "plans")])
    assert code == cli.EXIT_ERROR and "No plan or tasks found" in capsys.readouterr().err


def test_watch_stops_by_itself_when_nothing_is_running(cli_layout, monkeypatch, capsys):
    start_task(cli_layout, "T1", "building", "testing", "reviewing", "done")
    monkeypatch.setattr(cli.monitor.os, "system", lambda command: 0)  # don't clear the test's screen
    code = cli.main(["status", RUN, "--runs-dir", str(cli_layout["runs"]), "--watch", "1"])
    assert code == cli.EXIT_OK and "Nothing is running any more." in capsys.readouterr().out


def test_watch_refreshes_while_a_task_runs_and_ctrl_c_exits_cleanly(cli_layout, monkeypatch, capsys):
    start_task(cli_layout, "T1", "building")
    monkeypatch.setattr(cli.monitor.os, "system", lambda command: 0)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.monitor.time, "sleep", fake_sleep)
    code = cli.main(["status", RUN, "--runs-dir", str(cli_layout["runs"]), "--watch", "2"])
    assert code == cli.EXIT_OK and sleeps == [2.0, 2.0]
    assert capsys.readouterr().out.count("T1  building") == 2


def test_watch_rejects_a_too_short_interval(cli_layout, capsys):
    start_task(cli_layout, "T1", "building")
    code = cli.main(["status", RUN, "--runs-dir", str(cli_layout["runs"]), "--watch", "0.2"])
    assert code == cli.EXIT_REFUSED
