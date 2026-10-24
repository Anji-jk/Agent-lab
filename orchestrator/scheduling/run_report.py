"""One summary of a run (M4.8): what was done, on which branch, for how much.

``status`` shows a run while it works; this is what you read afterwards. It
answers: where is the result (the run's branch and its commit), what did it
cost (the planner and every attempt of every task), how long did it take,
which tasks finished and which did not and why, and what to do next.

It only reads (the plan, the task records, the stopped attempts' records under
``attempt-<n>/``, and the planner's progress file), so it can be made at any
time, while a run works or long after. ``run-plan`` saves it to
``runs/<run>/report.md`` when it stops; ``report <run>`` shows it, and with
``--save`` saves it too. Secrets in the text (reasons can quote command output)
are masked.

The run's branch is the branch of the task that finished last: since M4.2 every
task starts from the newest finished work, so that branch holds all of it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.execution import runner_adapter
from orchestrator.execution.budget import format_budget, task_spent
from orchestrator.monitoring.event_feed import mask_secrets
from orchestrator.monitoring.progress import attempt_of, planner_view
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.records.run_registry import run_directory
from orchestrator.records.task_registry import TASK_RECORD_NAME, list_tasks, task_directory
from orchestrator.scheduling.plan_progress import CommandLine, plan_progress
from orchestrator.scheduling.run_plan import next_action

REPORT_FILE_NAME = "report.md"
REASON_CHARS = 200


@dataclass(frozen=True, slots=True)
class AttemptReport:
    attempt: int
    status: str
    cost_usd: float | None
    seconds: float | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class TaskReport:
    task_id: str
    title: str
    state: str                     # from plan progress: done, failed, ready, blocked...
    detail: str | None             # why, for anything that is not done
    cost_usd: float                # every attempt
    seconds: float | None          # working time over every attempt
    commit: str | None
    branch: str | None
    attempts: tuple[AttemptReport, ...] = ()


@dataclass
class RunReport:
    run_id: str
    issue: dict[str, Any]
    base_commit: str
    plan_state: str
    tasks: list[TaskReport] = field(default_factory=list)
    planner_cost_usd: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    branch: str | None = None      # holds all finished work
    head_commit: str | None = None
    next_step: str = ""
    budget: str | None = None

    @property
    def tasks_cost_usd(self) -> float:
        return round(sum(t.cost_usd for t in self.tasks), 6)

    @property
    def total_cost_usd(self) -> float:
        return round(self.tasks_cost_usd + (self.planner_cost_usd or 0.0), 6)

    @property
    def working_seconds(self) -> float:
        return sum(t.seconds or 0.0 for t in self.tasks)


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(record: dict[str, Any]) -> float | None:
    start, end = _time(record.get("created_at")), _time(record.get("updated_at"))
    if start is None or end is None:
        return None
    return max((end - start).total_seconds(), 0.0)


def _clip(text: Any) -> str | None:
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= REASON_CHARS else text[: REASON_CHARS - 1] + "…"


def _archived(runs_dir: Path, run_id: str, task_id: str, attempt: int) -> dict[str, Any] | None:
    path = task_directory(runs_dir, run_id, task_id) / f"attempt-{attempt}" / TASK_RECORD_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _attempts(runs_dir: Path, run_id: str, task_id: str, record: dict[str, Any],
              total_cost: float) -> tuple[AttemptReport, ...]:
    """Every attempt, oldest first: the earlier ones from their summaries and archived records."""
    found: list[AttemptReport] = []
    earlier_cost = 0.0
    for summary in record.get("previous_attempts") or []:
        if not isinstance(summary, dict):
            continue
        given = summary.get("attempt")
        number = given if isinstance(given, int) and not isinstance(given, bool) else len(found) + 1
        archived = _archived(runs_dir, run_id, task_id, number)
        cost = summary.get("cost_usd") if isinstance(summary.get("cost_usd"), (int, float)) else None
        earlier_cost += cost or 0.0
        found.append(AttemptReport(number, str(summary.get("status")), cost,
                                   _seconds(archived) if archived else None,
                                   _clip((summary.get("reasons") or [None])[0])))
    cost = record.get("cost_usd")
    if not isinstance(cost, (int, float)):  # still running, or stopped: what its agents spent
        cost = max(round(total_cost - earlier_cost, 6), 0.0)
    found.append(AttemptReport(attempt_of(record), record["status"], cost, _seconds(record),
                               _clip((record.get("reasons") or [None])[0])))
    return tuple(found)


def _detail(progress_task: Any, states: dict[str, str]) -> str | None:
    if progress_task.state == "done":
        return None
    if progress_task.state == "blocked":
        return "blocked by " + ", ".join(f"{b} ({states.get(b, '?')})" for b in progress_task.blocked_by)
    if progress_task.state == "waiting":
        return "waiting for " + ", ".join(progress_task.waiting_for)
    if progress_task.state == "interrupted":
        return f"stopped while {progress_task.stage}: {progress_task.reason}"
    if progress_task.state == "running":
        return f"running ({progress_task.stage})"
    if progress_task.state == "ready":
        return "not started; can start now"
    return _clip(progress_task.reason)


def build_report(
    run_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    get_command_line: CommandLine | None = None,
) -> RunReport:
    plan = load_task_plan(plans_dir, run_id)
    progress = plan_progress(plan, runs_dir, run_id, get_command_line=get_command_line,
                             runner_script=Path(runner_script))
    records = list_tasks(runs_dir, run_id)
    planner = planner_view(run_id, runner_script=Path(runner_script))
    report = RunReport(run_id, dict(plan["issue"]), plan["config"]["base_commit"],
                       progress.state,
                       planner_cost_usd=planner.cost_usd if planner is not None else None,
                       budget=format_budget(runs_dir, run_id, Path(runner_script)))

    states = {t.task_id: t.state for t in progress.tasks}
    starts: list[datetime] = []
    ends: list[datetime] = []
    last_done: tuple[datetime, dict[str, Any]] | None = None
    for task in progress.tasks:
        record = records.get(task.task_id)
        if record is None:
            report.tasks.append(TaskReport(task.task_id, task.title, task.state,
                                           _detail(task, states), 0.0, None, None, None))
            continue
        spent = task_spent(run_id, task.task_id, record, Path(runner_script))
        attempts = _attempts(runs_dir, run_id, task.task_id, record, spent)
        seconds = [a.seconds for a in attempts if a.seconds is not None]
        report.tasks.append(TaskReport(
            task.task_id, task.title, task.state, _detail(task, states), spent,
            sum(seconds) if seconds else None, record.get("commit"), record.get("branch"),
            attempts))
        for number in range(1, attempt_of(record)):
            archived = _archived(runs_dir, run_id, task.task_id, number)
            if archived and _time(archived.get("created_at")):
                starts.append(_time(archived["created_at"]))  # type: ignore[arg-type]
        for key, bucket in (("created_at", starts), ("updated_at", ends)):
            moment = _time(record.get(key))
            if moment is not None:
                bucket.append(moment)
        finished = _time(record.get("updated_at"))
        if task.state == "done" and record.get("commit") and finished is not None:
            if last_done is None or finished >= last_done[0]:
                last_done = (finished, record)

    if starts:
        report.started_at = min(starts).isoformat()
    if ends:
        report.finished_at = max(ends).isoformat()
    if last_done is not None:
        report.branch, report.head_commit = last_done[1].get("branch"), last_done[1].get("commit")

    decision = next_action(progress, records)
    if decision is None:
        branch = report.branch or "the run's branch"
        report.next_step = (f"Review the work: git log --oneline {report.base_commit[:12]}..{branch}. "
                            "Nothing was pushed or merged; that is for you to do.")
    elif isinstance(decision, str):
        report.next_step = decision
    else:
        action, task_id = decision
        verb = {"run": "start", "retry": "retry", "continue": "continue"}[action]
        report.next_step = f"python -m orchestrator run-plan {run_id} would {verb} {task_id} next."
    return report


# --- showing it -------------------------------------------------------------------------------


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def _money(value: float | None) -> str:
    return f"${value:.4f}" if isinstance(value, (int, float)) else "not recorded"


def format_report(report: RunReport) -> str:
    issue = report.issue
    done = sum(1 for t in report.tasks if t.state == "done")
    lines = [f"# Run report: {report.run_id}", ""]
    lines.append(f"Issue:   {issue.get('repository', '?')}#{issue.get('number', '?')} "
                 f"{issue.get('title', '')}".rstrip())
    lines.append(f"Plan:    {report.plan_state}, {done} of {len(report.tasks)} tasks done")
    if report.branch:
        lines.append(f"Branch:  {report.branch} at {str(report.head_commit)[:12]} "
                     f"(the run started from {report.base_commit[:12]})")
    else:
        lines.append(f"Branch:  none yet; no task has finished (base {report.base_commit[:12]})")
    planner = _money(report.planner_cost_usd)
    lines.append(f"Cost:    ${report.total_cost_usd:.4f} "
                 f"(planner {planner}, tasks ${report.tasks_cost_usd:.4f})")
    if report.started_at and report.finished_at:
        wall = (_time(report.finished_at) - _time(report.started_at)).total_seconds()  # type: ignore[operator]
        lines.append(f"Time:    {_duration(wall)} from the first task's start to the last "
                     f"update; {_duration(report.working_seconds)} of task work")
    if report.budget:
        lines.append(report.budget)

    lines += ["", "## Tasks", ""]
    for task in report.tasks:
        count = len(task.attempts)
        tries = f"{count} attempt{'s' if count != 1 else ''}" if count else "not run"
        commit = str(task.commit)[:12] if task.commit else "-"
        lines.append(f"{task.task_id:<4} {task.state:<12} {tries:<11} ${task.cost_usd:.4f}  "
                     f"{_duration(task.seconds) if task.attempts else '-':>8}  {commit:<12}  {task.title}")
        if task.detail:
            lines.append(f"       {task.detail}")
        if count > 1:
            for attempt in task.attempts:
                why = f" - {attempt.reason}" if attempt.reason and attempt.status != "done" else ""
                lines.append(f"       attempt {attempt.attempt}: {attempt.status}, "
                             f"{_money(attempt.cost_usd)}, {_duration(attempt.seconds)}{why}")

    lines += ["", "## Next", "", report.next_step]
    return mask_secrets("\n".join(lines)) + "\n"


def report_path(runs_dir: Path, run_id: str) -> Path:
    return run_directory(Path(runs_dir), run_id) / REPORT_FILE_NAME


def save_report(runs_dir: Path, run_id: str, text: str) -> Path:
    path = report_path(runs_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)  # a reader never sees half a report
    return path
