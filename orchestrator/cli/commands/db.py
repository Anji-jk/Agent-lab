"""db rebuild and db summary: the project's database, an index of the run files.

rebuild deletes ``.agent-lab/agentlab.db`` and fills it again from the files; the files are
never changed. summary reads it and prints what it holds.
"""

from __future__ import annotations

import argparse

from orchestrator.cli.exit_codes import EXIT_OK
from orchestrator.core.runner_paths import agent_runs_root
from orchestrator.errors import ExecutionRefused
from orchestrator.records.project_db import TABLES, counts, db_path, query, rebuild

RECENT_RUNS = 10


def run_db(args: argparse.Namespace) -> int:
    if args.db_command == "rebuild":
        return _rebuild(args)
    return _summary(args)


def _rebuild(args: argparse.Namespace) -> int:
    path = db_path(args.runs_dir)
    found = rebuild(args.runs_dir, agent_runs_root(), plans_dir=args.runs_dir)
    print(f"Rebuilt {path}")
    print("  " + ", ".join(f"{table} {found[table]}" for table in TABLES))
    print("The run files were only read; nothing else was changed.")
    return EXIT_OK


def _money(value: object) -> str:
    return f"${value:.2f}" if isinstance(value, (int, float)) else "-"


def _summary(args: argparse.Namespace) -> int:
    path = db_path(args.runs_dir)
    if not path.is_file():
        raise ExecutionRefused(f"No database yet at {path}. Run: db rebuild")
    found = counts(path)
    print(f"Database: {path}")
    print("  " + ", ".join(f"{table} {found[table]}" for table in TABLES))
    runs = query(path, "SELECT * FROM runs ORDER BY updated_at DESC, run_id DESC LIMIT ?",
                 (RECENT_RUNS,))
    if runs:
        print(f"\nLatest runs (up to {RECENT_RUNS}):")
    for run in runs:
        item = f"#{run['item']}" if run["source_kind"] == "github" else run["item"]
        print(f"  {run['run_id']}  {item}  {run['status']}  tasks {run['tasks_done']}/"
              f"{run['tasks_total']} done  {_money(run['cost_usd'])}  {run['title'] or ''}")
    failing = query(path, "SELECT run_id, task_id, status, error FROM tasks WHERE error IS NOT "
                          "NULL ORDER BY updated_at DESC LIMIT ?", (RECENT_RUNS,))
    if failing:
        print("\nTasks that did not finish (latest first):")
    for task in failing:
        print(f"  {task['run_id']} {task['task_id']}  {task['status']}: "
              f"{' '.join(str(task['error']).split())[:150]}")
    return EXIT_OK
