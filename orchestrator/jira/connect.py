"""Which Jira space, board, work types and statuses a project uses.

``jira connect`` reads the project's Jira space once and saves what later steps need in
``runs/jira.json`` (beside the GitHub board's ``board.json``): the space, its board, the work
type for each level, and the status for each of four places an item can be:

* ``todo``: not started (waiting or ready; a blocked one also gets the label agentlab-blocked)
* ``in_progress``: being built, tested or reviewed by the agents
* ``in_review``: "In Review", a person must look: failed after its retry, cancelled,
  interrupted, or the reviewer said it is not correct
* ``done``: verified and committed on its run branch

Merged is not a status: a Done item carries the label agentlab-merged once its run branch is in
main, and agentlab-unmerged until then.

The levels use Jira's own work types only: Epic, then Story (or Task, or Bug) under it, then
Sub-task under a Story. A Story's pieces of work are its Sub-tasks, and a bug found while
building it is a Sub-task too, labelled agentlab-bug, so its parent says which Story it came
from. Task and Bug are optional: a Task holds small work that belongs to no Story, and a Bug
one reported apart from a Story (linked to the Story it affects).

Statuses and work types are matched by their names in your Jira, so they need not be called
exactly this; a name that cannot be matched can be given on the command line. Every problem
found is listed at once, and the file is saved only when none blocks the integration.
Nothing is written to Jira, and the token is never saved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.jira.client import JiraClient
from orchestrator.records.run_registry import utc_now, write_json_atomic

JIRA_FILE_NAME = "jira.json"
PLACES = ("todo", "in_progress", "in_review", "done")
PLACE_LABELS = {"todo": "not started", "in_progress": "being built",
                "in_review": "waiting for a person", "done": "done on its run branch"}
# Names each place is recognised by, lower case. Anything else: name it with --<place>.
KNOWN_STATUS_NAMES = {
    "todo": ("to do", "todo", "backlog", "open", "not started"),
    "in_progress": ("in progress", "in-progress", "doing", "running"),
    "in_review": ("in review", "needs attention", "needs review", "needs a person", "in_review"),
    "done": ("done", "complete", "completed", "finished"),
}
# Labels Agent Lab puts on items; saved in jira.json so later steps use the same words.
LABELS = {
    "merged": "agentlab-merged",      # Done, and its run branch is in main
    "unmerged": "agentlab-unmerged",  # Done, but its run branch is not in main yet
    "blocked": "agentlab-blocked",    # in To Do, waiting on work that is not finished
    "bug": "agentlab-bug",            # a Sub-task that fixes a bug found while building its Story
}
LEVELS = ("epic", "story", "subtask", "task", "bug")
LEVEL_LABELS = {"epic": "Epics", "story": "Stories", "subtask": "Sub-tasks", "task": "Tasks",
                "bug": "Bugs"}
# Jira's own work type names for each level (lower case). Older sites call it "Sub-task".
KNOWN_TYPE_NAMES = {
    "epic": ("epic",),
    "story": ("story",),
    "subtask": ("subtask", "sub-task"),
    "task": ("task",),
    "bug": ("bug",),
}
SUBTASK_LEVELS = frozenset({"subtask"})  # sits under a Story, so must be a sub-task type
OPTIONAL_LEVELS = {  # what happens without one
    "task": "small work that belongs to no Story becomes a Story of its own.",
    "bug": "a bug reported apart from a Story becomes a Story labelled agentlab-bug.",
}
PERMISSIONS = ("BROWSE_PROJECTS", "CREATE_ISSUES", "EDIT_ISSUES", "TRANSITION_ISSUES",
               "ADD_COMMENTS", "LINK_ISSUES")
COMPANY_MANAGED = "classic"  # Jira's name for company-managed; team-managed is "next-gen"

Check = tuple[bool | None, str]  # (ok?, what); False blocks saving, None is a warning


class JiraConnectError(ExecutionRefused):
    """The Jira space cannot be connected as it is."""


@dataclass
class Connection:
    config: dict[str, Any]
    checks: list[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(ok is not False for ok, _ in self.checks)


def jira_path(runs_dir: Path) -> Path:
    return Path(runs_dir) / JIRA_FILE_NAME


def _name(item: dict[str, Any]) -> str:
    return str(item.get("name") or "").strip()


def match_statuses(statuses: list[dict[str, Any]], chosen: dict[str, str] | None = None
                   ) -> tuple[dict[str, dict[str, str]], list[Check]]:
    """Each place's status {id, name}, and a check per place that could not be matched."""
    chosen = {k: v for k, v in (chosen or {}).items() if v}
    by_name = {_name(s).lower(): s for s in statuses if _name(s)}
    listed = ", ".join(sorted(_name(s) for s in statuses if _name(s))) or "(none)"
    places: dict[str, dict[str, str]] = {}
    checks: list[Check] = []
    for place in PLACES:
        wanted = chosen.get(place)
        if wanted:
            status = by_name.get(wanted.strip().lower())
        else:
            status = next((by_name[n] for n in KNOWN_STATUS_NAMES[place] if n in by_name), None)
        if status is None:
            flag = "--" + place.replace("_", "-")
            what = f"called {wanted!r}" if wanted else f"for items that are {PLACE_LABELS[place]}"
            checks.append((False, f"No status {what}. The space's statuses are: {listed}. "
                                  f"Add one to the workflow, or name it with {flag} \"<status>\"."))
            continue
        places[place] = {"id": str(status.get("id")), "name": _name(status)}
    used = [p["id"] for p in places.values()]
    if len(set(used)) != len(used):
        checks.append((False, "Two places got the same status: " + ", ".join(
            f"{place} = {p['name']}" for place, p in places.items()) + "."))
    return places, checks


