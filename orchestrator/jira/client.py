"""Minimal Jira Cloud client using Python's standard library.

It reads, and writes two things, both for ``jira approve``: a Blocks link between two sub-tasks,
and a comment on the blocked one saying why. It never deletes or edits anything.
Authentication is the Agent Lab user's email and API token (HTTP basic auth, as Jira Cloud expects). Neither ever appears in
an error message, and nothing here prints or logs them.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from orchestrator.jira.adf import text_to_adf

USER_AGENT = "agent-lab-orchestrator"


class JiraAPIError(RuntimeError):
    """A Jira request failed. The message never contains the credentials."""


def _explain(code: int, path: str, writing: bool = False) -> str:
    if code == 403 and writing:
        return (f"The Jira account may not change {path} (HTTP 403). It needs the Link work "
                "items and Add comments permissions in the project.")
    if code == 401:
        return ("Jira refused the email and API token (HTTP 401). Check JIRA_EMAIL and "
                "JIRA_API_TOKEN; an API token is made at id.atlassian.com, not your password.")
    if code == 403:
        return f"The Jira account may not read {path} (HTTP 403). Check its project permissions."
    if code == 404:
        return (f"Jira has nothing at {path} (HTTP 404): check jira_url and jira_project in "
                ".agent-lab.toml, and that the account can browse the project.")
    if code == 429:
        return "Jira says too many requests (HTTP 429). Wait a minute and run it again."
    return f"Jira returned HTTP {code} for {path}."


class JiraClient:
    """Read a Jira Cloud site with one account's email and API token."""

    def __init__(self, base_url: str, email: str, token: str, timeout: float = 15.0) -> None:
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("The Jira address must start with https://.")
        if not email or not token:
            raise ValueError("A Jira email and API token are both needed.")
        self.base_url = base_url
        self.timeout = timeout
        pair = f"{email}:{token}".encode("utf-8")
        self._authorization = "Basic " + base64.b64encode(pair).decode("ascii")

    def __repr__(self) -> str:  # never shows the credentials
        return f"JiraClient({self.base_url!r})"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One read-only request; ``path`` starts with /rest/."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/json",
            "Authorization": self._authorization,
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise JiraAPIError(_explain(exc.code, path)) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise JiraAPIError(f"Could not reach Jira at {self.base_url}: "
                               f"{type(exc).__name__}.") from None
        try:
            return json.loads(body.decode("utf-8")) if body else None
        except (UnicodeError, json.JSONDecodeError):
            raise JiraAPIError(f"Jira returned something that is not JSON for {path}.") from None

    def post(self, path: str, body: dict[str, Any]) -> Any:
        """One request that changes something; ``path`` starts with /rest/."""
        request = urllib.request.Request(
            self.base_url + path, method="POST", data=json.dumps(body).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "Authorization": self._authorization, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                answer = response.read()
        except urllib.error.HTTPError as exc:
            raise JiraAPIError(_explain(exc.code, path, writing=True)) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise JiraAPIError(f"Could not reach Jira at {self.base_url}: "
                               f"{type(exc).__name__}.") from None
        try:
            return json.loads(answer.decode("utf-8")) if answer else None
        except (UnicodeError, json.JSONDecodeError):
            return None  # Jira answers 201 with an empty body for a new link

    # --- what jira approve writes ----------------------------------------------------------

    def add_blocks_link(self, blocker: str, blocked: str) -> None:
        """Make ``blocked`` "is blocked by" ``blocker``.

        Jira's names are the wrong way round for this: the issue that *blocks* goes in
        ``inwardIssue``. jira approve reads the link back afterwards to be sure.
        """
        self.post("/rest/api/3/issueLink", {
            "type": {"name": "Blocks"},
            "inwardIssue": {"key": blocker},
            "outwardIssue": {"key": blocked},
        })

    def add_comment(self, key: str, text: str) -> None:
        """A comment on ``key``. (A comment sent with a link lands on the blocker instead.)"""
        self.post(f"/rest/api/3/issue/{key}/comment", {"body": text_to_adf(text)})

    # --- what jira connect reads ---------------------------------------------------------

    def myself(self) -> dict[str, Any]:
        """The account the token belongs to (proves the credentials work)."""
        return self._object(self.get("/rest/api/3/myself"), "the account")

    def project(self, key: str) -> dict[str, Any]:
        """The project (space): id, key, name, and style (classic = company-managed)."""
        return self._object(self.get(f"/rest/api/3/project/{urllib.parse.quote(key)}"),
                            "the project")

    def issue_types(self, project_id: str) -> list[dict[str, Any]]:
        """The work types the project can use, with ``subtask`` and ``hierarchyLevel``."""
        return self._list(self.get("/rest/api/3/issuetype/project",
                                   {"projectId": project_id}), "the work types")

    def project_statuses(self, key: str) -> list[dict[str, Any]]:
        """Every status the project uses, once each (Jira lists them per work type)."""
        per_type = self._list(self.get(f"/rest/api/3/project/{urllib.parse.quote(key)}/statuses"),
                              "the statuses")
        seen: dict[str, dict[str, Any]] = {}
        for entry in per_type:
            for status in entry.get("statuses") or []:
                if isinstance(status, dict) and status.get("id") is not None:
                    seen.setdefault(str(status["id"]), status)
        return list(seen.values())

    def boards(self, key: str) -> list[dict[str, Any]]:
        """The project's boards: id, name and type (kanban or scrum)."""
        data = self._object(self.get("/rest/agile/1.0/board",
                                     {"projectKeyOrId": key, "maxResults": 50}), "the boards")
        return [b for b in data.get("values") or [] if isinstance(b, dict)]

    def board_configuration(self, board_id: int) -> dict[str, Any]:
        """The board's columns and which statuses sit in each."""
        return self._object(self.get(f"/rest/agile/1.0/board/{int(board_id)}/configuration"),
                            "the board's columns")

    def my_permissions(self, key: str, permissions: tuple[str, ...]) -> dict[str, bool]:
        """Which of these project permissions the account has."""
        data = self._object(self.get("/rest/api/3/mypermissions", {
            "projectKey": key, "permissions": ",".join(permissions)}), "the permissions")
        found = data.get("permissions") or {}
        return {name: bool((found.get(name) or {}).get("havePermission")) for name in permissions}

    # --- what reading a story needs ------------------------------------------------------

    def issue(self, key: str, fields: tuple[str, ...]) -> dict[str, Any]:
        """One work item with only these fields."""
        return self._object(self.get(f"/rest/api/3/issue/{urllib.parse.quote(key)}",
                                     {"fields": ",".join(fields)}), f"work item {key}")

    def search(self, jql: str, fields: tuple[str, ...], *, page_size: int = 100,
               max_items: int = 500) -> list[dict[str, Any]]:
        """Every work item a JQL query finds, page by page (Jira's /search/jql)."""
        found: list[dict[str, Any]] = []
        token: str | None = None
        while len(found) < max_items:
            params: dict[str, Any] = {"jql": jql, "fields": ",".join(fields),
                                      "maxResults": page_size}
            if token:
                params["nextPageToken"] = token
            page = self._object(self.get("/rest/api/3/search/jql", params), "the search")
            found += [item for item in page.get("issues") or [] if isinstance(item, dict)]
            token = page.get("nextPageToken")
            if not token or page.get("isLast"):
                break
        return found[:max_items]

    @staticmethod
    def _object(data: Any, what: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise JiraAPIError(f"Unexpected answer from Jira for {what}.")
        return data

    @staticmethod
    def _list(data: Any, what: str) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            raise JiraAPIError(f"Unexpected answer from Jira for {what}.")
        return [item for item in data if isinstance(item, dict)]
