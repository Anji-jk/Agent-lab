"""The command-line parser: every command and its options."""

from __future__ import annotations

import argparse
from pathlib import Path

from orchestrator.cli.exit_codes import EPILOG
from orchestrator.monitoring import dashboard as dash
from orchestrator.planning.ai_planner import DEFAULT_PLANNING_MAX_TURNS
from orchestrator.planning.plan import DEFAULT_MAX_TURNS, DEFAULT_TIMEOUT_SECONDS

# Runs, plans and the board connection live in the project: <project>/.agent-lab/runs
# (orchestrator/core/project.py). None here means "the project's"; cli/commands fills it in.
PROJECT_RUNS = "(default: the project's .agent-lab/runs)"


def _add_plan_options(
    parser: argparse.ArgumentParser,
    max_turns_default: int = DEFAULT_MAX_TURNS,
) -> None:
    parser.add_argument("number", type=int, help="GitHub Issue number.")
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repository in OWNER/REPO format (default: AGENT_LAB_REPOSITORY).",
    )
    parser.add_argument("--base", default="main", help="Base ref for the branch.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Runner timeout in seconds.",
    )
    parser.add_argument("--model", help="Claude model to use.")
    parser.add_argument(
        "--max-turns", type=int, default=max_turns_default, help="Max agent turns."
    )
    parser.add_argument(
        "--run-id",
        help="Use this run ID (default: generate one). Fixes the branch name.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator",
        description="GitHub-driven Claude Code agent orchestration.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "init",
        help="Get the project ready: its .agent-lab/ folder, a .env for its tokens and an "
             ".agent-lab.toml. Writes only what is missing; no GitHub or AI calls.",
    )

    issue_parser = subparsers.add_parser(
        "issue",
        help="Fetch and display a GitHub Issue without modifying anything.",
    )
    issue_parser.add_argument(
        "number",
        type=int,
        help="GitHub Issue number.",
    )
    issue_parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repository in OWNER/REPO format (default: AGENT_LAB_REPOSITORY).",
    )

    ai_plan_parser = subparsers.add_parser(
        "ai-plan",
        help="Have a read-only AI agent break an Issue into tasks. Executes nothing.",
    )
    _add_plan_options(ai_plan_parser, DEFAULT_PLANNING_MAX_TURNS)
    ai_plan_parser.add_argument(
        "--plans-dir",
        type=Path,
        default=None,
        help="Where plans are stored (<plans-dir>/<run-id>/plan.json).",
    )

    show_plan_parser = subparsers.add_parser(
        "show-plan",
        help="Display a stored AI plan. No GitHub or AI calls.",
    )
    show_plan_parser.add_argument("run_id", help="Run ID the plan was stored under.")
    show_plan_parser.add_argument(
        "--plans-dir", type=Path, default=None, help=PROJECT_RUNS
    )

    run_task_parser = subparsers.add_parser(
        "run-task",
        help="Build, test and review one planned task; commit it locally if every check passes.",
    )
    run_task_parser.add_argument("run_id", help="Run ID the plan was stored under.")
    run_task_parser.add_argument("task_id", help="Task ID in the plan, e.g. T1.")
    run_task_parser.add_argument("--plans-dir", type=Path, default=None, help=PROJECT_RUNS)
    run_task_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)
    run_task_parser.add_argument(
        "--worktree-root", type=Path, default=None,
        help="Folder for task worktrees (default: AGENT_LAB_WORKTREE_ROOT or ~/agent-worktrees).",
    )
    run_task_parser.add_argument(
        "--continue", dest="continue_task", action="store_true",
        help="Continue a cancelled or interrupted task from the stage it reached, in the "
             "same worktree with its files kept, with fresh agents.",
    )
    run_task_parser.add_argument(
        "--retry", dest="retry_task", action="store_true",
        help="Retry a failed task once, on a fresh worktree, telling the builder what the "
             "checks found. run-plan does this by itself.",
    )
    run_task_parser.add_argument(
        "--background", action="store_true",
        help="Start the task as its own process and return at once; watch it with "
             "status or dashboard. It keeps running if this window closes.",
    )

    run_plan_parser = subparsers.add_parser(
        "run-plan",
        help="Run the plan's tasks (together where the plan allows) until it is complete or a "
             "task needs a person; commits locally, never pushes.",
    )
    run_plan_parser.add_argument("run_id", help="Run ID the plan was stored under.")
    run_plan_parser.add_argument("--plans-dir", type=Path, default=None, help=PROJECT_RUNS)
    run_plan_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)
    run_plan_parser.add_argument(
        "--worktree-root", type=Path, default=None,
        help="Folder for task worktrees (default: AGENT_LAB_WORKTREE_ROOT or ~/agent-worktrees).",
    )
    run_plan_parser.add_argument(
        "--no-board", action="store_true",
        help="Leave the GitHub board alone for this run, even when one is connected.",
    )
    run_plan_parser.add_argument(
        "--one-at-a-time", action="store_true",
        help="Run one task at a time, even where the plan allows several together "
             "(otherwise up to AGENT_LAB_MAX_PARALLEL, 3 if unset).",
    )

    board_parser = subparsers.add_parser(
        "board", help="The GitHub Project board that shows each task's status.",
    )
    board_commands = board_parser.add_subparsers(dest="board_command", required=True)
    connect_parser = board_commands.add_parser(
        "connect",
        help="Read the board and save which column each task state goes in. "
             "Writes nothing to GitHub.",
    )
    connect_parser.add_argument("--project", type=int, required=True, metavar="N",
                                help="The number in the board's address: .../projects/N.")
    connect_parser.add_argument("--owner", default=None,
                                help="The user who owns the board (default: the repository's owner).")
    connect_parser.add_argument("--repo", default=None,
                                help="The repository whose Issues go on the board "
                                     "(default: AGENT_LAB_REPOSITORY).")
    for place, what in (("todo", "not started"), ("in-progress", "running"),
                        ("attention", "waiting for a person"), ("done", "done")):
        connect_parser.add_argument(f"--{place}", metavar="COLUMN", default=None,
                                    help=f"The column for tasks that are {what}, if its name "
                                         "is not recognised.")
    connect_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)
    sync_parser = board_commands.add_parser(
        "sync",
        help="Put any task that is not on the board yet there, and move a run's cards to "
             "where its records say (run-plan does this by itself).",
    )
    sync_parser.add_argument("run_id")
    sync_parser.add_argument("--plans-dir", type=Path, default=None, help=PROJECT_RUNS)
    sync_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)

    report_parser = subparsers.add_parser(
        "report",
        help="Summarise a run: its branch, cost, time, and each task's result. No AI calls.",
    )
    report_parser.add_argument("run_id")
    report_parser.add_argument("--save", action="store_true",
                               help="Also save it as runs/<run>/report.md.")
    report_parser.add_argument("--plans-dir", type=Path, default=None, help=PROJECT_RUNS)
    report_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)

    budget_parser = subparsers.add_parser(
        "budget",
        help="Show, set or remove a run's spending limit for all its tasks. No AI calls.",
    )
    budget_parser.add_argument("run_id")
    budget_change = budget_parser.add_mutually_exclusive_group()
    budget_change.add_argument("--set", type=float, metavar="USD", dest="limit",
                               help="The most the run's task agents may spend in total.")
    budget_change.add_argument("--remove", action="store_true", help="Remove the limit.")
    budget_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)

    cancel_parser = subparsers.add_parser(
        "cancel",
        help="Stop a running task and everything it started; the worktree is kept.",
    )
    cancel_parser.add_argument("run_id")
    cancel_parser.add_argument("task_id")
    cancel_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)

    status_parser = subparsers.add_parser(
        "status",
        help="Show every task of a run and what its agents are doing now. Reads files only.",
    )
    status_parser.add_argument("run_id")
    status_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)
    status_parser.add_argument("--plans-dir", type=Path, default=None, help=PROJECT_RUNS)
    status_parser.add_argument(
        "--watch", type=float, metavar="SECONDS",
        help="Refresh every SECONDS until nothing is running (Ctrl+C to stop).",
    )

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Open a local web page showing this project and its runs, or one run. Reads files only.",
    )
    dashboard_parser.add_argument("run_id", nargs="?", help="Run to open (default: the project's page, with every run).")
    dashboard_parser.add_argument("--port", type=int, default=dash.DEFAULT_PORT)
    dashboard_parser.add_argument("--no-browser", action="store_true",
                                  help="Print the address without opening a browser.")
    dashboard_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)
    dashboard_parser.add_argument("--plans-dir", type=Path, default=None, help=PROJECT_RUNS)

    show_task_parser = subparsers.add_parser(
        "show-task", help="Display a task's record. No GitHub or AI calls."
    )
    show_task_parser.add_argument("run_id")
    show_task_parser.add_argument("task_id")
    show_task_parser.add_argument("--runs-dir", type=Path, default=None, help=PROJECT_RUNS)

    _add_project_option(parser)
    return parser


def _add_project_option(parser: argparse.ArgumentParser) -> None:
    """Every command takes --repo-path: the project's checkout (default: the folder it runs in)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _add_project_option(child)
            return
    parser.add_argument(
        "--repo-path", type=Path, default=None,
        help="The project's checkout (default: the Git repository of the current folder). "
             "Never the orchestrator's own.",
    )
