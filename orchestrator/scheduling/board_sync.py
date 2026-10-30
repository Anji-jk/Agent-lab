"""Keep the board in line with the records.

The records decide; the board shows. Every sync works out where each card
should be from the plan's progress (the same answer ``status`` gives), compares
that with where it last put the card, and moves only the cards that differ.
That replaces the Plan's outbox: a comparison cannot lose an update, send one
twice or deliver two in the wrong order, and after a network failure the next
sync simply catches up.

Where a task's card goes:

* ``todo``         waiting, ready
* ``in_progress``  building, testing, reviewing (and a task run-plan has launched whose
                   process has not written its record yet: it stays here, never back in Todo)
* ``attention``    failed, needs review, cancelled, interrupted, blocked
* ``done``         done; its Issue is closed, so the parent's progress counts it

The parent Issue's card follows the whole plan: Todo before anything ran, In
progress while it runs or between tasks, Done when every task is done, and
Needs Review when the plan has stopped on a task.

A card moved by hand on the board is put back by the next sync that has a
reason to move it; a sync never fails a task. Where the card was last put is
saved in ``board_items.json`` (``place``), so nothing is sent when nothing changed.

A sync also comments: on a task's Issue when one of its attempts ends,
and the run report on the parent Issue when the plan completes or stops. The
keys of what was posted are kept too, so a comment is never posted twice. Writes
are about a second apart, as in publish: GitHub limits writes that come quickly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.execution import runner_adapter
from orchestrator.github.client import GitHubAPIError, GitHubClient
from orchestrator.github.comments import comment_key, report_comment, task_comment
from orchestrator.github.projects import ProjectClient
from orchestrator.github.publish import (
    WRITE_PAUSE_SECONDS, load_board, load_items, load_parent, save_items,
)
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.records.task_registry import list_tasks
from orchestrator.scheduling.plan_progress import CommandLine, PlanProgress, plan_progress
from orchestrator.execution.left_behind import LeftBehind, left_behind
from orchestrator.scheduling.run_report import build_report, format_report

TASK_PLACES = {
    "ready": "todo", "waiting": "todo",
    "running": "in_progress",
    "failed": "attention", "needs_review": "attention", "cancelled": "attention",
    "interrupted": "attention", "blocked": "attention",
    "done": "done",
}


def task_place(state: str) -> str:
    return TASK_PLACES.get(state, "todo")


def parent_place(progress: PlanProgress, starting: Collection[str] | str | None = None) -> str:
    if progress.state == "complete":
        return "done"
    if starting or progress.state == "running":
        return "in_progress"
    if progress.state == "stopped":
        return "attention"
    started = any(t.state != "ready" and t.state != "waiting" for t in progress.tasks)
    return "in_progress" if started else "todo"


@dataclass
class Synced:
    # "T2 (#4) -> In progress", "parent #1 -> Done": a task and its Issue, or the plan's own Issue
    moved: list[str] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)     # task Issues closed
    commented: list[int] = field(default_factory=list)  # Issues that got a comment
    problems: list[str] = field(default_factory=list)   # what could not be done (the run goes on)

    def summary(self) -> str:
        parts = []
        if self.moved:
            parts.append("moved " + ", ".join(self.moved))
        if self.closed:
            parts.append("closed " + ", ".join(f"#{n}" for n in self.closed))
        if self.commented:
            parts.append("commented on " + ", ".join(f"#{n}" for n in self.commented))
        if self.problems:
            parts.append("could not update: " + "; ".join(self.problems))
        return "Board: " + ("; ".join(parts) if parts else "up to date")


def report_key(progress: PlanProgress) -> str | None:
    """Which finished or stopped state of the plan a report is about; None while it can go on."""
    if progress.state not in ("complete", "stopped"):
        return None
    return progress.state + ":" + ",".join(f"{t.task_id}={t.state}" for t in progress.tasks)


def _left_behind(record: dict[str, Any], runner_script: Path) -> LeftBehind | None:
    """What a cancelled task had done; None when it cannot be read (the comment goes without)."""
    try:
        return left_behind(record, runner_script=runner_script)
    except Exception:  # a comment is a report; it must not stop the sync
        return None


def sync_board(
    run_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    issues: GitHubClient,
    project: ProjectClient,
    starting: Collection[str] | str | None = None,
    get_command_line: CommandLine | None = None,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    comments: bool = True,
    pause: float = WRITE_PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Synced:
    """Move the run's cards to where the records say, and post what happened.

    Never raises for GitHub's sake: what fails is listed in ``problems`` and
    tried again by the next sync.
    """
    result = Synced()
    # Tasks run-plan has launched. Each starts as its own process and writes its record a moment
    # later, so until then the records still say "not started": they must not send the card back.
    launched = {starting} if isinstance(starting, str) else set(starting or ())
    board = load_board(runs_dir)
    columns: dict[str, dict[str, str]] = board["columns"]
    status_field = board.get("status_field_id")
    if not isinstance(status_field, int):
        result.problems.append("board.json has no Status field id; run board connect again")
        return result
    plan = load_task_plan(plans_dir, run_id)
    progress = plan_progress(plan, runs_dir, run_id, get_command_line=get_command_line,
                             runner_script=Path(runner_script))
    records = list_tasks(runs_dir, run_id)
    items = load_items(runs_dir, run_id)
    parent = load_parent(runs_dir, run_id)
    writes = 0

    def spaced() -> None:  # GitHub limits writes that come too quickly
        nonlocal writes
        if writes and pause > 0:
            sleep(pause)
        writes += 1

    def move(label: str, entry: dict[str, Any], place: str) -> bool:
        if entry.get("place") == place:
            return False
        spaced()
        try:
            project.set_field(int(entry["item_id"]), status_field, columns[place]["option_id"])
        except (GitHubAPIError, KeyError, ValueError) as exc:
            result.problems.append(f"{label}: {exc}")
            return False
        entry["place"] = place
        result.moved.append(f"{label} -> {columns[place]['name']}")
        return True

    def comment(number: int, entry: dict[str, Any], key: str, text: str, slot: str) -> bool:
        if key in (entry.get(slot) or []):
            return False
        spaced()
        try:
            issues.add_issue_comment(number, text)
        except (GitHubAPIError, ValueError) as exc:
            result.problems.append(f"commenting on #{number}: {exc}")
            return False
        entry[slot] = [*(entry.get(slot) or []), key]
        result.commented.append(number)
        return True

    for task in progress.tasks:
        entry = items.get(task.task_id)
        if not entry or "item_id" not in entry:
            continue  # not on the board yet: board publish puts it there
        place = task_place(task.state)
        if task.task_id in launched and place == "todo":
            place = "in_progress"
        number = entry.get("issue_number")
        changed = move(f"{task.task_id} (#{number})" if number else task.task_id, entry, place)
        if place == "done" and not entry.get("closed") and number:
            spaced()
            try:
                issues.close_issue(int(number))
            except GitHubAPIError as exc:
                result.problems.append(f"closing #{number}: {exc}")
            else:
                entry["closed"] = True
                result.closed.append(int(number))
                changed = True
        record = records.get(task.task_id)
        key = comment_key(record) if (comments and record and number) else None
        if key and number and key not in (entry.get("comments") or []):
            left = None
            if record and record.get("status") == "cancelled":  # what it had done
                left = _left_behind(record, Path(runner_script))
            changed |= comment(int(number), entry, key,
                               task_comment(run_id, task.task_id, record or {}, left), "comments")
        if changed:
            save_items(runs_dir, run_id, items, parent or None)

    number = int(plan["issue"]["number"])
    if "item_id" not in parent:  # the parent's card: on the board already, or added now
        try:
            parent["issue_id"] = parent.get("issue_id") or issues.get_issue(number)["id"]
            spaced()
            parent["item_id"] = project.add_issue(int(parent["issue_id"]))
        except (GitHubAPIError, KeyError, TypeError) as exc:
            result.problems.append(f"#{number}'s card: {exc}")
            return result
    move(f"parent #{number}", parent, parent_place(progress, launched))
    key = report_key(progress) if comments else None
    if key:  # the plan has finished or stopped: the run report goes on the parent Issue
        text = format_report(build_report(run_id, plans_dir=plans_dir, runs_dir=runs_dir,
                                          runner_script=Path(runner_script),
                                          get_command_line=get_command_line))
        comment(number, parent, key, report_comment(run_id, text, key), "reports")
    save_items(runs_dir, run_id, items, parent)
    return result
