
"""Command-line entry point for the Agent Lab orchestrator."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from orchestrator.ai_planner import (
    DEFAULT_PLANNING_MAX_TURNS,
    PlanningError,
    generate_ai_plan,
)
from orchestrator.executor import ApprovalRequired, ExecutionRefused, execute_plan
from orchestrator.github_client import GitHubAPIError, GitHubClient
from orchestrator.planning import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_SECONDS,
    ExecutionPlan,
    build_plan,
)
from orchestrator.profiles import profile_for
from orchestrator.run_registry import RunRegistryError
from orchestrator.task_plan import load_task_plan, plan_path
from orchestrator.tasks import Task
from orchestrator.worktrees import WorktreeError, verify_remote_repository

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs" / "orchestration"

EXIT_OK = 0
EXIT_ERROR = 1  # GitHub, Git, or registry failure
EXIT_USAGE = 2  # argparse
EXIT_NOT_APPROVED = 3
EXIT_REFUSED = 4  # invalid task, or the Issue prohibits execution
EXIT_RUN_FAILED = 5
EXIT_RUN_TIMED_OUT = 6
EXIT_PLANNING_FAILED = 7  # planner failed, or returned an unusable plan

EPILOG = """\
exit codes:
  0 success   1 error   2 usage   3 execution not approved
  4 task invalid or execution prohibited   5 run failed   6 run timed out
  7 AI planning failed or returned an invalid plan
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

    return parser


def display_issue(issue: dict) -> None:
    """Display the relevant fields from a GitHub Issue."""
    labels = [
        label["name"]
        for label in issue.get("labels", [])
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    ]

    print(f"Issue:       #{issue['number']}")
    print(f"Title:       {issue['title']}")
    print(f"State:       {issue['state']}")
    print(f"URL:         {issue['html_url']}")
    print(f"Labels:      {', '.join(labels) if labels else '(none)'}")
    print("\nDescription:")
    print(issue.get("body") or "(no description)")


def display_plan(plan: ExecutionPlan) -> None:
    task = plan.task
    print(f"Repository:  {task.repository}")
    print(f"Issue:       #{task.issue_number} - {task.title}")
    print(f"Run ID:      {plan.run_id}")
    print(f"Branch:      {plan.branch} (from {plan.base_ref})")
    print(f"Worktree:    {plan.worktree_path}")
    print(f"Timeout:     {plan.timeout_seconds}s, max {plan.max_turns} turns")
    print(f"Model:       {plan.model or '(Claude default)'}")
    print(f"Scope:       {plan.scope}")
    if plan.prohibitions:
        print("\nNOT EXECUTABLE - the Issue prohibits execution:")
        for reason in plan.prohibitions:
            print(f"  - {reason}")


def display_task_plan(record: dict, path: Path) -> None:
    issue, config, planner = record["issue"], record["config"], record["planner"]
    tasks = record["tasks"]
    print(f"AI plan for Issue #{issue['number']} - {issue['title']}")
    print(f"Repository:  {config['repository']}")
    print(f"Run ID:      {config['run_id']}")
    print(f"Branch:      {config['branch']} (from {config['base_ref']})")
    print(f"Model:       {config['model'] or '(Claude default)'}")
    print(f"Timeout:     {config['timeout_seconds']}s")
    if record.get("summary"):
        print(f"\nSummary:     {record['summary']}")

    print(f"\nTasks ({len(tasks)}):")
    for task in tasks:
        depends = ", ".join(task["depends_on"]) or "none"
        print(f"\n  {task['id']}  {task['title']}   (depends on: {depends})")
        print(f"      {task['description']}")
        print(f"      Outcome: {task['expected_outcome']}")
        for criterion in task["acceptance_criteria"]:
            print(f"      Accept:  {criterion}")
        parallel = "parallel-safe" if task["parallel_safe"] else "sequential"
        print(f"      Run as:  {task['complexity']}, {parallel} -> "
              f"{profile_for(task['complexity']).describe()}")
        if task["files"]:
            print(f"      Files:   {', '.join(task['files'])}")
        for step in task["verification"]:
            print(f"      Verify:  {step}")

    metrics = [
        f"{label} {format(planner[key], spec)}"
        for label, key, spec in (
            ("duration", "duration_seconds", ".1f"),
            ("cost USD", "total_cost_usd", ".4f"),
            ("turns", "num_turns", "g"),
        )
        if isinstance(planner.get(key), (int, float))
    ]
    if metrics:
        print(f"\nPlanner:     {', '.join(metrics)}")
    print(f"Plan file:   {path}")


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
