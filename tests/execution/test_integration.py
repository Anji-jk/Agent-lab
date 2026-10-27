"""M7.1: every finished task is merged into the run's own branch."""

import json
import sys

import pytest

from orchestrator.execution import integration as ig
from orchestrator.execution import task_runner as tr
from orchestrator.github.comments import task_comment
from orchestrator.records.task_registry import list_tasks
from task_runner_support import RUN_ID, git, run  # pyright: ignore[reportMissingImports]

BRANCH = f"agent/issue-7-{RUN_ID}"
PASSES = (sys.executable, "-c", "raise SystemExit(0)")
FAILS = (sys.executable, "-c", "raise SystemExit(1)")


def side_commit(world, name, files):
    """A commit on its own branch from the plan's base, like a task's work."""
    repo = world["repo"]
    git(repo, "checkout", "-q", "-b", name, world["base"])
    for path, text in files.items():
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return commit


def merge(world, task_id, commit, command=PASSES):
    return ig.merge_task(RUN_ID, task_id, commit, issue_number=7, base_commit=world["base"],
                         runs_dir=world["runs"], repo_path=world["repo"],
                         worktree_root=world["wt"], test_command=command, test_timeout=60,
                         env=None, title="t")


def branch_head(world):
    return git(world["repo"], "rev-parse", BRANCH)


# --- in a task ---------------------------------------------------------------------------------


def test_a_done_task_is_merged_into_the_run_branch(world, agents):
    record = run(world, agents)

    assert record["status"] == "done"
    assert record["merge"] == {"branch": BRANCH, "kind": "fast-forward", "head": record["commit"]}
    assert branch_head(world) == record["commit"]
    saved = json.loads(ig.integration_path(world["runs"], RUN_ID).read_text())
    assert saved["base_commit"] == world["base"] and saved["merges"][0]["task"] == "T1"
    # The task's own branch is kept, and nothing reaches your own branches.
    assert git(world["repo"], "rev-parse", record["branch"]) == record["commit"]
    assert git(world["repo"], "rev-parse", "main") == world["base"]


def test_the_next_task_starts_from_the_run_branch(world, agents):
    first = run(world, agents)
    agents.use_triple()
    second = run(world, agents, task_id="T2")

    assert second["base_commit"] == first["commit"]
    assert second["merge"]["kind"] == "fast-forward"
    assert branch_head(world) == second["commit"]


def test_a_merge_problem_fails_the_task_so_it_is_retried_from_the_new_head(world, agents, monkeypatch):
    def conflict(*args, **kwargs):
        raise ig.MergeProblem("T1's work conflicts with work merged into the run's branch "
                              "since it started (calc.py). The branch was left as it was.",
                              "conflict", {"files": ["calc.py"]})
    monkeypatch.setattr(tr, "merge_task", conflict)
    record = run(world, agents)

    assert record["status"] == "failed"
    assert "conflicts with work merged into the run's branch" in record["reasons"][0]
    assert record["merge"] == {"problem": "conflict", "files": ["calc.py"]}
    assert record["commit"]  # the work is kept on its own branch


def test_a_merge_that_cannot_be_done_needs_a_person(world, agents, monkeypatch):
    def broken(*args, **kwargs):
        raise ig.MergeProblem("The run's branch has lost its worktree.", "error")
    monkeypatch.setattr(tr, "merge_task", broken)
    record = run(world, agents)
    assert record["status"] == "needs_review" and record["merge"]["problem"] == "error"


def test_a_run_from_before_7_1_starts_its_branch_where_the_next_task_starts(world, agents):
    first = run(world, agents)
    # As if T1 had finished before M7.1: no run branch, only T1's own.
    git(world["repo"], "worktree", "remove", "--force",
        json.loads(ig.integration_path(world["runs"], RUN_ID).read_text())["worktree_path"])
    git(world["repo"], "branch", "-D", BRANCH)
    ig.integration_path(world["runs"], RUN_ID).unlink()

    agents.use_triple()
    second = run(world, agents, task_id="T2")

    assert second["base_commit"] == first["commit"]  # the M4.2 rule, as before
    saved = json.loads(ig.integration_path(world["runs"], RUN_ID).read_text())
    assert saved["base_commit"] == first["commit"]
    assert git(world["repo"], "merge-base", "--is-ancestor", first["commit"], BRANCH) == ""


def test_the_comment_says_where_the_work_went(world, agents):
    record = run(world, agents)
    text = task_comment(RUN_ID, "T1", record)
    assert f"Merged into the run's branch `{BRANCH}` (a fast-forward)." in text


# --- merging -------------------------------------------------------------------------------------


def test_work_from_the_branch_head_is_a_fast_forward_and_runs_nothing(world):
    one = side_commit(world, "one", {"one.py": "A = 1\n"})
    merged = merge(world, "T1", one, command=FAILS)  # a suite run would fail
    assert (merged.kind, merged.head, merged.suite) == ("fast-forward", one, None)


