"""Retry a failed task once, with what went wrong (M4.4).

Many failures are fixed once the builder is told precisely what was wrong; more
than one retry mostly pays for the same mistake again. So a task that ended
``failed`` gets exactly one more attempt: a fresh worktree and branch from the
run's current head, and a fresh builder whose prompt carries what the checks
found. If that fails too, the task stays ``failed`` and plan progress blocks
its dependents as before.

Only ``failed`` is retried. ``needs_review`` (a crash, a timeout, an unusable
report) means the pipeline could not judge the work, so a person looks. A task
that failed before it had a worktree, or whose builder said it was blocked,
is not retried either: the same instructions would meet the same wall.

Each attempt keeps its own record. The first attempt's task files move into
``tasks/<task>/attempt-1/``; the new ``task.json`` says ``attempt: 2`` and lists
the earlier attempts with their status, reasons and cost. Its agents get their
own runner folders (``<run>-<task>-r2-<role>``), so nothing of the first
attempt is overwritten and nothing is deleted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import runner_adapter
from orchestrator.execution import task_runner as tr
from orchestrator.execution.background import LAUNCH_FILE_NAME, LOG_FILE_NAME
from orchestrator.monitoring.event_feed import mask_secrets
from orchestrator.monitoring.progress import attempt_of
from orchestrator.records.run_registry import locked
from orchestrator.records.task_events import TASK_EVENTS_FILE_NAME
from orchestrator.records.task_registry import TASK_RECORD_NAME, load_task, task_directory

MAX_RETRIES = 1  # a continued attempt (M4.5) is not a retry, so attempts are not counted
MAX_ATTEMPTS = 9  # as for continuing: keeps the "-r<n>" names one digit
MAX_REASONS = 12
REASON_CHARS = 600
OUTPUT_CHARS = 1500
# The task's own files that belong to one attempt.
ATTEMPT_FILES = (TASK_RECORD_NAME, TASK_EVENTS_FILE_NAME, LAUNCH_FILE_NAME, LOG_FILE_NAME)


def retry_problem(record: dict[str, Any]) -> str | None:
    """Why this task is not retried, or None when it is."""
    task_id, status = record["task_id"], record["status"]
    if status != "failed":
        extra = " A person should look at it." if status == "needs_review" else ""
        return f"{task_id} is {status}; only a failed task is retried.{extra}"
    attempt = attempt_of(record)
    failed_before = [p for p in record.get("previous_attempts") or [] if p.get("status") == "failed"]
    if len(failed_before) >= MAX_RETRIES:
        return (f"{task_id} has already been retried (attempt {failed_before[-1].get('attempt')} "
                f"failed, and attempt {attempt} failed too); it stays failed.")
    if attempt >= MAX_ATTEMPTS:
        return f"{task_id} has had {attempt} attempts; a person should look at it."
    if not record.get("branch"):
        return (f"{task_id} failed before it had a worktree "
                f"({(record.get('reasons') or ['no reason recorded'])[0]}); "
                "trying again would meet the same problem.")
    blocked = (record.get("findings") or {}).get("builder_blocked")
    if blocked:
        return (f"The builder said {task_id} cannot be done ({blocked}); a retry would "
                "be given the same task, so a person should look.")
    return None


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def retry_feedback(record: dict[str, Any]) -> str:
    """What the checks found, as text for the next builder. Secrets are masked."""
    attempt = attempt_of(record)
    lines = [f"Attempt {attempt} of this task was built, tested and reviewed, and failed. "
             "What was found:"]
    reasons = [str(r) for r in (record.get("reasons") or [])]
    for reason in reasons[:MAX_REASONS]:
        lines.append(f"- {_clip(' '.join(reason.split()), REASON_CHARS)}")
    if len(reasons) > MAX_REASONS:
        lines.append(f"- ... and {len(reasons) - MAX_REASONS} more")
    checks = record.get("checks") or {}
    for label, key in (("full test suite", "suite"), ("tests the tester named", "named_tests")):
        check = checks.get(key) or {}
        if check.get("timed_out") or check.get("exit_code") not in (0, None):
            tail = str(check.get("output_tail") or "").strip()
            lines.append("")
            lines.append(f"The end of the {label} output"
                         + (" (it timed out)" if check.get("timed_out") else "") + ":")
            lines.append(_clip(tail, OUTPUT_CHARS) if tail else "(no output)")
    lines.append("")
    lines.append("This is a new attempt on a clean checkout: the earlier attempt's changes are "
                 "not here. Make the change again, and make sure these problems do not occur.")
    return mask_secrets("\n".join(lines))


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt": attempt_of(record),
        "status": record["status"],
        "reasons": record.get("reasons") or [],
        "cost_usd": record.get("cost_usd"),
        "branch": record.get("branch"),
        "worktree_path": record.get("worktree_path"),
        "base_commit": record.get("base_commit"),
        "finished_at": record.get("updated_at"),
    }


def archive_attempt(runs_dir: Path, run_id: str, task_id: str, attempt: int) -> Path:
    """Move the attempt's task files into ``attempt-<n>/`` (all of them, or none)."""
    directory = task_directory(runs_dir, run_id, task_id)
    target = directory / f"attempt-{attempt}"
    if target.exists():
        raise ExecutionRefused(f"{target} already exists; attempt {attempt} was archived before.")
    with locked(directory):
        target.mkdir()
        moved: list[str] = []
        try:
            for name in ATTEMPT_FILES:
                if (directory / name).exists():
                    os.replace(directory / name, target / name)
                    moved.append(name)
        except OSError as exc:
            for name in moved:
                os.replace(target / name, directory / name)
            target.rmdir()
            raise ExecutionRefused(
                f"The first attempt's files could not be moved ({type(exc).__name__}: {exc}). "
                "Nothing was changed; close anything that has them open and try again."
            ) from None
    return target


def restore_attempt(runs_dir: Path, run_id: str, task_id: str, attempt: int) -> None:
    """Undo ``archive_attempt`` after the retry failed to start."""
    directory = task_directory(runs_dir, run_id, task_id)
    target = directory / f"attempt-{attempt}"
    with locked(directory):
        for name in ATTEMPT_FILES:
            if (target / name).exists() and not (directory / name).exists():
                os.replace(target / name, directory / name)
        try:
            target.rmdir()
        except OSError:
            pass


def retry_task(
    run_id: str,
    task_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    worktree_root: Path | None = None,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    invoke_runner=runner_adapter.invoke_runner,
    test_command: tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a failed task's second attempt and return its record.

    A refusal leaves the failed record exactly as it was: the cheap checks run
    before anything moves, and if ``run_task`` refuses before it has made the
    new record (no pytest, say), the first attempt's files are moved back.
    """
    record = load_task(runs_dir, run_id, task_id)  # RunRegistryError if it never ran
    problem = retry_problem(record)
    if problem:
        raise ExecutionRefused(problem)
    tr.prepare(run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path)

    attempt = attempt_of(record)
    previous = [*(record.get("previous_attempts") or []), _summary(record)]
    feedback = retry_feedback(record)
    archive_attempt(runs_dir, run_id, task_id, attempt)
    try:
        return tr.run_task(
            run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path,
            worktree_root=worktree_root, runner_script=runner_script,
            invoke_runner=invoke_runner, test_command=test_command, env=env,
            attempt=attempt + 1, feedback=feedback, previous_attempts=previous,
        )
    except BaseException:
        # Not started at all (no new record): put the failed attempt back as it was.
        if not (task_directory(runs_dir, run_id, task_id) / TASK_RECORD_NAME).exists():
            restore_attempt(runs_dir, run_id, task_id, attempt)
        raise