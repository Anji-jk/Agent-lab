"""M5.2: a run's tasks become sub-issues and board cards. Fakes stand in for GitHub."""

import io
import json
import urllib.error
from email.message import Message

import pytest

import orchestrator.cli.commands as cli
from orchestrator.github import client as github_client
from orchestrator.github import publish as pub
from orchestrator.github.board import save_board
from orchestrator.github.client import GitHubAPIError, GitHubClient
from orchestrator.github.projects import ProjectClient
from github_fakes import PARENT, REPO, RUN, FakeIssues, FakeProject, make_lab, writes  # pyright: ignore[reportMissingImports]


@pytest.fixture
def lab(tmp_path):
    return make_lab(tmp_path)


def run(lab, issues, project, write=True, sleep=None):
    return pub.publish(RUN, plans_dir=lab["plans"], runs_dir=lab["runs"], issues=issues,
                       project=project, write=write, pause=1.0, sleep=sleep or (lambda s: None))


# --- a preview writes nothing -----------------------------------------------------------------


def test_without_yes_nothing_is_written(lab):
    issues, project = FakeIssues(), FakeProject()
    result = run(lab, issues, project, write=False)
    assert writes(issues, project) == []
    assert not pub.items_path(lab["runs"], RUN).exists()
    text = pub.format_published(result)
    assert 'would create the Issue "#7 T1 double", make it a sub-issue of #7, add it to the board' in text
    assert "Nothing was written. 2 task(s) to put on GitHub: run again with --yes." in text


# --- publishing ----------------------------------------------------------------------------------


def test_each_task_is_created_linked_and_added_in_that_order(lab):
    issues, project = FakeIssues(), FakeProject()
    result = run(lab, issues, project)
    assert [c[0] for c in issues.calls if c[0] != "list"] == ["create", "link", "create", "link"]
    assert [c for c in issues.calls if c[0] == "link"] == [("link", PARENT, 9101), ("link", PARENT, 9102)]
    assert project.calls == [("add", 9101), ("add", 9102)]
    assert [(c.task_id, c.issue_number, c.done) for c in result.cards] == [
        ("T1", 101, ["created", "linked", "added"]), ("T2", 102, ["created", "linked", "added"])]


def test_the_issue_carries_the_task_its_criteria_and_a_marker(lab):
    issues = FakeIssues()
    run(lab, issues, FakeProject())
    title, body = issues.calls[1][1], issues.calls[1][2]  # calls[0] is the sub-issue list
    assert title == "#7 T1 double"
    assert "Do double." in body
    assert "- [ ] double(2) is 4\n- [ ] double(-1) is -2" in body
    assert "Depends on: nothing" in body and "Part of #7." in body
    assert body.rstrip().endswith(pub.marker(RUN, "T1"))
    t2_body = [c for c in issues.calls if c[0] == "create"][1][2]
    assert "Depends on: T1" in t2_body


def test_what_was_made_is_saved(lab):
    run(lab, FakeIssues(), FakeProject())
    saved = pub.load_items(lab["runs"], RUN)
    assert saved["T1"]["issue_number"] == 101 and saved["T1"]["issue_id"] == 9101
    assert saved["T1"]["linked"] is True and saved["T1"]["item_id"] == 501


def test_running_it_again_writes_nothing(lab):
    run(lab, FakeIssues(), FakeProject())
    issues, project = FakeIssues(), FakeProject()
    result = run(lab, issues, project)
    assert writes(issues, project) == []
    assert "already on the board" in pub.format_published(result)


def test_writes_are_spaced_out(lab):
    pauses = []
    run(lab, FakeIssues(), FakeProject(), sleep=pauses.append)
    assert pauses == [1.0] * 5  # six writes, a pause before each but the first


# --- going on after a failure ----------------------------------------------------------------------


