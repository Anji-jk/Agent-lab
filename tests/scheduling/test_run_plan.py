"""Run-plan works through a plan one task at a time."""

import json
from pathlib import Path

import pytest

from orchestrator.execution.budget import set_budget
from orchestrator.execution.cancel import is_plan_process, is_task_process
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.records.task_registry import list_tasks, save_task, update_task
from orchestrator.scheduling import run_plan as rp
from run_plan_support import per_task, stopped, work
from task_runner_support import (  # pyright: ignore[reportMissingImports]  (conftest.py puts it on the path)
    RUN_ID,
    Stop,
    costly,
    git,
    run,
    stopping,
    wrong_double,
)


def actions(result):
    return [(s.action, s.task_id, s.status) for s in result.steps]


def roles_of(agents, task_id):
    return [c["role"] for c in agents.calls if c["run_id"].startswith(f"{RUN_ID}-{task_id}-")]


# --- working through a plan ------------------------------------------------------------------


def test_a_plan_runs_to_the_end_one_task_at_a_time(world, agents):
    result = work(world, per_task(agents))

    assert result.complete and result.stopped is None
    assert actions(result) == [("run", "T1", "done"), ("run", "T2", "done")]
    records = list_tasks(world["runs"], RUN_ID)
    # T2 was built on T1's commit: the run's work is one chain on one branch.
    assert git(world["repo"], "merge-base", "--is-ancestor",
               records["T1"]["commit"], records["T2"]["commit"]) == ""
    assert result.progress is not None and result.progress.state == "complete"


def test_nothing_is_pushed_or_merged(world, agents):
    work(world, per_task(agents))
    assert git(world["repo"], "rev-parse", "HEAD") == world["base"]
    assert git(world["repo"], "status", "--porcelain") == ""


def test_running_it_again_on_a_complete_plan_does_nothing(world, agents):
    work(world, per_task(agents))
    agents.calls.clear()
    again = work(world, per_task(agents))
    assert again.complete and again.steps == [] and agents.calls == []


def test_a_task_that_fails_is_retried_once_then_the_plan_goes_on(world, agents):
    prompts = {}

    def keep_prompt(**kwargs):
        prompts[kwargs["run_id"]] = Path(kwargs["prompt_file"]).read_text(encoding="utf-8")
        return agents(**kwargs)

    invoke = per_task(agents, inner=keep_prompt)
    builds = []

    def wrong_then_right(cwd):
        builds.append(cwd)
        return (wrong_double if len(builds) == 1 else agents.build_double)(cwd)

    invoke.behaviours["T1"]["builder"] = wrong_then_right
    result = work(world, invoke)

    assert actions(result) == [("run", "T1", "failed"), ("retry", "T1", "done"),
                               ("run", "T2", "done")]
    assert result.complete
    assert result.steps[1].attempt == 2
    # The retry's builder was told what went wrong.
    assert len(builds) == 2
    assert "Attempt 1 of this task" in prompts[f"{RUN_ID}-T1-r2-builder"]
    assert "Attempt 1 of this task" not in prompts[f"{RUN_ID}-T1-builder"]


def test_a_task_that_fails_twice_stops_the_plan(world, agents):
    invoke = per_task(agents)
    invoke.behaviours["T1"]["builder"] = wrong_double
    result = work(world, invoke)

    assert actions(result) == [("run", "T1", "failed"), ("retry", "T1", "failed")]
    assert not result.complete and "already been retried" in stopped(result)
    assert "T2" not in list_tasks(world["runs"], RUN_ID)


def test_a_task_that_needs_review_stops_the_plan_without_a_retry(world, agents):
    agents.fail["tester"] = RunnerOutcome("failure", 1, "boom", {})
    result = work(world, per_task(agents))

    assert actions(result) == [("run", "T1", "needs_review")]
    assert "needs review" in stopped(result) and "show-task" in stopped(result)
    assert "T2" not in list_tasks(world["runs"], RUN_ID)


def test_independent_tasks_are_not_run_around_a_stopped_one(with_independent, agents):
    agents.fail["tester"] = RunnerOutcome("failure", 1, "boom", {})
    result = work(with_independent, per_task(agents))

    assert actions(result) == [("run", "T1", "needs_review")]
    assert "T3" not in list_tasks(with_independent["runs"], RUN_ID)


def test_independent_tasks_run_in_plan_order(with_independent, agents):
    result = work(with_independent, per_task(agents))
    assert actions(result) == [("run", "T1", "done"), ("run", "T2", "done"), ("run", "T3", "done")]


# --- picking up where it stopped ---------------------------------------------------------------


def test_an_interrupted_task_is_continued_before_anything_new(world, agents):
    invoke = per_task(agents)
    real_tester = invoke.behaviours["T1"]["tester"]
    invoke.behaviours["T1"]["tester"] = stopping()
    with pytest.raises(Stop):  # like a closed window while T1 was testing
        work(world, invoke)
    invoke.behaviours["T1"]["tester"] = real_tester
    agents.calls.clear()

    result = work(world, invoke)

    assert actions(result) == [("continue", "T1", "done"), ("run", "T2", "done")]
    assert roles_of(agents, "T1") == ["tester", "reviewer"]  # the builder's work was kept
    assert result.complete


def test_the_lock_is_released_when_a_task_escapes(world, agents):
    invoke = per_task(agents)
    invoke.behaviours["T1"]["builder"] = stopping()
    with pytest.raises(Stop):
        work(world, invoke)
    assert not rp.lock_path(world["runs"], RUN_ID).exists()


