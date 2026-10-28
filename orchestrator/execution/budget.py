"""A spending limit for a whole run.

Every agent already has its own cap, taken from its task's size, so no single
agent can run away. What those caps do not stop is many sensible tasks, and
their retries, adding up to a surprise once ``run-plan`` works through a plan
unattended. A run budget is a total for all the run's tasks.

The rule is simple and strict: an agent starts only if what the run's tasks
have spent so far, plus that agent's whole cap, fits within the budget. An
agent that might be cut off half-way is not started, because a half-done
agent is money spent for nothing. So a run can stop with some budget left;
it never goes over.

When an agent is not started for this reason:

* before the task's first agent, the task is refused before anything is
  created, so it stays ready and nothing is spent;
* later in a task (the tester or the reviewer), the task ends ``needs_review``
  marked ``budget_stop``; its checkpoint is kept, so after raising the budget
  ``run-task --continue`` takes it up at that stage.

Spent means what the run's task agents have cost, across every attempt,
read from the agents' progress files and the task records. The planner is
not counted: it ran before the budget could be set. No budget file means no
run limit, as before tasks could run side by side.

Tasks side by side. Agents already running in other tasks have not spent
their whole cap yet, but they may. So the rule counts them too: what is spent,
plus what each running agent of another task may still spend (its cap minus what
it has cost so far, so nothing is counted twice), plus the new agent's cap, must
fit. To keep two tasks from both passing at the same instant, the check and the
agent's reservation happen together under a lock, and the reservation is written
into the task's record (``reservation``) until the agent ends. A reservation of a
task whose process has gone is ignored: nothing is running on it.

An agent refused because of other tasks' reservations is refused only for now:
those caps are freed as their agents end. The task stops like any budget stop,
and ``run-plan`` again goes on from the stage it reached.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import runner_adapter
from orchestrator.execution.roles import ROLE_NAMES
from orchestrator.core.attempts import agent_run_id, attempt_of
from orchestrator.monitoring.progress import progress_path, read_progress
from orchestrator.records.run_registry import (
    RunRegistryError, locked, run_directory, utc_now, write_json_atomic,
)
from orchestrator.records.task_registry import list_tasks, update_task

BUDGET_FILE_NAME = "budget.json"
MAX_BUDGET_USD = 1000.0
LOCK_FOLDER = "budget-lock"
LOCK_SECONDS = 60
RESERVATION_FIELD = "reservation"
IsAlive = Callable[[str, dict[str, Any]], "tuple[bool, str]"]


class BudgetExhausted(ExecutionRefused):
    """The next agent's cap does not fit in what is left of the run's budget."""


def budget_path(runs_dir: Path, run_id: str) -> Path:
    return run_directory(Path(runs_dir), run_id) / BUDGET_FILE_NAME


def load_budget(runs_dir: Path, run_id: str) -> float | None:
    """The run's limit in USD, or None when it has none.

    An unreadable budget file is an error, not "no limit": spending without a
    limit because a file was damaged is the mistake this exists to prevent.
    """
    path = budget_path(runs_dir, run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExecutionRefused(f"The run budget {path} could not be read ({type(exc).__name__}).") from None
    try:
        limit = json.loads(text).get("limit_usd")
    except (ValueError, AttributeError):
        limit = None
    if not isinstance(limit, (int, float)) or not _is_amount(limit):
        raise ExecutionRefused(f"The run budget {path} is damaged; set it again with the budget command.")
    return float(limit)


def _is_amount(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= MAX_BUDGET_USD


def set_budget(runs_dir: Path, run_id: str, limit_usd: float) -> Path:
    if not _is_amount(limit_usd):
        raise ValueError(f"A run budget must be more than $0 and at most ${MAX_BUDGET_USD:g}.")
    path = budget_path(runs_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"limit_usd": round(float(limit_usd), 4), "set_at": utc_now()})
    return path


def remove_budget(runs_dir: Path, run_id: str) -> bool:
    path = budget_path(runs_dir, run_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def _cost(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _agent_cost(run_id: str, task_id: str, role: str, attempt: int, runner_script: Path) -> float | None:
    progress = read_progress(progress_path(agent_run_id(run_id, task_id, role, attempt), runner_script))
    return _cost(progress.get("total_cost_usd")) if progress is not None else None


def task_spent(run_id: str, task_id: str, record: dict[str, Any],
               runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> float:
    """What one task's agents have cost, over all its attempts."""
    current = attempt_of(record)
    earlier = {p.get("attempt"): p for p in record.get("previous_attempts") or [] if isinstance(p, dict)}
    total = 0.0
    for attempt in range(1, current + 1):
        costs = {role: _agent_cost(run_id, task_id, role, attempt, Path(runner_script))
                 for role in ROLE_NAMES}
        if attempt == current:
            # No progress file (older runs): the record once the task has ended, or its
            # checkpoint while it runs. Both hold only this attempt's own agents.
            agents = record.get("agents") or (record.get("checkpoint") or {}).get("agents") or {}
            for role, cost in costs.items():
                if cost is None:
                    costs[role] = _cost((agents.get(role) or {}).get("total_cost_usd"))
        elif all(cost is None for cost in costs.values()):
            costs = {"recorded": _cost((earlier.get(attempt) or {}).get("cost_usd"))}
        total += sum(cost for cost in costs.values() if cost is not None)
    return round(total, 6)