def test_a_failure_part_way_keeps_what_was_done_and_says_how_to_go_on(lab):
    issues, project = FakeIssues(), FakeProject()
    # T1 goes through; T2's link fails once
    calls = {"n": 0}
    real_link = issues.add_sub_issue

    def link_once_failing(parent, sub_issue_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise GitHubAPIError("GitHub API returned HTTP 403 for issues/7/sub_issues.")
        real_link(parent, sub_issue_id)

    issues.add_sub_issue = link_once_failing
    with pytest.raises(pub.PublishError, match="make T2's Issue a sub-issue of #7.*GITHUB_TOKEN.*run board publish again"):
        run(lab, issues, project)
    saved = pub.load_items(lab["runs"], RUN)
    assert saved["T1"]["item_id"] and saved["T2"]["issue_number"] == 102 and not saved["T2"].get("linked")

    again_issues, again_project = FakeIssues(), FakeProject()
    run(lab, again_issues, again_project)
    assert [c[0] for c in again_issues.calls if c[0] != "list"] == ["link"]  # T2 not created twice
    assert again_project.calls == [("add", 9102)]


def test_a_board_refusal_names_the_project_token(lab):
    with pytest.raises(pub.PublishError, match="add T1's Issue to the board.*AGENT_LAB_PROJECT_TOKEN has the 'project' scope"):
        run(lab, FakeIssues(), FakeProject(fail=1))


def test_issues_made_before_are_found_by_their_marker_if_the_record_is_lost(lab):
    run(lab, FakeIssues(), FakeProject())
    pub.items_path(lab["runs"], RUN).unlink()
    found = [{"id": 9101, "number": 101, "body": "x\n" + pub.marker(RUN, "T1")},
             {"id": 9102, "number": 102, "body": pub.marker(RUN, "T2")},
             {"id": 1, "number": 1, "body": "someone else's sub-issue"}]
    issues, project = FakeIssues(existing=found), FakeProject()
    result = run(lab, issues, project)
    assert [c for c in issues.calls if c[0] in ("create", "link")] == []
    assert project.calls == [("add", 9101), ("add", 9102)]  # adding again returns the same card
    assert [c.done for c in result.cards] == [["found", "added"], ["found", "added"]]


# --- refused before anything --------------------------------------------------------------------


def test_no_board_connected(lab):
    pub.load_board(lab["runs"])  # present
    (lab["runs"] / "board.json").unlink()
    with pytest.raises(pub.PublishError, match="No board is connected.*board connect --project"):
        run(lab, FakeIssues(), FakeProject())


def test_a_board_for_another_repository_is_refused(lab):
    save_board(lab["runs"], {"owner": "x", "number": 1, "repository": "x/other", "url": "u",
                             "columns": {}})
    with pytest.raises(pub.PublishError, match="connected for x/other"):
        run(lab, FakeIssues(), FakeProject())


def test_a_damaged_record_is_refused_with_what_to_do(lab):
    pub.items_path(lab["runs"], RUN).parent.mkdir(parents=True, exist_ok=True)
    pub.items_path(lab["runs"], RUN).write_text("{not json")
    with pytest.raises(pub.PublishError, match="damaged.*found again by their marker"):
        run(lab, FakeIssues(), FakeProject())


# --- the requests themselves ---------------------------------------------------------------------


class Captured:
    def __init__(self, body=b"{}"):
        self.requests, self.body = [], body

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        body = self.body

        class R:
            headers = Message()

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return R()


def test_the_issue_requests(monkeypatch):
    captured = Captured(json.dumps({"id": 9101, "number": 101}).encode())
    monkeypatch.setattr(github_client.urllib.request, "urlopen", captured)
    client = GitHubClient(REPO, token="t")
    assert client.create_issue("#7 T1 double", "body")["number"] == 101
    client.add_sub_issue(7, 9101)
    created, linked = captured.requests
    assert created.full_url.endswith("/repos/Anji-jk/Agent-lab/issues") and created.get_method() == "POST"
    assert json.loads(created.data) == {"title": "#7 T1 double", "body": "body"}
    assert linked.full_url.endswith("/issues/7/sub_issues")
    assert json.loads(linked.data) == {"sub_issue_id": 9101}


def test_listing_sub_issues(monkeypatch):
    captured = Captured(json.dumps([{"id": 1, "number": 2, "body": "b"}]).encode())
    monkeypatch.setattr(github_client.urllib.request, "urlopen", captured)
    assert GitHubClient(REPO, token="t").list_sub_issues(7) == [{"id": 1, "number": 2, "body": "b"}]
    assert "/issues/7/sub_issues?per_page=100&page=1" in captured.requests[0].full_url


@pytest.mark.parametrize("answer", [{"id": 501, "node_id": "PVTI"}, {"value": {"id": 501}}])
def test_adding_to_the_board(answer):
    captured = Captured(json.dumps(answer).encode())
    project = ProjectClient("Anji-jk", 2, token="classic", opener=captured)
    assert project.add_issue(9101) == 501
    request = captured.requests[0]
    assert request.full_url == "https://api.github.com/users/Anji-jk/projectsV2/2/items"
    assert request.get_method() == "POST" and json.loads(request.data) == {"type": "Issue", "id": 9101}


def test_a_board_write_without_a_token_is_refused_before_any_request():
    captured = Captured()
    with pytest.raises(GitHubAPIError, match="no token is set"):
        ProjectClient("Anji-jk", 2, token=None, opener=captured).add_issue(1)
    assert captured.requests == []


def test_a_board_write_refusal_never_shows_the_token():
    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "no", Message(), io.BytesIO(b"{}"))

    with pytest.raises(GitHubAPIError) as caught:
        ProjectClient("Anji-jk", 2, token="ghp_secretsecretsecret", opener=refuse).add_issue(1)
    assert "ghp_secret" not in str(caught.value) and "HTTP 403" in str(caught.value)


