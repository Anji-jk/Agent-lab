"""The project a command works on is a Git repository other than the orchestrator's, and its
Agent Lab files live in its own .agent-lab/ folder, which Git ignores."""

import subprocess

import pytest

from orchestrator.core import project as pj
from orchestrator.core import runner_paths
from orchestrator.errors import ExecutionRefused
from orchestrator.settings import use_project


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout


def test_the_project_is_the_git_root_of_a_folder_inside_it(git_repo):
    inner = git_repo / "src" / "pkg"
    inner.mkdir(parents=True)
    found = pj.find_project(inner)
    assert found.root == git_repo.resolve()
    assert found.runs_dir == git_repo.resolve() / ".agent-lab" / "runs"
    assert found.agents_dir == git_repo.resolve() / ".agent-lab" / "agents"
    assert found.env_file == git_repo.resolve() / ".agent-lab" / ".env"


def test_the_orchestrators_own_repository_is_refused():
    with pytest.raises(ExecutionRefused, match="orchestrator's own repository"):
        pj.find_project(pj.ORCHESTRATOR_ROOT / "orchestrator")


def test_a_folder_outside_git_is_refused(tmp_path):
    with pytest.raises(ExecutionRefused, match="not inside a Git repository"):
        pj.find_project(tmp_path)


def test_the_data_folder_is_ignored_so_the_checkout_stays_clean(git_repo):
    project = pj.find_project(git_repo)
    pj.ensure_data_dir(project)
    (project.runs_dir / "run-1").mkdir(parents=True)
    (project.runs_dir / "run-1" / "plan.json").write_text("{}", encoding="utf-8")
    project.env_file.write_text("GITHUB_TOKEN=secret\n", encoding="utf-8")
    assert git(git_repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_ensuring_the_data_folder_keeps_what_is_there(git_repo):
    project = pj.find_project(git_repo)
    pj.ensure_data_dir(project)
    (project.data_dir / ".gitignore").write_text("*\n# mine\n", encoding="utf-8")
    pj.ensure_data_dir(project)
    assert (project.data_dir / ".gitignore").read_text(encoding="utf-8") == "*\n# mine\n"


def test_agents_files_go_to_the_project_unless_moved(git_repo, monkeypatch):
    monkeypatch.delenv("AGENT_LAB_AGENT_RUNS_DIR")
    project = pj.find_project(git_repo)
    use_project(project)
    try:
        assert runner_paths.runner_run_directory("r1") == project.agents_dir / "r1"
        monkeypatch.setenv("AGENT_LAB_AGENT_RUNS_DIR", str(git_repo.parent / "elsewhere"))
        assert runner_paths.runner_run_directory("r1") == (git_repo.parent / "elsewhere").resolve() / "r1"
    finally:
        use_project(None)
