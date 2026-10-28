"""Continue a stopped task from the stage it reached.

A task that stopped part-way (cancelled, or its process killed by a closed
window, a crash or a reboot) still has its worktree, with every file its
agents wrote. Continuing it runs the pipeline again in that same worktree,
starting at the stage it had reached, with fresh agents:

* With a checkpoint (``task_runner`` saves one after the builder's checks
  pass and another after the tests pass) the work already checked is taken
  up, not paid for again: stopped while testing, the builder's result is
  kept and a new tester runs; stopped while reviewing, only a new reviewer
  runs. If the files changed after the tests' checkpoint, the tests run again.
* Stopped by the run budget (``needs_review`` with ``budget_stop``):
  after the budget is raised it goes on at the stage it reached, like a stop.
* Stopped by Claude's usage limit (``needs_review`` with ``limit_stop``):
  once the limit has reset, it goes on at the stage it reached. Before the
  reset time it is refused, since the agent would only be refused again.
* Without a checkpoint (stopped while building, or a record made before
  checkpoints existed) a new builder starts, told that the worktree already holds the
  earlier changes, so it can finish them rather than start over.

The agents' conversations are not resumed: after an hour Claude's cache has
expired, so a resumed conversation would be re-read at full price.

Like a retry, a continue is a new attempt with its own record and agent
folders; the stopped attempt's files move to ``attempt-<n>/``. Unlike a retry,
it keeps the worktree and branch, and it does not count as the task's one
retry. For a run with its own branch it makes no difference that other tasks
finished since this one started: it is merged into the branch like any task. Only a run
from before that, whose tasks form one chain, refuses a task the run has moved on from,
because its commit would split the chain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import checks as tc
from orchestrator.execution import runner_adapter
from orchestrator.execution import task_runner as tr
from orchestrator.execution.diagnose import when_text
from orchestrator.execution.retry import archive_attempt, restore_attempt
from orchestrator.core.attempts import attempt_of
from orchestrator.execution.left_behind import left_behind
from orchestrator.execution.liveness import process_liveness
from orchestrator.records.task_registry import (
    ACTIVE_STATUSES, TASK_RECORD_NAME, load_task, task_directory,
)

MAX_ATTEMPTS = 9  # keeps the "-r<n>" names one digit; nine stops is a task to look at
IsAlive = tr.IsAlive


def limit_not_reset(record: dict[str, Any], now: datetime | None = None) -> str | None:
    """The reset time, when the task was stopped by a usage limit that has not reset yet."""
    limit = record.get("limit_stop")
    if not isinstance(limit, dict) or not limit.get("resets_at"):
        return None
    try:
        resets = datetime.fromisoformat(str(limit["resets_at"]))
    except ValueError:
        return None
    if resets.tzinfo is None:
        resets = resets.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return str(limit["resets_at"]) if now < resets else None


def continue_problem(record: dict[str, Any], is_alive: IsAlive,
                     now: datetime | None = None) -> str | None:
    """Why this task cannot be continued, or None when it can."""
    task_id, status = record["task_id"], record["status"]
    if status in ACTIVE_STATUSES or status == "pending":
        alive, why = is_alive(task_id, record)
        if alive:
            return f"{task_id} is still running ({why}); there is nothing to continue."
    elif status == "needs_review" and record.get("budget_stop"):
        pass  # stopped by the run budget before an agent started; it can go on once raised
    elif status == "needs_review" and record.get("limit_stop"):
        resets = limit_not_reset(record, now)
        if resets:
            return (f"{task_id} was stopped by Claude's usage limit, which resets at "
                    f"{when_text(resets)}. Continue it after that.")
    elif status != "cancelled":
        return (f"{task_id} is {status}. Only a cancelled or interrupted task is continued"
                + ("; a failed one is retried instead." if status == "failed" else "."))
    worktree = record.get("worktree_path")
    if not record.get("branch") or not worktree:
        return f"{task_id} stopped before it had a worktree, so there is nothing to continue in."
    if not Path(worktree).is_dir():
        return f"{task_id}'s worktree {worktree} no longer exists, so its files are gone."
    if attempt_of(record) >= MAX_ATTEMPTS:
        return f"{task_id} has been stopped {MAX_ATTEMPTS} times; a person should look at it."
    return None


def resume_point(record: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str]:
    """(stage to start at, checkpoint to take up, why), from the record and the worktree."""
    checkpoint = record.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("stage") not in ("testing", "reviewing"):
        return "building", None, "there is no checkpoint, so the builder runs again"
    if checkpoint["stage"] == "reviewing":
        now = tc.snapshot(Path(record["worktree_path"]))
        if now == checkpoint.get("after_tests"):
            return "reviewing", checkpoint, "the builder's and the tests' results are kept"
        return "testing", checkpoint, "files changed after the tests ran, so the tests run again"
    return "testing", checkpoint, "the builder's result is kept"


def earlier_note(record: dict[str, Any], stage: str) -> str:
    """What the next agents are told about the worktree they find."""
    try:
        changed = tc.changed_paths(Path(record["worktree_path"]))
    except tc.CheckError:
        changed = []
    files = ", ".join(changed) if changed else "(none)"
    if stage == "building":
        return ("This task was stopped part-way and is being continued. The worktree still "
                f"has the earlier attempt's changes (changed files: {files}). They have not "
                "been checked. Inspect them, keep what is right, fix what is not, and finish "
                "the task; do not start over without a reason.")
    return ("This task was stopped part-way and is being continued. The builder's change "
            "has been checked. tests/ may already hold tests an earlier, stopped tester "
            f"wrote (changed files: {files}); keep the ones that are right.")


def _summary(record: dict[str, Any], status: str, spent: float) -> dict[str, Any]:
    return {
        "attempt": attempt_of(record),
        "status": status,
        "reasons": record.get("reasons") or [f"Stopped while {record['status']}."],
        "cost_usd": spent,
        "branch": record.get("branch"),
        "worktree_path": record.get("worktree_path"),
        "base_commit": record.get("base_commit"),
        "finished_at": record.get("updated_at"),
    }


def continue_task(
    run_id: str,
    task_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    invoke_runner=runner_adapter.invoke_runner,
    test_command: tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
    is_alive: IsAlive | None = None,
    worktree_root: Path | None = None,
) -> dict[str, Any]:
    """Run a stopped task again from the stage it reached; return its new record.

    ``worktree_root``: where the run's branch gets its worktree if this task's merge is
    the run's first; the task itself keeps the worktree it already has.
    """
    record = load_task(runs_dir, run_id, task_id)  # RunRegistryError if it never ran
    alive = is_alive or process_liveness(runs_dir, run_id, runner_script=Path(runner_script))
    problem = continue_problem(record, alive)
    if problem:
        raise ExecutionRefused(problem)
    ctx = tr.prepare(run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir,
                     repo_path=repo_path, is_alive=alive)
    if (ctx.base_commit != record.get("base_commit")
            and tr.integration_head(runs_dir, run_id, repo_path) is None):
        raise tr.TaskNotReady(
            f"{task_id} started from {str(record.get('base_commit'))[:12]}, but the run has moved "
            f"on to {ctx.base_commit[:12]} since (another task finished). Continuing it would "
            "split the run's branch, so it is not continued."
        )

    stage, checkpoint, _why = resume_point(record)
    stopped = {"cancelled": "cancelled",
               "needs_review": ("stopped by Claude's usage limit" if record.get("limit_stop")
                                else "stopped by the run budget")}.get(record["status"], "interrupted")
    spent = left_behind(record, runner_script=Path(runner_script)).spent
    previous = [*(record.get("previous_attempts") or []), _summary(record, stopped, spent)]
    attempt = attempt_of(record)
    continue_from = {
        "stage": stage, "checkpoint": checkpoint, "earlier": earlier_note(record, stage),
        "branch": record["branch"], "worktree_path": record["worktree_path"],
        "base_commit": record["base_commit"],
    }
    archive_attempt(runs_dir, run_id, task_id, attempt)
    try:
        return tr.run_task(
            run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path,
            runner_script=runner_script, invoke_runner=invoke_runner,
            test_command=test_command, env=env, attempt=attempt + 1,
            previous_attempts=previous, continue_from=continue_from,
            worktree_root=worktree_root,
        )
    except BaseException:
        if not (task_directory(runs_dir, run_id, task_id) / TASK_RECORD_NAME).exists():
            restore_attempt(runs_dir, run_id, task_id, attempt)
        raise
