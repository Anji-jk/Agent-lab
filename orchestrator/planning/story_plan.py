"""A Jira story's plan: the Jira fields, the planner's choices, and their validation.

A story plan has one task per open sub-task, in Jira's order. Jira-owned fields are copied
unchanged; the planner supplies implementation details and may add dependencies, but cannot
remove Jira links. Added dependencies are recorded as Jira links to write after approval.

Nothing here reads or writes Jira, Git or files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from orchestrator.jira.story import Story, WorkItem
from orchestrator.planning.plan_file import MAX_TEXT, MAX_TITLE, PlanValidationError, validate_tasks
from orchestrator.planning.profiles import profile_for

WAITING, APPROVED = "waiting", "approved"
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?")
PLANNER_FIELDS = (
    "files", "depends_on", "dependency_reasons", "parallel_safe", "complexity",
    "expected_outcome", "verification", "doc_target", "context",
)


def open_subtasks(story: Story) -> list[WorkItem]:
    return [item for item in story.subtasks if not item.done]


def criteria_list(text: str | None) -> list[str]:
    """Split Jira criteria by line, removing list markers but preserving the words."""
    found = []
    for line in (text or "").splitlines():
        line = _BULLET.sub("", line).strip()
        if line:
            found.append(line)
    return found


def _cut(text: str, limit: int) -> str:
    text = (text or "").strip()
    marker = "\n... (cut short)"
    return text if len(text) <= limit else text[:limit - len(marker)] + marker


def jira_tasks(story: Story) -> list[dict[str, Any]]:
    """Prepare Jira-owned fields before calling the planner."""
    items = open_subtasks(story)
    if not items:
        raise PlanValidationError(f"{story.story.key} has no open sub-tasks to plan.")
    missing = [item.key for item in items if not criteria_list(item.acceptance_criteria)]
    if missing:
        raise PlanValidationError(
            "No acceptance criteria in " + ", ".join(missing) + ". Add them in Jira (see jira "
            "check); the planner was not started.")
    if len(items) > 30:
        raise PlanValidationError("A story plan may have at most 30 open sub-tasks.")
    return [{
        "id": f"T{number}",
        "jira_key": item.key,
        "title": _cut(item.summary, MAX_TITLE),
        "description": _cut(item.description or item.summary, MAX_TEXT),
        "acceptance_criteria": criteria_list(item.acceptance_criteria),
    } for number, item in enumerate(items, 1)]


def _jira_reason(item: WorkItem, blocker: str) -> str:
    return f"Jira: {item.key} is blocked by {blocker}"


def _inside(path: str, folder: str) -> bool:
    return PurePosixPath(path.replace("\\", "/")).as_posix().startswith(folder + "/")


@dataclass
class _Review:
    """What checking the planner's answer found for one story."""
    story: Story
    by_key: dict[str, dict[str, Any]]
    done: set[str]
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    jira_links: list[dict[str, str]] = field(default_factory=list)


def _answers(review: _Review, raw_tasks: list[Any]) -> dict[str, dict[str, Any]]:
    """Index the planner's entries by sub-task key, reporting entries it cannot use."""
    answers: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_tasks, 1):
        key = raw.get("key") if isinstance(raw, dict) else None
        if not isinstance(key, str) or not key:
            review.problems.append(f"Entry {index} names no sub-task.")
        elif key in answers:
            review.problems.append(f"{key} is planned twice.")
        elif key in review.done:
            review.notes.append(f"{key} is done in Jira; the planner's entry for it was left out.")
        elif key not in review.by_key:
            review.problems.append(f"{key} is not an open sub-task of {review.story.story.key}.")
        else:
            answers[key] = raw
    review.problems += [f"{key} has no entry in the planner's answer."
                        for key in review.by_key if key not in answers]
    return answers


