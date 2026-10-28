"""budget: show, set or remove a run's spending limit."""

from __future__ import annotations

import argparse

from orchestrator.cli.exit_codes import EXIT_OK
from orchestrator.execution.budget import format_budget, remove_budget, set_budget


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