def test_a_cancelled_task_stops_the_plan_and_says_how_to_go_on(world, agents):
    run(world, agents, invoke_runner=per_task(agents))  # T1 done
    save_task(world["runs"], RUN_ID, "T2", {"status": "building", "pid": 4242})
    update_task(world["runs"], RUN_ID, "T2", {"status": "cancelled"})
    agents.calls.clear()

    result = work(world, per_task(agents))

    assert result.steps == [] and agents.calls == []
    assert "cancelled" in stopped(result) and f"run-task {RUN_ID} T2 --continue" in stopped(result)


def test_a_task_running_elsewhere_stops_the_plan(world, agents):
    save_task(world["runs"], RUN_ID, "T1", {"status": "building", "pid": 4242})

    def lines(pid):
        return f"python -m orchestrator run-task {RUN_ID} T1" if pid == 4242 else None

    result = work(world, per_task(agents), get_command_line=lines)
    assert result.steps == [] and agents.calls == []
    assert "T1 is running in another process" in stopped(result)


# --- the run budget ------------------------------------------------------------------------------


def test_a_budget_too_small_for_the_first_agent_stops_the_plan_before_anything(world, agents):
    set_budget(world["runs"], RUN_ID, 0.10)
    result = work(world, per_task(agents))

    assert result.refused and result.steps == [] and agents.calls == []
    assert stopped(result).startswith("T1 was not started: Run budget:")
    assert list_tasks(world["runs"], RUN_ID) == {}


def test_a_budget_stop_part_way_stops_the_whole_plan_and_it_goes_on_once_raised(world, agents):
    set_budget(world["runs"], RUN_ID, 0.75)  # the builder (cap 0.60) fits; then 0.55 + 0.25 does not
    invoke = per_task(agents, inner=costly(agents, {"builder": 0.55}))
    result = work(world, invoke)

    assert actions(result) == [("run", "T1", "needs_review")]
    assert result.steps[0].budget_stop
    assert "stopped by the run budget" in stopped(result) and "Run budget:" in stopped(result)
    assert "T2" not in list_tasks(world["runs"], RUN_ID)

    set_budget(world["runs"], RUN_ID, 5)
    agents.calls.clear()
    again = work(world, invoke)

    assert actions(again) == [("continue", "T1", "done"), ("run", "T2", "done")]
    assert roles_of(agents, "T1") == ["tester", "reviewer"]


# --- one run-plan per run --------------------------------------------------------------------


def test_a_second_run_plan_on_the_same_run_is_refused(world, agents):
    path = rp.lock_path(world["runs"], RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4242}))

    def lines(pid):
        return f"python -m orchestrator run-plan {RUN_ID}" if pid == 4242 else None

    with pytest.raises(rp.PlanAlreadyRunning, match="process 4242"):
        work(world, per_task(agents), get_command_line=lines)
    assert agents.calls == []
    assert json.loads(path.read_text())["pid"] == 4242  # the live one's lock is left alone


def test_a_lock_left_by_a_run_plan_that_has_gone_is_taken_over(world, agents):
    path = rp.lock_path(world["runs"], RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4242}))

    result = work(world, per_task(agents))  # process 4242 has gone
    assert result.complete
    assert not path.exists()


def test_a_lock_whose_process_id_is_now_another_program_is_taken_over(world, agents):
    path = rp.lock_path(world["runs"], RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4242}))
    result = work(world, per_task(agents), get_command_line=lambda pid: "notepad.exe")
    assert result.complete


def test_a_damaged_lock_is_taken_over(world, agents):
    path = rp.lock_path(world["runs"], RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert work(world, per_task(agents)).complete


# --- which process runs a task ------------------------------------------------------------------


@pytest.mark.parametrize("line, task_id, expected", [
    ("python -m orchestrator run-task run1 T1", "T1", True),
    ("python -m orchestrator run-task run1 T2", "T1", False),
    ("python -m orchestrator run-plan run1", "T1", True),   # run-plan runs every task of its run
    ("python -m orchestrator run-plan run1", "T7", True),
    ("python -m orchestrator run-plan run2", "T1", False),
    ("python -m orchestrator status run1 T1", "T1", False),
    ("notepad run-plan run1", "T1", False),
])
def test_a_task_runs_in_its_run_task_or_its_runs_run_plan(line, task_id, expected):
    assert is_task_process(line, "run1", task_id) is expected


def test_only_the_runs_own_run_plan_holds_its_lock():
    assert is_plan_process("C:\\py\\python.exe -m orchestrator run-plan run1", "run1")
    assert not is_plan_process("python -m orchestrator run-plan run2", "run1")
    assert not is_plan_process("python -m orchestrator run-task run1 T1", "run1")


# --- what to do next, and showing it ---------------------------------------------------------------


def test_format_step():
    assert rp.format_step("run", "T1", None) == "Starting T1..."
    assert rp.format_step("retry", "T1", None).startswith("Retrying T1 (it failed")
    done = rp.Step("T1", "run", "done", 1, 0.4)
    assert rp.format_step("run", "T1", done) == "T1: done, $0.4000"
    failed = rp.Step("T1", "retry", "failed", 2, 0.5, "The full test suite fails.")
    assert rp.format_step("retry", "T1", failed) == "T1 (attempt 2): failed, $0.5000: The full test suite fails."


def test_the_cli_offers_run_plan_and_run_task_retry():
    import orchestrator.cli.commands as cli
    parser = cli.build_parser()
    assert parser.parse_args(["run-plan", "r"]).command == "run-plan"
    assert parser.parse_args(["run-task", "r", "T1", "--retry"]).retry_task is True
