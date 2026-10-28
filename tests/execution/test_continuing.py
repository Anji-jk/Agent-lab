"""M4.5: continue a stopped task from its stage."""

from pathlib import Path

import pytest

from orchestrator.execution import task_runner as tr
from orchestrator.errors import ExecutionRefused
from orchestrator.records.task_registry import load_task
from task_runner_support import (
    GOOD_DOUBLE,
    RUN_ID,
    git,
    gone,
    orchestrator_steps,
    resume,
    retry,
    run,
    stop_at,
    task_folder,
    use_note,
    wrong_double,
)


def test_a_task_interrupted_while_testing_continues_with_a_new_tester(world, agents):
    stopped = stop_at(world, agents, "tester")
    assert stopped["status"] == "testing" and stopped["checkpoint"]["stage"] == "testing"
    record = resume(world, agents)
    assert record["status"] == "done", record["reasons"]
    assert agents.roles == ["tester", "reviewer"]  # the builder's checked result was kept
    assert [c["run_id"] for c in agents.calls] == [f"{RUN_ID}-T1-r2-tester", f"{RUN_ID}-T1-r2-reviewer"]
    # Same worktree and branch; the commit holds the first builder's change.
    assert record["branch"] == stopped["branch"] and record["worktree_path"] == stopped["worktree_path"]
    assert record["attempt"] == 2 and record["continued_at"] == "testing"
    assert [h["status"] for h in record["history"]] == ["testing", "reviewing", "done"]
    assert set(record["reused_agents"]) == {"builder"} and set(record["agents"]) == {"tester", "reviewer"}
    assert "def double" in git(Path(record["worktree_path"]), "show", "HEAD:calc.py")
    assert record["previous_attempts"][0]["status"] == "interrupted"


def test_a_cancelled_task_continues_too(world, agents):
    from orchestrator.records.task_registry import update_task
    stop_at(world, agents, "tester")
    update_task(world["runs"], RUN_ID, "T1", {"status": "cancelled", "reasons": ["Cancelled by you."]})
    record = resume(world, agents)
    assert record["status"] == "done" and agents.roles == ["tester", "reviewer"]
    assert record["previous_attempts"][0]["status"] == "cancelled"
    assert record["previous_attempts"][0]["reasons"] == ["Cancelled by you."]


def test_a_task_stopped_while_reviewing_only_needs_a_new_reviewer(world, agents):
    stopped = stop_at(world, agents, "reviewer")
    assert stopped["checkpoint"]["stage"] == "reviewing"
    record = resume(world, agents)
    assert record["status"] == "done" and agents.roles == ["reviewer"]
    assert set(record["reused_agents"]) == {"builder", "tester"}
    steps = [e["step"] for e in orchestrator_steps(world)]
    assert "suite_started" not in steps  # the tests' results were kept, not run again


def test_files_changed_after_the_tests_ran_mean_the_tests_run_again(world, agents):
    stopped = stop_at(world, agents, "reviewer")
    Path(stopped["worktree_path"], "tests", "test_extra.py").write_text("def test_x():\n    pass\n")
    record = resume(world, agents)
    assert agents.roles == ["tester", "reviewer"] and record["continued_at"] == "testing"


def test_a_task_stopped_while_building_gets_a_builder_told_what_is_there(world, agents):
    stopped = stop_at(world, agents, "builder", work=lambda cwd: (cwd / "calc.py").write_text(
        (cwd / "calc.py").read_text() + "\n" + GOOD_DOUBLE))
    assert "checkpoint" not in stopped
    record = resume(world, agents)
    assert agents.roles == ["builder", "tester", "reviewer"] and record["continued_at"] == "building"
    prompt = agents.prompts["builder"]
    assert "About this worktree" in prompt and "changed files: calc.py" in prompt
    assert "Problems found by an earlier attempt" not in prompt  # not a retry


def test_a_record_from_before_checkpoints_continues_from_the_builder(world, agents):
    from orchestrator.records.task_registry import update_task
    stop_at(world, agents, "tester")
    update_task(world["runs"], RUN_ID, "T1", {"checkpoint": None})
    record = resume(world, agents)
    assert agents.roles == ["builder", "tester", "reviewer"] and record["status"] == "done"


