"""Command-line entry point for the Agent Lab orchestrator.

One module per group of commands; ``main`` parses, finds the project, checks the settings
and hands over.

The project is the Git repository of the folder the command runs in, or ``--repo-path``; it is
never the orchestrator's own. Its runs, plans, board connection and agents' files are in its
``.agent-lab/`` folder, and its own ``.agent-lab/.env`` and ``.agent-lab.toml`` come before the
orchestrator's settings.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from orchestrator.cli.commands import board, budget, monitor, plan, run, setup, task
from orchestrator.cli.display import display_task  # noqa: F401  (re-exported)
from orchestrator.cli.exit_codes import (  # noqa: F401  (re-exported)
    EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, EXIT_PLANNING_FAILED, EXIT_REFUSED, EXIT_TASK_FAILED,
    EXIT_TASK_NEEDS_REVIEW, EXIT_USAGE,
)
from orchestrator.cli.parser import build_parser
from orchestrator.core.project import Project, ensure_data_dir, find_project
from orchestrator.core.runner_paths import PROJECT_ROOT  # noqa: F401  (re-exported)
from orchestrator.errors import ExecutionRefused
from orchestrator.execution.worktrees import WorktreeError
from orchestrator.github.client import GitHubAPIError
from orchestrator.planning.ai_planner import PlanningError
from orchestrator.records.run_registry import RunRegistryError
from orchestrator.settings import load_settings, use_project

# command -> (module, handler); the handler is looked up when the command runs.
HANDLERS = {
    "init": (setup, "run_init"),
    "issue": (plan, "run_issue"),
    "ai-plan": (plan, "run_ai_plan"),
    "show-plan": (plan, "run_show_plan"),
    "run-task": (run, "run_run_task"),
    "run-plan": (run, "run_run_plan"),
    "show-task": (task, "run_show_task"),
    "cancel": (task, "run_cancel"),
    "report": (task, "run_report"),
    "budget": (budget, "run_budget"),
    "board": (board, "run_board"),
    "status": (monitor, "run_status_command"),
    "dashboard": (monitor, "run_dashboard"),
}


# Commands that make worktrees in, or plan from, the project's checkout.
NEEDS_CHECKOUT = frozenset({"ai-plan", "run-task", "run-plan"})
# Commands that need a project but make its folder themselves.
NEEDS_PROJECT = NEEDS_CHECKOUT | {"init"}


def settle_project(args: argparse.Namespace) -> Project | None:
    """Find the project, make it the active one, and fill in the folders it decides.

    A folder given on the command line (``--runs-dir`` and the like) is used as given. When
    nothing needs the project and none is found, the command goes on without one.
    """
    # Plans are kept with the runs: one given on the command line stands for the other.
    runs, plans = getattr(args, "runs_dir", ""), getattr(args, "plans_dir", "")
    if runs is None and plans:
        args.runs_dir = plans
    if plans is None and runs:
        args.plans_dir = runs
    given = args.repo_path
    try:
        project = find_project(given if given is not None else Path.cwd())
    except ExecutionRefused as exc:
        if given is not None:
            raise
        project, problem = None, exc
    missing = [name for name in ("runs_dir", "plans_dir") if getattr(args, name, "") is None]
    if project is None:
        if missing or args.command in NEEDS_PROJECT:
            raise problem
        return None
    use_project(project)
    args.repo_path = project.root
    if missing or args.command in NEEDS_CHECKOUT:
        ensure_data_dir(project)
    for name in missing:
        setattr(args, name, project.runs_dir)
    return project


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    finally:
        use_project(None)  # the project belongs to this command, not to the process


def _main(argv: Sequence[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settle_project(args)
        settings = load_settings()  # a bad setting stops the command here, naming it
        if hasattr(args, "repo") and args.repo is None:
            if not settings.repository:
                raise ExecutionRefused(
                    "No repository given: put repository = \"OWNER/REPO\" in the project's "
                    ".agent-lab.toml, set AGENT_LAB_REPOSITORY, or use --repo OWNER/REPO.")
            args.repo = settings.repository
        module, handler = HANDLERS[args.command]
        return getattr(module, handler)(args)
    except (ExecutionRefused, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        # `issue` predates the refusal codes and has always returned 1.
        return EXIT_ERROR if args.command == "issue" else EXIT_REFUSED
    except PlanningError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_PLANNING_FAILED
    except (GitHubAPIError, WorktreeError, RunRegistryError, FileExistsError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
