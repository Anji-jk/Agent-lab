
"""Command-line entry point for the Agent Lab orchestrator."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from orchestrator.planning.ai_planner import (
    DEFAULT_PLANNING_MAX_TURNS,
    PlanningError,
    generate_ai_plan,
)
from orchestrator.cli.display import (
    display_issue,
    display_plan,
    display_task,
    display_task_plan,
    event_lines,
)
from orchestrator.errors import ApprovalRequired, ExecutionRefused
from orchestrator.execution.background import start_task_in_background
from orchestrator.execution.budget import format_budget, remove_budget, set_budget
from orchestrator.execution.cancel import cancel_task
from orchestrator.execution.continuing import continue_task
from orchestrator.execution.retry import retry_task
from orchestrator.legacy.executor import execute_plan
from orchestrator.github.board import board_path, connect_board, format_connection, save_board
from orchestrator.github.client import GitHubAPIError, GitHubClient
from orchestrator.github.projects import ProjectClient, project_token
from orchestrator.github.publish import format_published, load_board, publish
from orchestrator.github.sync import sync_board
from orchestrator.github.tokens import load_tokens
from orchestrator.legacy.planning import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_SECONDS,
    ExecutionPlan,
    build_plan,
)
from orchestrator.monitoring.progress import format_status, planner_view, run_status, stale_agents
from orchestrator.monitoring import dashboard as dash
from orchestrator.monitoring.event_feed import stable_index
from orchestrator.records.run_registry import RunRegistryError
from orchestrator.records.task_registry import list_tasks, load_task
from orchestrator.execution.task_runner import run_task
from orchestrator.planning.plan_file import load_task_plan, plan_path
from orchestrator.scheduling.left_behind import format_left_behind, is_stopped, left_behind
from orchestrator.scheduling.run_plan import (
    describe_next, format_step, next_action, run_plan as work_through_plan,
)
from orchestrator.scheduling.run_report import build_report, format_report, save_report
from orchestrator.scheduling.plan_progress import (
    format_plan_progress, plan_progress, process_liveness,
)
from orchestrator.github.issue import Task
from orchestrator.execution.worktrees import (
    WorktreeError,
    default_worktree_root,
    verify_remote_repository,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs" / "orchestration"

EVENTS_POLL_SECONDS = 1.0

EXIT_OK = 0
EXIT_ERROR = 1  # GitHub, Git, or registry failure
EXIT_USAGE = 2  # argparse
EXIT_NOT_APPROVED = 3
EXIT_REFUSED = 4  # invalid task, or the Issue prohibits execution
EXIT_RUN_FAILED = 5
EXIT_RUN_TIMED_OUT = 6
EXIT_PLANNING_FAILED = 7  # planner failed, or returned an unusable plan
EXIT_TASK_FAILED = 8  # a check caught a problem; nothing was committed
EXIT_TASK_NEEDS_REVIEW = 9  # the task could not be judged; a person must look

EPILOG = """\
exit codes:
  0 success   1 error   2 usage   3 execution not approved
  4 task invalid or execution prohibited   5 run failed   6 run timed out
  7 AI planning failed or returned an invalid plan
  8 task failed verification   9 task needs review
