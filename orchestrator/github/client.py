
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


    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> Any:
        """Send an authenticated JSON POST request."""
        if not self.token:
            raise GitHubAPIError(
                "A GITHUB_TOKEN is required for write operations."
            )

        url = f"{self.base_url}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "agent-lab-orchestrator",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:
                response_body = response.read()
                if not response_body:
                    return None
                return json.loads(response_body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GitHubAPIError(
                f"GitHub API returned HTTP {exc.code} for {path}."
            ) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubAPIError(
                f"GitHub API request failed: {type(exc).__name__}."
            ) from None
        except json.JSONDecodeError:
            raise GitHubAPIError(
                "GitHub API returned invalid JSON."
            ) from None

    def add_issue_comment(
        self,
        number: int,
        comment: str,
    ) -> dict[str, Any]:
        """Add a comment to an Issue."""
        if number < 1:
            raise ValueError("Issue number must be positive.")
        if not comment.strip():
            raise ValueError("Comment cannot be empty.")

        result = self._post_json(
            f"issues/{number}/comments",
            {"body": comment},
        )
        if not isinstance(result, dict):
            raise GitHubAPIError("Unexpected response when adding comment.")
        return result

    def add_issue_labels(
        self,
        number: int,
        labels: list[str],
    ) -> list[dict[str, Any]]:
        """Add labels without replacing existing labels."""
        if number < 1:
            raise ValueError("Issue number must be positive.")
        if not labels or any(not x.strip() for x in labels):
            raise ValueError("Provide one or more non-empty labels.")

        result = self._post_json(
            f"issues/{number}/labels",
            {"labels": labels},
        )
        if not isinstance(result, list):
            raise GitHubAPIError("Unexpected response when adding labels.")
        return result
