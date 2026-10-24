from pathlib import Path

import pytest

from orchestrator.planning.plan_file import save_task_plan
from task_runner_support import (
    FakeAgents,
    RUN_ID,
    add_task,
    git,
    task,
)


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "conftest.py").write_text("")
    # Like the real repository: test runs create folders that .gitignore ignores.
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.py[cod]\n")
    (repo / "README.md").write_text("docs\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_add.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    base = git(repo, "rev-parse", "HEAD")

    plans, runs = tmp_path / "plans", tmp_path / "runs"
    save_task_plan(plans, RUN_ID, {
        "issue": {"repository": "o/r", "number": 7, "title": "Add helpers",
                  "url": "u", "description": "Add double and triple."},
        "config": {"repository": "o/r", "base_ref": "main", "base_commit": base,
                   "model": None, "timeout_seconds": 60, "run_id": RUN_ID,
                   "branch": "b", "worktree_path": "w"},
        "summary": "s",
        "tasks": [
            task("T1", "double", ["double(2) is 4", "double(-1) is -2"]),
            task("T2", "triple", ["triple(2) is 6"], depends=["T1"]),
        ],
        "planner": {"run_id": f"{RUN_ID}-plan"},
    })
    return {"repo": repo, "base": base, "plans": plans, "runs": runs,
            "wt": tmp_path / "wt", "script": Path(__file__)}


@pytest.fixture
def agents(world):
    return FakeAgents(world["repo"])


@pytest.fixture
def with_independent(world):
    add_task(world, task("T3", "note", ["NOTE is 1"], files=("notes.py",)))
    return world