def match_types(types: list[dict[str, Any]], chosen: dict[str, str] | None = None
                ) -> tuple[dict[str, dict[str, str] | None], list[Check]]:
    """Each level's work type {id, name} (None for a missing optional level), and checks."""
    chosen = {k: v for k, v in (chosen or {}).items() if v}
    checks: list[Check] = []
    found: dict[str, dict[str, str] | None] = {}
    taken: set[str] = set()
    for level in LEVELS:
        subtask = level in SUBTASK_LEVELS
        fitting = {_name(t).lower(): t for t in types
                   if _name(t) and bool(t.get("subtask")) == subtask
                   and str(t.get("id")) not in taken}
        names = (chosen[level].strip().lower(),) if level in chosen else KNOWN_TYPE_NAMES[level]
        kind = next((fitting[n] for n in names if n in fitting), None)
        if kind is None and subtask and level not in chosen and len(fitting) == 1:
            kind = next(iter(fitting.values()))  # the site's only sub-task type, renamed

        if kind is None:
            found[level] = None
            same_name_wrong_kind = [t for t in types if _name(t).lower() in names
                                    and bool(t.get("subtask")) != subtask]
            flag = f"--{level}-type"
            if same_name_wrong_kind:
                why = (f"{_name(same_name_wrong_kind[0])!r} exists but is "
                       + ("a standard type; it must be a sub-task type to sit under a Story."
                          if subtask else "a sub-task type; it must be a standard type."))
            else:
                listed = ", ".join(sorted(_name(t) for t in types if _name(t))) or "(none)"
                why = f"None of its work types fit (it has: {listed})."
            if level in OPTIONAL_LEVELS:
                checks.append((None, f"No type for {LEVEL_LABELS[level]}: {why} Without it, "
                                     f"{OPTIONAL_LEVELS[level]} Or name one with {flag}."))
            else:
                checks.append((False, f"No {'sub-task ' if subtask else ''}type for "
                                      f"{LEVEL_LABELS[level]}: {why} Name one with {flag}."))
            continue
        taken.add(str(kind.get("id")))
        found[level] = {"id": str(kind.get("id")), "name": _name(kind)}
    return found, checks


def choose_board(boards: list[dict[str, Any]], board_id: int | None) -> dict[str, Any]:
    if board_id is not None:
        for board in boards:
            if str(board.get("id")) == str(board_id):
                return board
        raise JiraConnectError(f"The space has no board with id {board_id}. "
                               + _list_boards(boards))
    if not boards:
        raise JiraConnectError("The space has no board. Create a Kanban board for it in Jira, "
                               "then run jira connect again.")
    if len(boards) > 1:
        raise JiraConnectError("The space has more than one board; choose one with --board <id>. "
                               + _list_boards(boards))
    return boards[0]


def _list_boards(boards: list[dict[str, Any]]) -> str:
    return "Its boards: " + (", ".join(f"{b.get('id')} ({_name(b)}, {b.get('type')})"
                                        for b in boards) or "(none)") + "."


def _column_of(status_id: str, columns: list[dict[str, Any]]) -> str | None:
    for column in columns:
        if any(str(s.get("id")) == status_id for s in column.get("statuses") or []
               if isinstance(s, dict)):
            return _name(column)
    return None


