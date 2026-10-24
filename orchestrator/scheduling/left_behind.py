"""What a stopped task left behind (M4.3).

A cancelled or interrupted task has no verdict: the agents' JSON reports are
written only when they finish, so a stopped agent leaves none. Its work is
still on disk, though, and a person (or the retry in 4.4 and the continue in
4.5) should not have to dig for it. This module reads it back: the files
changed in the task's worktree, and for each agent its last message, the last
test command it ran, and what it spent.

Everything comes from files. No Claude call is made and nothing is written.
It is worked out each time rather than saved, so it always matches the
worktree as it is now, and it also works for an interrupted task, whose
process died before it could write anything.

It is **unverified**: nobody checked these files, and the agents were stopped
before they could say whether the work was right.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.execution import checks as tc
from orchestrator.execution import runner_adapter
from orchestrator.execution.roles import ROLE_NAMES
from orchestrator.monitoring.event_feed import agent_items, mask_secrets
from orchestrator.monitoring.progress import STAGE_ROLE, agent_run_id, attempt_of, task_agent_view
from orchestrator.records.task_registry import ACTIVE_STATUSES

MESSAGE_LIMIT = 400
COMMAND_LIMIT = 120
MAX_FILES_SHOWN = 15
UNVERIFIED = "unverified: the task was stopped before its checks and review finished"
_PYTEST = re.compile(r"\bpytest\b")

IsAlive = Callable[[str, dict[str, Any]], tuple[bool, str]]


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    kind: str  # new, modified, deleted
    added: int | None
    removed: int | None


@dataclass(frozen=True, slots=True)
class LastTests:
    command: str
    outcome: str  # passed, failed, blocked, running, not finished
    result: str  # the last line of its output, if any


@dataclass(frozen=True, slots=True)
class AgentLeft:
    role: str
    line: str  # the agent as `status` describes it: state, turns, tokens, cost
    last_message: str | None
    last_tests: LastTests | None
    cost_usd: float | None
    finished: bool = False  # it ended by itself before the stop (not ended by cancel)


@dataclass(frozen=True, slots=True)
class LeftBehind:
    run_id: str
    task_id: str
    status: str
    stage: str | None  # the stage the task had reached when it stopped
    base_commit: str
    files: list[FileChange] | None  # None: the worktree could not be read
    files_note: str | None
    agents: list[AgentLeft]
    spent: float

    @property
    def unverified(self) -> bool:
        return True


def is_stopped(record: dict[str, Any], is_alive: IsAlive) -> bool:
    """Cancelled, or still at a stage with its process gone (interrupted)."""
    status = record.get("status")
    if status == "cancelled":
        return True
    if status in ACTIVE_STATUSES:
        alive, _ = is_alive(str(record.get("task_id")), record)
        return not alive
    return False


def stage_reached(record: dict[str, Any]) -> str | None:
    """The last working stage in the record's history."""
    for step in reversed(record.get("history") or []):
        status = step.get("status") if isinstance(step, dict) else None
        if status in STAGE_ROLE:
            return status
    return None


def _clean(text: str, limit: int) -> str:
    text = mask_secrets(" ".join(text.split()))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _test_run(item: Any) -> LastTests:
    result = item.result
    if result is None:
        outcome, line = "not finished", ""
    else:
        status = result.get("status")
        outcome = {"ok": "passed", "error": "failed", "blocked": "blocked",
                   "background": "running"}.get(status, str(status))
        detail = str(result.get("detail") or "").strip().splitlines()
        line = _clean(detail[-1], COMMAND_LIMIT) if detail else ""
    return LastTests(_clean(item.title, COMMAND_LIMIT), outcome, line)


def agent_left(role: str, run_id: str, task_id: str, record: dict[str, Any],
               runner_script: Path, now: datetime) -> AgentLeft:
    view = task_agent_view(run_id, task_id, role, record, runner_script, now)
    events = runner_adapter.runner_run_directory(
        agent_run_id(run_id, task_id, role, attempt_of(record)), runner_script)
    items = agent_items(events / "events.jsonl")
    said = [item for item in items if item.kind == "say"]
    message = None
    if said:
        message = _clean(said[-1].detail or said[-1].title, MESSAGE_LIMIT) or None
    tests = [item for item in items
             if item.kind == "tool" and item.tool == "Bash" and _PYTEST.search(item.title)]
    return AgentLeft(role, view.describe(), message, _test_run(tests[-1]) if tests else None,
                     view.cost_usd,
                     finished=view.state == "finished" and view.status != "cancelled")


def left_behind(
    record: dict[str, Any],
    *,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    now: datetime | None = None,
) -> LeftBehind:
    run_id, task_id = str(record["run_id"]), str(record["task_id"])
    now = now or datetime.now(timezone.utc)
    files: list[FileChange] | None = None
    note: str | None = None
    worktree = record.get("worktree_path")
    if not worktree:
        note = "no worktree was created"
    elif not Path(worktree).is_dir():
        note = f"the worktree {worktree} no longer exists"
    else:
        try:
            files = [FileChange(*row) for row in tc.change_sizes(Path(worktree))]
        except tc.CheckError as exc:
            note = f"the worktree could not be read ({exc})"
    agents = [agent_left(role, run_id, task_id, record, Path(runner_script), now)
              for role in ROLE_NAMES]
    spent = round(sum(a.cost_usd for a in agents if a.cost_usd is not None), 4)
    return LeftBehind(run_id, task_id, str(record.get("status")), stage_reached(record),
                      str(record.get("base_commit") or ""), files, note, agents, spent)


def _size(change: FileChange) -> str:
    if change.added is None:
        return "binary or large"
    parts = [f"+{change.added}"] if change.added else []
    if change.removed:
        parts.append(f"-{change.removed}")
    return " ".join(parts) or "no line changes"


def _who(left: LeftBehind) -> str:
    """Who was working: the stage's agent, or the orchestrator's checks once it had ended."""
    role = STAGE_ROLE.get(left.stage or "")
    if role is None:
        return ""
    agent = next((a for a in left.agents if a.role == role), None)
    if agent is not None and agent.finished:
        return f" (the {role} had finished; the orchestrator's own checks were running)"
    return f" (the {role})"


def format_left_behind(left: LeftBehind) -> str:
    lines = [f"Left behind by {left.task_id} ({UNVERIFIED})"]
    lines.append(f"  Stopped at:  {left.stage or 'before any stage'}{_who(left)}")
    if left.files is None:
        lines.append(f"  Files:       unknown, {left.files_note}")
    elif not left.files:
        lines.append("  Files:       none changed since the base commit "
                     f"{left.base_commit[:12]}")
    else:
        lines.append(f"  Files changed since the base commit {left.base_commit[:12]} "
                     f"({len(left.files)}):")
        width = max(len(change.path) for change in left.files[:MAX_FILES_SHOWN])
        for change in left.files[:MAX_FILES_SHOWN]:
            lines.append(f"    {change.kind:<9} {change.path:<{width}}  {_size(change)}")
        if len(left.files) > MAX_FILES_SHOWN:
            lines.append(f"    ... and {len(left.files) - MAX_FILES_SHOWN} more")
    for agent in left.agents:
        lines.append(f"  {agent.line}")
        if agent.last_tests:
            tests = agent.last_tests
            result = f" ({tests.result})" if tests.result else ""
            lines.append(f"      last tests:  {tests.command} -> {tests.outcome}{result}")
        if agent.last_message:
            lines.append(f"      last said:   {agent.last_message}")
    lines.append(f"  Spent so far: ${left.spent:.4f}")
    return "\n".join(lines)