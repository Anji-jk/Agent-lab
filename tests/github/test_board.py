"""Connect the board. A fake GitHub stands in: no network, nothing written."""

import io
import json
import urllib.error
from email.message import Message

import pytest

import orchestrator.cli.commands as cli
from orchestrator.github import board as bd
from orchestrator.github.client import GitHubAPIError
from orchestrator.github.projects import PROJECT_TOKEN_VARIABLE, ProjectClient, project_token

SECRET = "ghp_" + "S" * 36
OWNER, NUMBER, REPO = "Anji-jk", 2, "Anji-jk/Agent-lab"
BASE = f"https://api.github.com/users/{OWNER}/projectsV2/{NUMBER}"


def option(oid, name):
    return {"id": oid, "name": {"raw": name, "html": name}, "color": "GRAY"}


# The board as you made it: Todo, In progress, Needs Review, Done.
YOUR_COLUMNS = [option("o1", "Todo"), option("o2", "In progress"),
                option("o3", "Needs Review"), option("o4", "Done")]


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeResponse:
    def __init__(self, body, scopes):
        self._body = json.dumps(body).encode()
        self.headers = Headers({"X-OAuth-Scopes": scopes} if scopes is not None else {})

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    """Answers the two reads 5.1 makes, and records every request."""

    def __init__(self, columns=YOUR_COLUMNS, scopes: str | None = "project",
                 status_type="single_select", error: int | None = None):
        self.requests = []
        self.columns, self.scopes, self.status_type, self.error = columns, scopes, status_type, error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise urllib.error.HTTPError(request.full_url, self.error, "no", Message(), io.BytesIO(b"{}"))
        if request.full_url == BASE:
            return FakeResponse({"id": 77, "node_id": "PVT_x", "number": NUMBER,
                                 "title": "Agent-Lab"}, self.scopes)
        if request.full_url.startswith(BASE + "/fields"):
            return FakeResponse([
                {"id": 10, "node_id": "F_title", "name": "Title", "data_type": "title"},
                {"id": 11, "node_id": "F_status", "name": "Status", "data_type": self.status_type,
                 "options": self.columns},
            ], self.scopes)
        raise AssertionError(f"unexpected request {request.full_url}")


def client(github, token: str | None = SECRET, variable=PROJECT_TOKEN_VARIABLE):
    return ProjectClient(OWNER, NUMBER, token=token, token_variable=variable, opener=github)


def connect(github, **kw):
    return bd.connect_board(OWNER, NUMBER, REPO, client=client(github), **kw)


# --- reading your board -------------------------------------------------------------------------


def test_your_board_is_matched_column_by_column():
    connection = connect(FakeGitHub())
    columns = connection.config["columns"]
    assert {p: c["name"] for p, c in columns.items()} == {
        "todo": "Todo", "in_progress": "In progress", "attention": "Needs Review", "done": "Done"}
    assert columns["attention"]["option_id"] == "o3"
    assert connection.config["status_field_id"] == 11 and connection.config["project_id"] == 77
    assert connection.config["url"] == "https://github.com/users/Anji-jk/projects/2"


def test_only_reads_are_made():
    github = FakeGitHub()
    connect(github)
    assert [r.get_method() for r in github.requests] == ["GET", "GET"]
    assert all(r.data is None for r in github.requests)


def test_a_column_with_another_name_can_be_named():
    columns = [option("a", "Backlog"), option("b", "Doing"), option("c", "Stuck"), option("d", "Shipped")]
    with pytest.raises(bd.BoardError, match="waiting for a person.*Backlog, Doing, Stuck, Shipped"):
        connect(FakeGitHub(columns=columns))
    connection = connect(FakeGitHub(columns=columns), chosen={"attention": "stuck", "done": "Shipped"})
    assert connection.config["columns"]["attention"]["name"] == "Stuck"
    assert connection.config["columns"]["done"]["name"] == "Shipped"


def test_a_named_column_that_does_not_exist_is_refused():
    with pytest.raises(bd.BoardError, match="no column called 'Blocked'"):
        connect(FakeGitHub(), chosen={"attention": "Blocked"})


def test_two_places_cannot_share_a_column():
    with pytest.raises(bd.BoardError, match="same column"):
        connect(FakeGitHub(), chosen={"attention": "Done"})


