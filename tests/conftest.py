import subprocess
from pathlib import Path

import pytest

from orchestrator.execution.runner_adapter import RUNS_DIR_VARIABLE


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


@pytest.fixture(autouse=True)
def agent_runs_dir(tmp_path_factory, monkeypatch):
    """Agent run folders made by a test go to its own temporary folder, not runs/.

    The runner and the orchestrator both read RUNS_DIR_VARIABLE, so setting it here
    covers runs started in the test's process and runner processes it starts. One
    folder per test, so tests that reuse a run ID cannot collide.
    """
    folder = tmp_path_factory.mktemp("agent-runs")
    monkeypatch.setenv(RUNS_DIR_VARIABLE, str(folder))
    return folder


@pytest.fixture(autouse=True)
def no_env_file(tmp_path_factory, monkeypatch):
    """Commands run in tests never read the real .env and its tokens."""
    missing = tmp_path_factory.mktemp("no-env-file") / ".env"
    monkeypatch.setenv("AGENT_LAB_ENV_FILE", str(missing))
    monkeypatch.setenv("AGENT_LAB_REPOSITORY", "Anji-jk/Agent-lab")
    return missing
