"""show-task, cancel and report: look at one task or one run, or stop a task."""

from __future__ import annotations

import argparse

from orchestrator.cli.display import display_task
from orchestrator.cli.exit_codes import EXIT_OK
from orchestrator.execution.cancel import cancel_task
from orchestrator.execution.left_behind import format_left_behind, is_stopped, left_behind
from orchestrator.records.task_registry import load_task
from orchestrator.scheduling.plan_progress import process_liveness
from orchestrator.scheduling.run_report import build_report, format_report, save_report


def run_show_task(args: argparse.Namespace) -> int:
    record = load_task(args.runs_dir, args.run_id, args.task_id)
    display_task(record)
    # A cancelled or interrupted task has no verdict; show what its agents left behind.
    if is_stopped(record, process_liveness(args.runs_dir, args.run_id)):
        print("\n" + format_left_behind(left_behind(record)))
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


def run_report(args: argparse.Namespace) -> int:
    text = format_report(build_report(args.run_id, plans_dir=args.plans_dir,
                                      runs_dir=args.runs_dir))
    print(text, end="")
    if args.save:
        print(f"\nSaved: {save_report(args.runs_dir, args.run_id, text)}")
    return EXIT_OK