def test_a_board_without_a_status_field_is_refused():
    with pytest.raises(bd.BoardError, match="no single-select Status field"):
        connect(FakeGitHub(status_type="text"))


# --- what the token may do -----------------------------------------------------------------------


def test_a_classic_token_with_the_project_scope_may_write():
    checks = connect(FakeGitHub(scopes="project, public_repo")).checks
    assert (True, "It may write to the board (classic token with the 'project' scope).") in checks
    assert all(ok is not False for ok, _ in checks)


def test_a_read_only_classic_token_is_reported():
    checks = connect(FakeGitHub(scopes="read:project")).checks
    assert any(ok is False and "'project' scope" in text for ok, text in checks)


def test_a_project_only_token_is_explained():
    checks = connect(FakeGitHub(scopes="project")).checks
    assert any(ok is None and "no repository scope" in text for ok, text in checks)


def test_a_token_that_says_nothing_about_itself_is_not_claimed_able_to_write():
    checks = connect(FakeGitHub(scopes=None)).checks
    assert any(ok is None and "untested until 5.2" in text for ok, text in checks)


@pytest.mark.parametrize("code", [401, 403, 404])
def test_a_refusal_explains_the_token_without_showing_it(code):
    with pytest.raises(GitHubAPIError) as caught:
        connect(FakeGitHub(error=code))
    text = str(caught.value)
    assert SECRET not in text
    assert f"HTTP {code}" in text and "classic token" in text and "fine-grained" in text
    assert PROJECT_TOKEN_VARIABLE in text  # which variable it used, by name


def test_no_token_says_which_variable_to_set():
    with pytest.raises(GitHubAPIError, match=f"no token is set.*{PROJECT_TOKEN_VARIABLE}"):
        bd.connect_board(OWNER, NUMBER, REPO, client=client(FakeGitHub(error=401), token=None))


def test_the_board_token_is_preferred_and_github_token_is_the_fallback(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fine")
    monkeypatch.delenv(PROJECT_TOKEN_VARIABLE, raising=False)
    assert project_token() == ("fine", "GITHUB_TOKEN")
    monkeypatch.setenv(PROJECT_TOKEN_VARIABLE, "classic")
    assert project_token() == ("classic", PROJECT_TOKEN_VARIABLE)


def test_the_board_token_never_reaches_an_agent(monkeypatch):
    from agent_env import build_agent_env  # pyright: ignore[reportMissingImports]  (scripts/ is on the test path)
    env = build_agent_env({PROJECT_TOKEN_VARIABLE: SECRET, "GITHUB_TOKEN": SECRET, "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


# --- saving it, and the command ---------------------------------------------------------------------


def test_the_saved_board_has_no_token(tmp_path):
    path = bd.save_board(tmp_path, connect(FakeGitHub()).config)
    text = path.read_text(encoding="utf-8")
    assert path == tmp_path / "board.json" and SECRET not in text
    assert json.loads(text)["token_variable"] == PROJECT_TOKEN_VARIABLE


def test_board_connect_prints_the_columns_and_saves(tmp_path, monkeypatch, capsys):
    github = FakeGitHub()
    monkeypatch.setattr(cli.board, "ProjectClient",
                        lambda owner, number, token, token_variable: client(github, token, token_variable))
    monkeypatch.setenv(PROJECT_TOKEN_VARIABLE, SECRET)
    code = cli.main(["board", "connect", "--project", "2", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "Board:    Agent-Lab  https://github.com/users/Anji-jk/projects/2" in out
    assert "Needs Review     for tasks that are waiting for a person" in out
    assert "Nothing was written to GitHub." in out and SECRET not in out
    assert (tmp_path / "board.json").exists()


def test_board_connect_refuses_with_the_reason(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(status_type="text")
    monkeypatch.setattr(cli.board, "ProjectClient",
                        lambda owner, number, token, token_variable: client(github, token, token_variable))
    code = cli.main(["board", "connect", "--project", "2", "--runs-dir", str(tmp_path)])
    assert code == cli.EXIT_REFUSED
    assert "no single-select Status field" in capsys.readouterr().err
    assert not (tmp_path / "board.json").exists()
