"""Put a planned run's tasks on GitHub: one Issue per task, under its parent, on the board (M5.2).

For each task of the plan, three writes, in this order:

1. create an Issue for it in the repository (title ``#<parent> <task> <title>``,
   body: what to do, the acceptance criteria as a checklist, and a marker);
2. make it a sub-issue of the Issue the plan came from, so the parent lists its
   tasks with their progress;
3. add it to the board as a card.

After every write, what was created is saved in ``runs/<run>/board_items.json``,
so running ``board publish`` again, or after a crash, picks up where it
stopped and never creates a second Issue for a task. If that file is lost, a
task's Issue is still found among the parent's sub-issues by the marker in its
body.

Without ``--yes`` nothing is written: it shows what it would do. The Issue
work uses ``GITHUB_TOKEN`` (fine-grained, Issues: read and write); the board
uses ``AGENT_LAB_PROJECT_TOKEN`` (classic, 'project' scope). Where each card
goes is 5.3's job.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.github.board import board_path
from orchestrator.github.client import GitHubAPIError, GitHubClient
from orchestrator.github.projects import ProjectClient
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.records.run_registry import run_directory, utc_now, write_json_atomic

ITEMS_FILE_NAME = "board_items.json"
WRITE_PAUSE_SECONDS = 1.0  # GitHub limits creating content quickly
MAX_TITLE = 240


class PublishError(ExecutionRefused):
    """The run's tasks cannot be put on the board as things stand."""


def marker(run_id: str, task_id: str) -> str:
    """Hidden in each task Issue's body; how a task's Issue is recognised."""
    return f"<!-- agent-lab task: run={run_id} task={task_id} -->"


def task_title(parent: int, task: dict[str, Any]) -> str:
    title = f"#{parent} {task['id']} {task['title']}"
    return title if len(title) <= MAX_TITLE else title[: MAX_TITLE - 1] + "…"


def task_body(run_id: str, parent: int, task: dict[str, Any]) -> str:
    lines = [str(task.get("description") or "").strip(), "", "### Acceptance criteria", ""]
    lines += [f"- [ ] {criterion}" for criterion in task.get("acceptance_criteria") or []]
    depends = task.get("depends_on") or []
    lines += ["", f"Depends on: {', '.join(depends)}" if depends else "Depends on: nothing",
              f"Files planned: {', '.join(task.get('files') or []) or '(none)'}", "",
              f"Part of #{parent}. Planned and run by Agent Lab (run `{run_id}`); "
              "the board card and the comments here follow the run.",
              "", marker(run_id, task["id"])]
    return "\n".join(lines).strip() + "\n"


# --- what has been done, kept between runs ------------------------------------------------------


def items_path(runs_dir: Path, run_id: str) -> Path:
    return run_directory(Path(runs_dir), run_id) / ITEMS_FILE_NAME


