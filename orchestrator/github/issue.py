
"""Validated task representation for GitHub-driven orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Task:
    repository: str
    issue_number: int
    title: str
    description: str
    issue_url: str

    @property
    def task_id(self) -> str:
        return f"issue-{self.issue_number}"

    @classmethod
    def from_issue(
        cls,
        repository: str,
        issue: dict[str, Any],
    ) -> "Task":
        number = issue.get("number")
        title = issue.get("title")
        body = issue.get("body")
        url = issue.get("html_url")

        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("Issue number must be a positive integer.")

        if not isinstance(title, str) or not title.strip():
            raise ValueError("Issue title is required.")

        if body is not None and not isinstance(body, str):
            raise ValueError("Issue description must be text or null.")

        if not isinstance(url, str) or not url.startswith(
            "https://github.com/"
        ):
            raise ValueError("A valid GitHub Issue URL is required.")

        if "pull_request" in issue:
            raise ValueError("Pull requests cannot be treated as Issues.")

        return cls(
            repository=repository,
            issue_number=number,
            title=title.strip(),
            description=(body or "").strip(),
            issue_url=url,
        )
