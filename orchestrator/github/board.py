"""Which board, and which column each task state goes in.

``board connect`` reads the project once and saves what the sync needs in
``runs/orchestration/board.json``: the project, its Status field, and the
option (column) for each of four places a task can be:

* ``todo``: not started (waiting or ready)
* ``in_progress``: building, testing or reviewing
* ``attention``: a person should look (failed after its retry, needs review,
  cancelled, interrupted, or blocked by such a task)
* ``done``: done

The columns are matched by their names on your board, so they need not be
called exactly this; a name that cannot be matched is asked for. The token is
never saved. Reading the board changes nothing on GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.github.client import GitHubAPIError
from orchestrator.github.projects import PROJECT_TOKEN_VARIABLE, ProjectClient
from orchestrator.records.run_registry import utc_now, write_json_atomic

BOARD_FILE_NAME = "board.json"
PLACES = ("todo", "in_progress", "attention", "done")
PLACE_LABELS = {"todo": "not started", "in_progress": "running",
                "attention": "waiting for a person", "done": "done"}
# Names each place is recognised by, lower case. Anything else: name it with --<place>.
KNOWN_NAMES = {
    "todo": ("todo", "to do", "backlog", "not started"),
    "in_progress": ("in progress", "in-progress", "doing", "running"),
    "attention": ("needs review", "needs a person", "needs attention", "blocked", "attention"),
    "done": ("done", "complete", "completed", "finished"),
}
STATUS_FIELD = "status"


class BoardError(ExecutionRefused):
    """The board could not be used as it is."""


@dataclass
class Connection:
    config: dict[str, Any]
    checks: list[tuple[bool | None, str]] = field(default_factory=list)  # (ok?, what); None = unknown


def board_path(runs_dir: Path) -> Path:
    return Path(runs_dir) / BOARD_FILE_NAME


def _option_name(option: dict[str, Any]) -> str:
    name = option.get("name")
    if isinstance(name, dict):  # the REST API gives {"raw": ..., "html": ...}
        name = name.get("raw") or name.get("html")
    return str(name or "").strip()


def match_columns(options: list[dict[str, Any]],
                  chosen: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """Each place's column {name, option_id}, by the names on the board."""
    chosen = {k: v for k, v in (chosen or {}).items() if v}
    by_name = {_option_name(o).lower(): o for o in options if _option_name(o)}
    listed = ", ".join(_option_name(o) for o in options) or "(none)"
    columns: dict[str, dict[str, str]] = {}
    for place in PLACES:
        if place in chosen:
            option = by_name.get(chosen[place].strip().lower())
            if option is None:
                raise BoardError(f"The board has no column called {chosen[place]!r}. "
                                 f"Its columns are: {listed}.")
        else:
            option = next((by_name[n] for n in KNOWN_NAMES[place] if n in by_name), None)
            if option is None:
                flag = "--" + place.replace("_", "-")
                raise BoardError(f"No column for tasks that are {PLACE_LABELS[place]}. Its "
                                 f"columns are: {listed}. Name one with {flag} \"<column>\".")
        columns[place] = {"name": _option_name(option), "option_id": str(option.get("id"))}
    used = [c["name"] for c in columns.values()]
    if len(set(used)) != len(used):
        raise BoardError("Two places were given the same column: "
                         + ", ".join(f"{p} = {c['name']}" for p, c in columns.items()) + ".")
    return columns


def _status_field(fields: list[dict[str, Any]]) -> dict[str, Any]:
    for item in fields:
        if str(item.get("name", "")).strip().lower() == STATUS_FIELD \
                and item.get("data_type") == "single_select":
            return item
    names = ", ".join(str(f.get("name")) for f in fields) or "(none)"
    raise BoardError(f"The project has no single-select Status field (its fields: {names}). "
                     "The Board template has one; its options are the board's columns.")


def _token_checks(client: ProjectClient, scopes: frozenset[str] | None) -> list[tuple[bool | None, str]]:
    checks: list[tuple[bool | None, str]] = [
        (True, f"The token in {client.token_variable} can read the project.")]
    if scopes is None:
        checks.append((None, "GitHub does not say what this token may do (it is not a classic "
                             "token), so writing to the board is untested until 5.2."))
    elif "project" in scopes:
        checks.append((True, "It may write to the board (classic token with the 'project' scope)."))
    else:
        checks.append((False, "It may not write to the board: it has "
                              + (", ".join(sorted(scopes)) or "no scopes")
                              + ". Give the classic token the 'project' scope."))
    if scopes is not None and not scopes & {"repo", "public_repo"}:
        checks.append((None, "It has no repository scope. That is right when GITHUB_TOKEN "
                             "(fine-grained, Issues: read and write) does the Issue work; "
                             "adding a private repository's Issue to the board may need more."))
    return checks


def connect_board(owner: str, number: int, repository: str, *, client: ProjectClient,
                  chosen: dict[str, str] | None = None) -> Connection:
    """Read the board and work out the configuration. Nothing is written to GitHub."""
    project = client.project()
    body = project.body if isinstance(project.body, dict) else {}
    status = _status_field(client.fields())
    options = [o for o in status.get("options") or [] if isinstance(o, dict)]
    columns = match_columns(options, chosen)
    config = {
        "owner": owner,
        "number": number,
        "repository": repository,
        "title": body.get("title"),
        "project_id": body.get("id"),
        "project_node_id": body.get("node_id"),
        "url": f"https://github.com/users/{owner}/projects/{number}",
        "status_field_id": status.get("id"),
        "status_field_node_id": status.get("node_id"),
        "columns": columns,
        "token_variable": client.token_variable,  # the name only, never the token
        "connected_at": utc_now(),
    }
    return Connection(config, _token_checks(client, project.scopes))


def save_board(runs_dir: Path, config: dict[str, Any]) -> Path:
    path = board_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, config)
    return path


def format_connection(connection: Connection) -> str:
    c = connection.config
    lines = [f"Board:    {c.get('title') or '(untitled)'}  {c['url']}",
             f"Issues:   {c['repository']}", "Columns:"]
    for place in PLACES:
        lines.append(f"  {c['columns'][place]['name']:<16} for tasks that are {PLACE_LABELS[place]}")
    lines.append("Token:")
    mark = {True: "ok ", False: "NO ", None: "?  "}
    lines += [f"  {mark[ok]} {text}" for ok, text in connection.checks]
    return "\n".join(lines)


__all__ = [
    "BOARD_FILE_NAME", "BoardError", "Connection", "GitHubAPIError", "PLACES",
    "PROJECT_TOKEN_VARIABLE", "board_path", "connect_board", "format_connection",
    "match_columns", "save_board",
]
