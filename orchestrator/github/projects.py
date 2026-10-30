"""Read a GitHub Project (the board) through GitHub's REST API.

GitHub added REST endpoints for Projects in September 2025, so the board needs
no GraphQL and no new library: the same standard-library requests as the
Issues client.

Which token: a project owned by a user account (``users/<owner>/projectsV2``)
cannot be used with a fine-grained token; GitHub's reference says so for every
user-project endpoint. A classic token with the ``project`` scope is needed for
the board. So the board uses ``AGENT_LAB_PROJECT_TOKEN`` when it is set, and
``GITHUB_TOKEN`` otherwise: a fine-grained ``GITHUB_TOKEN`` can then stay
limited to this repository's Issues while a classic token with only the
``project`` scope handles the board.

Neither token reaches an agent: agents get an allowlist of variables
(``scripts/agent_env.py``) that names neither. Tokens never appear in errors.

It reads (5.1), adds Issues to the board (5.2) and moves cards between columns (5.3).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestrator.github.client import GitHubAPIError
from orchestrator.settings import PROJECT_TOKEN, load_settings

PROJECT_TOKEN_VARIABLE = PROJECT_TOKEN
API = "https://api.github.com"
MAX_PAGES = 20
# After GitHub says an Issue is already on the board, its card may not be in the list yet
# (the board's own "add sub-issues" rule just put it there): look a few times, a moment apart.
CARD_LOOKUPS = 5
CARD_LOOKUP_WAIT_SECONDS = 2.0

Opener = Callable[..., Any]  # urllib.request.urlopen, or a fake in tests


def project_token() -> tuple[str | None, str]:
    """(token, the variable it came from) for the board."""
    return load_settings().board_token()


class ProjectAPIError(GitHubAPIError):
    """GitHub refused a request about the board; ``code`` is its HTTP status."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Response:
    body: Any
    scopes: frozenset[str] | None  # a classic token's scopes; None for other tokens
    next_url: str | None = None    # the next page, from the Link header


def _next_link(header: str | None) -> str | None:
    """The rel="next" address of a Link header; GitHub pages project lists this way."""
    for part in (header or "").split(","):
        if 'rel="next"' in part and "<" in part and ">" in part:
            return part[part.index("<") + 1:part.index(">")]
    return None


