"""jira connect: read the project's Jira space once and save what later steps need."""

from __future__ import annotations

import argparse

from orchestrator.cli.exit_codes import EXIT_OK, EXIT_REFUSED
from orchestrator.core.project_config import CONFIG_FILE_NAME, load_project_config
from orchestrator.errors import ExecutionRefused
from orchestrator.jira.client import JiraClient
from orchestrator.jira.connect import connect_jira, format_connection, save_jira
from orchestrator.settings import load_settings


def run_jira(args: argparse.Namespace) -> int:
    # Only "connect" exists for now; the parser refuses anything else.
    config = load_project_config(args.repo_path) if args.repo_path else None
    url = args.url or (config.jira_url if config else None)
    key = args.project or (config.jira_project if config else None)
    if not url or not key:
        raise ExecutionRefused(
            f"No Jira site or space: put jira_url = \"https://<site>.atlassian.net\" and "
            f"jira_project = \"<KEY>\" in the project's {CONFIG_FILE_NAME}, or use --url and "
            "--project.")
    email, token = load_settings().jira_credentials()
    client = JiraClient(url, email, token)
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