def _read_items_file(runs_dir: Path, run_id: str) -> dict[str, Any]:
    path = items_path(runs_dir, run_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise PublishError(f"{path} is damaged. Move it aside and run board publish again: the "
                           "task Issues already made are found again by their marker.") from None
    return data if isinstance(data, dict) else {}


def load_items(runs_dir: Path, run_id: str) -> dict[str, dict[str, Any]]:
    tasks = _read_items_file(runs_dir, run_id).get("tasks")
    return {k: v for k, v in (tasks or {}).items() if isinstance(v, dict)}


def load_parent(runs_dir: Path, run_id: str) -> dict[str, Any]:
    """The parent Issue's card (M5.3): its ``issue_id``, ``item_id`` and last ``place``."""
    parent = _read_items_file(runs_dir, run_id).get("parent")
    return parent if isinstance(parent, dict) else {}


def save_items(runs_dir: Path, run_id: str, items: dict[str, dict[str, Any]],
               parent: dict[str, Any] | None = None) -> None:
    """Save the tasks' entries; the parent's entry is kept unless a new one is given."""
    path = items_path(runs_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {k: v for k, v in items.items() if v}  # a task with nothing on GitHub yet: no entry
    parent = load_parent(runs_dir, run_id) if parent is None else parent
    data: dict[str, Any] = {"run_id": run_id, "updated_at": utc_now(), "tasks": kept}
    if parent:
        data["parent"] = parent
    write_json_atomic(path, data)


def load_board(runs_dir: Path) -> dict[str, Any]:
    path = board_path(runs_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PublishError("No board is connected. Run: python -m orchestrator board connect "
                           "--project <N>") from None
    except (OSError, ValueError):
        raise PublishError(f"{path} is damaged; run board connect again.") from None
    if not isinstance(data, dict) or not isinstance(data.get("columns"), dict):
        raise PublishError(f"{path} is incomplete; run board connect again.")
    return data


# --- publishing ------------------------------------------------------------------------------------


@dataclass
class TaskCard:
    task_id: str
    title: str
    issue_number: int | None = None
    done: list[str] = field(default_factory=list)  # what this call did: created, adopted, linked, added
    todo: list[str] = field(default_factory=list)  # what it would do (preview) or could not do


@dataclass
class Published:
    run_id: str
    parent: int
    repository: str
    board_url: str
    cards: list[TaskCard]
    written: bool


def _with_hint(exc: GitHubAPIError, what: str, token: str) -> PublishError:
    return PublishError(f"Could not {what}: {exc} Check that {token}. What was done before "
                        "this is saved; run board publish again to go on.")


def publish(
    run_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    issues: GitHubClient,
    project: ProjectClient,
    write: bool,
    pause: float = WRITE_PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Published:
    """Create, link and add each task's Issue (``write``), or show what that would do."""
    plan = load_task_plan(plans_dir, run_id)
    board = load_board(runs_dir)
    repository = plan["issue"].get("repository") or plan["config"].get("repository")
    parent = int(plan["issue"]["number"])
    if board.get("repository") != repository:
        raise PublishError(f"The board is connected for {board.get('repository')}, but run "
                           f"{run_id} is for {repository}. Run board connect --repo {repository}.")
    if issues.repository != repository:
        raise PublishError(f"The Issue client is for {issues.repository}, not {repository}.")

    items = load_items(runs_dir, run_id)
    existing: dict[str, dict[str, Any]] | None = None  # the parent's sub-issues, read once

    def sub_issues() -> dict[str, dict[str, Any]]:
        nonlocal existing
        if existing is None:
            found = {}
            for issue in issues.list_sub_issues(parent):
                for task in plan["tasks"]:
                    if marker(run_id, task["id"]) in str(issue.get("body") or ""):
                        found[task["id"]] = issue
            existing = found
        return existing

    writes = 0

    def spaced() -> None:
        nonlocal writes
        if writes and pause > 0:
            sleep(pause)
        writes += 1

    cards: list[TaskCard] = []
    for task in plan["tasks"]:
        task_id = task["id"]
        entry = items.setdefault(task_id, {})
        card = TaskCard(task_id, task_title(parent, task), entry.get("issue_number"))
        cards.append(card)

        # 1. the Issue: ours from before, found by its marker, or new
        if "issue_id" not in entry:
            found = sub_issues().get(task_id)
            if found is not None:
                entry.update(issue_id=found["id"], issue_number=found["number"], linked=True)
                card.issue_number = found["number"]
                card.done.append("found")
                save_items(runs_dir, run_id, items)
            elif not write:
                card.todo.append(f"create the Issue \"{card.title}\"")
            else:
                spaced()
                try:
                    created = issues.create_issue(card.title, task_body(run_id, parent, task))
                except GitHubAPIError as exc:
                    raise _with_hint(exc, f"create the Issue for {task_id}",
                                     f"GITHUB_TOKEN may write Issues in {repository}") from None
                entry.update(issue_id=created["id"], issue_number=created["number"],
                             created_at=utc_now())
                card.issue_number = created["number"]
                card.done.append("created")
                save_items(runs_dir, run_id, items)

        # 2. under the parent
        if not entry.get("linked"):
            if not write:
                card.todo.append(f"make it a sub-issue of #{parent}")
            else:
                spaced()
                try:
                    issues.add_sub_issue(parent, entry["issue_id"])
                except GitHubAPIError as exc:
                    raise _with_hint(exc, f"make {task_id}'s Issue a sub-issue of #{parent}",
                                     f"GITHUB_TOKEN may write Issues in {repository}") from None
                entry["linked"] = True
                card.done.append("linked")
                save_items(runs_dir, run_id, items)

        # 3. on the board
        if "item_id" not in entry:
            if not write:
                card.todo.append("add it to the board")
            else:
                spaced()
                try:
                    entry["item_id"] = project.add_issue(entry["issue_id"])
                except GitHubAPIError as exc:
                    raise _with_hint(exc, f"add {task_id}'s Issue to the board",
                                     f"{project.token_variable} has the 'project' scope") from None
                card.done.append("added")
                save_items(runs_dir, run_id, items)

    return Published(run_id, parent, repository, str(board.get("url")), cards, write)


def format_published(result: Published) -> str:
    lines = [f"Run {result.run_id}: {len(result.cards)} tasks of #{result.parent} "
             f"({result.repository}) on {result.board_url}"]
    for card in result.cards:
        number = f"#{card.issue_number}" if card.issue_number else "(new)"
        if card.todo:
            state = "would " + ", ".join(card.todo) if not result.written else "not finished"
        elif card.done:
            state = ", ".join(card.done)
        else:
            state = "already on the board"
        lines.append(f"  {card.task_id:<4} {number:<6} {state}")
    if not result.written:
        pending = sum(1 for c in result.cards if c.todo)
        lines.append("")
        lines.append(f"Nothing was written. {pending} task(s) to put on GitHub: run again with --yes."
                     if pending else "Nothing was written; every task is already on the board.")
    return "\n".join(lines)