def connect_jira(client: JiraClient, project_key: str, *, board_id: int | None = None,
                 statuses: dict[str, str] | None = None,
                 types: dict[str, str] | None = None) -> Connection:
    """Read the space and work out the configuration. Nothing is written to Jira."""
    checks: list[Check] = []
    me = client.myself()
    checks.append((True, f"Signed in as {me.get('displayName') or 'the Agent Lab user'}."))

    project = client.project(project_key)
    style = str(project.get("style") or "")
    if style == COMPANY_MANAGED:
        checks.append((True, "The space is company-managed: card moves can be limited to the "
                             "Agent Lab user."))
    else:
        checks.append((None, "The space is team-managed: card moves cannot be limited to one "
                             "user, so the sync will put moved cards back and warn instead."))

    kinds, kind_checks = match_types(client.issue_types(str(project.get("id"))), types)
    checks += kind_checks
    places, place_checks = match_statuses(client.project_statuses(project_key), statuses)
    checks += place_checks

    board = choose_board(client.boards(project_key), board_id)
    if str(board.get("type")) == "kanban":
        checks.append((True, f"Board {_name(board)!r} is a Kanban board."))
    elif str(board.get("type")) == "simple":
        # Team-managed spaces report every board as "simple", Kanban and Scrum alike.
        checks.append((None, f"Board {_name(board)!r} is a team-managed board, so Jira does not "
                             "say whether it is Kanban or Scrum. In the board's settings, make "
                             "sure sprints are off, or new items stay in the backlog."))
    else:
        checks.append((False, f"Board {_name(board)!r} is a {board.get('type')} board. Use a "
                              "Kanban board: a Scrum board keeps new items in the backlog until "
                              "a sprint starts."))
    configuration = client.board_configuration(int(board["id"]))
    columns = [c for c in (configuration.get("columnConfig") or {}).get("columns") or []
               if isinstance(c, dict)]
    for place, status in places.items():
        column = _column_of(status["id"], columns)
        status["column"] = column or ""
        if column is None:
            checks.append((False, f"Status {status['name']!r} is on no column of the board, so "
                                  "its items would not show. Map it to a column in the board's "
                                  "settings."))

    allowed = client.my_permissions(project_key, PERMISSIONS)
    missing = [name for name, ok in allowed.items() if not ok]
    if missing:
        checks.append((False, "The account lacks these project permissions: "
                              + ", ".join(missing) + "."))
    else:
        checks.append((True, "The account may browse, create, edit, move, comment and link."))
    checks.append((None, "Workflow transitions are listed per item in Jira, so they are tried "
                         "on a test item later, not here."))

    config = {
        "url": client.base_url,
        "project": {"key": project.get("key") or project_key, "id": str(project.get("id")),
                    "name": project.get("name"), "style": style},
        "board": {"id": board.get("id"), "name": _name(board), "type": board.get("type")},
        "types": kinds,
        "places": places,
        "labels": dict(LABELS),
        "account": me.get("displayName"),
        "credentials": ["JIRA_EMAIL", "JIRA_API_TOKEN"],  # the names only, never the values
        "connected_at": utc_now(),
    }
    return Connection(config, checks)


def save_jira(runs_dir: Path, config: dict[str, Any]) -> Path:
    path = jira_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, config)
    return path


def format_connection(connection: Connection) -> str:
    c = connection.config
    project, board = c["project"], c["board"]
    lines = [f"Jira:     {c['url']}",
             f"Space:    {project['key']} ({project.get('name') or 'unnamed'})",
             f"Board:    {board['name']} ({board['type']}, id {board['id']})",
             "Work types:"]
    for level in LEVELS:
        kind = c["types"].get(level)
        lines.append(f"  {LEVEL_LABELS[level]:<10} {kind['name'] if kind else '(none)'}")
    lines.append("Statuses:")
    for place in PLACES:
        status = c["places"].get(place)
        where = (f"{status['name']} (column {status['column'] or 'none'})"
                 if status else "(none)")
        lines.append(f"  {PLACE_LABELS[place]:<24} {where}")
    labels = c.get("labels") or {}
    lines.append("Labels:   " + ", ".join(labels.values()))
    lines.append("Checks:")
    mark = {True: "ok ", False: "NO ", None: "?  "}
    lines += [f"  {mark[ok]} {text}" for ok, text in connection.checks]
    return "\n".join(lines)


__all__ = [
    "Connection", "JIRA_FILE_NAME", "JiraConnectError", "LEVELS", "PLACES", "choose_board",
    "connect_jira", "format_connection", "jira_path", "match_statuses", "match_types",
    "save_jira",
]
