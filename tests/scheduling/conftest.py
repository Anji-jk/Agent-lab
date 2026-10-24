"""Shared setup for the scheduling tests that run real tasks (run-plan, M4.7).

They use the same fake repository and fake agents as the task runner tests, which
live in tests/execution/task_runner_support.py. pytest applies a folder's
conftest.py to that folder only, so the fixtures are declared again here, from
the same helpers.
"""

import sys
from pathlib import Path

import pytest

EXECUTION_TESTS = Path(__file__).resolve().parents[1] / "execution"
if str(EXECUTION_TESTS) not in sys.path:
    sys.path.insert(0, str(EXECUTION_TESTS))

from task_runner_support import FakeAgents, add_task, make_world, task  # noqa: E402  # pyright: ignore[reportMissingImports]


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
