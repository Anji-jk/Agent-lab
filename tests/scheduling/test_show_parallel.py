"""Status, the report and the dashboard show several running tasks and the merges."""

import json
from datetime import datetime, timedelta, timezone

import orchestrator.cli.commands as cli
from orchestrator.execution import integration as ig
from orchestrator.execution import task_runner as tr
from orchestrator.monitoring import dashboard as dash
from orchestrator.records.task_registry import TASK_RECORD_NAME, load_task, task_directory
from orchestrator.scheduling import plan_progress as pp
from orchestrator.scheduling import run_report as rr
from run_plan_support import no_process, per_task, work
from task_runner_support import RUN_ID, run  # pyright: ignore[reportMissingImports]

# --- how a merge is told ------------------------------------------------------------------------


def test_each_kind_of_merge_has_its_own_line():
    assert str(ig.describe_merge({"kind": "fast-forward"})).startswith("merged: fast-forward")
    assert ig.describe_merge({"kind": "merge", "suite": {"seconds": 640}}) == (
        "merged: real merge, the full suite passed on the merged result in 11 min")
    assert ig.describe_merge({"kind": "already"}) == "merged: its commit was already on the run's branch"
    assert ig.describe_merge({"problem": "conflict", "files": ["a.py", "b.py"]}) == (
        "not merged: conflicts with work merged before it (a.py, b.py)")
    assert "did not finish" in str(ig.describe_merge({"problem": "suite", "suite": {"timed_out": True}}))
    assert "failed on the merged result" in str(ig.describe_merge({"problem": "suite", "suite": {}}))
    assert ig.describe_merge({"problem": "error"}) == "not merged: the merge itself failed"
    assert ig.describe_merge(None) is None and ig.describe_merge({}) is None


def test_the_branch_line_names_the_branch_its_head_and_every_merge():
    data = {"branch": "agent/issue-10-r", "head": "a" * 40, "base_commit": "b" * 40,
            "merges": [{"task": "T1", "kind": "fast-forward"}, {"task": "T3", "kind": "merge"}]}
    assert ig.branch_line(data) == ("Run branch: agent/issue-10-r at aaaaaaaaaaaa · merges: "
                                    "T1 fast-forward, T3 real merge")
    assert str(ig.branch_line({"branch": "b", "base_commit": "c" * 40, "merges": []})).endswith(
        "at cccccccccccc · merges: none yet")
    assert ig.branch_line(None) is None


# --- which tasks ran together ----------------------------------------------------------------


def at(minute):
    return datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)


def test_tasks_whose_times_overlap_ran_together():
    spans = {"T1": [(at(0), at(10))], "T2": [(at(20), at(30))], "T3": [(at(5), at(15))]}
    assert rr.ran_together(spans) == [["T1", "T3"]]


def test_a_chain_of_overlaps_is_one_group_and_one_after_the_other_is_none():
    chain = {"T1": [(at(0), at(10))], "T2": [(at(9), at(20))], "T3": [(at(19), at(30))]}
    assert rr.ran_together(chain) == [["T1", "T2", "T3"]]
    apart = {"T1": [(at(0), at(10))], "T2": [(at(10), at(20))]}  # one ends as the next starts
    assert rr.ran_together(apart) == []


def test_any_attempt_counts():
    spans = {"T1": [(at(0), at(5)), (at(40), at(50))], "T2": [(at(45), at(55))]}
    assert rr.ran_together(spans) == [["T1", "T2"]]


# --- status --------------------------------------------------------------------------------------


def shape(*tasks):
    return {"tasks": [{"id": t, "title": t, "depends_on": []} for t in tasks]}


def alive(*ids):
    return lambda task_id, record: (task_id in ids, "its process has gone")


def test_a_running_task_that_is_merging_says_so():
    records = {"T1": {"status": "reviewing", "merging": "2026-10-28T10:00:00+00:00"},
               "T2": {"status": "reviewing"}}
    progress = pp.compute_progress(RUN_ID, shape("T1", "T2"), records, alive("T1", "T2"))
    text = pp.format_plan_progress(progress)
    assert "T1   running      reviewing, merging into the run's branch" in text
    assert "T2   running      reviewing\n" in text + "\n"


def test_an_interrupted_merge_is_not_shown_as_merging():
    records = {"T1": {"status": "reviewing", "merging": "2026-10-28T10:00:00+00:00"}}
    progress = pp.compute_progress(RUN_ID, shape("T1"), records, alive())
    assert progress.task("T1").state == "interrupted" and not progress.task("T1").merging