"""


def _add_plan_options(
    parser: argparse.ArgumentParser,
    max_turns_default: int = DEFAULT_MAX_TURNS,
) -> None:
    parser.add_argument("number", type=int, help="GitHub Issue number.")
    parser.add_argument(
        "--repo",
        default="Anji-jk/Agent-lab",
        help="GitHub repository in OWNER/REPO format.",
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=PROJECT_ROOT,
        help="Local checkout to create the worktree from.",
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
        default="Anji-jk/Agent-lab",
        help="GitHub repository in OWNER/REPO format.",
    )

    plan_parser = subparsers.add_parser(
        "plan",
        help="Show what executing an Issue would do. Creates nothing.",
    )
    _add_plan_options(plan_parser)

    execute_parser = subparsers.add_parser(
        "execute",
        help="Run Claude on an Issue in an isolated worktree (needs --approve).",
    )
    _add_plan_options(execute_parser)
    execute_parser.add_argument(
        "--approve",
        action="store_true",
        help="Explicitly approve execution. Without it nothing is created.",
    )
    execute_parser.add_argument(
        "--runs-dir",
        type=Path,
        default=DEFAULT_RUNS_DIR,
        help="Where orchestration run records are stored.",
    )

    ai_plan_parser = subparsers.add_parser(
        "ai-plan",
        help="Have a read-only AI agent break an Issue into tasks. Executes nothing.",
    )
    _add_plan_options(ai_plan_parser, DEFAULT_PLANNING_MAX_TURNS)
    ai_plan_parser.add_argument(
        "--plans-dir",
        type=Path,
        default=DEFAULT_RUNS_DIR,
        help="Where plans are stored (<plans-dir>/<run-id>/plan.json).",
    )

    show_plan_parser = subparsers.add_parser(
        "show-plan",
        help="Display a stored AI plan. No GitHub or AI calls.",
    )
    show_plan_parser.add_argument("run_id", help="Run ID the plan was stored under.")
    show_plan_parser.add_argument(
        "--plans-dir", type=Path, default=DEFAULT_RUNS_DIR
    )

    run_task_parser = subparsers.add_parser(
        "run-task",
        help="Build, test and review one planned task; commit it locally if every check passes.",
    )
    run_task_parser.add_argument("run_id", help="Run ID the plan was stored under.")
    run_task_parser.add_argument("task_id", help="Task ID in the plan, e.g. T1.")
    run_task_parser.add_argument("--plans-dir", type=Path, default=DEFAULT_RUNS_DIR)
    run_task_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    run_task_parser.add_argument(
        "--repo-path", type=Path, default=PROJECT_ROOT,
        help="Local checkout to create the worktree from.",
    )
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
        help="Run the plan's tasks one at a time until it is complete or a task needs a "
             "person; commits locally, never pushes.",
    )
    run_plan_parser.add_argument("run_id", help="Run ID the plan was stored under.")
    run_plan_parser.add_argument("--plans-dir", type=Path, default=DEFAULT_RUNS_DIR)
    run_plan_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    run_plan_parser.add_argument(
        "--repo-path", type=Path, default=PROJECT_ROOT,
        help="Local checkout to create the worktrees from.",
    )
    run_plan_parser.add_argument(
        "--worktree-root", type=Path, default=None,
        help="Folder for task worktrees (default: AGENT_LAB_WORKTREE_ROOT or ~/agent-worktrees).",
    )
    run_plan_parser.add_argument(
        "--no-board", action="store_true",
        help="Leave the GitHub board alone for this run, even when one is connected.",
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
    connect_parser.add_argument("--repo", default="Anji-jk/Agent-lab",
                                help="The repository whose Issues go on the board.")
    for place, what in (("todo", "not started"), ("in-progress", "running"),
                        ("attention", "waiting for a person"), ("done", "done")):
        connect_parser.add_argument(f"--{place}", metavar="COLUMN", default=None,
                                    help=f"The column for tasks that are {what}, if its name "
                                         "is not recognised.")
    connect_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    publish_parser = board_commands.add_parser(
        "publish",
        help="Put a run's tasks on GitHub: an Issue each, as sub-issues of the run's Issue, "
             "on the board. Without --yes it only shows what it would do.",
    )
    publish_parser.add_argument("run_id")
    publish_parser.add_argument("--yes", action="store_true",
                                help="Create the Issues and cards. Without it nothing is written.")
    publish_parser.add_argument("--plans-dir", type=Path, default=DEFAULT_RUNS_DIR)
    publish_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    sync_parser = board_commands.add_parser(
        "sync",
        help="Move a run's cards to where its records say (run-plan does this by itself).",
    )
    sync_parser.add_argument("run_id")
    sync_parser.add_argument("--plans-dir", type=Path, default=DEFAULT_RUNS_DIR)
    sync_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)

    report_parser = subparsers.add_parser(
        "report",
        help="Summarise a run: its branch, cost, time, and each task's result. No AI calls.",
    )
    report_parser.add_argument("run_id")
    report_parser.add_argument("--save", action="store_true",
                               help="Also save it as runs/<run>/report.md.")
    report_parser.add_argument("--plans-dir", type=Path, default=DEFAULT_RUNS_DIR)
    report_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)

    budget_parser = subparsers.add_parser(
        "budget",
        help="Show, set or remove a run's spending limit for all its tasks. No AI calls.",
    )
    budget_parser.add_argument("run_id")
    budget_change = budget_parser.add_mutually_exclusive_group()
    budget_change.add_argument("--set", type=float, metavar="USD", dest="limit",
                               help="The most the run's task agents may spend in total.")
    budget_change.add_argument("--remove", action="store_true", help="Remove the limit.")
    budget_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)

    cancel_parser = subparsers.add_parser(
        "cancel",
        help="Stop a running task and everything it started; the worktree is kept.",
    )
    cancel_parser.add_argument("run_id")
    cancel_parser.add_argument("task_id")
    cancel_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)

    status_parser = subparsers.add_parser(
        "status",
        help="Show every task of a run and what its agents are doing now. Reads files only.",
    )
    status_parser.add_argument("run_id")
    status_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    status_parser.add_argument("--plans-dir", type=Path, default=DEFAULT_RUNS_DIR)
    status_parser.add_argument(
        "--watch", type=float, metavar="SECONDS",
        help="Refresh every SECONDS until nothing is running (Ctrl+C to stop).",
    )

    events_parser = subparsers.add_parser(
        "events",
        help="Print what an agent (or the orchestrator) did, step by step. Reads files only.",
    )
    events_parser.add_argument("run_id")
    events_parser.add_argument("task_id", help="A task ID such as T1, or 'plan' for the planner.")
    events_parser.add_argument(
        "--role", choices=(*dash.TABS,), default="orchestrator",
        help="Whose events to show for a task (default: orchestrator).",
    )
    events_parser.add_argument("--follow", action="store_true",
                               help="Keep printing new events until the agent finishes.")
    events_parser.add_argument("--details", action="store_true",
                               help="Also print each event's input and result (shortened).")
    events_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    events_parser.add_argument("--plans-dir", type=Path, default=DEFAULT_RUNS_DIR)

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Open a local web page showing every task and agent of a run. Reads files only.",
    )
    dashboard_parser.add_argument("run_id", nargs="?", help="Run to open first (default: the newest).")
    dashboard_parser.add_argument("--port", type=int, default=dash.DEFAULT_PORT)
    dashboard_parser.add_argument("--no-browser", action="store_true",
                                  help="Print the address without opening a browser.")
    dashboard_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    dashboard_parser.add_argument("--plans-dir", type=Path, default=DEFAULT_RUNS_DIR)

    show_task_parser = subparsers.add_parser(
        "show-task", help="Display a task's record. No GitHub or AI calls."
    )
    show_task_parser.add_argument("run_id")
    show_task_parser.add_argument("task_id")
    show_task_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)

    return parser


def load_plan(args: argparse.Namespace) -> ExecutionPlan:
    """Fetch the Issue (read-only) and build a plan. Creates nothing."""
    client = GitHubClient(repository=args.repo)
    task = Task.from_issue(args.repo, client.get_issue(args.number))
    verify_remote_repository(args.repo_path, args.repo)
    return build_plan(
        task,
        args.repo_path,
        base_ref=args.base,
        timeout_seconds=args.timeout,
        model=args.model,
        max_turns=args.max_turns,
        run_id=args.run_id,
    )


def run_plan(args: argparse.Namespace) -> int:
    plan = load_plan(args)
    display_plan(plan)
    if not plan.executable:
        return EXIT_REFUSED
    print(
        f"\nNothing was executed. To run it:\n"
        f"  python -m orchestrator execute {args.number} "
        f"--run-id {plan.run_id} --approve"
    )
    return EXIT_OK


def run_ai_plan(args: argparse.Namespace) -> int:
    plan = load_plan(args)
    if not plan.executable:
        display_plan(plan)
        return EXIT_REFUSED

    print(
        f"Planning Issue #{plan.task.issue_number} with a read-only AI agent "
        f"(run {plan.run_id}). This calls Claude and may take a while...",
        file=sys.stderr,
    )
    record = generate_ai_plan(plan, plans_dir=args.plans_dir)
    display_task_plan(record, plan_path(args.plans_dir, plan.run_id))
    print("\nNothing was executed. No branch, worktree, or run record was created.")
    return EXIT_OK


def run_show_plan(args: argparse.Namespace) -> int:
    record = load_task_plan(args.plans_dir, args.run_id)
    display_task_plan(record, plan_path(args.plans_dir, args.run_id))
    return EXIT_OK


def run_run_task(args: argparse.Namespace) -> int:
    plan = load_task_plan(args.plans_dir, args.run_id)
    verify_remote_repository(args.repo_path, plan["config"]["repository"])
    if args.continue_task and args.retry_task:
        print("Use --continue for a stopped task or --retry for a failed one, not both.",
              file=sys.stderr)
        return EXIT_USAGE
    if args.background and (args.continue_task or args.retry_task):
        print("--continue and --retry run in this window for now. Run it again without "
              "--background.", file=sys.stderr)
        return EXIT_USAGE
    if args.background:
        return run_task_in_background(args)
    if args.retry_task:
        print(f"Retrying {args.task_id} of run {args.run_id} on a fresh worktree; fresh agents "
              "call Claude and may take a while...", file=sys.stderr)
        record = retry_task(args.run_id, args.task_id, plans_dir=args.plans_dir,
                            runs_dir=args.runs_dir, repo_path=args.repo_path,
                            worktree_root=args.worktree_root or default_worktree_root())
        display_task(record)
        print("\nNothing was pushed or merged.")
        return {"done": EXIT_OK, "failed": EXIT_TASK_FAILED}.get(record["status"], EXIT_TASK_NEEDS_REVIEW)
    if args.continue_task:
        print(f"Continuing {args.task_id} of run {args.run_id} from where it stopped; "
              "fresh agents call Claude and may take a while...", file=sys.stderr)
        record = continue_task(args.run_id, args.task_id, plans_dir=args.plans_dir,
                               runs_dir=args.runs_dir, repo_path=args.repo_path)
        display_task(record)
        print("\nNothing was pushed or merged.")
        return {"done": EXIT_OK, "failed": EXIT_TASK_FAILED}.get(record["status"], EXIT_TASK_NEEDS_REVIEW)
    print(
        f"Running {args.task_id} of run {args.run_id}: builder, tester and reviewer "
        "agents call Claude and may take a while...",
        file=sys.stderr,
    )
    record = run_task(
        args.run_id, args.task_id,
        plans_dir=args.plans_dir, runs_dir=args.runs_dir, repo_path=args.repo_path,
        worktree_root=args.worktree_root or default_worktree_root(),
    )
    display_task(record)
    print("\nNothing was pushed or merged.")
    return {
        "done": EXIT_OK,
        "failed": EXIT_TASK_FAILED,
    }.get(record["status"], EXIT_TASK_NEEDS_REVIEW)


def run_run_plan(args: argparse.Namespace) -> int:
    plan = load_task_plan(args.plans_dir, args.run_id)
    verify_remote_repository(args.repo_path, plan["config"]["repository"])
    print(f"Working through the plan of run {args.run_id}, one task at a time. Agents call "
          "Claude, so this may take a long while. Ctrl+C stops it; run-plan again goes on "
          "from where it stopped.", file=sys.stderr)

    board = _BoardFollower(args)
    board.start()

    def show(action: str, task_id: str, step) -> None:
        print(format_step(action, task_id, step), flush=True)
        board.sync(starting=task_id if step is None else None)

    result = work_through_plan(
        args.run_id, plans_dir=args.plans_dir, runs_dir=args.runs_dir,
        repo_path=args.repo_path, worktree_root=args.worktree_root or default_worktree_root(),
        on_step=show,
    )
    # Not after Ctrl+C: this process still owns the stopped task until it exits, so it
    # would look running. The next run-plan, or board sync, moves its card.
    board.sync()
    text = format_report(build_report(args.run_id, plans_dir=args.plans_dir,
                                      runs_dir=args.runs_dir))
    print("\n" + text)
    print(f"Saved this report: {save_report(args.runs_dir, args.run_id, text)}")
    if result.complete:
        print("\nThe plan is complete. Nothing was pushed or merged.")
        return EXIT_OK
    print(f"\nStopped: {result.stopped}")
    print("Nothing was pushed or merged.")
    if result.refused:
        return EXIT_REFUSED
    if result.steps and result.steps[-1].status == "failed":
        return EXIT_TASK_FAILED
    return EXIT_TASK_NEEDS_REVIEW


def run_task_in_background(args: argparse.Namespace) -> int:
    started = start_task_in_background(
        args.run_id, args.task_id,
        plans_dir=args.plans_dir, runs_dir=args.runs_dir, repo_path=args.repo_path,
        worktree_root=args.worktree_root or default_worktree_root(),
    )
    print(f"Started {args.task_id} of run {args.run_id} in the background (process {started.pid}).")
    if not started.record_seen:
        print("It has not written its task record yet; if status does not show it soon, "
              "read the log.")
    print(f"Log:    {started.log}")
    print(f"Watch:  python -m orchestrator status {args.run_id} --watch 2")
    print(f"        python -m orchestrator dashboard {args.run_id}")
    print("It keeps running if you close this window. Nothing will be pushed or merged.")
    return EXIT_OK


def run_board(args: argparse.Namespace) -> int:
    if args.board_command == "publish":
        return run_board_publish(args)
    if args.board_command == "sync":
        clients = _board_clients(args.runs_dir)
        print(sync_board(args.run_id, plans_dir=args.plans_dir, runs_dir=args.runs_dir,
                         issues=clients[0], project=clients[1]).summary())
        return EXIT_OK
    owner = args.owner or args.repo.split("/")[0]
    token, variable = project_token()
    client = ProjectClient(owner, args.project, token=token, token_variable=variable)
    chosen = {"todo": args.todo, "in_progress": args.in_progress,
              "attention": args.attention, "done": args.done}
    connection = connect_board(owner, args.project, args.repo, client=client, chosen=chosen)
    print(format_connection(connection))
    print(f"\nSaved: {save_board(args.runs_dir, connection.config)}")
    print("Nothing was written to GitHub.")
    return EXIT_OK


def _board_clients(runs_dir: Path) -> tuple[GitHubClient, ProjectClient]:
    """The Issue and board clients for the connected board (BoardError/PublishError if none)."""
    board = load_board(runs_dir)
    token, variable = project_token()
    project = ProjectClient(board["owner"], int(board["number"]), token=token,
                            token_variable=variable)
    return GitHubClient(repository=board["repository"]), project


class _BoardFollower:
    """run-plan's board work (M5.3): put the tasks on it once, then keep it in line.

    Whatever goes wrong on GitHub is printed and the run goes on: the board shows
    the run, it never stops it. board sync catches up afterwards.
    """

    def __init__(self, args: argparse.Namespace):
        self.args, self.clients = args, None
        if getattr(args, "no_board", False) or not board_path(args.runs_dir).exists():
            return
        try:
            self.clients = _board_clients(args.runs_dir)
        except (ExecutionRefused, GitHubAPIError) as exc:
            self._warn(f"not used: {exc}")

    def _warn(self, text: str) -> None:
        print(f"Board {text} The run goes on; python -m orchestrator board sync "
              f"{self.args.run_id} catches up later.", file=sys.stderr)

    def start(self) -> None:
        if self.clients is None:
            return
        try:  # option B: the tasks reach GitHub when you start the run, not when planned
            result = publish(self.args.run_id, plans_dir=self.args.plans_dir,
                             runs_dir=self.args.runs_dir, issues=self.clients[0],
                             project=self.clients[1], write=True)
        except (ExecutionRefused, GitHubAPIError) as exc:
            self._warn(f"publish failed: {exc}")
            return
        made = [c for c in result.cards if c.done]
        if made:
            print(f"Board: put {len(made)} task(s) on {result.board_url}", flush=True)
        self.sync()

    def sync(self, starting: str | None = None) -> None:
        if self.clients is None:
            return
        try:
            synced = sync_board(self.args.run_id, plans_dir=self.args.plans_dir,
                                runs_dir=self.args.runs_dir, issues=self.clients[0],
                                project=self.clients[1], starting=starting)
        except (ExecutionRefused, GitHubAPIError) as exc:
            self._warn(f"sync failed: {exc}")
            return
        if synced.moved or synced.closed or synced.problems:
            print(synced.summary(), flush=True)


def run_board_publish(args: argparse.Namespace) -> int:
    issues, project = _board_clients(args.runs_dir)
    if args.yes:
        print("Creating the task Issues and cards on GitHub...", file=sys.stderr)
    result = publish(args.run_id, plans_dir=args.plans_dir, runs_dir=args.runs_dir,
                     issues=issues, project=project, write=args.yes)
    print(format_published(result))
    return EXIT_OK


def run_report(args: argparse.Namespace) -> int:
    text = format_report(build_report(args.run_id, plans_dir=args.plans_dir,
                                      runs_dir=args.runs_dir))
    print(text, end="")
    if args.save:
        print(f"\nSaved: {save_report(args.runs_dir, args.run_id, text)}")
    return EXIT_OK


def run_budget(args: argparse.Namespace) -> int:
    if args.limit is not None:
        set_budget(args.runs_dir, args.run_id, args.limit)
        print(f"Set the budget of run {args.run_id} to ${args.limit:.2f}.")
    elif args.remove:
        removed = remove_budget(args.runs_dir, args.run_id)
        print(f"Removed the budget of run {args.run_id}." if removed
              else f"Run {args.run_id} had no budget.")
        return EXIT_OK
    line = format_budget(args.runs_dir, args.run_id)
    print(line or f"Run {args.run_id} has no budget: only each agent's own cap limits it.")
    return EXIT_OK


def run_cancel(args: argparse.Namespace) -> int:
    result = cancel_task(args.run_id, args.task_id, runs_dir=args.runs_dir)
    print(f"Cancelled {args.task_id} of run {args.run_id}.")
    if result.stopped:
        print(f"Stopped:  process {', '.join(map(str, result.stopped))} and everything it started.")
    else:
        print("Stopped:  nothing; no process of this task was still running.")
    if result.agents:
        print(f"Agent:    {', '.join(result.agents)} was running; it is marked cancelled.")
    if result.not_ours:
        print(f"Left alone: process {', '.join(map(str, result.not_ours))}; that ID now "
              "belongs to another program.")
    if result.worktree:
        print(f"Worktree: kept at {result.worktree}")
        print(f'          remove it when done: git worktree remove --force "{result.worktree}"')
    return EXIT_OK


def _print_status(run_id: str, runs_dir: Path, plans_dir: Path) -> bool:
    """Print the run's status once; True while any task is still running."""
    tasks = run_status(runs_dir, run_id)
    planner = planner_view(run_id)
    has_plan = plan_path(plans_dir, run_id).exists()
    if not tasks and planner is None and not has_plan:
        raise RunRegistryError(f"No plan or tasks found for run {run_id}.")
    print(format_status(run_id, tasks, planner))
    stale = stale_agents(tasks)
    if stale:
        print(f"\nWarning: no heartbeat from {', '.join(stale)}. Its process may have stopped.")
    if not has_plan:
        return any(task.active_role for task in tasks)
    progress = plan_progress(load_task_plan(plans_dir, run_id), runs_dir, run_id)
    # "next" as run-plan would do it: an interrupted task first, a failed one retried first.
    upcoming = describe_next(next_action(progress, list_tasks(runs_dir, run_id)))
    print("\n" + format_plan_progress(progress, next_step=upcoming))
    budget = format_budget(runs_dir, run_id)
    if budget:
        print(budget)
    # An interrupted task's record still says "testing"; only a live process counts.
    return progress.state == "running"


