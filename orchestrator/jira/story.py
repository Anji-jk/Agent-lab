"""Read one Jira story: the story, its epic, and every sub-task, ready for planning.

A story run starts from a story key such as SKM-9. This reads, and only reads:

* the story: summary, description (as plain text), labels, priority, status, its links
* its epic (the story's parent): key and summary
* each sub-task, in the order the story lists them: the same fields, plus the acceptance
  criteria taken from its description, and the sub-tasks it is blocked by

Acceptance criteria are the lines under an "Acceptance criteria" label in the description, up to
the next label (Dependencies, Labels, Priority...). A sub-task without them is still read; the
check before planning (Step 4) decides what to do about it.

Dependencies come from Jira's issue links: a "Blocks" link that says this sub-task "is blocked
by" another. Text such as "Dependencies: <name>" is kept in the description for the planner.

Nothing is written to Jira, saved or printed with the credentials.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from orchestrator.errors import ExecutionRefused
from orchestrator.jira.adf import adf_to_text
from orchestrator.jira.client import JiraClient

STORY_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,9}-[1-9][0-9]{0,9}")
ITEM_FIELDS = ("summary", "description", "issuetype", "status", "labels", "priority",
               "parent", "issuelinks", "project")
STORY_FIELDS = (*ITEM_FIELDS, "subtasks")
_SECTION_LABELS = (
    "acceptance criteria", "dependencies", "dependency", "labels", "label", "priority",
    "description", "business context", "context", "notes", "note", "definition of done",
    "proposed sub-tasks", "proposed subtasks", "sub-tasks", "subtasks", "technical notes",
    "out of scope", "assumptions", "risks",
)
_LABEL_LINE = re.compile(r"^\s*(?:#{1,6}\s*|[-*]\s+)?(?:\*\*)?(?P<label>[A-Za-z][A-Za-z -]*?)"
                         r"(?:\*\*)?\s*:(?:\*\*)?\s*(?P<rest>.*)$")
_HEADING_LINE = re.compile(r"^\s*#{1,6}\s*(?P<label>[A-Za-z][A-Za-z -]*?)\s*$")
_BLOCKS = "blocks"


@dataclass(frozen=True, slots=True)
class Link:
    kind: str
    phrase: str
    key: str
    summary: str


@dataclass(frozen=True, slots=True)
class WorkItem:
    key: str
    type: str
    summary: str
    description: str
    status: str
    done: bool
    labels: tuple[str, ...]
    priority: str | None
    url: str
    links: tuple[Link, ...] = ()
    acceptance_criteria: str | None = None
    blocked_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Epic:
    key: str
    summary: str


@dataclass(frozen=True, slots=True)
class Story:
    story: WorkItem
    epic: Epic | None
    subtasks: tuple[WorkItem, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StoryError(ExecutionRefused):
    """The key does not name a story that can be read."""


def check_story_key(key: str, project: str | None = None) -> str:
    """A story key such as SKM-9, upper case, and in the project's own Jira space."""
    key = (key or "").strip().upper()
    if not STORY_KEY.fullmatch(key):
        raise StoryError(f"{key or '(empty)'} is not a Jira key such as SKM-9.")
    if project and key.split("-")[0] != project.upper():
        raise StoryError(f"{key} is not in this project's Jira space {project.upper()} "
                         "(jira_project in .agent-lab.toml).")
    return key


def acceptance_criteria(description: str) -> str | None:
    """The lines under an "Acceptance criteria" label, up to the next known label."""
    found: list[str] = []
    inside = False
    for line in description.splitlines():
        label, rest = _label(line)
        if label == "acceptance criteria":
            inside = True
            if rest:
                found.append(rest)
            continue
        if inside and label is not None:
            break
        if inside:
            found.append(line)
    text = "\n".join(found).strip("\n")
    lines = text.splitlines()
    if any(line.strip() for line in lines):
        indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
        text = "\n".join(line[indent:] for line in lines).strip()
    return text or None


def _label(line: str) -> tuple[str | None, str]:
    """(the known label this line starts, the text after it), or (None, "")."""
    for pattern in (_HEADING_LINE, _LABEL_LINE):
        match = pattern.match(line)
        if match:
            label = re.sub(r"\s+", " ", match.group("label")).strip().lower()
            if label in _SECTION_LABELS:
                return label, (match.groupdict().get("rest") or "").strip()
    return None, ""


def _links(fields: dict[str, Any]) -> tuple[Link, ...]:
    links: list[Link] = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        kind = link.get("type") or {}
        for side, phrase in (("inwardIssue", kind.get("inward")),
                             ("outwardIssue", kind.get("outward"))):
            other = link.get(side)
            if isinstance(other, dict) and other.get("key"):
                links.append(Link(str(kind.get("name") or ""), str(phrase or ""),
                                  str(other["key"]),
                                  str((other.get("fields") or {}).get("summary") or "")))
    return tuple(links)


