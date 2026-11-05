"""Approving a story plan: jira approve.

The plan's ``approval`` block starts as ``waiting`` with the Blocks links the planner found
(the dependencies that are not yet links in Jira). Approving adds each link in Jira, then a
comment on the blocked sub-task giving the reason, records both in plan.json as it goes
(``written_at``, ``comment_at``), reads the links back, and only then marks the plan
``approved``. A stop half-way keeps what was done; running it again finishes the rest. A link
already in Jira is not added again, and gets no comment. Nothing is ever deleted.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.jira.client import JiraAPIError, JiraClient
from orchestrator.jira.story import Story, read_story
from orchestrator.planning.plan_file import save_approval
from orchestrator.planning.story_plan import APPROVED, link_comment
from orchestrator.records.run_registry import utc_now


@dataclass
class Approval:
    """What approving did: the plan as stored now, and the links added or found there."""
    record: dict[str, Any]
    added: list[dict[str, str]] = field(default_factory=list)
    already: list[dict[str, str]] = field(default_factory=list)
    commented: list[dict[str, str]] = field(default_factory=list)


def missing_comment(link: dict[str, str]) -> bool:
    """A link Agent Lab added whose comment is not in Jira yet."""
    return link.get("how") == "added" and not link.get("comment_at")


def _write(action, arguments: tuple[str, ...], what: str, done: Approval) -> None:
    try:
        action(*arguments)
    except JiraAPIError as exc:
        before = ", ".join(_phrase(item) for item in done.added) or "none"
        raise JiraAPIError(
            f"Could not {what} in Jira: {exc} Links added before it: {before}. The plan still "
            "waits for approval; run jira approve again to finish.") from None


def _phrase(link: dict[str, str]) -> str:
    return f"{link['subtask']} is blocked by {link['blocked_by']}"


def _blocked_by(story: Story) -> dict[str, set[str]]:
    return {item.key: set(item.blocked_by) for item in story.subtasks}


def approve_story_plan(client: JiraClient, plans_dir: Path, run_id: str,
                       record: dict[str, Any], story: Story, project: str | None) -> Approval:
    """Write the plan's links to Jira and mark it approved. ``story`` is Jira as read just now."""
    approval = copy.deepcopy(record["approval"])
    approved = approval.get("status") == APPROVED
    links = approval.get("jira_links") or []
    done = Approval(record)
    now = _blocked_by(story)
    for link in (item for item in links if not item.get("written_at")):
        if link["blocked_by"] in now.get(link["subtask"], set()):
            link.update(written_at=utc_now(), how="already in Jira")
            done.already.append(link)
        else:
            _write(client.add_blocks_link, (link["blocked_by"], link["subtask"]),
                   f"add {_phrase(link)}", done)
            link.update(written_at=utc_now(), how="added")
            done.added.append(link)
        done.record = save_approval(plans_dir, run_id, approval)
    if done.added:
        _read_back(client, record, links, project, plans_dir, run_id, approval)

    # The comment goes on the blocked sub-task, once its link reads right in Jira.
    for link in (item for item in links if missing_comment(item)):
        _write(client.add_comment, (link["subtask"], link_comment(link, record)),
               f"add the comment on {link['subtask']}", done)
        link["comment_at"] = utc_now()
        done.commented.append(link)
        done.record = save_approval(plans_dir, run_id, approval)
    if not approved:
        approval.update(status=APPROVED, approved_at=utc_now())
        done.record = save_approval(plans_dir, run_id, approval)
    return done


def _read_back(client: JiraClient, record: dict[str, Any], links: list[dict[str, str]],
               project: str | None, plans_dir: Path, run_id: str,
               approval: dict[str, Any]) -> None:
    """Check in Jira that every added link reads the right way round."""
    now = _blocked_by(read_story(client, record["source"]["key"], project))
    for link in links:
        if link.get("how") != "added" or link["blocked_by"] in now.get(link["subtask"], set()):
            continue
        if link["subtask"] in now.get(link["blocked_by"], set()):
            raise JiraAPIError(
                f"Jira shows the link the wrong way round: {link['blocked_by']} is blocked by "
                f"{link['subtask']}. Remove that link in Jira by hand (Agent Lab never deletes) "
                "and report this; the plan still waits for approval.")
        for key in ("written_at", "how"):  # not there: written again next time
            link.pop(key, None)
        save_approval(plans_dir, run_id, approval)
        raise JiraAPIError(
            f"Jira accepted {_phrase(link)} but does not show it. The plan still waits for "
            "approval; run jira approve again.")