def run_status_command(args: argparse.Namespace) -> int:
    if not args.watch:
        _print_status(args.run_id, args.runs_dir, args.plans_dir)
        return EXIT_OK
    if args.watch < 1:
        raise ValueError("--watch must be at least 1 second.")
    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            print(time.strftime("%H:%M:%S"), "(Ctrl+C to stop)\n")
            if not _print_status(args.run_id, args.runs_dir, args.plans_dir):
                print("\nNothing is running any more.")
                return EXIT_OK
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return EXIT_OK


def run_events(args: argparse.Namespace) -> int:
    data = dash.DashboardData(args.runs_dir, args.plans_dir)
    source = ["planner"] if args.task_id == "plan" else [args.task_id, args.role]
    printed = 0
    try:
        while True:
            items = data.items(args.run_id, source)
            # While following, print an item only once it can no longer change.
            stable = items if not args.follow else items[: stable_index(items)]
            for item in stable[printed:]:
                for line in event_lines(item.to_dict(), args.details):
                    print(line)
            printed = max(printed, len(stable))
            if not args.follow:
                if not items:
                    print("No events recorded for this source yet.")
                return EXIT_OK
            if not data.source_live(args.run_id, source) and printed >= len(items):
                print("-- finished --")
                return EXIT_OK
            time.sleep(EVENTS_POLL_SECONDS)
    except KeyboardInterrupt:
        return EXIT_OK