def test_work_from_an_older_head_is_merged_and_the_suite_runs_on_the_result(world):
    one = side_commit(world, "one", {"one.py": "A = 1\n"})
    two = side_commit(world, "two", {"two.py": "B = 2\n"})
    merge(world, "T1", one)
    merged = merge(world, "T3", two)

    assert merged.kind == "merge" and merged.suite["exit_code"] == 0
    head = branch_head(world)
    assert head == merged.head and head not in (one, two)
    for commit in (one, two):
        assert git(world["repo"], "merge-base", "--is-ancestor", commit, head) == ""
    assert git(world["repo"], "log", "-1", "--format=%s", head) == "Merge T3: t into the run's branch"
    assert len(json.loads(ig.integration_path(world["runs"], RUN_ID).read_text())["merges"]) == 2


def test_a_conflict_leaves_the_branch_as_it_was(world):
    one = side_commit(world, "one", {"calc.py": "X = 1\n"})
    two = side_commit(world, "two", {"calc.py": "X = 2\n"})
    merge(world, "T1", one)

    with pytest.raises(ig.MergeProblem) as caught:
        merge(world, "T3", two)

    assert caught.value.kind == "conflict" and caught.value.detail["files"] == ["calc.py"]
    assert "(calc.py)" in caught.value.reason
    assert branch_head(world) == one
    worktree = json.loads(ig.integration_path(world["runs"], RUN_ID).read_text())["worktree_path"]
    assert git(worktree, "status", "--porcelain") == ""


def test_a_merged_result_that_fails_its_suite_is_undone(world):
    one = side_commit(world, "one", {"one.py": "A = 1\n"})
    two = side_commit(world, "two", {"two.py": "B = 2\n"})
    merge(world, "T1", one)

    with pytest.raises(ig.MergeProblem) as caught:
        merge(world, "T3", two, command=FAILS)

    assert caught.value.kind == "suite"
    assert "merged with the work already on the run's branch the full test suite fails" in caught.value.reason
    assert branch_head(world) == one


def test_a_commit_already_on_the_branch_is_not_merged_again(world):
    one = side_commit(world, "one", {"one.py": "A = 1\n"})
    merge(world, "T1", one)
    assert merge(world, "T1", one).kind == "already"


def test_changes_left_in_the_branch_worktree_stop_the_merge(world):
    one = side_commit(world, "one", {"one.py": "A = 1\n"})
    two = side_commit(world, "two", {"two.py": "B = 2\n"})
    merge(world, "T1", one)
    worktree = json.loads(ig.integration_path(world["runs"], RUN_ID).read_text())["worktree_path"]
    (ig.Path(worktree) / "stray.py").write_text("x = 1\n")

    with pytest.raises(ig.MergeProblem, match="uncommitted changes") as caught:
        merge(world, "T3", two)
    assert caught.value.kind == "error" and branch_head(world) == one


def test_a_lost_branch_worktree_is_named(world):
    one = side_commit(world, "one", {"one.py": "A = 1\n"})
    merge(world, "T1", one)
    worktree = json.loads(ig.integration_path(world["runs"], RUN_ID).read_text())["worktree_path"]
    git(world["repo"], "worktree", "remove", "--force", worktree)

    with pytest.raises(ig.MergeProblem, match="lost its worktree"):
        merge(world, "T1", one)


def test_the_head_is_read_from_git(world):
    assert ig.integration_head(world["runs"], RUN_ID, world["repo"]) is None
    one = side_commit(world, "one", {"one.py": "A = 1\n"})
    merge(world, "T1", one)
    assert ig.integration_head(world["runs"], RUN_ID, world["repo"]) == one


def test_the_report_names_the_last_task_branch_for_a_run_without_one(world, agents):
    from orchestrator.scheduling.run_report import build_report
    record = run(world, agents)
    ig.integration_path(world["runs"], RUN_ID).unlink()
    result = build_report(RUN_ID, plans_dir=world["plans"], runs_dir=world["runs"],
                          runner_script=world["script"], get_command_line=lambda pid: None)
    assert result.branch == record["branch"]
    assert list_tasks(world["runs"], RUN_ID)["T1"]["status"] == "done"


def test_show_task_says_where_the_work_went(world, agents, capsys):
    from orchestrator.cli.display import display_task
    display_task(run(world, agents))
    assert f"Merged into: {BRANCH} (fast-forward)" in capsys.readouterr().out


def test_a_continued_task_puts_the_run_branch_in_the_worktree_folder(world, agents):
    """Found in 7.2: --continue did not pass the worktree folder on, so the run's branch
    got its worktree beside the repository instead."""
    from task_runner_support import resume, stop_at  # pyright: ignore[reportMissingImports]
    stop_at(world, agents, "reviewer")
    record = resume(world, agents, worktree_root=world["wt"])
    assert record["status"] == "done"
    saved = json.loads(ig.integration_path(world["runs"], RUN_ID).read_text())
    assert ig.Path(saved["worktree_path"]).parent == ig.Path(world["wt"]).resolve()
