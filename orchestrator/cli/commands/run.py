"""run-task and run-plan: do the work the plan describes."""

from __future__ import annotations

import argparse
import sys

from orchestrator.cli.commands.board import BoardFollower
from orchestrator.cli.display import display_task
from orchestrator.cli.exit_codes import (
    EXIT_INTERRUPTED, EXIT_OK, EXIT_REFUSED, EXIT_TASK_FAILED, EXIT_TASK_NEEDS_REVIEW, EXIT_USAGE,
)
from orchestrator.execution.background import start_task_in_background
from orchestrator.execution.continuing import continue_task
from orchestrator.execution.retry import retry_task
from orchestrator.execution.task_runner import run_task
from orchestrator.execution.worktrees import default_worktree_root, verify_remote_repository
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.scheduling.run_plan import format_step, max_parallel_setting
from orchestrator.scheduling.run_plan import run_plan as work_through_plan
from orchestrator.scheduling.run_report import build_report, format_report, save_report
from orchestrator.scheduling.task_process import ProcessLauncher

NOTHING_PUSHED = "Nothing was pushed, and your own branches were not changed."


def _exit_code(record: dict) -> int:
    return {"done": EXIT_OK, "failed": EXIT_TASK_FAILED}.get(record["status"], EXIT_TASK_NEEDS_REVIEW)


# run-task has three ways to work on a task; each is told what it does, then runs the same way.
# (The functions are looked up when called, so a test can replace run_task and the others.)


def _fresh(args: argparse.Namespace) -> dict:
    return run_task(
        args.run_id, args.task_id,
        plans_dir=args.plans_dir, runs_dir=args.runs_dir, repo_path=args.repo_path,
        worktree_root=args.worktree_root or default_worktree_root(),
    )


def _retry(args: argparse.Namespace) -> dict:
    return retry_task(args.run_id, args.task_id, plans_dir=args.plans_dir,
                      runs_dir=args.runs_dir, repo_path=args.repo_path,
                      worktree_root=args.worktree_root or default_worktree_root())


def _continue(args: argparse.Namespace) -> dict:
    return continue_task(args.run_id, args.task_id, plans_dir=args.plans_dir,
                         runs_dir=args.runs_dir, repo_path=args.repo_path,
                         worktree_root=args.worktree_root or default_worktree_root())


def _announcement(mode: str, args: argparse.Namespace) -> str:
    task, run = args.task_id, args.run_id
    return {
        "run": f"Running {task} of run {run}: builder, tester and reviewer agents call Claude "
               "and may take a while...",
        "retry": f"Retrying {task} of run {run} on a fresh worktree; fresh agents call Claude "
                 "and may take a while...",
        "continue": f"Continuing {task} of run {run} from where it stopped; fresh agents call "
                    "Claude and may take a while...",
    }[mode]


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
    mode = "retry" if args.retry_task else "continue" if args.continue_task else "run"
    print(_announcement(mode, args), file=sys.stderr)
    record = {"run": _fresh, "retry": _retry, "continue": _continue}[mode](args)
    display_task(record)
    print("\n" + NOTHING_PUSHED)
    return _exit_code(record)


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
    print("It keeps running if you close this window. Nothing will be pushed.")
    return EXIT_OK


def run_run_plan(args: argparse.Namespace) -> int:
    plan = load_task_plan(args.plans_dir, args.run_id)
    verify_remote_repository(args.repo_path, plan["config"]["repository"])
    together = max_parallel_setting(args.one_at_a_time)
    how = ("one task at a time" if together == 1 else
           f"up to {together} tasks at a time where the plan allows it (--one-at-a-time for one)")
    print(f"Working through the plan of run {args.run_id}, {how}. Agents call "
          "Claude, so this may take a long while. Ctrl+C stops it; run-plan again goes on "
          "from where it stopped.", file=sys.stderr)

    board = BoardFollower(args)
    board.start()

    def show(action: str, task_id: str, step) -> None:
        print(format_step(action, task_id, step), flush=True)
        # A task launched alongside others stays In progress until its own step ends.
        (board.launched.add if step is None else board.launched.discard)(task_id)
        board.sync()

    worktree_root = args.worktree_root or default_worktree_root()
    # Each task runs as its own process; its output goes to runs/<run>/run-plan-logs/.
    launch = ProcessLauncher(args.run_id, plans_dir=args.plans_dir, runs_dir=args.runs_dir,
                             repo_path=args.repo_path, worktree_root=worktree_root)
    try:
        result = work_through_plan(
            args.run_id, plans_dir=args.plans_dir, runs_dir=args.runs_dir,
            repo_path=args.repo_path, worktree_root=worktree_root, on_step=show, launch=launch,
            max_parallel=together,
        )
    except KeyboardInterrupt:
        print(f"\nStopped by Ctrl+C. The tasks that were running are interrupted; run "
              f"python -m orchestrator run-plan {args.run_id} again and each continues from "
              "where it stopped. Nothing was pushed.", file=sys.stderr)
        return EXIT_INTERRUPTED
    # Not after Ctrl+C: this process still owns the stopped task until it exits, so it
    # would look running. The next run-plan, or board sync, moves its card.
    board.sync()
    text = format_report(build_report(args.run_id, plans_dir=args.plans_dir,
                                      runs_dir=args.runs_dir))
    print("\n" + text)
    print(f"Saved this report: {save_report(args.runs_dir, args.run_id, text)}")
    if result.complete:
        print("\nThe plan is complete. Nothing was pushed, and your own branches were not changed.")
        return EXIT_OK
    print(f"\nStopped: {result.stopped}")
    print("Nothing was pushed, and your own branches were not changed.")
    if result.refused:
        return EXIT_REFUSED
    if result.steps and result.steps[-1].status == "failed":
        return EXIT_TASK_FAILED
    return EXIT_TASK_NEEDS_REVIEW