def run_dashboard(args: argparse.Namespace) -> int:
    data = dash.DashboardData(args.runs_dir, args.plans_dir)
    try:
        server = dash.make_server(data, port=args.port)
    except OSError as exc:
        print(f"Error: could not listen on port {args.port} ({exc}). Try --port {args.port + 1}.",
              file=sys.stderr)
        return EXIT_ERROR
    url = f"http://127.0.0.1:{server.server_address[1]}/" + (f"#{args.run_id}" if args.run_id else "")
    print(f"Dashboard: {url}")
    print("Reads files only; reachable from this computer only. Ctrl+C to stop.")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK


def run_show_task(args: argparse.Namespace) -> int:
    record = load_task(args.runs_dir, args.run_id, args.task_id)
    display_task(record)
    # A cancelled or interrupted task has no verdict; show what its agents left behind.
    if is_stopped(record, process_liveness(args.runs_dir, args.run_id)):
        print("\n" + format_left_behind(left_behind(record)))
    return EXIT_OK


def run_execute(args: argparse.Namespace) -> int:
    plan = load_plan(args)
    display_plan(plan)

    # execute_plan enforces this too; refusing here keeps it out of the way.
    if not plan.executable:
        return EXIT_REFUSED

    if not args.approve:
        print(
            "\nNot approved: nothing was created or executed. "
            "Re-run with --approve to execute this plan.",
            file=sys.stderr,
        )
        return EXIT_NOT_APPROVED

    record = execute_plan(plan, approved=args.approve, runs_dir=args.runs_dir)

    print(f"\nRun {record['run_id']}: {record['status']}")
    print(f"Branch:      {record['branch']}")
    print(f"Worktree:    {record['worktree_path']}")
    if record.get("error"):
        print(f"Error:       {record['error']}")
    result = record.get("runner_result") or {}
    for label, key in (("Duration", "duration_seconds"), ("Cost (USD)", "total_cost_usd")):
        if result.get(key) is not None:
            print(f"{label + ':':<13}{result[key]}")
    print(f"Record:      {args.runs_dir / record['run_id'] / 'orchestration.json'}")

    return {
        "succeeded": EXIT_OK,
        "timed_out": EXIT_RUN_TIMED_OUT,
    }.get(record["status"], EXIT_RUN_FAILED)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The GitHub tokens may be kept in the project's .env (M5.1); a set variable wins.
    load_tokens(PROJECT_ROOT)

    try:
        if args.command == "issue":
            client = GitHubClient(repository=args.repo)
            issue = client.get_issue(args.number)
            display_issue(issue)
            return EXIT_OK
        if args.command == "plan":
            return run_plan(args)
        if args.command == "execute":
            return run_execute(args)
        if args.command == "ai-plan":
            return run_ai_plan(args)
        if args.command == "show-plan":
            return run_show_plan(args)
        if args.command == "run-task":
            return run_run_task(args)
        if args.command == "run-plan":
            return run_run_plan(args)
        if args.command == "show-task":
            return run_show_task(args)
        if args.command == "cancel":
            return run_cancel(args)
        if args.command == "budget":
            return run_budget(args)
        if args.command == "report":
            return run_report(args)
        if args.command == "board":
            return run_board(args)
        if args.command == "status":
            return run_status_command(args)
        if args.command == "events":
            return run_events(args)
        if args.command == "dashboard":
            return run_dashboard(args)
    except ApprovalRequired as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_NOT_APPROVED
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

    parser.error("Unknown command.")
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
