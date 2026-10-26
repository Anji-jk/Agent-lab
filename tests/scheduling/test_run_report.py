"""M4.8: one summary of a run: branch, cost, time, and each task's result."""

import os

import orchestrator.cli.commands as cli
from orchestrator.execution.runner_adapter import RunnerOutcome, runner_run_directory
from orchestrator.records.run_registry import write_json_atomic
from orchestrator.records.task_registry import list_tasks, update_task
from orchestrator.scheduling import run_plan as rp
from orchestrator.scheduling import run_report as rr
from task_runner_support import RUN_ID, Stop, stopping, wrong_double  # pyright: ignore[reportMissingImports]
from run_plan_support import no_process, per_task, work


def report(world) -> rr.RunReport:
    return rr.build_report(RUN_ID, plans_dir=world["plans"], runs_dir=world["runs"],
                           runner_script=world["script"], get_command_line=no_process)


def text(world) -> str:
    return rr.format_report(report(world))


def by_id(result: rr.RunReport):
    return {t.task_id: t for t in result.tasks}


# --- a finished run ----------------------------------------------------------------------------


def test_a_complete_run_names_its_branch_and_the_last_commit(world, agents):
    work(world, per_task(agents))
    records = list_tasks(world["runs"], RUN_ID)
    result = report(world)

    assert result.plan_state == "complete"
    # The run's own branch (M7.1) holds both tasks; one at a time, each merge was a
    # fast-forward, so it ends at T2's commit.
    assert result.branch == f"agent/issue-7-{RUN_ID}"
    assert result.head_commit == records["T2"]["commit"]
    shown = rr.format_report(result)
    assert f"Branch:  {result.branch} at {records['T2']['commit'][:12]}" in shown
    assert f"git log --oneline {world['base'][:12]}..{result.branch}" in shown
    assert "Nothing was pushed or merged" in shown


def test_the_cost_is_every_agent_of_every_task(world, agents):
    work(world, per_task(agents))  # the fake agents cost $0.10 each, three per task
    result = report(world)
    assert by_id(result)["T1"].cost_usd == 0.3 and by_id(result)["T2"].cost_usd == 0.3
    assert result.tasks_cost_usd == 0.6
    assert result.planner_cost_usd is None  # no planner progress file in this test
    assert "Cost:    $0.6000 (planner not recorded, tasks $0.6000)" in rr.format_report(result)


def test_the_planners_cost_is_added_when_recorded(world, agents):
    planner_folder = runner_run_directory(f"{RUN_ID}-plan")
    planner_folder.mkdir(parents=True)
    write_json_atomic(planner_folder / "progress.json",
                      {"state": "finished", "status": "succeeded", "total_cost_usd": 0.1027})
    work(world, per_task(agents))
    result = report(world)
    assert result.planner_cost_usd == 0.1027
    assert result.total_cost_usd == 0.7027
    assert "Cost:    $0.7027 (planner $0.1027, tasks $0.6000)" in rr.format_report(result)


def test_each_task_line_shows_status_attempts_cost_and_commit(world, agents):
    work(world, per_task(agents))
    records = list_tasks(world["runs"], RUN_ID)
    lines = text(world).splitlines()
    t1 = next(line for line in lines if line.startswith("T1 "))
    assert "done" in t1 and "1 attempt " in t1 and "$0.3000" in t1
    assert records["T1"]["commit"][:12] in t1 and t1.endswith("double")


def test_time_is_counted_from_the_records(world, agents):
    work(world, per_task(agents))
    result = report(world)
    assert result.started_at and result.finished_at
    assert result.started_at <= result.finished_at
    assert all(t.seconds is not None and t.seconds >= 0 for t in result.tasks)
    assert "Time:    " in rr.format_report(result)


# --- retries, stops and runs that have not started ---------------------------------------------


def test_a_retried_task_shows_both_attempts_and_their_total(world, agents):
    invoke = per_task(agents)
    builds = []

    def wrong_then_right(cwd):
        builds.append(cwd)
        return (wrong_double if len(builds) == 1 else agents.build_double)(cwd)

    invoke.behaviours["T1"]["builder"] = wrong_then_right
    work(world, invoke)
    t1 = by_id(report(world))["T1"]

    assert [(a.attempt, a.status) for a in t1.attempts] == [(1, "failed"), (2, "done")]
    # Attempt 1 failed its tests, so no reviewer ran: builder and tester, $0.20.
    assert [a.cost_usd for a in t1.attempts] == [0.2, 0.3]
    assert t1.cost_usd == 0.5  # both attempts
    assert all(a.seconds is not None for a in t1.attempts)  # attempt 1's from attempt-1/task.json
    shown = text(world)
    assert "2 attempts" in shown
    assert "attempt 1: failed, $0.2000" in shown and "attempt 2: done, $0.3000" in shown


