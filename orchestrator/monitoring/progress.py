"""Read what every agent of a run is doing, for the ``status`` command.

The runner keeps one ``progress.json`` per agent (see ``scripts/claude_runner.py``);
the task records say which stage each task is at. This module only reads them,
so ``status`` can never disturb a running task.

An agent whose heartbeat is older than ``STALE_SECONDS`` while it still claims
to be running is flagged: its runner has most likely died. The runner writes a
heartbeat at least every 5 seconds, so 30 seconds is six missed beats.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.execution import runner_adapter
from orchestrator.execution.roles import ROLE_NAMES
from orchestrator.records.task_registry import list_tasks

PROGRESS_FILE_NAME = "progress.json"
STALE_SECONDS = 30

# Which agent works on a task in each stage.
STAGE_ROLE = {"building": "builder", "testing": "tester", "reviewing": "reviewer"}


def read_progress(path: Path) -> dict[str, Any] | None:
    """A progress file, or None if it is missing, half-written or not an object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def agent_run_id(run_id: str, task_id: str, role: str) -> str:
    """The runner's ID for one agent of a task; ``task_runner`` names them this way."""
    return f"{run_id}-{task_id}-{role}"


def progress_path(run_id: str, runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> Path:
    return runner_adapter.runner_run_directory(run_id, runner_script) / PROGRESS_FILE_NAME


def _seconds_since(stamp: object, now: datetime) -> float | None:
    if not isinstance(stamp, str):
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds())


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True, slots=True)
class AgentView:
    role: str
    state: str  # "not started", "starting", "running", "finished"
    turns: int
    tokens_read: int  # new input plus cached input: what the agent actually read
    tokens_written: int
    last_tool: str | None
    heartbeat_age: float | None
    cost_usd: float | None
    status: str | None  # the runner's final status, once finished
    from_record: bool = False  # no progress file: taken from the task record's summary

    @property
    def stale(self) -> bool:
        return (
            self.state in ("starting", "running")
            and self.heartbeat_age is not None
            and self.heartbeat_age > STALE_SECONDS
        )

    def describe(self) -> str:
        if self.state == "not started":
            return f"{self.role}: not started"
        parts = [f"{self.role}: {self.state if self.status is None else self.status}"]
        parts.append(f"{self.turns} turns")
        if not self.from_record:  # the record has no cached-token counts, so its totals mislead
            parts.append(f"{_short(self.tokens_read)} tokens read, {_short(self.tokens_written)} written")
        if self.last_tool:
            parts.append(f"last tool {self.last_tool}")
        if self.cost_usd is not None:
            parts.append(f"${self.cost_usd:.4f}")
        if self.state != "finished" and self.heartbeat_age is not None:
            parts.append(f"heartbeat {self.heartbeat_age:.0f}s ago")
        line = ", ".join(parts)
        if self.stale:
            line += "  <- no heartbeat: stopped?"
        return line


def _short(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def agent_view(role: str, progress: dict[str, Any] | None, now: datetime) -> AgentView:
    if progress is None:
        return AgentView(role, "not started", 0, 0, 0, None, None, None, None)
    tokens_read = sum(
        _number(progress.get(field))
        for field in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    cost = progress.get("total_cost_usd")
    state = progress.get("state")
    status = progress.get("status")
    last_tool = progress.get("last_tool")
    return AgentView(
        role=role,
        state=state if state in ("starting", "running", "finished") else "running",
        turns=_number(progress.get("turns")),
        tokens_read=tokens_read,
        tokens_written=_number(progress.get("output_tokens")),
        last_tool=last_tool if isinstance(last_tool, str) else None,
        heartbeat_age=_seconds_since(progress.get("heartbeat_at"), now),
        cost_usd=cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        status=status if isinstance(status, str) else None,
    )


def agent_view_from_record(role: str, summary: object) -> AgentView | None:
    """A finished agent as the task record summarised it, for runs older than progress files."""
    if not isinstance(summary, dict):
        return None
    status = summary.get("status")
    cost = summary.get("total_cost_usd")
    return AgentView(
        role=role,
        state="finished",
        turns=_number(summary.get("num_turns")),
        tokens_read=0,
        tokens_written=0,
        last_tool=None,
        heartbeat_age=None,
        cost_usd=cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        status=status if isinstance(status, str) else "finished",
        from_record=True,
    )


def task_agent_view(run_id: str, task_id: str, role: str, record: dict[str, Any],
                runner_script: Path, now: datetime) -> AgentView:
    """Live progress if the runner left a file, else the record's summary, else not started."""
    progress = read_progress(progress_path(agent_run_id(run_id, task_id, role), runner_script))
    if progress is not None:
        return agent_view(role, progress, now)
    agents = record.get("agents")
    summary = agents.get(role) if isinstance(agents, dict) else None
    return agent_view_from_record(role, summary) or agent_view(role, None, now)


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: str
    status: str
    title: str
    agents: tuple[AgentView, ...]

    @property
    def active_role(self) -> str | None:
        return STAGE_ROLE.get(self.status)

    @property
    def spent_usd(self) -> float:
        return round(sum(a.cost_usd or 0.0 for a in self.agents), 6)


def run_status(
    runs_dir: Path,
    run_id: str,
    *,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    now: datetime | None = None,
) -> list[TaskView]:
    """Every task of a run with each of its agents, read from disk only."""
    now = now or datetime.now(timezone.utc)
    views: list[TaskView] = []
    for task_id, record in list_tasks(runs_dir, run_id).items():
        agents = tuple(
            task_agent_view(run_id, task_id, role, record, runner_script, now) for role in ROLE_NAMES
        )
        views.append(TaskView(task_id, record["status"], str(record.get("title", "")), agents))
    return views


def planner_view(
    run_id: str,
    *,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    now: datetime | None = None,
) -> AgentView | None:
    """The planner's progress for a run, if it left a progress file."""
    progress = read_progress(progress_path(f"{run_id}-plan", runner_script))
    if progress is None:
        return None
    return agent_view("planner", progress, now or datetime.now(timezone.utc))


def format_status(run_id: str, tasks: list[TaskView], planner: AgentView | None = None) -> str:
    lines = [f"Run {run_id}"]
    if planner is not None:
        lines.append(f"  {planner.describe()}")
    if not tasks:
        lines.append("  No task has been started yet.")
    for task in tasks:
        lines.append(f"\n  {task.task_id}  {task.status:<12}  {task.title}")
        for agent in task.agents:
            marker = "->" if agent.role == task.active_role else "  "
            lines.append(f"    {marker} {agent.describe()}")
        if task.spent_usd:
            lines.append(f"       spent so far: ${task.spent_usd:.4f}")
    return "\n".join(lines)


def stale_agents(tasks: list[TaskView]) -> list[str]:
    """``T1 builder``-style names of agents that look dead."""
    return [f"{t.task_id} {a.role}" for t in tasks for a in t.agents if a.stale]

