"""AI-driven task planning.

A planning agent (Claude Code, launched through the existing runner) reads
the Issue and the repository and returns a task-level plan as JSON, which is
validated and stored as ``<plans_dir>/<run_id>/plan.json``.

Planning is read-only by construction, with a tripwire behind it:

* the runner launches the agent with ``--read-only``: only Read, Grep and
  Glob exist, MCP tools are removed, user/project settings (and their allow
  rules) are ignored, and anything needing approval is denied;
* the checkout must be clean and at ``base_ref``, so the agent reads the
  same code an execution will start from; that commit is recorded;
* no branch, worktree, or ``orchestration.json`` is ever created;
* tracked/untracked Git state is compared before and after, and a plan is
  discarded if it changed. This tripwire cannot see gitignored files or
  anything outside the repository; the tool restriction is the real guard.

The planner's own runner artifacts go to ``runs/<run_id>-plan/``, which is
distinct from the ``runs/<run_id>/`` directory a later execution will use.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.execution import runner_adapter
from orchestrator.execution.diagnose import failure_reason
from orchestrator.errors import ExecutionRefused
from orchestrator.planning.plan import ExecutionPlan
from orchestrator.planning.plan_file import (
    PlanValidationError,
    load_task_plan,
    parse_plan_output,
    plan_path,
    save_task_plan,
)
from orchestrator.execution.worktrees import run_git
from orchestrator.prompts.loader import render

DEFAULT_PLANNING_MAX_TURNS = 10
DEFAULT_PLANNER_MODEL = "opus"
# The runner passes the prompt as a command-line argument, and Windows caps
# a command line at about 32k characters. Keep well inside that.
MAX_DESCRIPTION_CHARS = 12_000
MAX_CONTEXT_CHARS = 8_000

PLANNER_METRIC_FIELDS = (
    "status",
    "duration_seconds",
    "total_cost_usd",
    "input_tokens",
    "output_tokens",
    "num_turns",
    "model",
    "model_used",
    "session_id",
)


class PlanningError(RuntimeError):
    """The planner failed, was unusable, or violated its read-only rule."""


_CRITERIA_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*acceptance criteria\s*(?:\*\*|__)?\s*:?\s*$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")


def extract_acceptance_criteria(description: str) -> list[str]:
    """Best-effort: list items under an 'Acceptance criteria' heading."""
    criteria: list[str] = []
    collecting = False
    for line in description.splitlines():
        if _CRITERIA_HEADING.match(line):
            collecting = True
            continue
        if not collecting:
            continue
        if _MARKDOWN_HEADING.match(line):
            break
        item = _LIST_ITEM.match(line)
        if item:
            criteria.append(item.group(1))
    return criteria


def repository_context(repository_root: Path, limit: int = MAX_CONTEXT_CHARS) -> str:
    """Tracked file paths: cheap orientation before the agent reads anything."""
    try:
        listing = run_git(repository_root, "ls-files")
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return "(file listing unavailable)"
    if len(listing) <= limit:
        return listing
    cut = listing[:limit].rsplit("\n", 1)[0]
    return f"{cut}\n... (listing truncated)"


def build_planner_prompt(
    plan: ExecutionPlan,
    acceptance_criteria: list[str],
    context: str,
) -> str:
    task = plan.task
    description = task.description or "(no description)"
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS] + "\n... (description truncated)"

    if acceptance_criteria:
        criteria = "\n".join(f"- {item}" for item in acceptance_criteria)
    else:
        criteria = "(none found as a list; check the description)"

    return render(
        "planner",
        repository=task.repository,
        base_ref=plan.base_ref,
        issue_number=task.issue_number,
        issue_title=task.title,
        description=description,
        criteria=criteria,
        context=context,
    )


def _repository_state(root: Path) -> dict[str, str]:
    """Everything planning must leave untouched."""
    try:
        return {
            "status": run_git(root, "status", "--porcelain"),
            "head": run_git(root, "rev-parse", "HEAD"),
            "branches": run_git(root, "branch", "--list"),
            "worktrees": run_git(root, "worktree", "list", "--porcelain"),
        }
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        raise PlanningError("Could not inspect the repository's Git state.") from None


def check_planning_base(root: Path, base_ref: str) -> str:
    """Require a clean checkout at ``base_ref``; return that commit SHA."""
    state = _repository_state(root)
    if state["status"]:
        raise PlanningError(
            "The checkout has uncommitted or untracked changes. Commit or "
            "stash them first, so the plan matches what execution starts from."
        )
    try:
        base_commit = run_git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        raise PlanningError(f"Base ref {base_ref!r} does not exist.") from None
    if state["head"] != base_commit:
        raise PlanningError(
            f"The checkout is at {state['head'][:12]}, but {base_ref} is "
            f"{base_commit[:12]}. Check out {base_ref} before planning."
        )
    return base_commit


def generate_ai_plan(
    plan: ExecutionPlan,
    *,
    plans_dir: Path,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    invoke_runner=runner_adapter.invoke_runner,
) -> dict[str, Any]:
    """Run the planning agent for ``plan`` and store the resulting task plan.

    ``plan`` carries the execution configuration (repository, base ref,
    model, timeout, run ID), which is recorded in the stored plan unchanged.
    Returns the stored (re-validated) plan.
    """
    if not plan.executable:
        raise ExecutionRefused(
            "The Issue prohibits execution: " + "; ".join(plan.prohibitions)
        )

    # Pre-flight: nothing has been created yet.
    if not Path(runner_script).is_file():
        raise PlanningError(f"Runner script not found: {runner_script}")
    planner_run_id = f"{plan.run_id}-plan"
    planner_dir = runner_adapter.runner_run_directory(planner_run_id, runner_script)
    if planner_dir.exists():
        raise FileExistsError(f"Planner directory already exists: {planner_dir}")
    if plan_path(plans_dir, plan.run_id).exists():
        raise FileExistsError(f"A plan already exists for run {plan.run_id}.")

    task = plan.task
    root = plan.repository_root
    base_commit = check_planning_base(root, plan.base_ref)
    before = _repository_state(root)

    prompt = build_planner_prompt(
        plan,
        extract_acceptance_criteria(task.description),
        repository_context(root),
    )

    with tempfile.TemporaryDirectory(prefix="agent-lab-plan-") as scratch:
        prompt_file = Path(scratch) / "planner-prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        outcome = invoke_runner(
            run_id=planner_run_id,
            worktree_path=root,
            prompt_file=prompt_file,
            timeout_seconds=plan.timeout_seconds,
            model=plan.model,
            max_turns=plan.max_turns,
            keep_result_text=True,
            read_only=True,
            role="planner",
            runner_script=runner_script,
        )

    if outcome.outcome != "success":
        raise PlanningError(
            f"Planner {outcome.outcome.replace('_', ' ')}: "
            f"{failure_reason(outcome.outcome, outcome.error, outcome.result, outcome.process_exit_code)} "
            f"(artifacts: {planner_dir})"
        )

    if _repository_state(root) != before:
        raise PlanningError(
            "The repository's Git state changed while planning, so the plan "
            "was not stored. The planner may have violated its read-only "
            f"rule; inspect `git status` and {planner_dir}."
        )

    try:
        parsed = parse_plan_output(outcome.result.get("result_text"))
    except PlanValidationError as exc:
        raise PlanningError(
            f"The planner's output was not a valid plan: {exc} "
            f"(artifacts: {planner_dir})"
        ) from None

    document = {
        "issue": {
            "repository": task.repository,
            "number": task.issue_number,
            "title": task.title,
            "url": task.issue_url,
            "description": task.description,
        },
        "config": {
            "repository": task.repository,
            "base_ref": plan.base_ref,
            "base_commit": base_commit,
            "model": plan.model,
            "timeout_seconds": plan.timeout_seconds,
            "run_id": plan.run_id,
            "branch": plan.branch,
            "worktree_path": str(plan.worktree_path),
        },
        "summary": parsed["summary"],
        "tasks": parsed["tasks"],
        "planner": {
            "run_id": planner_run_id,
            "max_turns": plan.max_turns,
            **{
                key: outcome.result[key]
                for key in PLANNER_METRIC_FIELDS
                if key in outcome.result
            },
        },
    }
    save_task_plan(plans_dir, plan.run_id, document)
    return load_task_plan(plans_dir, plan.run_id)
