"""Execute an approved plan: worktree, runner, and run-record bookkeeping.

Approval is a parameter that only a caller's explicit action can set. It is
checked before anything else, so an unapproved call has no side effects:
no run record, no worktree, no prompt file, no Claude process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.errors import ApprovalRequired, ExecutionRefused
from orchestrator.execution import runner_adapter, worktrees
from orchestrator.legacy.planning import ExecutionPlan, build_prompt
from orchestrator.records.run_registry import (
    run_directory,
    save_run,
    update_run,
    utc_now,
)

PROMPT_FILE_NAME = "prompt.txt"

# runner outcome -> run-record status
OUTCOME_STATUS = {
    "success": "succeeded",
    "failure": "failed",
    "timeout": "timed_out",
    "launch_error": "failed",
}


def _fail(runs_dir: Path, run_id: str, message: str) -> None:
    """Best-effort: mark the run failed without masking the real error."""
    try:
        update_run(runs_dir, run_id, {"status": "failed", "error": message})
    except Exception:
        pass


def execute_plan(
    plan: ExecutionPlan,
    *,
    approved: bool,
    runs_dir: Path,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    create_worktree=worktrees.create_worktree,
    invoke_runner=runner_adapter.invoke_runner,
) -> dict[str, Any]:
    """Run ``plan`` and return the final run record."""
    if approved is not True:
        raise ApprovalRequired(
            "Execution requires explicit approval (pass --approve)."
        )
    if plan.prohibitions:
        raise ExecutionRefused(
            "The Issue prohibits execution: " + "; ".join(plan.prohibitions)
        )

    # Pre-flight checks that must pass before anything is created.
    if not Path(runner_script).is_file():
        raise ExecutionRefused(f"Runner script not found: {runner_script}")
    runner_dir = runner_adapter.runner_run_directory(plan.run_id, runner_script)
    if runner_dir.exists():
        raise FileExistsError(f"Runner directory already exists: {runner_dir}")
    if (run_directory(runs_dir, plan.run_id) / "orchestration.json").exists():
        raise FileExistsError(f"Run already exists: {plan.run_id}")

    task = plan.task
    save_run(
        runs_dir,
        plan.run_id,
        {
            "status": "queued",
            "task_id": task.task_id,
            "repository": task.repository,
            "issue_number": task.issue_number,
            "issue_url": task.issue_url,
            "title": task.title,
            "base_ref": plan.base_ref,
            "branch": plan.branch,
            "worktree_path": str(plan.worktree_path),
            "timeout_seconds": plan.timeout_seconds,
            "model": plan.model,
            "max_turns": plan.max_turns,
            "approved_at": utc_now(),
            "runner_run_dir": str(runner_dir),
        },
    )

    try:
        branch, worktree_path = create_worktree(
            plan.repository_root,
            task.issue_number,
            plan.run_id,
            plan.base_ref,
        )
    except Exception as exc:
        _fail(runs_dir, plan.run_id, f"Worktree creation failed: {exc}")
        raise

    try:
        prompt_file = run_directory(runs_dir, plan.run_id) / PROMPT_FILE_NAME
        prompt_file.write_text(build_prompt(plan), encoding="utf-8")
        update_run(
            runs_dir,
            plan.run_id,
            {
                "status": "running",
                "branch": branch,
                "worktree_path": str(worktree_path),
                "started_at": utc_now(),
            },
        )
        outcome = invoke_runner(
            run_id=plan.run_id,
            worktree_path=worktree_path,
            prompt_file=prompt_file,
            timeout_seconds=plan.timeout_seconds,
            model=plan.model,
            max_turns=plan.max_turns,
            runner_script=runner_script,
        )
    except Exception as exc:
        _fail(runs_dir, plan.run_id, f"{type(exc).__name__} during execution.")
        raise

    return update_run(
        runs_dir,
        plan.run_id,
        {
            "status": OUTCOME_STATUS[outcome.outcome],
            "finished_at": utc_now(),
            "runner_outcome": outcome.outcome,
            "runner_exit_code": outcome.process_exit_code,
            "error": outcome.error,
            "runner_result": outcome.result,
        },
    )