def _dependencies(
    review: _Review,
    item: WorkItem,
    given: list[Any],
    reasons_given: dict[str, Any],
) -> dict[str, str]:
    """Merge Jira dependencies with planner-added dependencies."""
    by_key = review.by_key
    task_id = by_key[item.key]["id"]
    reasons: dict[str, str] = {}
    for blocker in (key for key in item.blocked_by if key in by_key):
        if blocker not in given:
            review.notes.append(f"{task_id} ({item.key}) waits for {by_key[blocker]['id']} "
                                f"({blocker}): a Jira link the planner left out, put back.")
        reason = reasons_given.get(blocker)
        reasons[blocker] = reason.strip() if isinstance(reason, str) and reason.strip() \
            else _jira_reason(item, blocker)
    for blocker in given:
        if not isinstance(blocker, str):
            review.problems.append(f"{item.key} has a dependency that is not a sub-task key.")
            continue
        if blocker in reasons:
            continue
        reason = reasons_given.get(blocker)
        if blocker in review.done:
            review.notes.append(f"{item.key}: its dependency {blocker} is already done in "
                                "Jira; left out.")
        elif blocker not in by_key or blocker == item.key:
            review.problems.append(f"{item.key} depends on {blocker}, which is not another "
                                   f"open sub-task of {review.story.story.key}.")
        elif not isinstance(reason, str) or not reason.strip():
            review.problems.append(f"{item.key} depends on {blocker} without a reason.")
        else:
            reasons[blocker] = reason.strip()
            review.jira_links.append({"subtask": item.key, "blocked_by": blocker,
                                      "reason": reason.strip()})
    return reasons


def _doc_target(review: _Review, key: str, target: Any, docs_dir: str | None) -> Any:
    """Keep doc targets within the configured docs folder."""
    if target and not docs_dir:
        review.notes.append(f"{key}: no docs folder in .agent-lab.toml, so its doc_target "
                            f"{target} was left out.")
        return None
    if isinstance(target, str) and target and not _inside(target, docs_dir or ""):
        review.problems.append(f"{key}: doc_target {target} is not in the docs folder "
                               f"{docs_dir}/.")
    return target


def merge_planner_output(
    story: Story,
    output: dict[str, Any],
    docs_dir: str | None,
) -> dict[str, Any]:
    """Join planner choices to Jira fields and refuse any answer that does not fit."""
    by_key = {task["jira_key"]: task for task in jira_tasks(story)}
    items = {item.key: item for item in open_subtasks(story)}
    review = _Review(story, by_key, {item.key for item in story.subtasks if item.done})
    raw_tasks = output.get("tasks")
    if not isinstance(raw_tasks, list):
        raise PlanValidationError("The planner's answer has no task list.")
    answers = _answers(review, raw_tasks)

    tasks = []
    for key, raw in ((key, answers[key]) for key in by_key if key in answers):
        given = raw.get("depends_on") or []
        reasons_given = raw.get("dependency_reasons") or {}
        if not isinstance(given, list) or not isinstance(reasons_given, dict):
            review.problems.append(f"{key}: depends_on must be a list and dependency_reasons "
                                   "an object.")
            continue
        reasons = _dependencies(review, items[key], given, reasons_given)
        merged = {**by_key[key], **{name: raw[name] for name in PLANNER_FIELDS if name in raw}}
        merged["depends_on"] = [by_key[blocker]["id"] for blocker in reasons]
        merged["dependency_reasons"] = {
            by_key[blocker]["id"]: reason for blocker, reason in reasons.items()
        }
        merged["doc_target"] = _doc_target(review, key, merged.get("doc_target"), docs_dir)
        merged.setdefault("context", {"read_first": [], "notes": ""})
        tasks.append(merged)
    if review.problems:
        raise PlanValidationError(" ".join(review.problems))

    claimed = {task["id"] for task in tasks if task.get("parallel_safe") is True}
    checked = validate_tasks(tasks)
    review.notes += [
        f"{task['id']} ({task['jira_key']}) runs one at a time: it shares files "
        "with a task it is not ordered against, or does not list its files."
        for task in checked if task["id"] in claimed and not task["parallel_safe"]
    ]
    summary = output.get("summary")
    return {"summary": summary.strip() if isinstance(summary, str) else "",
            "tasks": checked, "notes": review.notes, "jira_links": review.jira_links}


def is_story_plan(record: dict[str, Any]) -> bool:
    return (record.get("source") or {}).get("kind") == "jira"


