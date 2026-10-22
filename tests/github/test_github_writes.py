"""GitHub write operations. urlopen is always mocked: no network, no writes."""

import io
import json
import urllib.error

import pytest

from orchestrator.github import client as github_client
from orchestrator.github.client import GitHubAPIError, GitHubClient

SECRET = "ghp_SUPERSECRETTOKEN123"


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def no_env_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def capture(monkeypatch):
    """Replace urlopen and record the requests it receives."""
    state = {"requests": [], "body": b"{}", "error": None}

    def fake_urlopen(request, timeout=None):
        state["requests"].append(request)
        if state["error"] is not None:
            raise state["error"]
        return FakeResponse(state["body"])

    monkeypatch.setattr(github_client.urllib.request, "urlopen", fake_urlopen)
    return state


def test_comment_payload_and_headers(capture, no_env_token):
    capture["body"] = json.dumps({"id": 9, "body": "hi"}).encode()
    client = GitHubClient("Anji-jk/Agent-lab", token=SECRET)

    result = client.add_issue_comment(5, "Run started.")

    (request,) = capture["requests"]
    assert result == {"id": 9, "body": "hi"}
    assert request.get_method() == "POST"
    assert request.full_url == (
        "https://api.github.com/repos/Anji-jk/Agent-lab/issues/5/comments"
    )
    assert json.loads(request.data) == {"body": "Run started."}
    assert request.get_header("Authorization") == f"Bearer {SECRET}"
    assert request.get_header("Content-type") == "application/json"


def test_label_payload_adds_rather_than_replaces(capture, no_env_token):
    capture["body"] = json.dumps([{"name": "bug"}, {"name": "agent"}]).encode()
    client = GitHubClient(token=SECRET)

    result = client.add_issue_labels(5, ["agent"])

    (request,) = capture["requests"]
    assert [label["name"] for label in result] == ["bug", "agent"]
    # POST .../labels appends; PUT (or PATCH on the issue) would replace.
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/issues/5/labels")
    assert json.loads(request.data) == {"labels": ["agent"]}


def test_missing_token_fails_before_any_request(capture, no_env_token):
    client = GitHubClient()

    with pytest.raises(GitHubAPIError, match="GITHUB_TOKEN"):
        client.add_issue_comment(5, "hello")
    with pytest.raises(GitHubAPIError, match="GITHUB_TOKEN"):
        client.add_issue_labels(5, ["agent"])

    assert capture["requests"] == []


def test_token_is_read_from_environment(capture, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    capture["body"] = b"{}"

    GitHubClient().add_issue_comment(5, "hello")

    assert capture["requests"][0].get_header("Authorization") == f"Bearer {SECRET}"


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.add_issue_comment(0, "x"),
        lambda c: c.add_issue_comment(5, "   "),
        lambda c: c.add_issue_labels(0, ["a"]),
        lambda c: c.add_issue_labels(5, []),
        lambda c: c.add_issue_labels(5, ["ok", "  "]),
    ],
)
def test_invalid_input_sends_nothing(capture, no_env_token, call):
    with pytest.raises(ValueError):
        call(GitHubClient(token=SECRET))
    assert capture["requests"] == []


def test_http_error_message_has_no_secrets_or_body(capture, no_env_token):
    capture["error"] = urllib.error.HTTPError(
        "https://api.github.com/x",
        403,
        f"Forbidden {SECRET}",
        {"Authorization": f"Bearer {SECRET}"},
        io.BytesIO(f"body mentions {SECRET}".encode()),
    )
    client = GitHubClient(token=SECRET)

    with pytest.raises(GitHubAPIError) as info:
        client.add_issue_comment(5, "hello")

    assert "HTTP 403" in str(info.value)
    assert SECRET not in str(info.value)
    assert SECRET not in repr(info.value)


def test_network_error_message_has_no_secrets(capture, no_env_token):
    capture["error"] = urllib.error.URLError(f"cannot connect with {SECRET}")
    client = GitHubClient(token=SECRET)

    with pytest.raises(GitHubAPIError) as info:
        client.add_issue_labels(5, ["agent"])

    assert "URLError" in str(info.value)
    assert SECRET not in str(info.value)


def test_timeout_is_reported_safely(capture, no_env_token):
    capture["error"] = TimeoutError(SECRET)
    with pytest.raises(GitHubAPIError) as info:
        GitHubClient(token=SECRET).add_issue_comment(5, "hello")
    assert SECRET not in str(info.value)


def test_invalid_json_response(capture, no_env_token):
    capture["body"] = b"<html>not json</html>"
    with pytest.raises(GitHubAPIError, match="invalid JSON"):
        GitHubClient(token=SECRET).add_issue_comment(5, "hello")


def test_unexpected_response_shapes(capture, no_env_token):
    client = GitHubClient(token=SECRET)

    capture["body"] = b"[]"
    with pytest.raises(GitHubAPIError, match="adding comment"):
        client.add_issue_comment(5, "hello")

    capture["body"] = b"{}"
    with pytest.raises(GitHubAPIError, match="adding labels"):
        client.add_issue_labels(5, ["agent"])
