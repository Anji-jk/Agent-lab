
import json
import urllib.error

import pytest

from orchestrator.github.client import GitHubAPIError, GitHubClient


def test_list_issues_excludes_pull_requests(monkeypatch):
    client = GitHubClient()
    monkeypatch.setattr(
        client,
        "_get",
        lambda path: [
            {"number": 1, "title": "Example issue"},
            {"number": 2, "title": "Example PR", "pull_request": {}},
        ],
    )

    result = client.list_issues()

    assert len(result) == 1
    assert result[0]["number"] == 1


def test_rejects_invalid_state():
    with pytest.raises(ValueError):
        GitHubClient().list_issues(state="invalid")


def test_rejects_invalid_limit():
    with pytest.raises(ValueError):
        GitHubClient().list_issues(limit=101)


def test_rejects_invalid_issue_number():
    with pytest.raises(ValueError):
        GitHubClient().get_issue(0)


def test_api_error_does_not_expose_response_body(monkeypatch):
    client = GitHubClient()

    def fail(path):
        raise GitHubAPIError(
            "GitHub API returned HTTP 404 for issues/999."
        )

    monkeypatch.setattr(client, "_get", fail)

    with pytest.raises(GitHubAPIError, match="HTTP 404"):
        client.get_issue(999)
