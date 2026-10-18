
"""Command-line entry point for the Agent Lab orchestrator."""

from __future__ import annotations

import argparse
import sys

from orchestrator.github_client import GitHubAPIError, GitHubClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator",
        description="GitHub-driven Claude Code agent orchestration.",
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


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "issue":
        try:
            client = GitHubClient(repository=args.repo)
            issue = client.get_issue(args.number)
            display_issue(issue)
            return 0
        except (GitHubAPIError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    parser.error("Unknown command.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