# --- the command ------------------------------------------------------------------------------------


def test_board_publish_previews_by_default(lab, monkeypatch, capsys):
    issues, project = FakeIssues(), FakeProject()
    monkeypatch.setattr(cli, "GitHubClient", lambda repository: issues)
    monkeypatch.setattr(cli, "ProjectClient", lambda *a, **k: project)
    code = cli.main(["board", "publish", RUN, "--plans-dir", str(lab["plans"]),
                     "--runs-dir", str(lab["runs"])])
    assert code == cli.EXIT_OK and writes(issues, project) == []
    assert "run again with --yes" in capsys.readouterr().out


# --- an Issue already on the board (found live: GitHub answers 422) -----------------------------


class Board:
    """A fake board API: POST /items answers 422; the item list comes in two linked pages."""

    def __init__(self, add_code=422, items=((7001, 9101), (7002, 8004)), link_to=None):
        self.requests, self.add_code = [], add_code
        self.items = [{"id": item, "content_type": "Issue", "content": {"id": issue, "number": 4}}
                      for item, issue in items]
        self.link_to = link_to

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = request.full_url
        if request.get_method() == "POST":
            raise urllib.error.HTTPError(url, self.add_code, "no", Message(), io.BytesIO(b"{}"))
        first = "after=" not in url
        page = self.items[:1] if first else self.items[1:]
        headers = Message()
        if first:
            headers["Link"] = (f'<{self.link_to or url + "&after=c1"}>; rel="next", '
                               '<https://api.github.com/x>; rel="last"')
        body = json.dumps(page).encode()

        class R:
            def __init__(self):
                self.headers = headers

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return R()


def test_an_issue_already_on_the_board_gets_its_existing_card():
    board = Board()
    project = ProjectClient("Anji-jk", 2, token="classic", opener=board)
    assert project.add_issue(8004) == 7002  # found on the second page
    methods = [r.get_method() for r in board.requests]
    assert methods == ["POST", "GET", "GET"]
    assert board.requests[2].full_url.endswith("&after=c1")


def test_a_refused_issue_that_is_not_on_the_board_says_so():
    project = ProjectClient("Anji-jk", 2, token="classic", opener=Board(items=((7001, 1),)))
    with pytest.raises(GitHubAPIError, match="refused to add Issue id 8004.*not on the board either"):
        project.add_issue(8004)


def test_other_refusals_are_not_looked_up():
    board = Board(add_code=403)
    with pytest.raises(GitHubAPIError, match="HTTP 403"):
        ProjectClient("Anji-jk", 2, token="classic", opener=board).add_issue(8004)
    assert [r.get_method() for r in board.requests] == ["POST"]


def test_a_next_page_outside_github_is_not_followed():
    board = Board(link_to="https://evil.example/steal")
    with pytest.raises(GitHubAPIError, match="outside api.github.com"):
        ProjectClient("Anji-jk", 2, token="classic", opener=board).add_issue(8004)
    assert all(r.full_url.startswith("https://api.github.com/") for r in board.requests)