def test_a_stopped_run_says_why_and_what_is_blocked(world, agents):
    agents.fail["tester"] = RunnerOutcome("failure", 1, "boom", {})
    work(world, per_task(agents))
    result = report(world)
    tasks = by_id(result)

    assert result.plan_state == "stopped" and result.branch is None
    assert tasks["T1"].state == "needs_review" and "boom" in (tasks["T1"].detail or "")
    assert tasks["T2"].detail == "blocked by T1 (needs_review)"
    assert "needs review" in result.next_step and "show-task" in result.next_step
    assert "Branch:  none yet; no task has finished" in rr.format_report(result)


def test_an_interrupted_task_counts_what_its_agents_spent(world, agents):
    invoke = per_task(agents)
    invoke.behaviours["T1"]["tester"] = stopping()
    try:
        work(world, invoke)
    except Stop:
        pass
    t1 = by_id(report(world))["T1"]
    assert t1.state == "interrupted" and "stopped while testing" in (t1.detail or "")
    assert t1.cost_usd == 0.1  # the builder ran; the tester was stopped before it reported
    assert report(world).next_step == f"python -m orchestrator run-plan {RUN_ID} would continue T1 next."


def test_a_run_that_has_not_started(world):
    result = report(world)
    shown = rr.format_report(result)
    assert result.plan_state == "ready" and result.total_cost_usd == 0
    assert "not run" in shown and "Time:" not in shown
    assert result.next_step == f"python -m orchestrator run-plan {RUN_ID} would start T1 next."


def test_secrets_in_reasons_are_masked(world, agents):
    agents.fail["tester"] = RunnerOutcome("failure", 1, "boom", {})
    work(world, per_task(agents))
    secret = "ghp_" + "a" * 30
    update_task(world["runs"], RUN_ID, "T1", {"reasons": [f"the log said token {secret}"]})
    shown = text(world)
    assert secret not in shown and "[masked]" in shown


# --- saving it, and the commands -----------------------------------------------------------------


def test_save_report_writes_the_file_whole(world):
    path = rr.save_report(world["runs"], RUN_ID, "# report\n")
    assert path == world["runs"] / RUN_ID / "report.md"
    assert path.read_text(encoding="utf-8") == "# report\n"
    assert not list(path.parent.glob("*.partial"))


def test_the_report_command_prints_and_saves_only_when_asked(world, capsys):
    args = ["report", RUN_ID, "--plans-dir", str(world["plans"]), "--runs-dir", str(world["runs"])]
    assert cli.main(args) == cli.EXIT_OK
    assert f"# Run report: {RUN_ID}" in capsys.readouterr().out
    assert not rr.report_path(world["runs"], RUN_ID).exists()  # read-only by default

    assert cli.main([*args, "--save"]) == cli.EXIT_OK
    assert rr.report_path(world["runs"], RUN_ID).exists()


def test_run_plan_saves_the_report_when_it_stops(world, agents, monkeypatch, capsys):
    monkeypatch.setattr(cli, "verify_remote_repository", lambda *a: None)
    real = rp.run_plan

    def fake_agents_run_plan(run_id, **options):
        return real(run_id, **{**options, "runner_script": world["script"],
                               "invoke_runner": per_task(agents), "get_command_line": no_process,
                               "env": {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}})

    monkeypatch.setattr(cli, "work_through_plan", fake_agents_run_plan)
    code = cli.main(["run-plan", RUN_ID, "--plans-dir", str(world["plans"]),
                     "--runs-dir", str(world["runs"]), "--repo-path", str(world["repo"]),
                     "--worktree-root", str(world["wt"])])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "Starting T1..." in out and "# Run report:" in out and "complete, 2 of 2 tasks done" in out
    saved = rr.report_path(world["runs"], RUN_ID).read_text(encoding="utf-8")
    assert "## Tasks" in saved and "complete, 2 of 2 tasks done" in saved