def _item(data: dict[str, Any], site: str) -> WorkItem:
    fields = data.get("fields") or {}
    key = str(data.get("key") or "")
    status = fields.get("status") or {}
    description = adf_to_text(fields.get("description"))
    return WorkItem(
        key=key,
        type=str((fields.get("issuetype") or {}).get("name") or ""),
        summary=str(fields.get("summary") or "").strip(),
        description=description,
        status=str(status.get("name") or ""),
        done=str((status.get("statusCategory") or {}).get("key") or "") == "done",
        labels=tuple(str(x) for x in fields.get("labels") or []),
        priority=(fields.get("priority") or {}).get("name"),
        url=f"{site}/browse/{key}",
        links=_links(fields),
        acceptance_criteria=acceptance_criteria(description),
    )


def _blocked_by(item: WorkItem, siblings: set[str]) -> tuple[str, ...]:
    """Sibling sub-tasks this one "is blocked by" (the inward side of a Blocks link)."""
    return tuple(dict.fromkeys(
        link.key for link in item.links
        if link.kind.lower() == _BLOCKS and link.phrase.lower() == "is blocked by"
        and link.key in siblings and link.key != item.key))


def read_story(client: JiraClient, key: str, project: str | None = None) -> Story:
    """The story, its epic and its sub-tasks. Reads only."""
    key = check_story_key(key, project)
    data = client.issue(key, STORY_FIELDS)
    fields = data.get("fields") or {}
    kind = fields.get("issuetype") or {}
    if kind.get("subtask"):
        parent = (fields.get("parent") or {}).get("key")
        raise StoryError(f"{key} is a sub-task" + (f"; start from its story {parent}." if parent
                                                   else "; start from its story."))
    if int(kind.get("hierarchyLevel") or 0) >= 1 or str(kind.get("name") or "").lower() == "epic":
        raise StoryError(f"{key} is an epic; start from one of its stories.")
    story = _item(data, client.base_url)

    parent = fields.get("parent") or {}
    epic = (Epic(str(parent["key"]), str((parent.get("fields") or {}).get("summary") or ""))
            if parent.get("key") else None)

    listed = [str(subtask["key"]) for subtask in fields.get("subtasks") or []
              if isinstance(subtask, dict) and subtask.get("key")]
    found: dict[str, WorkItem] = {}
    if listed:
        for raw in client.search(f"parent = {key} ORDER BY key ASC", ITEM_FIELDS):
            item = _item(raw, client.base_url)
            found[item.key] = item
    order = listed + sorted(found.keys() - set(listed))
    siblings = set(order)
    subtasks = tuple(replace(found[subtask_key],
                             blocked_by=_blocked_by(found[subtask_key], siblings))
                     for subtask_key in order if subtask_key in found)
    return Story(story, epic, subtasks)


def format_story(story: Story) -> str:
    story_item = story.story
    lines = [f"Story:   {story_item.key}  {story_item.summary}",
             f"         {story_item.status}"
             + (f" · {story_item.priority}" if story_item.priority else "")
             + (f" · labels {', '.join(story_item.labels)}" if story_item.labels else ""),
             f"         {story_item.url}"]
    lines.append(f"Epic:    {story.epic.key}  {story.epic.summary}" if story.epic else
                 "Epic:    (none)")
    lines.append(f"Sub-tasks: {len(story.subtasks)}")
    for item in story.subtasks:
        details = [item.status]
        if item.priority:
            details.append(item.priority)
        if item.labels:
            details.append("labels " + ", ".join(item.labels))
        lines.append(f"  {item.key}  {item.summary}")
        lines.append(f"      {' · '.join(details)}")
        lines.append("      blocked by " + ", ".join(item.blocked_by) if item.blocked_by else
                     "      blocked by nothing")
        criteria = item.acceptance_criteria
        if criteria:
            count = sum(1 for line in criteria.splitlines() if line.strip())
            lines.append(f"      acceptance criteria: {count} line{'s' if count != 1 else ''}")
        else:
            lines.append("      acceptance criteria: NONE FOUND")
    missing = [item.key for item in story.subtasks if not item.acceptance_criteria]
    if not story.subtasks:
        lines.append("\nThe story has no sub-tasks.")
    elif missing:
        lines.append(f"\nNo acceptance criteria found in: {', '.join(missing)}. Add an "
                     "\"Acceptance criteria:\" part to their descriptions.")
    return "\n".join(lines)


__all__ = ["Epic", "Link", "Story", "StoryError", "WorkItem", "acceptance_criteria",
           "check_story_key", "format_story", "read_story"]
