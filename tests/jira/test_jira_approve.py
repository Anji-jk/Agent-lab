"""jira approve: the plan's Blocks links go to a fake Jira, then plan.json is marked approved."""

import copy
import json

import pytest

import orchestrator.cli.commands as cli
from orchestrator.cli.commands import jira as jira_command
from orchestrator.jira import client as jc
from orchestrator.jira.adf import adf_to_text, text_to_adf
from test_jira_check import git
from test_jira_plan import RUN, setup_project
from test_jira_story import STORY, SUBTASKS, FakeJira, blocked_by, blocks


@pytest.fixture
def repo(tmp_path):
    """A project on main with a CLAUDE.md, committed."""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "CLAUDE.md").write_text("# P\n\n## Project structure\nx\n\n## Commands\nx\n\n"
                                    "## Rules\nx\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


class LinkingJira(FakeJira):
    """A Jira that applies the links it is asked to add, so they can be read back."""

    def __init__(self, subtasks=SUBTASKS, *, reversed_links=False, fail=False):
        super().__init__(STORY, copy.deepcopy(list(subtasks)))
        self.posts, self.reversed_links, self.fail = [], reversed_links, fail

    def _item(self, key):
        return next(item for item in self.subtasks if item["key"] == key)

    def links(self):
        return [body for path, body in self.posts if path == "/rest/api/3/issueLink"]

    def comments(self):
        return [(path.split("/")[-2], adf_to_text(body["body"])) for path, body in self.posts
                if path.endswith("/comment")]

    def post(self, path, body):
        self.posts.append((path, body))
        if self.fail:
            raise jc.JiraAPIError(f"The Jira account may not change {path} (HTTP 403).")
        if path.endswith("/comment"):
            return None
        assert "comment" not in body  # a comment sent with the link would land on the blocker
        blocker, blocked = body["inwardIssue"]["key"], body["outwardIssue"]["key"]
        if self.reversed_links:
            blocker, blocked = blocked, blocker
        self._item(blocked)["fields"]["issuelinks"].append(blocked_by(blocker))
        self._item(blocker)["fields"]["issuelinks"].append(blocks(blocked))


def plan_file(repo):
    return repo / ".agent-lab" / "runs" / RUN / "plan.json"


def planned(repo, monkeypatch, tmp_path, capsys, jira):
    setup_project(repo, monkeypatch, tmp_path)
    assert cli.main(["jira", "plan", "SKM-9", "--run-id", RUN,
                     "--repo-path", str(repo)]) == cli.EXIT_OK
    capsys.readouterr()
    monkeypatch.setattr(jira_command, "JiraClient", lambda url, email, token: jira)
    return json.loads(plan_file(repo).read_text())


def approve(repo, *more):
    return cli.main(["jira", "approve", RUN, *more, "--repo-path", str(repo)])


def test_approving_adds_the_link_with_its_reason_and_marks_the_plan(repo, monkeypatch,
                                                                    tmp_path, capsys):
    jira = LinkingJira()
    before = planned(repo, monkeypatch, tmp_path, capsys, jira)
    assert approve(repo, "--yes") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Approved the plan of story SKM-9." in out
    assert "added in Jira: SKM-11 is blocked by SKM-10, with a comment on SKM-11" in out

    (body,) = jira.links()
    assert body["type"] == {"name": "Blocks"}
    assert body["inwardIssue"] == {"key": "SKM-10"} and body["outwardIssue"] == {"key": "SKM-11"}
    ((where, comment),) = jira.comments()
    assert where == "SKM-11"  # on the sub-task that waits, not on the blocker
    assert comment.startswith("Blocked by SKM-10: The endpoints serve the screen SKM-10 designs.")
    assert f"the plan {RUN} of story SKM-9 was approved" in comment
    assert [path for path, _ in jira.posts] == ["/rest/api/3/issueLink",
                                                "/rest/api/3/issue/SKM-11/comment"]

    after = json.loads(plan_file(repo).read_text())
    assert {k: v for k, v in after.items() if k != "approval"} == \
        {k: v for k, v in before.items() if k != "approval"}  # only the approval changed
    approval = after["approval"]
    assert approval["status"] == "approved" and approval["approved_at"]
    ((link),) = approval["jira_links"]
    assert link["how"] == "added" and link["written_at"] and link["comment_at"]

    assert cli.main(["show-plan", RUN, "--repo-path", str(repo)]) == cli.EXIT_OK
    shown = capsys.readouterr().out
    assert "+ SKM-11 is blocked by SKM-10  (added)" in shown and "Status:      approved 20" in shown
    assert cli.main(["run-plan", RUN, "--repo-path", str(repo)]) == cli.EXIT_REFUSED
    assert "not supported yet" in capsys.readouterr().err

    assert approve(repo, "--yes") == cli.EXIT_OK  # a second time: nothing more
    assert "already approved. Nothing was changed." in capsys.readouterr().out
    assert len(jira.posts) == 2


def test_a_link_already_in_jira_is_not_added_again(repo, monkeypatch, tmp_path, capsys):
    jira = LinkingJira()
    planned(repo, monkeypatch, tmp_path, capsys, jira)
    jira._item("SKM-11")["fields"]["issuelinks"].append(blocked_by("SKM-10"))  # added by hand
    assert approve(repo, "--yes") == cli.EXIT_OK
    assert "already in Jira: SKM-11 is blocked by SKM-10 (no comment added)" in \
        capsys.readouterr().out
    assert jira.posts == []
    approval = json.loads(plan_file(repo).read_text())["approval"]
    assert approval["status"] == "approved"
    assert approval["jira_links"][0]["how"] == "already in Jira"


def test_a_story_changed_since_planning_is_refused(repo, monkeypatch, tmp_path, capsys):
    jira = LinkingJira()
    planned(repo, monkeypatch, tmp_path, capsys, jira)
    jira._item("SKM-11")["fields"]["summary"] = "Something else now"
    assert approve(repo, "--yes") == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "SKM-9 changed in Jira since this plan was made" in err
    assert "SKM-11: its title changed in Jira." in err and "jira plan SKM-9" in err
    assert jira.posts == []
    assert json.loads(plan_file(repo).read_text())["approval"]["status"] == "waiting"


def test_a_failed_write_keeps_the_plan_waiting_and_a_rerun_finishes(repo, monkeypatch,
                                                                    tmp_path, capsys):
    jira = LinkingJira(fail=True)
    planned(repo, monkeypatch, tmp_path, capsys, jira)
    assert approve(repo, "--yes") == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "Could not add SKM-11 is blocked by SKM-10 in Jira" in err
    assert "run jira approve again to finish" in err
    assert json.loads(plan_file(repo).read_text())["approval"]["status"] == "waiting"

    jira.fail = False
    assert approve(repo, "--yes") == cli.EXIT_OK
    assert len(jira.links()) == 2 and len(jira.comments()) == 1  # failed try, then it worked
    assert json.loads(plan_file(repo).read_text())["approval"]["status"] == "approved"


def test_a_failed_comment_is_added_by_the_rerun_without_a_second_link(repo, monkeypatch,
                                                                      tmp_path, capsys):
    jira = LinkingJira()
    planned(repo, monkeypatch, tmp_path, capsys, jira)
    real_post = jira.post

    def no_comments(path, body):
        if path.endswith("/comment"):
            jira.posts.append((path, body))
            raise jc.JiraAPIError("Jira is down (HTTP 503).")
        return real_post(path, body)

    monkeypatch.setattr(jira, "post", no_comments)
    assert approve(repo, "--yes") == cli.EXIT_ERROR
    assert "Could not add the comment on SKM-11" in capsys.readouterr().err
    assert json.loads(plan_file(repo).read_text())["approval"]["status"] == "waiting"

    monkeypatch.setattr(jira, "post", real_post)
    assert approve(repo, "--yes") == cli.EXIT_OK
    assert len(jira.links()) == 1  # the link was not added twice
    assert [where for where, _ in jira.comments()] == ["SKM-11", "SKM-11"]


def test_an_approved_plan_without_its_comment_gets_it(repo, monkeypatch, tmp_path, capsys):
    """A plan approved before comments went on the blocked sub-task: approve adds them."""
    jira = LinkingJira()
    planned(repo, monkeypatch, tmp_path, capsys, jira)
    assert approve(repo, "--yes") == cli.EXIT_OK
    record = json.loads(plan_file(repo).read_text())
    del record["approval"]["jira_links"][0]["comment_at"]
    plan_file(repo).write_text(json.dumps(record, indent=2))
    jira._item("SKM-11")["fields"]["summary"] = "Edited since"  # does not matter now
    capsys.readouterr()

    assert approve(repo, "--yes") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "comment added on SKM-11: why it is blocked by SKM-10" in out
    assert len(jira.links()) == 1 and [w for w, _ in jira.comments()] == ["SKM-11", "SKM-11"]
    assert json.loads(plan_file(repo).read_text())["approval"]["jira_links"][0]["comment_at"]
    assert approve(repo, "--yes") == cli.EXIT_OK
    assert "already approved" in capsys.readouterr().out


def test_a_link_jira_shows_the_wrong_way_round_stops_it(repo, monkeypatch, tmp_path, capsys):
    jira = LinkingJira(reversed_links=True)
    planned(repo, monkeypatch, tmp_path, capsys, jira)
    assert approve(repo, "--yes") == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "wrong way round: SKM-10 is blocked by SKM-11" in err
    assert "Agent Lab never deletes" in err
    assert json.loads(plan_file(repo).read_text())["approval"]["status"] == "waiting"
    assert jira.comments() == []  # no comment for a link that reads wrong


def test_it_asks_first(repo, monkeypatch, tmp_path, capsys):
    jira = LinkingJira()
    planned(repo, monkeypatch, tmp_path, capsys, jira)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert approve(repo) == cli.EXIT_REFUSED
    assert "Add --yes to approve" in capsys.readouterr().err

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda question: asked.append(question) or "n")
    assert approve(repo) == cli.EXIT_REFUSED
    assert "add 1 link(s) in Jira, each with a comment on the blocked sub-task" in asked[0]
    assert "Not approved. Nothing was changed." in capsys.readouterr().out
    assert jira.posts == []

    monkeypatch.setattr("builtins.input", lambda question: "y")
    assert approve(repo) == cli.EXIT_OK
    assert len(jira.links()) == 1 and len(jira.comments()) == 1