@pytest.mark.parametrize("setup,expected", [
    ("running", "still running"),
    ("failed", "failed. Only a cancelled or interrupted task is continued; a failed one is retried"),
    ("done", "done. Only a cancelled or interrupted task"),
    ("no_worktree", "worktree .* no longer exists"),
])
def test_tasks_that_are_not_continued(world, agents, setup, expected):
    import shutil
    if setup in ("failed", "done"):
        if setup == "failed":
            agents.behaviour["builder"] = wrong_double
        run(world, agents)
    else:
        stopped = stop_at(world, agents, "tester")
        if setup == "no_worktree":
            shutil.rmtree(stopped["worktree_path"])
    record = load_task(world["runs"], RUN_ID, "T1")
    alive = (lambda task_id, rec: (True, "process 7 is running it")) if setup == "running" else gone
    calls = len(agents.calls)
    with pytest.raises(ExecutionRefused, match=expected):
        resume(world, agents, is_alive=alive)
    assert len(agents.calls) == calls and load_task(world["runs"], RUN_ID, "T1") == record
    assert not (task_folder(world) / "attempt-1").exists()


def test_a_task_continued_after_the_run_moved_on_is_merged_into_the_run_branch(with_independent, agents):
    """M7.3: with the run's branch, tasks side by side may finish in any order."""
    world = with_independent
    stopped = stop_at(world, agents, "tester")  # T1 stopped; its process is gone
    use_note(agents)
    other = run(world, agents, task_id="T3")  # T3 starts from the plan's base and finishes
    assert other["status"] == "done"
    agents.behaviour = {"builder": agents.build_double, "tester": agents.test_double,
                        "reviewer": agents.review_pass}  # back to T1's own agents
    record = resume(world, agents)

    assert record["status"] == "done", record["reasons"]
    assert record["base_commit"] == stopped["base_commit"]  # the base its worktree was made from
    assert record["merge"]["kind"] == "merge" and record["merge"]["suite"]["exit_code"] == 0
    head = git(world["repo"], "rev-parse", record["merge"]["branch"])
    for commit in (other["commit"], record["commit"]):
        assert git(world["repo"], "merge-base", "--is-ancestor", commit, head) == ""


def test_a_run_without_a_branch_still_refuses_a_task_the_run_has_moved_on_from(with_independent, agents):
    """A run from before M7.1 has one chain of commits; a task from an older base would split it."""
    from orchestrator.execution.integration import integration_path
    world = with_independent
    stop_at(world, agents, "tester")
    use_note(agents)
    run(world, agents, task_id="T3")
    integration_path(world["runs"], RUN_ID).unlink()
    git(world["repo"], "worktree", "remove", "--force", next(
        line.split()[1] for line in git(world["repo"], "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ") and line.endswith(f"issue-7-{RUN_ID}")))
    git(world["repo"], "branch", "-D", f"agent/issue-7-{RUN_ID}")
    with pytest.raises(tr.TaskNotReady, match="moved on"):
        resume(world, agents)
    assert not (task_folder(world) / "attempt-1").exists()


def test_continuing_does_not_use_up_the_retry(world, agents):
    agents.behaviour["builder"] = wrong_double
    stop_at(world, agents, "tester")
    failed = resume(world, agents)  # attempt 2: the wrong double fails the tests
    assert failed["status"] == "failed" and failed["attempt"] == 2
    agents.behaviour["builder"] = agents.build_double
    third = retry(world, agents)
    assert third["status"] == "done" and third["attempt"] == 3
    assert third["branch"].endswith("-r3")
    assert [p["status"] for p in third["previous_attempts"]] == ["interrupted", "failed"]


def test_run_task_continue_cannot_run_in_the_background(world, monkeypatch, capsys):
    import orchestrator.cli.commands as cli
    monkeypatch.setattr(cli, "verify_remote_repository", lambda *args: None)
    code = cli.main(["run-task", RUN_ID, "T1", "--continue", "--background",
                     "--plans-dir", str(world["plans"]), "--runs-dir", str(world["runs"]),
                     "--repo-path", str(world["repo"])])
    assert code == cli.EXIT_USAGE and "without --background" in capsys.readouterr().err


def test_show_task_says_what_was_kept(world, agents, capsys):
    from orchestrator.cli.display import display_task
    stop_at(world, agents, "tester")
    display_task(resume(world, agents))
    out = capsys.readouterr().out
    assert "-> kept from the stopped attempt" in out
    assert "the stopped attempt 1 continued from testing, in the same worktree" in out


def test_show_task_of_a_stopped_task_does_not_say_its_agents_never_ran(world, agents, capsys):
    from orchestrator.cli.display import display_task
    display_task(stop_at(world, agents, "tester"))
    out = capsys.readouterr().out
    assert "-> not run" not in out and "no result recorded (the task was stopped)" in out