class ProjectClient:
    """One user-owned project: ``https://github.com/users/<owner>/projects/<number>``."""

    def __init__(self, owner: str, number: int, token: str | None = None,
                 token_variable: str = PROJECT_TOKEN_VARIABLE,
                 timeout: float = 15.0, opener: Opener | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not owner or "/" in owner:
            raise ValueError("The project owner is a GitHub user name, such as Anji-jk.")
        if number < 1:
            raise ValueError("The project number is the number in its address, such as 2.")
        self.owner, self.number = owner, number
        self.token, self.token_variable = token, token_variable
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self.base = f"{API}/users/{urllib.parse.quote(owner)}/projectsV2/{number}"

    def _get(self, path: str = "") -> Response:
        return self._get_url(self.base + path)

    def _get_url(self, url: str) -> Response:
        if not url.startswith(API + "/"):  # a Link header never sends the token elsewhere
            raise GitHubAPIError("GitHub sent a next page outside api.github.com; not followed.")
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "agent-lab-orchestrator",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._open(request, timeout=self.timeout) as response:
                headers_in = response.headers
                raw = headers_in.get("X-OAuth-Scopes") if headers_in else None
                link = headers_in.get("Link") if headers_in else None
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ProjectAPIError(self._explain(exc.code), exc.code) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubAPIError(f"Could not reach GitHub: {type(exc).__name__}.") from None
        except json.JSONDecodeError:
            raise GitHubAPIError("GitHub returned something that is not JSON.") from None
        scopes = (frozenset(s.strip() for s in raw.split(",") if s.strip())
                  if raw is not None else None)
        return Response(body, scopes, _next_link(link))

    def _pages(self, path: str) -> list[dict[str, Any]]:
        """Every entry of a list, following GitHub's next-page links."""
        found: list[dict[str, Any]] = []
        response = self._get(path)
        for _ in range(MAX_PAGES):
            if not isinstance(response.body, list):
                raise GitHubAPIError("GitHub's answer for a project list was not a list.")
            found += [entry for entry in response.body if isinstance(entry, dict)]
            if not response.next_url:
                return found
            response = self._get_url(response.next_url)
        return found

    def _explain(self, code: int) -> str:
        where = f"project {self.number} of {self.owner}"
        if code in (401, 403, 404) and not self.token:
            return (f"GitHub returned HTTP {code} for {where}: no token is set. Set "
                    f"{PROJECT_TOKEN_VARIABLE} to a classic token with the 'project' scope.")
        if code in (401, 403, 404):
            return (f"GitHub returned HTTP {code} for {where} with the token in "
                    f"{self.token_variable}. A user's project needs a classic token with the "
                    "'project' scope; fine-grained tokens do not work for it. Check the "
                    "owner and the number too: GitHub answers 404 for a project it will not show.")
        return f"GitHub returned HTTP {code} for {where}."

    def _send(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        """A write to the board Needs the classic token's 'project' scope."""
        if not self.token:
            raise GitHubAPIError(self._explain(401))
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"), method=method,
            headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json",
                     "User-Agent": "agent-lab-orchestrator", "X-GitHub-Api-Version": "2022-11-28",
                     "Authorization": f"Bearer {self.token}"})
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ProjectAPIError(self._explain(exc.code), exc.code) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubAPIError(f"Could not reach GitHub: {type(exc).__name__}.") from None
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            raise GitHubAPIError("GitHub returned something that is not JSON.") from None

    def issue_item(self, issue_id: int) -> int | None:
        """The card of an Issue (by its ``id``) already on the board, or None."""
        for item in self._pages("/items?per_page=100"):
            content = item.get("content")
            if item.get("content_type") == "Issue" and isinstance(content, dict) \
                    and content.get("id") == issue_id and isinstance(item.get("id"), int):
                return item["id"]
        return None

    def add_issue(self, issue_id: int) -> int:
        """Put an Issue on the board (by its ``id``); returns the board item's id.

        GitHub refuses (HTTP 422) to add an Issue that is already on the board, as
        your board's #4 showed live; then its existing card is looked up and used. A board
        rule that adds new sub-issues puts the card there a moment before this request, and
        GitHub's list can lag behind, so the lookup is repeated a few times before giving up.
        """
        try:
            body = self._send("POST", "/items", {"type": "Issue", "id": issue_id})
        except ProjectAPIError as exc:
            if exc.code != 422:
                raise
            existing = self.issue_item(issue_id)
            for _ in range(CARD_LOOKUPS - 1):
                if existing is not None:
                    break
                self._sleep(CARD_LOOKUP_WAIT_SECONDS)
                existing = self.issue_item(issue_id)
            if existing is None:
                raise GitHubAPIError(
                    f"GitHub refused to add Issue id {issue_id} to project {self.number} "
                    "(HTTP 422) and it is not on the board either. Check that the Issue "
                    "exists and that the board's token may see its repository.") from None
            return existing
        item = body.get("value", body) if isinstance(body, dict) and "id" not in body else body
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise GitHubAPIError("Unexpected response when adding an Issue to the board.")
        return item["id"]

    def set_field(self, item_id: int, field_id: int, option_id: str) -> None:
        """Set a single-select field (the Status column) of a card to one of its options."""
        self._send("PATCH", f"/items/{item_id}", {"fields": [{"id": field_id, "value": option_id}]})

    def project(self) -> Response:
        return self._get()

    def fields(self) -> list[dict[str, Any]]:
        return self._pages("/fields?per_page=100")