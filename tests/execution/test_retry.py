"""M4.4: retry a failed task once, with the findings."""

import json
from pathlib import Path

import pytest

from orchestrator.execution import checks as tc
from orchestrator.execution import task_runner as tr
from orchestrator.errors import ExecutionRefused
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.records.task_registry import load_task
from task_runner_support import (
    RUN_ID,
    orchestrator_steps,
    retry,
    run,
    task_folder,
    wrong_double,
)


def test_a_task_that_fails_once_then_passes(world, agents):
    agents.behaviour["builder"] = wrong_double
    first = run(world, agents)
    assert first["status"] == "failed"
    agents.behaviour["builder"] = agents.build_double
    agents.calls.clear()

    second = retry(world, agents)
    assert second["status"] == "done", second["reasons"]
    assert second["attempt"] == 2 and second["commit"]
    # A fresh worktree and branch, from the same head the first attempt started from.
    assert second["base_commit"] == first["base_commit"] == world["base"]
    assert second["branch"] == first["branch"] + "-r2"
    assert second["worktree_path"] != first["worktree_path"]
    assert Path(first["worktree_path"]).is_dir()  # the first attempt's worktree is kept
    # Its own agents, so the first attempt's runner folders stay as they were.
    assert [c["run_id"] for c in agents.calls] == [
        f"{RUN_ID}-T1-r2-builder", f"{RUN_ID}-T1-r2-tester", f"{RUN_ID}-T1-r2-reviewer"]
    # The builder was told what went wrong.
    prompt = agents.prompts["builder"]
    assert "Problems found by an earlier attempt" in prompt
    assert first["reasons"][0][:60] in prompt
    assert "clean checkout" in prompt


def test_each_attempt_keeps_its_own_record_and_cost(world, agents):
    agents.behaviour["builder"] = wrong_double
    first = run(world, agents)
    agents.behaviour["builder"] = agents.build_double
    second = retry(world, agents)
    archived = json.loads((task_folder(world) / "attempt-1" / "task.json").read_text())
    assert archived["status"] == "failed" and archived["cost_usd"] == first["cost_usd"]
    assert (task_folder(world) / "attempt-1" / "events.jsonl").is_file()
    assert second["previous_attempts"] == [{
        "attempt": 1, "status": "failed", "reasons": first["reasons"],
        "cost_usd": first["cost_usd"], "branch": first["branch"],
        "worktree_path": first["worktree_path"], "base_commit": first["base_commit"],
        "finished_at": first["updated_at"]}]
    assert second["cost_usd"] == pytest.approx(0.3)  # this attempt's own three agents
    steps = [e["step"] for e in orchestrator_steps(world)]
    assert steps[0] == "task_started" and steps.count("task_started") == 1  # a fresh log


def test_a_task_that_fails_twice_stays_failed(world, agents):
    from orchestrator.errors import ExecutionRefused as Refused
    agents.behaviour["builder"] = wrong_double
    run(world, agents)
    second = retry(world, agents)
    assert second["status"] == "failed" and second["attempt"] == 2 and second["commit"] is None
    with pytest.raises(Refused, match="already been retried"):
        retry(world, agents)
    assert load_task(world["runs"], RUN_ID, "T1")["status"] == "failed"
    # Its dependent is blocked, as for any failed task.
    with pytest.raises(tr.TaskNotReady, match="T1 is failed, not done"):
        run(world, agents, task_id="T2")


@pytest.mark.parametrize("make,expected", [
    ("done", "only a failed task is retried"),
    ("needs_review", "A person should look"),
    ("blocked", "cannot be done .no API"),
    ("no_worktree", "failed before it had a worktree"),
])
def test_tasks_that_are_not_retried(world, agents, make, expected):
    if make == "needs_review":
        agents.fail["builder"] = RunnerOutcome("failure", 1, "Claude exited with code 1", {})
    elif make == "blocked":
        agents.behaviour["builder"] = lambda cwd: json.dumps({"blocked": True, "blocker": "no API"})
    record = run(world, agents, worktree_root=world["repo"] / "inside") if make == "no_worktree" \
        else run(world, agents)
    calls = len(agents.calls)
    with pytest.raises(ExecutionRefused, match=expected):
        retry(world, agents)
    assert len(agents.calls) == calls  # nothing was paid for
    assert load_task(world["runs"], RUN_ID, "T1") == record  # and nothing moved
    assert not (task_folder(world) / "attempt-1").exists()


