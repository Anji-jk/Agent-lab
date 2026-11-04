"""jira connect and jira story: the project's Jira space. Both only read from Jira."""

from __future__ import annotations

import argparse
import json

from orchestrator.cli.exit_codes import EXIT_OK, EXIT_REFUSED
from orchestrator.core.project_config import CONFIG_FILE_NAME, load_project_config
from orchestrator.errors import ExecutionRefused
from orchestrator.jira.client import JiraClient
from orchestrator.jira.connect import connect_jira, format_connection, save_jira
from orchestrator.jira.story import format_story, read_story
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
