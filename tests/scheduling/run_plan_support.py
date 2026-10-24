"""Shared helpers for the run-plan and run report tests (M4.7, M4.8).

A file of its own, with a unique name, for the same reason as
tests/execution/task_runner_support.py: the test folders have no __init__.py.
"""

import os
from typing import Any

from orchestrator.scheduling import run_plan as rp
from task_runner_support import (  # pyright: ignore[reportMissingImports]  (conftest.py puts it on the path)
    RUN_ID,
    build_note,
    write_note_tests,
)


def no_process(pid):
    return None  # every recorded process has gone


class per_task:
    """The fake agents, with each task's own behaviour (T1 double, T2 triple, T3 note)."""

    def __init__(self, agents, inner=None):
        self.agents, self.inner = agents, inner or agents
        self.behaviours = {
            "T1": dict(agents.behaviour),
            "T2": {"builder": agents.build_triple, "tester": agents.test_triple,
                   "reviewer": agents.review_pass_one},
            "T3": {"builder": build_note, "tester": write_note_tests,
                   "reviewer": agents.review_pass_one},
        }

    def __call__(self, **kwargs):
        task_id = kwargs["run_id"][len(RUN_ID) + 1:].split("-")[0]
        self.agents.behaviour = self.behaviours[task_id]
        return self.inner(**kwargs)


def stopped(result) -> str:
    assert result.stopped is not None, "the plan was expected to stop"
    return result.stopped


def work(world, invoke, **overrides) -> rp.PlanRun:
    options: dict[str, Any] = dict(plans_dir=world["plans"], runs_dir=world["runs"], repo_path=world["repo"],
                   worktree_root=world["wt"], runner_script=world["script"],
                   invoke_runner=invoke, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                   get_command_line=no_process)
    options.update(overrides)
    return rp.run_plan(RUN_ID, **options)