def run_spent(runs_dir: Path, run_id: str,
              runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> float:
    """What every task of the run has cost so far."""
    return round(sum(task_spent(run_id, task_id, record, runner_script)
                     for task_id, record in list_tasks(Path(runs_dir), run_id).items()), 6)


def reserved_elsewhere(runs_dir: Path, run_id: str, task_id: str, is_alive: IsAlive,
                       runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> list[tuple[str, str, float]]:
    """(task, role, USD still to spend) for each agent running in another task of the run."""
    found = []
    for other, record in list_tasks(Path(runs_dir), run_id).items():
        reservation = record.get(RESERVATION_FIELD)
        if other == task_id or not isinstance(reservation, dict):
            continue
        cap, role = _cost(reservation.get("cap_usd")), reservation.get("role")
        if cap is None or not isinstance(role, str) or not is_alive(other, record)[0]:
            continue
        attempt = reservation.get("attempt")
        spent = _agent_cost(run_id, other, role, attempt if isinstance(attempt, int) else attempt_of(record),
                            Path(runner_script)) or 0.0
        if cap - spent > 1e-9:
            found.append((other, role, round(cap - spent, 6)))
    return found


def budget_problem(runs_dir: Path, run_id: str, role: str, cap_usd: float,
                   runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
                   task_id: str | None = None, is_alive: IsAlive | None = None) -> str | None:
    """Why the next agent may not start, or None when it may (or there is no budget).

    With ``task_id`` and ``is_alive``, what agents running in the run's other tasks may
    still spend is counted too.
    """
    limit = load_budget(runs_dir, run_id)
    if limit is None:
        return None
    spent = run_spent(runs_dir, run_id, runner_script)
    running = (reserved_elsewhere(runs_dir, run_id, task_id, is_alive, runner_script)
               if task_id is not None and is_alive is not None else [])
    kept = sum(amount for _, _, amount in running)
    if spent + kept + cap_usd <= limit + 1e-9:
        return None
    if not running:
        return (f"Run budget: ${spent:.4f} of ${limit:.2f} is spent, and the {role} may spend up "
                f"to ${cap_usd:.2f}, which could go over it, so it was not started. Raise the "
                f"budget (python -m orchestrator budget {run_id} --set <amount>) to go on.")
    who = ", ".join(f"{other}'s {name}" for other, name, _ in running)
    return (f"Run budget: ${spent:.4f} of ${limit:.2f} is spent, ${kept:.4f} more may still be "
            f"spent by agents running in other tasks ({who}), and the {role} may spend up to "
            f"${cap_usd:.2f}, which could go over it, so it was not started. Their unused caps "
            "are freed as they finish: run run-plan again then, or raise the budget "
            f"(python -m orchestrator budget {run_id} --set <amount>) to go on.")


def reserve_agent(runs_dir: Path, run_id: str, task_id: str, role: str, attempt: int,
                  cap_usd: float, is_alive: IsAlive,
                  runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> str | None:
    """Check the budget and, if the agent fits, reserve its cap; a reason when it does not.

    The check and the reservation are one step under a lock, so two tasks starting an
    agent at the same moment cannot both count on the same money. With no budget there is
    nothing to reserve. ``release_agent`` ends the reservation when the agent has ended.
    """
    if load_budget(runs_dir, run_id) is None:
        return None
    with locked(run_directory(Path(runs_dir), run_id) / LOCK_FOLDER, timeout=LOCK_SECONDS):
        problem = budget_problem(runs_dir, run_id, role, cap_usd, runner_script, task_id, is_alive)
        if problem is None:
            update_task(Path(runs_dir), run_id, task_id, {RESERVATION_FIELD: {
                "role": role, "cap_usd": cap_usd, "attempt": attempt, "at": utc_now()}})
        return problem


def release_agent(runs_dir: Path, run_id: str, task_id: str) -> None:
    """End a task's reservation (the agent ended, or never started)."""
    try:
        update_task(Path(runs_dir), run_id, task_id, {RESERVATION_FIELD: None})
    except RunRegistryError:  # the record is gone: nothing is left to release
        return


def format_budget(runs_dir: Path, run_id: str,
                  runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> str | None:
    """One line for ``status``, or None when the run has no budget."""
    limit = load_budget(runs_dir, run_id)
    if limit is None:
        return None
    spent = run_spent(runs_dir, run_id, runner_script)
    return f"Budget: ${spent:.4f} of ${limit:.2f} spent on tasks, ${max(limit - spent, 0):.4f} left"
