import pytest

from task_runner_support import (  # noqa: F401  (git: test_worktrees imports it from here)
    FakeAgents,
    RUN_ID,
    add_task,
    git,
    make_world,
    task,
)


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


@pytest.fixture
def agents(world):
    return FakeAgents(world["repo"])


@pytest.fixture
def with_independent(world):
    add_task(world, task("T3", "note", ["NOTE is 1"], files=("notes.py",)))
    return world
