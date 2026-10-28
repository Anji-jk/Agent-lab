"""Command-line entry point for the Agent Lab orchestrator.

One module per group of commands; ``main`` parses, checks the settings and hands over.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from orchestrator.cli.commands import board, budget, monitor, plan, run, task
from orchestrator.cli.display import display_task  # noqa: F401  (re-exported)
from orchestrator.cli.exit_codes import (  # noqa: F401  (re-exported)
    EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, EXIT_PLANNING_FAILED, EXIT_REFUSED, EXIT_TASK_FAILED,
    EXIT_TASK_NEEDS_REVIEW, EXIT_USAGE,
)
from orchestrator.cli.parser import DEFAULT_RUNS_DIR, build_parser  # noqa: F401  (re-exported)
from orchestrator.core.runner_paths import PROJECT_ROOT  # noqa: F401  (re-exported)
from orchestrator.errors import ExecutionRefused
from orchestrator.execution.worktrees import WorktreeError
from orchestrator.github.client import GitHubAPIError
from orchestrator.planning.ai_planner import PlanningError
from orchestrator.records.run_registry import RunRegistryError
from orchestrator.settings import load_settings

# command -> (module, handler); the handler is looked up when the command runs.
HANDLERS = {
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings()  # a bad setting stops the command here, naming it
        if hasattr(args, "repo") and args.repo is None:
            if not settings.repository:
                raise ExecutionRefused(
                    "No repository given: use --repo OWNER/REPO or set AGENT_LAB_REPOSITORY "
                    "(in .env or the window).")
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