def test_a_task_that_never_ran_cannot_be_retried(world, agents):
    from orchestrator.records.run_registry import RunRegistryError
    with pytest.raises(RunRegistryError):
        retry(world, agents)


def test_a_refused_retry_leaves_the_failed_attempt_in_place(world, agents, monkeypatch):
    agents.behaviour["builder"] = wrong_double
    record = run(world, agents)
    monkeypatch.setattr(tc, "pytest_problem", lambda *args, **kwargs: "No module named pytest")
    with pytest.raises(ExecutionRefused):
        retry(world, agents, test_command=None)
    assert load_task(world["runs"], RUN_ID, "T1") == record
    assert not (task_folder(world) / "attempt-1").exists()


def test_a_retry_that_cannot_start_puts_the_first_attempt_back(world, agents, monkeypatch):
    agents.behaviour["builder"] = wrong_double
    record = run(world, agents)

    def explode(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(tr, "run_task", explode)
    with pytest.raises(OSError):
        retry(world, agents)
    assert load_task(world["runs"], RUN_ID, "T1") == record
    assert (task_folder(world) / "events.jsonl").is_file()
    assert not (task_folder(world) / "attempt-1").exists()


def test_the_feedback_names_the_failures_and_hides_secrets():
    from orchestrator.execution import retry as rt
    record = {
        "task_id": "T1", "status": "failed",
        "reasons": ["The full test suite failed.", "Reviewer: double(-1) returns 1"],
        "checks": {
            "suite": {"exit_code": 1, "timed_out": False,
                      "output_tail": "FAILED tests/test_double.py::test_negative\n"
                                     "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
                                     "1 failed, 4 passed"},
            "named_tests": {"exit_code": 0, "timed_out": False, "output_tail": "2 passed"},
        },
    }
    text = rt.retry_feedback(record)
    assert text.startswith("Attempt 1 of this task")
    assert "- The full test suite failed." in text and "- Reviewer: double(-1) returns 1" in text
    assert "The end of the full test suite output:" in text and "1 failed, 4 passed" in text
    assert "tests the tester named" not in text  # it passed, so it is not repeated
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in text


def test_the_feedback_says_when_the_tests_timed_out_and_cuts_long_text():
    from orchestrator.execution import retry as rt
    record = {"task_id": "T1", "status": "failed", "reasons": ["x" * 2000] * 20,
              "checks": {"suite": {"exit_code": None, "timed_out": True, "output_tail": ""}}}
    text = rt.retry_feedback(record)
    assert "(it timed out)" in text and "(no output)" in text
    assert "... and 8 more" in text
    assert max(len(line) for line in text.splitlines()) <= rt.REASON_CHARS + 2


def test_agent_ids_and_the_status_follow_the_attempt(world, agents, monkeypatch):
    from orchestrator.monitoring import progress
    assert progress.agent_run_id("r", "T1", "builder") == "r-T1-builder"
    assert progress.agent_run_id("r", "T1", "builder", 2) == "r-T1-r2-builder"
    assert progress.attempt_of({}) == 1 and progress.attempt_of({"attempt": 2}) == 2
    agents.behaviour["builder"] = wrong_double
    run(world, agents)
    second = retry(world, agents)
    from orchestrator.execution.runner_adapter import runner_run_directory
    folder = runner_run_directory(f"{RUN_ID}-T1-r2-builder", world["script"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "progress.json").write_text(json.dumps(
        {"state": "finished", "status": "succeeded", "turns": 4, "total_cost_usd": 0.5}))
    from datetime import datetime, timezone
    view = progress.task_agent_view(RUN_ID, "T1", "builder", second, world["script"],
                                    datetime.now(timezone.utc))
    assert view.turns == 4 and view.cost_usd == 0.5  # attempt 2's builder, not attempt 1's


def test_show_task_lists_the_earlier_attempt(world, agents, capsys):
    from orchestrator.cli.display import display_task
    agents.behaviour["builder"] = wrong_double
    run(world, agents)
    agents.behaviour["builder"] = agents.build_double
    display_task(retry(world, agents))
    out = capsys.readouterr().out
    assert "Attempt 1: failed, $" in out
    assert "This is attempt 2 (a retry, told what attempt 1 got wrong)." in out