def story_plan_refusal(record: dict[str, Any]) -> str | None:
    """Why run-task and run-plan do not run this plan, or None for an Issue plan."""
    if not is_story_plan(record):
        return None
    key, run_id = record["source"].get("key"), record.get("run_id")
    if (record.get("approval") or {}).get("status") != APPROVED:
        return (f"The plan of story {key} is waiting for approval. Look at it with: show-plan "
                f"{run_id}")
    return f"Running a story plan is not supported yet (story {key}, run {run_id})."


def format_story_plan(record: dict[str, Any], path: object) -> str:
    """Format a story plan as it is shown before approval."""
    source, config, planner = record["source"], record["config"], record.get("planner") or {}
    approval = record.get("approval") or {}
    tasks = record["tasks"]
    by_id = {task["id"]: task for task in tasks}
    new_links = {(link["subtask"], link["blocked_by"]) for link in approval.get("jira_links", [])}
    lines = [f"Plan for story {source['key']} - {record['issue']['title']}"]
    if source.get("epic"):
        lines.append(f"Epic:        {source['epic']['key']} {source['epic']['summary']}")
    lines += [f"Run ID:      {config['run_id']}",
              f"Branch:      {config['branch']} (from {config['base_ref']} at "
              f"{str(config['base_commit'])[:12]})",
              f"Model:       {planner.get('model_used') or config.get('model') or '(default)'}"]
    if record.get("summary"):
        lines.append(f"Summary:     {record['summary']}")
    lines.append(f"\nTasks ({len(tasks)}), in Jira's order:")
    for task in tasks:
        lines.append(f"\n  {task['id']}  {task['jira_key']}  {task['title']}")
        if not task["depends_on"]:
            lines.append("      Needs:   nothing")
        for blocker in task["depends_on"]:
            other = by_id[blocker]["jira_key"]
            new = " [new: added to Jira after approval]" if (task["jira_key"], other) in new_links \
                else ""
            lines.append(f"      Needs:   {blocker} ({other}): "
                         f"{task.get('dependency_reasons', {}).get(blocker, '')}{new}")
        parallel = "parallel-safe" if task["parallel_safe"] else "one at a time"
        lines.append(f"      Run as:  {task['complexity']}, {parallel} -> "
                     f"{profile_for(task['complexity']).describe()}")
        if task["files"]:
            lines.append(f"      Files:   {', '.join(task['files'])}")
        lines += [f"      Accept:  {criterion}" for criterion in task["acceptance_criteria"]]
        lines.append(f"      Outcome: {task['expected_outcome']}")
        lines += [f"      Verify:  {step}" for step in task["verification"]]
        if task.get("doc_target"):
            lines.append(f"      Doc:     {task['doc_target']}")
        context = task.get("context") or {}
        if context.get("read_first"):
            lines.append(f"      Read:    {', '.join(context['read_first'])}")
        if context.get("notes"):
            lines.append(f"      Notes:   {' '.join(context['notes'].split())}")
    if record.get("skipped_subtasks"):
        lines.append("\nLeft out, done in Jira: " + ", ".join(
            f"{item['key']} ({item['status']})" for item in record["skipped_subtasks"]))
    if record.get("orchestrator_notes"):
        lines.append("\nWhat the orchestrator changed in the planner's answer:")
        lines += [f"  - {note}" for note in record["orchestrator_notes"]]
    links = approval.get("jira_links") or []
    lines.append(f"\nJira changes after approval ({len(links)}):")
    lines += [f"  + {link['subtask']} is blocked by {link['blocked_by']}" for link in links] \
        or ["  none: every dependency is already a Jira link"]
    status = "approved" if approval.get("status") == APPROVED else "waiting for approval"
    lines.append(f"Status:      {status}")
    metrics = [f"{label} {format(planner[key], spec)}" for label, key, spec in (
        ("duration", "duration_seconds", ".1f"), ("cost USD", "total_cost_usd", ".4f"),
        ("turns", "num_turns", "g")) if isinstance(planner.get(key), (int, float))]
    if metrics:
        lines.append(f"Planner:     {', '.join(metrics)}")
    lines.append(f"Plan file:   {path}")
    return "\n".join(lines)
