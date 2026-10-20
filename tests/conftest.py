import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway repository with one commit on ``main`` and a GitHub origin.

    Worktrees are created as siblings, so everything stays inside tmp_path.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", "https://github.com/Anji-jk/Agent-lab.git")
    return repo
