"""A spending limit for a whole run (M4.6).

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
run limit, as before M4.6.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import runner_adapter
from orchestrator.execution.roles import ROLE_NAMES
from orchestrator.monitoring.progress import agent_run_id, attempt_of, progress_path, read_progress
from orchestrator.records.run_registry import run_directory, utc_now, write_json_atomic
from orchestrator.records.task_registry import list_tasks

BUDGET_FILE_NAME = "budget.json"
MAX_BUDGET_USD = 1000.0


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


def budget_problem(runs_dir: Path, run_id: str, role: str, cap_usd: float,
                   runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> str | None:
    """Why the next agent may not start, or None when it may (or there is no budget)."""
    limit = load_budget(runs_dir, run_id)
    if limit is None:
        return None
    spent = run_spent(runs_dir, run_id, runner_script)
    if spent + cap_usd <= limit + 1e-9:
        return None
    return (f"Run budget: ${spent:.4f} of ${limit:.2f} is spent, and the {role} may spend up "
            f"to ${cap_usd:.2f}, which could go over it, so it was not started. Raise the "
            f"budget (python -m orchestrator budget {run_id} --set <amount>) to go on.")


def format_budget(runs_dir: Path, run_id: str,
                  runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> str | None:
    """One line for ``status``, or None when the run has no budget."""
    limit = load_budget(runs_dir, run_id)
    if limit is None:
        return None
    spent = run_spent(runs_dir, run_id, runner_script)
    return f"Budget: ${spent:.4f} of ${limit:.2f} spent on tasks, ${max(limit - spent, 0):.4f} left"