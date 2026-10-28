"""Where a stored plan stands, worked out from the task records."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import orchestrator.cli.commands as cli
from orchestrator.execution.background import launch_path
from orchestrator.execution.runner_adapter import runner_run_directory
from orchestrator.planning.plan_file import save_task_plan
from orchestrator.records.run_registry import write_json_atomic
from orchestrator.records.task_registry import save_task, update_task
from orchestrator.execution import liveness
from orchestrator.scheduling import plan_progress as pp

RUN = "run-p"
NOW = datetime(2026, 10, 23, 8, 0, 0, tzinfo=timezone.utc)


def plan(*tasks):
    """tasks: (id, [dependencies]) pairs, in plan order."""
    return {"tasks": [{"id": i, "title": f"Task {i}", "depends_on": list(d)} for i, d in tasks]}


def rec(status, **extra):
    return {"status": status, **extra}


def alive(*running):
    """A liveness check that says the listed tasks are running and no others are."""
    return lambda task_id, record: (task_id in running, "its process (9) is no longer running")


def states(progress):
    return {t.task_id: t.state for t in progress.tasks}


# --- tasks that have not started ---------------------------------------------------------------


def test_a_fresh_plan_has_its_roots_ready_and_the_rest_waiting():
    p = pp.compute_progress(RUN, plan(("T1", []), ("T2", ["T1"]), ("T3", [])), {}, alive())
    assert states(p) == {"T1": "ready", "T2": "waiting", "T3": "ready"}
    assert p.task("T2").waiting_for == ("T1",)
    assert p.ready == ["T1", "T3"] and p.next_task == "T1"  # plan order
    assert p.state == "ready"


def test_a_task_becomes_ready_once_every_dependency_is_done():
    shape = plan(("T1", []), ("T2", []), ("T3", ["T1", "T2"]))
    half = pp.compute_progress(RUN, shape, {"T1": rec("done")}, alive())
    assert half.task("T3").state == "waiting" and half.task("T3").waiting_for == ("T2",)
    both = pp.compute_progress(RUN, shape, {"T1": rec("done"), "T2": rec("done")}, alive())
    assert both.task("T3").state == "ready" and both.next_task == "T3"


@pytest.mark.parametrize("final", ["failed", "needs_review", "cancelled"])
def test_a_task_after_an_unfinished_one_is_blocked_and_names_it(final):
    p = pp.compute_progress(RUN, plan(("T1", []), ("T2", ["T1"])), {"T1": rec(final)}, alive())
    assert p.task("T2").state == "blocked" and p.task("T2").blocked_by == ("T1",)


def test_blocking_passes_down_a_chain_and_names_the_task_that_failed():
    shape = plan(("T1", []), ("T2", ["T1"]), ("T3", ["T2"]), ("T4", ["T3"]))
    p = pp.compute_progress(RUN, shape, {"T1": rec("failed")}, alive())
    assert states(p) == {"T1": "failed", "T2": "blocked", "T3": "blocked", "T4": "blocked"}
    assert p.task("T4").blocked_by == ("T1",)  # the cause, not the task next to it


def test_a_failure_does_not_stop_tasks_that_do_not_depend_on_it():
    shape = plan(("T1", []), ("T2", ["T1"]), ("T3", []), ("T4", ["T3"]))
    p = pp.compute_progress(RUN, shape, {"T1": rec("failed"), "T3": rec("done")}, alive())
    assert states(p) == {"T1": "failed", "T2": "blocked", "T3": "done", "T4": "ready"}
    assert p.state == "ready" and p.next_task == "T4"


def test_blocked_wins_over_waiting():
    # T3 can never finish while T1 has failed, whatever T2 does.
    shape = plan(("T1", []), ("T2", []), ("T3", ["T1", "T2"]))
    p = pp.compute_progress(RUN, shape, {"T1": rec("failed"), "T2": rec("testing")}, alive("T2"))
    assert p.task("T3").state == "blocked" and p.task("T3").blocked_by == ("T1",)


def test_a_diamond_names_each_cause_once():
    shape = plan(("T1", []), ("T2", ["T1"]), ("T3", ["T1"]), ("T4", ["T2", "T3"]))
    p = pp.compute_progress(RUN, shape, {"T1": rec("failed")}, alive())
    assert p.task("T4").blocked_by == ("T1",)


def test_plan_order_does_not_have_to_follow_dependencies():
    p = pp.compute_progress(RUN, plan(("T2", ["T1"]), ("T1", [])), {"T1": rec("done")}, alive())
    assert states(p) == {"T2": "ready", "T1": "done"}


# --- tasks with a record ------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["pending", "building", "testing", "reviewing"])
def test_a_task_at_a_stage_is_running_only_while_its_process_is(stage):
    shape = plan(("T1", []), ("T2", ["T1"]))
    live = pp.compute_progress(RUN, shape, {"T1": rec(stage)}, alive("T1"))
    assert live.task("T1").state == "running" and live.task("T1").stage == stage
    assert live.state == "running" and live.task("T2").state == "waiting"
    dead = pp.compute_progress(RUN, shape, {"T1": rec(stage)}, alive())
    assert dead.task("T1").state == "interrupted" and dead.task("T1").stage == stage
    reason = dead.task("T1").reason
    assert reason is not None and "no longer running" in reason
    assert dead.task("T2").state == "waiting"  # an interrupted task can still be continued
    assert dead.state == "stopped" and [t.task_id for t in dead.stopped_by] == ["T1"]


def test_a_finished_record_keeps_its_first_reason():
    long = "Builder: the change broke " + "x" * 300
    p = pp.compute_progress(RUN, plan(("T1", [])), {"T1": rec("failed", reasons=[long, "second"])}, alive())
    reason = p.task("T1").reason
    assert reason is not None
    assert reason.startswith("Builder: the change broke") and len(reason) == pp.REASON_LIMIT


def test_the_plan_is_complete_when_every_task_is_done():
    p = pp.compute_progress(RUN, plan(("T1", []), ("T2", ["T1"])),
                            {"T1": rec("done"), "T2": rec("done")}, alive())
    assert p.state == "complete" and p.next_task is None and p.stopped_by == []


def test_a_plan_with_nothing_left_to_start_is_stopped_and_says_why():
    shape = plan(("T1", []), ("T2", ["T1"]), ("T3", []))
    p = pp.compute_progress(RUN, shape, {"T1": rec("failed"), "T3": rec("done")}, alive())
    assert p.state == "stopped" and [t.task_id for t in p.stopped_by] == ["T1"]


def test_a_record_for_a_task_the_plan_does_not_have_is_reported():
    p = pp.compute_progress(RUN, plan(("T1", [])), {"T1": rec("done"), "T9": rec("done")}, alive())
    assert p.unknown_records == ("T9",) and p.state == "complete"


# --- is the process still running? --------------------------------------------------------------

OURS = f"C:\\lab\\.venv\\Scripts\\python.exe -m orchestrator run-task {RUN} T1 --runs-dir x"


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "orchestration"


def started(runs, status="testing", **fields):
    save_task(runs, RUN, "T1", {"title": "t", **fields})
    for step in ("building", "testing", "reviewing")[: ("building", "testing", "reviewing").index(status) + 1]:
        update_task(runs, RUN, "T1", {"status": step})
    from orchestrator.records.task_registry import load_task
    return load_task(runs, RUN, "T1")


def test_a_process_that_is_still_this_tasks_counts_as_running(runs):
    record = started(runs, pid=111)
    check = pp.process_liveness(runs, RUN, get_command_line={111: OURS}.get)
    assert check("T1", record) == (True, "process 111 is running it")


def test_a_reused_process_id_does_not_count(runs):
    record = started(runs, pid=111)
    check = pp.process_liveness(runs, RUN, get_command_line={111: "C:\\Windows\\notepad.exe"}.get)
    alive_now, why = check("T1", record)
    assert not alive_now and "(111) is no longer running" in why


def test_a_background_run_is_found_by_its_launcher_too(runs):
    # The task record's own process has gone, but the launcher in launch.json still runs it.
    record = started(runs, pid=111)
    write_json_atomic(launch_path(runs, RUN, "T1"), {"pid": 222})
    check = pp.process_liveness(runs, RUN, get_command_line={222: OURS}.get)
    assert check("T1", record) == (True, "process 222 is running it")


def heartbeat(role, seconds_ago, state="running"):
    path = runner_run_directory(f"{RUN}-T1-{role}") / "progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (NOW - timedelta(seconds=seconds_ago)).isoformat()
    path.write_text(json.dumps({"state": state, "heartbeat_at": stamp}), encoding="utf-8")


def test_an_older_record_without_a_process_id_uses_the_working_agents_heartbeat(runs):
    record = started(runs, status="testing")  # no pid: written before M3.8
    check = pp.process_liveness(runs, RUN, get_command_line=lambda pid: pytest.fail("no pid to check"), now=NOW)
    heartbeat("tester", 5)
    assert check("T1", record) == (True, "the tester's heartbeat is recent")
    heartbeat("tester", 300)
    assert check("T1", record)[0] is False
    heartbeat("builder", 1)  # the builder is not the agent working in "testing"
    heartbeat("tester", 300)
    assert check("T1", record)[0] is False


def test_plan_progress_reads_the_records_from_disk(runs):
    started(runs, status="building", pid=111)
    p = pp.plan_progress(plan(("T1", []), ("T2", ["T1"])), runs, RUN, get_command_line=lambda pid: None)
    assert states(p) == {"T1": "interrupted", "T2": "waiting"}
    assert p.task("T1").reason == "its process (111) is no longer running"


# --- showing it -----------------------------------------------------------------------------------


def test_the_summary_names_what_runs_next_and_what_blocks_what():
    shape = plan(("T1", []), ("T2", ["T1"]), ("T3", []), ("T4", ["T3"]), ("T5", ["T2"]))
    p = pp.compute_progress(RUN, shape, {"T1": rec("failed", reasons=["Tests failed."]),
                                         "T3": rec("done")}, alive())
    text = pp.format_plan_progress(p)
    lines = text.splitlines()
    assert lines[0] == "Plan: 1 of 5 done · next T4"
    assert "T1   failed       Tests failed." in text
    assert "T2   blocked      by T1 (failed)" in text
    assert "T4   ready        can start now" in text
    assert "T5   blocked      by T1 (failed)" in text


def test_the_summary_of_a_stopped_plan_says_what_stopped_it():
    p = pp.compute_progress(RUN, plan(("T1", []), ("T2", ["T1"])), {"T1": rec("testing")}, alive())
    text = pp.format_plan_progress(p)
    assert text.splitlines()[0] == "Plan: 0 of 2 done · stopped: T1 interrupted"
    assert "T1   interrupted  stopped while testing: its process (9) is no longer running" in text
    assert "T2   waiting      for T1" in text


def test_the_summary_of_a_complete_plan():
    p = pp.compute_progress(RUN, plan(("T1", [])), {"T1": rec("done")}, alive())
    assert pp.format_plan_progress(p).splitlines()[0] == "Plan: 1 of 1 done · complete"


# --- the status command ----------------------------------------------------------------------------


def stored_plan(runs):
    task = {"title": "t", "description": "d", "expected_outcome": "o", "files": ["README.md"],
            "verification": [], "acceptance_criteria": ["it works"], "complexity": "small",
            "parallel_safe": False}
    save_task_plan(runs, RUN, {
        "issue": {"repository": "Anji-jk/Agent-lab", "number": 5, "title": "t", "description": "x", "url": "u"},
        "config": {"repository": "Anji-jk/Agent-lab", "base_ref": "main", "base_commit": "a" * 40,
                   "model": None, "timeout_seconds": 60, "run_id": RUN, "branch": "b", "worktree_path": "w"},
        "summary": "s",
        "tasks": [{"id": "T1", "depends_on": [], **task}, {"id": "T2", "depends_on": ["T1"], **task}],
        "planner": {"run_id": f"{RUN}-plan"},
    })


def test_status_shows_the_plan_progress(runs, monkeypatch, capsys):
    stored_plan(runs)
    started(runs, status="testing", pid=111)
    monkeypatch.setattr(liveness, "command_line", lambda pid: None)  # its process has gone
    code = cli.main(["status", RUN, "--runs-dir", str(runs), "--plans-dir", str(runs)])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    # run-plan would continue T1, so the plan is not stopped
    assert "Plan: 0 of 2 done · next: continue T1" in out and "stopped:" not in out
    assert "T2   waiting      for T1" in out


def test_status_before_any_task_ran_shows_the_first_task_ready(runs, capsys):
    stored_plan(runs)
    assert cli.main(["status", RUN, "--runs-dir", str(runs), "--plans-dir", str(runs)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Plan: 0 of 2 done · next: start T1" in out and "T1   ready        can start now" in out


def test_status_watch_stops_once_the_only_running_task_is_interrupted(runs, monkeypatch, capsys):
    # Before 4.1 the record's "testing" kept --watch going for ever.
    stored_plan(runs)
    started(runs, status="testing", pid=111)
    monkeypatch.setattr(liveness, "command_line", lambda pid: None)
    monkeypatch.setattr(cli.monitor.time, "sleep", lambda s: pytest.fail("should not wait"))
    monkeypatch.setattr(cli.monitor.os, "system", lambda command: 0)
    code = cli.main(["status", RUN, "--watch", "2", "--runs-dir", str(runs), "--plans-dir", str(runs)])
    assert code == cli.EXIT_OK and "Nothing is running any more." in capsys.readouterr().out

# --- M5.0: "next" as run-plan would do it ------------------------------------------------------


def test_next_names_what_run_plan_would_do_not_just_the_first_ready_task():
    shape = plan(("T1", []), ("T2", ["T1"]), ("T3", []), ("T4", ["T3"]), ("T5", ["T2"]))
    p = pp.compute_progress(RUN, shape, {"T1": rec("failed", reasons=["Tests failed."]),
                                         "T3": rec("done")}, alive())
    assert pp.format_plan_progress(p).splitlines()[0] == "Plan: 1 of 5 done · next T4"  # without it
    assert pp.format_plan_progress(p, next_step="retry T1").splitlines()[0] == (
        "Plan: 1 of 5 done · next: retry T1")


def test_a_plan_run_plan_would_stop_on_says_stopped_and_names_no_next():
    shape = plan(("T1", []), ("T2", ["T1"]))
    p = pp.compute_progress(RUN, shape, {"T1": rec("needs_review", reasons=["Crashed."])}, alive())
    line = pp.format_plan_progress(p, next_step="").splitlines()[0]
    assert line == "Plan: 0 of 2 done · stopped: T1 needs_review"


def test_describe_next():
    from orchestrator.scheduling.run_plan import describe_next
    assert describe_next(("continue", "T2")) == "continue T2"
    assert describe_next(("run", "T3")) == "start T3"
    assert describe_next(("retry", "T1")) == "retry T1"
    assert describe_next("T1 needs review.") == "" and describe_next(None) == ""
