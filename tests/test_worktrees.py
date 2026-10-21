import subprocess

import pytest

from orchestrator import worktrees
from orchestrator.worktrees import (
    WorktreeError,
    create_worktree,
    list_worktrees,
    remove_worktree,
    verify_remote_repository,
)
from conftest import git


def test_create_worktree_records_branch_and_path(git_repo):
    branch, path = create_worktree(git_repo, 7, "run-a", "main")

    assert branch == "agent/issue-7-run-a"
    assert path.is_dir()
    assert path.parent == git_repo.resolve().parent
    registered = {w.path: w.branch for w in list_worktrees(git_repo)}
    assert registered[path.resolve()] == branch
    assert (path / "README.md").exists()


def test_two_runs_for_one_issue_do_not_collide(git_repo):
    first = create_worktree(git_repo, 7, "run-a")
    second = create_worktree(git_repo, 7, "run-b")

    assert first[0] != second[0]
    assert first[1] != second[1]


def test_duplicate_run_id_collides_on_path(git_repo):
    create_worktree(git_repo, 7, "run-a")

    with pytest.raises(FileExistsError, match="path already exists"):
        create_worktree(git_repo, 7, "run-a")


def test_existing_branch_collides(git_repo):
    git(git_repo, "branch", "agent/issue-7-run-a")

    with pytest.raises(FileExistsError, match="Branch already exists"):
        create_worktree(git_repo, 7, "run-a")

    assert not (git_repo.parent / "repo-agent-issue-7-run-a").exists()


@pytest.mark.parametrize("issue", [0, -1, True, "1", None])
def test_invalid_issue_number(git_repo, issue):
    with pytest.raises(ValueError, match="Issue number"):
        create_worktree(git_repo, issue, "run-a")


@pytest.mark.parametrize("run_id", ["", "../x", "a/b", "-x", "x" * 65])
def test_invalid_run_id(git_repo, run_id):
    with pytest.raises(ValueError, match="run ID"):
        create_worktree(git_repo, 1, run_id)


@pytest.mark.parametrize(
    "base_ref", ["--upload-pack=evil", "-b", "a..b", "has space", "", "x/"]
)
def test_invalid_base_ref(git_repo, base_ref):
    with pytest.raises(ValueError, match="base reference"):
        create_worktree(git_repo, 1, "run-a", base_ref)


def test_missing_base_ref_creates_nothing(git_repo):
    with pytest.raises(WorktreeError):
        create_worktree(git_repo, 1, "run-a", "does-not-exist")

    assert not git(git_repo, "branch", "--list", "agent/issue-1-run-a")
    assert not (git_repo.parent / "repo-agent-issue-1-run-a").exists()


def test_rejects_non_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError):
        create_worktree(plain, 1, "run-a")


def test_rejects_missing_directory(tmp_path):
    with pytest.raises(WorktreeError, match="not a directory"):
        create_worktree(tmp_path / "nope", 1, "run-a")


def test_failed_add_reports_leftovers_and_deletes_nothing(git_repo, monkeypatch):
    unrelated = git_repo.parent / "unrelated.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    real_run_git = worktrees.run_git

    def fail_add(root, *args):
        if args[:2] == ("worktree", "add"):
            # Simulate git creating the branch and then failing.
            real_run_git(root, "branch", args[3], args[5])
            raise subprocess.CalledProcessError(
                128, ["git"], stderr="fatal: simulated failure"
            )
        return real_run_git(root, *args)

    monkeypatch.setattr(worktrees, "run_git", fail_add)

    with pytest.raises(WorktreeError, match="simulated failure") as info:
        create_worktree(git_repo, 1, "run-a")

    assert "branch agent/issue-1-run-a" in str(info.value)
    assert git(git_repo, "branch", "--list", "agent/issue-1-run-a")  # not deleted
    assert unrelated.read_text(encoding="utf-8") == "keep me"


def test_remove_clean_worktree_keeps_branch(git_repo):
    branch, path = create_worktree(git_repo, 1, "run-a")

    remove_worktree(git_repo, path, branch)

    assert not path.exists()
    assert git(git_repo, "branch", "--list", branch)  # branch never deleted