def test_an_issue_plan_is_not_approved(repo, monkeypatch, tmp_path, capsys):
    planned(repo, monkeypatch, tmp_path, capsys, LinkingJira())
    record = json.loads(plan_file(repo).read_text())
    del record["source"]
    plan_file(repo).write_text(json.dumps(record))
    assert approve(repo, "--yes") == cli.EXIT_REFUSED
    assert "is not a story plan" in capsys.readouterr().err


def test_text_becomes_a_jira_comment():
    document = text_to_adf("line one\n\nline two")
    assert document["type"] == "doc" and len(document["content"]) == 2
    assert adf_to_text(document) == "line one\n\nline two"


def test_the_client_sends_blocks_links_the_way_jira_reads_them(monkeypatch):
    client = jc.JiraClient("https://x.atlassian.net", "a@b.c", "t")
    sent = []
    monkeypatch.setattr(client, "post", lambda path, body: sent.append((path, body)))
    client.add_blocks_link("KAN-12", "KAN-13")
    client.add_comment("KAN-13", "why")
    (link_path, link), (comment_path, comment) = sent
    assert link == {"type": {"name": "Blocks"}, "inwardIssue": {"key": "KAN-12"},
                    "outwardIssue": {"key": "KAN-13"}}
    assert comment_path == "/rest/api/3/issue/KAN-13/comment"
    assert adf_to_text(comment["body"]) == "why"
