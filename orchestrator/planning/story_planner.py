"""Plan a Jira story with a read-only AI agent: jira plan."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.execution import runner_adapter
from orchestrator.execution.diagnose import failure_reason
from orchestrator.execution.worktrees import plan_story_worktree, validate_base_ref
from orchestrator.jira.story import Story, WorkItem
from orchestrator.planning.ai_planner import (
    DEFAULT_PLANNING_MAX_TURNS, PLANNER_METRIC_FIELDS, PlanningError, _repository_state,
    check_planning_base, repository_context,
)
from orchestrator.planning.plan import DEFAULT_TIMEOUT_SECONDS
from orchestrator.planning.plan_file import (
    PlanValidationError, _candidate_documents, load_task_plan, plan_path, save_task_plan,
)
from orchestrator.planning.story_plan import WAITING, jira_tasks, merge_planner_output, open_subtasks
from orchestrator.prompts.loader import render
from orchestrator.records.run_registry import new_run_id, validate_run_id

MAX_ITEM_CHARS = 4_000


def _cut(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    text = (text or "").strip() or "(no description)"
    return text if len(text) <= limit else text[:limit] + "\n... (description truncated)"


def _subtask_block(item: WorkItem) -> str:
    labels = ", ".join(item.labels) or "none"
    lines = [f"### {item.key}  {item.summary}",
             f"Status: {item.status} · Priority: {item.priority or 'none'} · Labels: {labels}",
             f"Blocked by in Jira: {', '.join(item.blocked_by) or 'nothing'}",
             "", _cut(item.description)]
    return "\n".join(lines)


def build_story_planner_prompt(story: Story, *, base_ref: str, docs_dir: str | None,
                               context: str) -> str:
    done = [f"- {item.key}  {item.summary} ({item.status})"
            for item in story.subtasks if item.done]
    epic = f"{story.epic.key} {story.epic.summary}" if story.epic else "(none)"
    return render(
        "story_planner",
        base_ref=base_ref,
        docs_dir=f"{docs_dir}/" if docs_dir else "(none: this project has no docs folder)",
        story_key=story.story.key,
        story_title=story.story.summary,
        epic=epic,
        story_description=_cut(story.story.description, 12_000),
        subtasks="\n\n".join(_subtask_block(item) for item in open_subtasks(story)),
        done="\n".join(done) or "(none)",
        context=context,
    )


def parse_planner_answer(text: object) -> dict[str, Any]:
    """Parse the JSON object in the planner's last message."""
    if not isinstance(text, str) or not text.strip():
        raise PlanValidationError("The planner returned no output.")
    for candidate in _candidate_documents(text):
        try:
            document = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict):
            return document
    raise PlanValidationError("The planner's output did not contain a JSON object.")


def generate_story_plan(
    story: Story,
    repository_root: Path,
    *,
    plans_dir: Path,
    docs_dir: str | None = None,
    base_ref: str = "main",
    model: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_turns: int = DEFAULT_PLANNING_MAX_TURNS,
    run_id: str | None = None,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    invoke_runner=runner_adapter.invoke_runner,
) -> dict[str, Any]:
    """Run the planner for a story and store its checked plan."""
    validate_base_ref(base_ref)
    run_id = validate_run_id(run_id) if run_id else new_run_id()
    try:
        jira_tasks(story)
    except PlanValidationError as exc:
        raise PlanningError(str(exc)) from None
    root, branch, worktree_path = plan_story_worktree(repository_root, story.story.key, run_id)

    if not Path(runner_script).is_file():
        raise PlanningError(f"Runner script not found: {runner_script}")
    planner_run_id = f"{run_id}-plan"
    planner_dir = runner_adapter.runner_run_directory(planner_run_id, runner_script)
    if planner_dir.exists():
        raise FileExistsError(f"Planner directory already exists: {planner_dir}")
    if plan_path(plans_dir, run_id).exists():
        raise FileExistsError(f"A plan already exists for run {run_id}.")

    base_commit = check_planning_base(root, base_ref)
    before = _repository_state(root)
    prompt = build_story_planner_prompt(story, base_ref=base_ref, docs_dir=docs_dir,
                                        context=repository_context(root))
    with tempfile.TemporaryDirectory(prefix="agent-lab-story-plan-") as scratch:
        prompt_file = Path(scratch) / "planner-prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        outcome = invoke_runner(
            run_id=planner_run_id, worktree_path=root, prompt_file=prompt_file,
            timeout_seconds=timeout_seconds, model=model, max_turns=max_turns,
            keep_result_text=True, read_only=True, role="planner", runner_script=runner_script,
        )
    if outcome.outcome != "success":
        reason = failure_reason(outcome.outcome, outcome.error, outcome.result,
                                outcome.process_exit_code)
        raise PlanningError(f"Planner {outcome.outcome.replace('_', ' ')}: {reason} "
                            f"(artifacts: {planner_dir})")
    if _repository_state(root) != before:
        raise PlanningError(
            "The repository's Git state changed while planning, so the plan was not stored. "
            f"The planner may have broken its read-only rule; look at git status and "
            f"{planner_dir}.")
    try:
        checked = merge_planner_output(
            story, parse_planner_answer(outcome.result.get("result_text")), docs_dir)
    except PlanValidationError as exc:
        raise PlanningError(f"The planner's answer does not fit the story: {exc} "
                            f"(artifacts: {planner_dir})") from None

    item, epic = story.story, story.epic
    document = {
        "source": {"kind": "jira", "key": item.key, "url": item.url,
                   "epic": {"key": epic.key, "summary": epic.summary} if epic else None},
        "issue": {"repository": None, "number": None, "key": item.key, "title": item.summary,
                  "url": item.url, "description": item.description},
        "config": {"repository": None, "base_ref": base_ref, "base_commit": base_commit,
                   "model": model, "timeout_seconds": timeout_seconds, "run_id": run_id,
                   "branch": branch, "worktree_path": str(worktree_path),
                   "docs_dir": docs_dir},
        "summary": checked["summary"],
        "tasks": checked["tasks"],
        "skipped_subtasks": [{"key": sub.key, "status": sub.status}
                             for sub in story.subtasks if sub.done],
        "orchestrator_notes": checked["notes"],
        "approval": {"status": WAITING, "jira_links": checked["jira_links"]},
        "planner": {"run_id": planner_run_id, "max_turns": max_turns,
                    **{key: outcome.result[key] for key in PLANNER_METRIC_FIELDS
                       if key in outcome.result}},
    }
    save_task_plan(plans_dir, run_id, document)
    return load_task_plan(plans_dir, run_id)
