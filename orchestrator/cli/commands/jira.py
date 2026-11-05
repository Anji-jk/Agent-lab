"""jira connect, story, check and plan: the project's Jira space. All only read from Jira."""

from __future__ import annotations

import argparse
import json
import sys

from orchestrator.cli.exit_codes import EXIT_OK, EXIT_REFUSED
from orchestrator.core.project_config import CONFIG_FILE_NAME, load_project_config
from orchestrator.core.runner_paths import agent_runs_root
from orchestrator.errors import ExecutionRefused
from orchestrator.execution.story_check import format_story_check, run_story_checks
from orchestrator.jira.client import JiraClient
from orchestrator.jira.connect import connect_jira, format_connection, save_jira
from orchestrator.jira.story import format_story, read_story
from orchestrator.planning.ai_planner import DEFAULT_PLANNER_MODEL
from orchestrator.planning.plan_file import plan_path
from orchestrator.planning.story_plan import format_story_plan
from orchestrator.planning.story_planner import generate_story_plan
from orchestrator.records.project_db import try_refresh_run
from orchestrator.records.run_registry import new_run_id
from orchestrator.settings import load_settings


def _site(args: argparse.Namespace) -> tuple[JiraClient, str]:
    """A client for the project's Jira site, and its space's key."""
    config = load_project_config(args.repo_path) if args.repo_path else None
    url = getattr(args, "url", None) or (config.jira_url if config else None)
    key = getattr(args, "project", None) or (config.jira_project if config else None)
    if not url or not key:
        raise ExecutionRefused(
            f"No Jira site or space: put jira_url = \"https://<site>.atlassian.net\" and "
            f"jira_project = \"<KEY>\" in the project's {CONFIG_FILE_NAME}, or use --url and "
            "--project.")
    email, token = load_settings().jira_credentials()
    return JiraClient(url, email, token), key


def run_jira(args: argparse.Namespace) -> int:
    if args.jira_command == "story":
        return _story(args)
    if args.jira_command == "check":
        return _check(args)
    if args.jira_command == "plan":
        return _plan(args)
    return _connect(args)


def _connect(args: argparse.Namespace) -> int:
    client, key = _site(args)
    statuses = {"todo": args.todo, "in_progress": args.in_progress,
                "in_review": args.in_review, "done": args.done}
    types = {"epic": args.epic_type, "story": args.story_type, "subtask": args.subtask_type,
             "task": args.task_type, "bug": args.bug_type}
    connection = connect_jira(client, key, board_id=args.board, statuses=statuses, types=types)
    print(format_connection(connection))
    if not connection.ready:
        print("\nNot saved: fix the items marked NO in Jira (or name them with the options "
              "shown), then run jira connect again. Nothing was written to Jira.")
        return EXIT_REFUSED
    print(f"\nSaved: {save_jira(args.runs_dir, connection.config)}")
    print("Nothing was written to Jira.")
    return EXIT_OK


def _story(args: argparse.Namespace) -> int:
    """Show a story and its sub-tasks as a story run will see them. Saves nothing."""
    client, project = _site(args)
    story = read_story(client, args.key, project)
    if args.json:
        print(json.dumps(story.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_story(story))
        print("\nNothing was written to Jira or saved.")
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    """The checks before planning a story. Refused (exit 4) when one fails; writes nothing."""
    if not args.repo_path:
        raise ExecutionRefused("No project found. Run jira check in the project's folder.")
    client, project = _site(args)
    story = read_story(client, args.key, project)
    print(f"Reading the project and Git{'' if args.skip_tests else ', then running its tests'}"
          "...", file=sys.stderr)
    result = run_story_checks(story, args.repo_path, load_project_config(args.repo_path),
                              base_ref=args.base, run_tests=not args.skip_tests)
    print(format_story_check(result))
    print("\nNothing was written to Jira or saved.")
    return EXIT_OK if result.ready else EXIT_REFUSED


def _plan(args: argparse.Namespace) -> int:
    """Plan a story with a read-only AI agent and save the plan, waiting for approval."""
    if not args.repo_path:
        raise ExecutionRefused("No project found. Run jira plan in the project's folder.")
    client, project = _site(args)
    story = read_story(client, args.key, project)
    config = load_project_config(args.repo_path)
    run_id = args.run_id or new_run_id()
    print(f"Planning story {story.story.key} with a read-only AI agent (run {run_id}). This "
          "calls Claude and may take a while...", file=sys.stderr)
    record = generate_story_plan(
        story, args.repo_path, plans_dir=args.plans_dir, docs_dir=config.docs_dir,
        base_ref=args.base, model=args.model or DEFAULT_PLANNER_MODEL,
        timeout_seconds=args.timeout, max_turns=args.max_turns, run_id=run_id)
    print(format_story_plan(record, plan_path(args.plans_dir, run_id)))
    problem = try_refresh_run(args.plans_dir, run_id, agent_runs_root(), plans_dir=args.plans_dir)
    if problem:
        print(f"(The project database was not updated: {problem}. db rebuild fixes it.)",
              file=sys.stderr)
    print("\nNothing was written to Jira, and nothing was run. The plan waits for approval.")
    return EXIT_OK
