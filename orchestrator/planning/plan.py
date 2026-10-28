"""Build an execution plan for a task without performing any side effects.

Planning reads Git state (read-only) but never creates a worktree, writes a
run record, or launches Claude.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.execution.worktrees import plan_worktree, validate_base_ref
from orchestrator.github.issue import Task
from orchestrator.planning.safety import find_execution_prohibitions
from orchestrator.records.run_registry import new_run_id, validate_run_id

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_TURNS = 5

SCOPE = (
    "Claude Code runs headless in the isolated worktree with its default "
    "permission mode (actions needing approval are not granted). "
    "The orchestrator does not commit, push, open pull requests, or merge."
)

@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    task: Task
    run_id: str
    branch: str
    worktree_path: Path
    repository_root: Path
    base_ref: str
    timeout_seconds: int
    model: str | None
    max_turns: int
    scope: str
    prohibitions: tuple[str, ...]

    @property
    def executable(self) -> bool:
        return not self.prohibitions


def build_plan(
    task: Task,
    repository_root: Path,
    *,
    base_ref: str = "main",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    run_id: str | None = None,
) -> ExecutionPlan:
    """Describe what execution would do. Creates nothing."""
    if timeout_seconds < 1:
        raise ValueError("Timeout must be at least 1 second.")
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1.")
    validate_base_ref(base_ref)

    run_id = validate_run_id(run_id) if run_id else new_run_id()
    root, branch, worktree_path = plan_worktree(
        repository_root, task.issue_number, run_id
    )
    return ExecutionPlan(
        task=task,
        run_id=run_id,
        branch=branch,
        worktree_path=worktree_path,
        repository_root=root,
        base_ref=base_ref,
        timeout_seconds=timeout_seconds,
        model=model,
        max_turns=max_turns,
        scope=SCOPE,
        prohibitions=tuple(find_execution_prohibitions(task)),
    )
