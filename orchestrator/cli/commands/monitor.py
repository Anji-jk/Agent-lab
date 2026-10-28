"""status and dashboard: watch a run."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from orchestrator.cli.exit_codes import EXIT_ERROR, EXIT_OK
from orchestrator.execution.budget import format_budget
from orchestrator.execution.integration import branch_line, describe_merge, load_integration
from orchestrator.monitoring import dashboard as dash
from orchestrator.monitoring.progress import format_status, planner_view, run_status, stale_agents
from orchestrator.planning.plan_file import load_task_plan, plan_path
from orchestrator.records.run_registry import RunRegistryError
from orchestrator.records.task_registry import list_tasks
from orchestrator.scheduling.plan_progress import format_plan_progress, plan_progress
from orchestrator.scheduling.run_plan import describe_next, next_action


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
    plan = load_task_plan(plans_dir, run_id)
    progress = plan_progress(plan, runs_dir, run_id)
    records = list_tasks(runs_dir, run_id)
    # "next" as run-plan would do it: an interrupted task first, a failed one retried first.
    upcoming = describe_next(next_action(progress, records))
    merged = {task_id: text for task_id, record in records.items()
              if (text := describe_merge(record.get("merge")))}
    safe = {task["id"]: bool(task.get("parallel_safe")) for task in plan["tasks"]}
    print("\n" + format_plan_progress(progress, next_step=upcoming, merged=merged, safe=safe))
    branch = branch_line(load_integration(runs_dir, run_id))
    if branch:
        print(branch)
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