def test_remove_refuses_uncommitted_changes(git_repo):
    branch, path = create_worktree(git_repo, 1, "run-a")
    (path / "work.txt").write_text("agent output", encoding="utf-8")

    with pytest.raises(WorktreeError, match="uncommitted"):
        remove_worktree(git_repo, path, branch)

    assert (path / "work.txt").exists()


def test_remove_can_discard_changes_only_when_explicit(git_repo):
    branch, path = create_worktree(git_repo, 1, "run-a")
    (path / "work.txt").write_text("agent output", encoding="utf-8")

    remove_worktree(git_repo, path, branch, discard_changes=True)

    assert not path.exists()


def test_remove_refuses_main_checkout(git_repo):
    with pytest.raises(WorktreeError, match="main checkout"):
        remove_worktree(git_repo, git_repo, "main")
    assert (git_repo / "README.md").exists()


def test_remove_refuses_unregistered_path(git_repo, tmp_path):
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "file.txt").write_text("x", encoding="utf-8")

    with pytest.raises(WorktreeError, match="Not a registered worktree"):
        remove_worktree(git_repo, stray, "agent/issue-1-run-a")

    assert (stray / "file.txt").exists()


def test_remove_refuses_branch_mismatch(git_repo):
    branch, path = create_worktree(git_repo, 1, "run-a")

    with pytest.raises(WorktreeError, match="mismatch"):
        remove_worktree(git_repo, path, "agent/issue-1-other")

    assert path.exists()


def test_remove_refuses_non_agent_branch(git_repo):
    other = git_repo.parent / "repo-feature"
    git(git_repo, "worktree", "add", "-b", "feature/x", str(other), "main")

    with pytest.raises(WorktreeError, match="non-agent"):
        remove_worktree(git_repo, other, "feature/x")

    assert other.exists()


def test_verify_remote_repository(git_repo):
    verify_remote_repository(git_repo, "Anji-jk/Agent-lab")
    verify_remote_repository(git_repo, "anji-jk/agent-lab")

    with pytest.raises(WorktreeError, match="does not match"):
        verify_remote_repository(git_repo, "someone/else")


def test_verify_remote_repository_without_origin(git_repo):
    git(git_repo, "remote", "remove", "origin")
    with pytest.raises(WorktreeError, match="no 'origin'"):
        verify_remote_repository(git_repo, "Anji-jk/Agent-lab")


# --- worktrees outside the repository's folder ------------------------------


def test_worktree_root_places_the_worktree_there(git_repo, tmp_path):
    root = tmp_path / "elsewhere" / "worktrees"

    branch, path = create_worktree(git_repo, 7, "run-a", "main", worktree_root=root)

    assert path.parent == root.resolve()
    assert path.is_dir() and (path / "README.md").exists()
    registered = {w.path: w.branch for w in list_worktrees(git_repo)}
    assert registered[path.resolve()] == branch


def test_without_a_worktree_root_the_old_sibling_rule_holds(git_repo):
    _, path = create_worktree(git_repo, 7, "run-a")
    assert path.parent == git_repo.resolve().parent


def test_worktree_root_inside_the_repository_is_refused(git_repo):
    with pytest.raises(WorktreeError, match="outside the repository"):
        create_worktree(git_repo, 7, "run-a", "main", worktree_root=git_repo / "wt")
    assert not (git_repo / "wt").exists()


def test_worktree_root_equal_to_the_repository_is_refused(git_repo):
    with pytest.raises(WorktreeError, match="outside the repository"):
        create_worktree(git_repo, 7, "run-a", "main", worktree_root=git_repo)


def test_default_root_is_in_the_home_folder_unless_overridden(monkeypatch, tmp_path):
    monkeypatch.delenv(worktrees.WORKTREE_ROOT_ENV, raising=False)
    assert worktrees.default_worktree_root().name == "agent-worktrees"

    monkeypatch.setenv(worktrees.WORKTREE_ROOT_ENV, str(tmp_path / "mine"))
    assert worktrees.default_worktree_root() == tmp_path / "mine"


def test_a_commit_can_be_the_base(git_repo, tmp_path):
    sha = git(git_repo, "rev-parse", "HEAD")

    branch, path = create_worktree(
        git_repo, 7, "run-a", sha, worktree_root=tmp_path / "wt"
    )

    assert git(path, "rev-parse", "HEAD") == sha
    assert git(path, "branch", "--show-current") == branch
