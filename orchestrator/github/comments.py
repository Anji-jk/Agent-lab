"""What the comments on the task Issues and the parent Issue say (M5.4).

A task's Issue gets one comment each time one of its attempts ends (done,
failed, needs review, cancelled): which attempt it was, what it cost and how
long it took, what each agent did, what the tests showed, and what the tester
and reviewer concluded; why it did not pass; for a done task, its commit. The
parent Issue gets the run report (M4.8) when the plan completes or stops.

Everything here is read from the records; secrets are masked, and long notes
are cut. A hidden key at the end of each comment names what it is about, and the
sync keeps the keys it has posted, so nothing is posted twice.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from orchestrator.execution.roles import ROLE_NAMES
from orchestrator.monitoring.event_feed import mask_secrets
from orchestrator.monitoring.progress import attempt_of

if TYPE_CHECKING:
    from orchestrator.scheduling.left_behind import LeftBehind

FINAL_STATUSES = ("done", "failed", "needs_review", "cancelled")
STATUS_WORDS = {"done": "done", "failed": "failed", "needs_review": "needs review",
                "cancelled": "cancelled"}
MAX_ITEMS = 5
MAX_FILES = 15
ITEM_CHARS = 300


def comment_key(record: dict[str, Any]) -> str | None:
    """Which ending of which attempt a comment is about; None until the attempt has ended."""
    if record.get("status") not in FINAL_STATUSES:
        return None
    return f"attempt-{attempt_of(record)}:{record['status']}"


def _clip(text: Any) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= ITEM_CHARS else text[: ITEM_CHARS - 1] + "…"


def _items(values: Any) -> list[str]:
    values = [v for v in (values or []) if str(v).strip()]
    shown = [f"- {_clip(v)}" for v in values[:MAX_ITEMS]]
    if len(values) > MAX_ITEMS:
        shown.append(f"- … and {len(values) - MAX_ITEMS} more")
    return shown


def _money(value: Any) -> str:
    return f"${value:.4f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "-"


def _minutes(record: dict[str, Any]) -> str | None:
    try:
        start = datetime.fromisoformat(str(record["created_at"]))
        end = datetime.fromisoformat(str(record["updated_at"]))
    except (KeyError, ValueError):
        return None
    seconds = max((end - start).total_seconds(), 0)
    return f"{seconds:.0f} s" if seconds < 60 else f"{round(seconds / 60)} min"


def _attempt_line(record: dict[str, Any]) -> str:
    attempt = attempt_of(record)
    if attempt == 1:
        return "attempt 1"
    how = (f"continued from {record['continued_at']}" if record.get("continued_at")
           else "a retry, told what went wrong")
    return f"attempt {attempt}, {how}"


def _agent_rows(record: dict[str, Any]) -> list[str]:
    agents = record.get("agents") or {}
    kept = record.get("reused_agents") or {}
    rows = ["| Agent | Result | Turns | Cost |", "| --- | --- | --- | --- |"]
    for role in ROLE_NAMES:
        if role in agents:
            agent = agents[role] or {}
            result = str(agent.get("status") or agent.get("outcome") or "-").replace("_", " ")
            rows.append(f"| {role} | {result} | {agent.get('num_turns', '-')} "
                        f"| {_money(agent.get('total_cost_usd'))} |")
        elif role in kept:
            rows.append(f"| {role} | kept from the stopped attempt | - | - |")
        else:
            rows.append(f"| {role} | not run | - | - |")
    return rows


def _tests_line(findings: dict[str, Any]) -> str | None:
    def said(value: Any, timed_out: Any, what: str) -> str | None:
        if timed_out:
            return f"{what} timed out"
        if value is True:
            return f"{what} passed"
        if value is False:
            return f"{what} failed"
        return None

    parts = [p for p in (said(findings.get("suite_passed"), findings.get("suite_timed_out"),
                              "the full suite"),
                         said(findings.get("named_passed"), findings.get("named_timed_out"),
                              "the tester's tests")) if p]
    return ("**Tests:** " + "; ".join(parts) + ".") if parts else None


def _left_lines(left: LeftBehind) -> list[str]:
    """What a cancelled attempt had done before it was stopped, from its worktree and agents."""
    lines = ["", "**What it had done before it was stopped** (not checked or reviewed):",
             f"- Stopped at: {left.stage or 'before any stage'}"]
    if left.files is None:
        lines.append(f"- Files: unknown, {left.files_note}")
    elif not left.files:
        lines.append("- Files: none changed since the base commit")
    else:
        lines.append(f"- Files changed since the base commit `{left.base_commit[:12]}` "
                     f"({len(left.files)}):")
        for change in left.files[:MAX_FILES]:
            size = ("binary or large" if change.added is None
                    else f"+{change.added or 0} -{change.removed or 0}")
            lines.append(f"  - {change.kind} `{change.path}` ({size})")
        if len(left.files) > MAX_FILES:
            lines.append(f"  - … and {len(left.files) - MAX_FILES} more")
    for agent in left.agents:
        if agent.cost_usd is None and not agent.last_message and not agent.last_tests:
            continue  # never started
        lines.append(f"- {_clip(agent.line)}")
        if agent.last_tests:
            tests = agent.last_tests
            result = f" ({_clip(tests.result)})" if tests.result else ""
            lines.append(f"  - last tests: `{tests.command}` → {tests.outcome}{result}")
        if agent.last_message:
            lines.append(f"  - last said: {_clip(agent.last_message)}")
    lines.append(f"- Spent before the stop: {_money(left.spent)}")
    return lines


def task_comment(run_id: str, task_id: str, record: dict[str, Any],
                 left: LeftBehind | None = None) -> str:
    """The comment for the attempt that just ended.

    ``left``: for a cancelled attempt, what it had done up to the stop (M5.5).
    """
    status = str(record.get("status"))
    cost = record.get("cost_usd")
    if cost is None and left is not None:
        cost = left.spent  # a cancelled attempt has no total of its own
    head = [f"**{task_id}: {STATUS_WORDS.get(status, status)}** ({_attempt_line(record)})",
            f"Cost {_money(cost)}"]
    took = _minutes(record)
    if took:
        head.append(took)
    # A cancelled attempt's agents never reported back; what they did is in its own section.
    lines = [" · ".join(head), *([] if left is not None else ["", *_agent_rows(record)])]
    findings = record.get("findings") or {}
    tests = _tests_line(findings)
    if tests:
        lines += ["", tests]
    verdict = findings.get("reviewer_verdict")
    if verdict or findings.get("reviewer_problems") or findings.get("reviewer_notes"):
        lines += ["", f"**Reviewer:** {verdict or 'no verdict'}."]
        problems = _items(findings.get("reviewer_problems"))
        if problems:
            lines += ["Problems:", *problems]
        notes = _items(findings.get("reviewer_notes"))
        if notes:
            lines += ["Notes:", *notes]
    tester_notes = _items(findings.get("tester_notes"))
    if tester_notes:
        lines += ["", "**Tester's notes:**", *tester_notes]
    if left is not None:
        lines += _left_lines(left)
    if status != "done":
        why = _items(record.get("reasons"))
        if why:
            lines += ["", "**Why:**", *why]
    elif record.get("commit"):
        merge = record.get("merge") or {}
        into = ""
        if merge.get("branch") and not merge.get("problem"):
            how = {"fast-forward": "a fast-forward", "merge": "a merge, with the full suite "
                   "passing on the result", "already": "already there"}.get(
                       str(merge.get("kind")), str(merge.get("kind")))
            into = f" Merged into the run's branch `{merge['branch']}` ({how})."
        lines += ["", f"Committed `{str(record['commit'])[:12]}` on `{record.get('branch')}` "
                      f"(local only; nothing was pushed).{into}"]
    lines += ["", f"<!-- agent-lab comment: run={run_id} task={task_id} {comment_key(record)} -->"]
    return mask_secrets("\n".join(lines)) + "\n"


def report_comment(run_id: str, report_text: str, key: str) -> str:
    """The run report as the parent Issue's comment."""
    return mask_secrets(report_text.rstrip()) + f"\n\n<!-- agent-lab report: run={run_id} {key} -->\n"
