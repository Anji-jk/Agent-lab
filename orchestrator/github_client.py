
"""Minimal GitHub Issues client using Python's standard library."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class GitHubAPIError(RuntimeError):
    """Raised when the GitHub API request fails."""


class GitHubClient:
    """Read GitHub Issues and their metadata."""

    def __init__(
        self,
        repository: str = "Anji-jk/Agent-lab",
        token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        if "/" not in repository:
            raise ValueError("Repository must be in OWNER/REPO format.")

        self.repository = repository
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.timeout = timeout
        self.base_url = (
            "https://api.github.com/repos/"
            + urllib.parse.quote(repository, safe="/")
        )

    def _get(self, path: str) -> Any:
        """Perform a read-only GitHub API request."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-lab-orchestrator",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Do not include credentials or request headers in error messages.
            raise GitHubAPIError(
                f"GitHub API returned HTTP {exc.code} for {path}."
            ) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubAPIError(
                f"Could not reach GitHub API: {type(exc).__name__}."
            ) from None
        except json.JSONDecodeError:
            raise GitHubAPIError("GitHub API returned invalid JSON.") from None

    def list_issues(
        self,
        state: str = "open",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List Issues, excluding pull requests."""
        if state not in {"open", "closed", "all"}:
            raise ValueError("state must be open, closed, or all.")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")

        query = urllib.parse.urlencode(
            {"state": state, "per_page": limit}
        )
        items = self._get(f"issues?{query}")

        if not isinstance(items, list):
            raise GitHubAPIError("Unexpected response when listing Issues.")

        return [
            item
            for item in items
            if isinstance(item, dict) and "pull_request" not in item
        ]

    def get_issue(self, number: int) -> dict[str, Any]:
        """Retrieve one Issue by number."""
        if number < 1:
            raise ValueError("Issue number must be positive.")

        item = self._get(f"issues/{number}")
        if not isinstance(item, dict) or "pull_request" in item:
            raise GitHubAPIError(f"#{number} is not a GitHub Issue.")

        return item
