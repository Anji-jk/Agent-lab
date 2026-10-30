"""issue, ai-plan and show-plan: look at an Issue and plan it. Nothing is executed."""

from __future__ import annotations

import argparse
import sys

from orchestrator.cli.display import display_issue, display_plan, display_task_plan
from orchestrator.cli.exit_codes import EXIT_OK, EXIT_REFUSED
from orchestrator.execution.worktrees import verify_remote_repository
from orchestrator.github.client import GitHubClient
from orchestrator.github.issue import Task
from orchestrator.planning.ai_planner import generate_ai_plan, DEFAULT_PLANNER_MODEL
from orchestrator.planning.plan import ExecutionPlan, build_plan
from orchestrator.planning.plan_file import load_task_plan, plan_path


def run_issue(args: argparse.Namespace) -> int:
    display_issue(GitHubClient(repository=args.repo).get_issue(args.number))
    return EXIT_OK


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
        model=args.model or DEFAULT_PLANNER_MODEL,
        max_turns=args.max_turns,
        run_id=args.run_id,
    )


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
