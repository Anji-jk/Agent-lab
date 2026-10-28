"""board connect / board sync, and the board work run-plan does by itself."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from orchestrator.cli.exit_codes import EXIT_OK
from orchestrator.errors import ExecutionRefused
from orchestrator.github.board import board_path, connect_board, format_connection, save_board
from orchestrator.github.client import GitHubAPIError, GitHubClient
from orchestrator.github.projects import ProjectClient, project_token
from orchestrator.github.publish import format_published, load_board, publish
from orchestrator.scheduling.board_sync import sync_board


def run_board(args: argparse.Namespace) -> int:
    if args.board_command == "sync":
        clients = _board_clients(args.runs_dir)
        # Tasks that are not on the board yet are published first (what board publish did).
        published = publish(args.run_id, plans_dir=args.plans_dir, runs_dir=args.runs_dir,
                            issues=clients[0], project=clients[1], write=True)
        if any(card.done for card in published.cards):
            print(format_published(published))
        print(sync_board(args.run_id, plans_dir=args.plans_dir, runs_dir=args.runs_dir,
                         issues=clients[0], project=clients[1]).summary())
        return EXIT_OK
    owner = args.owner or args.repo.split("/")[0]
    token, variable = project_token()
    client = ProjectClient(owner, args.project, token=token, token_variable=variable)
    chosen = {"todo": args.todo, "in_progress": args.in_progress,
              "attention": args.attention, "done": args.done}
    connection = connect_board(owner, args.project, args.repo, client=client, chosen=chosen)
    print(format_connection(connection))
    print(f"\nSaved: {save_board(args.runs_dir, connection.config)}")
    print("Nothing was written to GitHub.")
    return EXIT_OK


def _board_clients(runs_dir: Path) -> tuple[GitHubClient, ProjectClient]:
    """The Issue and board clients for the connected board (BoardError/PublishError if none)."""
    board = load_board(runs_dir)
    token, variable = project_token()
    project = ProjectClient(board["owner"], int(board["number"]), token=token,
                            token_variable=variable)
    return GitHubClient(repository=board["repository"]), project


class BoardFollower:
    """run-plan's board work: put the tasks on it once, then keep it in line.

    Whatever goes wrong on GitHub is printed and the run goes on: the board shows
    the run, it never stops it. board sync catches up afterwards.
    """

    def __init__(self, args: argparse.Namespace):
        self.args, self.clients = args, None
        if getattr(args, "no_board", False) or not board_path(args.runs_dir).exists():
            return
        try:
            self.clients = _board_clients(args.runs_dir)
        except (ExecutionRefused, GitHubAPIError) as exc:
            self._warn(f"not used: {exc}")

    def _warn(self, text: str) -> None:
        print(f"Board {text} The run goes on; python -m orchestrator board sync "
              f"{self.args.run_id} catches up later.", file=sys.stderr)

    def start(self) -> None:
        if self.clients is None:
            return
        try:  # option B: the tasks reach GitHub when you start the run, not when planned
            result = publish(self.args.run_id, plans_dir=self.args.plans_dir,
                             runs_dir=self.args.runs_dir, issues=self.clients[0],
                             project=self.clients[1], write=True)
        except (ExecutionRefused, GitHubAPIError) as exc:
            self._warn(f"publish failed: {exc}")
            return
        made = [c for c in result.cards if c.done]
        if made:
            print(f"Board: put {len(made)} task(s) on {result.board_url}", flush=True)
        self.sync()

    def sync(self, starting: str | None = None) -> None:
        if self.clients is None:
            return
        try:
            synced = sync_board(self.args.run_id, plans_dir=self.args.plans_dir,
                                runs_dir=self.args.runs_dir, issues=self.clients[0],
                                project=self.clients[1], starting=starting)
        except (ExecutionRefused, GitHubAPIError) as exc:
            self._warn(f"sync failed: {exc}")
            return
        if synced.moved or synced.closed or synced.problems:
            print(synced.summary(), flush=True)