def test_a_ready_task_says_why_it_waits_while_others_run():
    records = {"T1": {"status": "building"}}
    progress = pp.compute_progress(RUN_ID, shape("T1", "T2"), records, alive("T1"))
    both_safe = pp.format_plan_progress(progress, safe={"T1": True, "T2": True})
    assert "T2   ready        can start when one of the running tasks ends" in both_safe
    it_alone = pp.format_plan_progress(progress, safe={"T1": True, "T2": False})
    assert "T2   ready        can start once T1 has ended (it runs by itself)" in it_alone
    other_alone = pp.format_plan_progress(progress, safe={"T1": False, "T2": True})
    assert "can start once T1 has ended (T1 runs by itself)" in other_alone


def test_a_ready_task_with_nothing_running_can_start_now():
    progress = pp.compute_progress(RUN_ID, shape("T1"), {}, alive())
    assert "T1   ready        can start now" in pp.format_plan_progress(progress, safe={})


def test_status_shows_each_merge_and_the_run_branch(world, agents, capsys):
    work(world, per_task(agents))
    code = cli.main(["status", RUN_ID, "--runs-dir", str(world["runs"]),
                     "--plans-dir", str(world["plans"])])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert out.count("merged: fast-forward") == 2
    assert f"Run branch: agent/issue-7-{RUN_ID} at " in out
    assert "merges: T1 fast-forward, T2 fast-forward" in out


# --- the task records the merge as it happens ---------------------------------------------------


def test_the_record_says_merging_while_the_merge_runs_and_not_after(world, agents, monkeypatch):
    seen = []
    real = tr.merge_task

    def watch(*args, **kwargs):
        seen.append(load_task(world["runs"], RUN_ID, "T1").get("merging"))
        return real(*args, **kwargs)

    monkeypatch.setattr(tr, "merge_task", watch)
    record = run(world, agents)
    assert record["status"] == "done"
    assert len(seen) == 1 and seen[0]  # a time
    assert load_task(world["runs"], RUN_ID, "T1")["merging"] is None


# --- the report ------------------------------------------------------------------------------------


def rewrite(world, task_id, **fields):
    path = task_directory(world["runs"], RUN_ID, task_id) / TASK_RECORD_NAME
    record = json.loads(path.read_text())
    record.update(fields)
    path.write_text(json.dumps(record))


def report(world):
    return rr.build_report(RUN_ID, plans_dir=world["plans"], runs_dir=world["runs"],
                           runner_script=world["script"], get_command_line=no_process)


def test_the_report_shows_who_ran_together_and_each_merge(world, with_independent, agents):
    work(world, per_task(agents))
    # As if T1 and T3 had run side by side and T3's merge was a real one.
    rewrite(world, "T1", created_at=at(0).isoformat(), updated_at=at(10).isoformat())
    rewrite(world, "T3", created_at=at(2).isoformat(), updated_at=at(12).isoformat(),
            merge={"branch": "b", "kind": "merge", "head": "h", "suite": {"seconds": 30}})
    rewrite(world, "T2", created_at=at(13).isoformat(), updated_at=at(20).isoformat())

    result = report(world)
    shown = rr.format_report(result)
    assert result.together == [["T1", "T3"]]
    assert "Together: T1, T3 ran at the same time" in shown
    assert "Merges:  T1 fast-forward, T2 fast-forward, T3 fast-forward" in shown  # integration.json
    tasks = {t.task_id: t for t in result.tasks}
    assert tasks["T3"].merge == "merged: real merge, the full suite passed on the merged result in 30 s"
    assert "       merged: real merge" in shown
    assert "Nothing was pushed, and your own branches were not changed" in shown


# --- the dashboard -----------------------------------------------------------------------------------


def test_the_dashboard_shows_the_run_branch_the_merges_and_who_is_merging(world, agents):
    work(world, per_task(agents))
    rewrite(world, "T2", status="reviewing", merging="2026-10-28T10:00:00+00:00", pid=4242)
    data = dash.DashboardData(world["runs"], world["plans"], runner_script=world["script"],
                              get_command_line=lambda pid: f"python -m orchestrator run-task {RUN_ID} T2"
                              if pid == 4242 else None)
    shown = data.run(RUN_ID)

    branch = shown["integration"]
    assert branch["branch"] == f"agent/issue-7-{RUN_ID}"
    assert [m["task"] for m in branch["merges"]] == ["T1", "T2"]
    assert branch["line"].startswith("Run branch: ")
    tasks = {t["id"]: t for t in shown["tasks"]}
    assert tasks["T1"]["merge_text"].startswith("merged: fast-forward")
    assert tasks["T1"]["merge"]["kind"] == "fast-forward" and not tasks["T1"]["merging"]
    assert tasks["T2"]["merging"] and tasks["T2"]["active_role"] is None  # the reviewer is done
    assert shown["running"] == ["T2"] and shown["active"]
