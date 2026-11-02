"""Connect a Jira space. A fake Jira stands in: no network, nothing written to Jira."""

import json
import urllib.error

import pytest

import orchestrator.cli.commands as cli
from orchestrator.core.project_config import parse_project_config
from orchestrator.errors import ExecutionRefused
from orchestrator.jira import client as jc
from orchestrator.jira import connect as jn
from orchestrator.settings import load_settings

SITE = "https://example.atlassian.net"
EMAIL, SECRET = "agent-lab@example.com", "ATATT" + "S" * 40


def status(sid, name):
    return {"id": sid, "name": name}


def kind(tid, name, subtask=False):
    return {"id": tid, "name": name, "subtask": subtask, "hierarchyLevel": -1 if subtask else 0}


STATUSES = [status("1", "To Do"), status("2", "In Progress"), status("3", "In Review"),
            status("4", "Done")]
# Jira's own work types, nothing added.
TYPES = [kind("10", "Epic"), kind("11", "Story"), kind("12", "Task"), kind("13", "Bug"),
         kind("14", "Subtask", True)]
COLUMNS = [{"name": "To Do", "statuses": [{"id": "1"}]},
           {"name": "In Progress", "statuses": [{"id": "2"}]},
           {"name": "In Review", "statuses": [{"id": "3"}]},
           {"name": "Done", "statuses": [{"id": "4"}]}]
PERMS = {name: {"havePermission": True} for name in jn.PERMISSIONS}


class FakeJira(jc.JiraClient):
    """Answers the reads jira connect makes; records every path."""

    def __init__(self, **changes):
        super().__init__(SITE, EMAIL, SECRET)
        self.answers = {
            "/rest/api/3/myself": {"displayName": "Agent Lab"},
            "/rest/api/3/project/LEDG": {"id": "100", "key": "LEDG", "name": "Ledger",
                                         "style": "classic"},
            "/rest/api/3/issuetype/project": TYPES,
            "/rest/api/3/project/LEDG/statuses": [{"name": "Story", "statuses": STATUSES}],
            "/rest/agile/1.0/board": {"values": [{"id": 7, "name": "LEDG board",
                                                  "type": "kanban"}]},
            "/rest/agile/1.0/board/7/configuration": {"columnConfig": {"columns": COLUMNS}},
            "/rest/api/3/mypermissions": {"permissions": PERMS},
        }
        self.answers.update(changes)
        self.paths = []

    def get(self, path, params=None):
        self.paths.append(path)
        return self.answers[path]


def test_a_ready_space_connects_with_every_place_and_type():
    connection = jn.connect_jira(FakeJira(), "LEDG")
    assert connection.ready
    c = connection.config
    assert c["board"] == {"id": 7, "name": "LEDG board", "type": "kanban"}
    assert {p: s["name"] for p, s in c["places"].items()} == {
        "todo": "To Do", "in_progress": "In Progress", "in_review": "In Review", "done": "Done"}
    assert c["places"]["in_review"]["column"] == "In Review"
    assert {lvl: t["name"] for lvl, t in c["types"].items()} == {
        "epic": "Epic", "story": "Story", "subtask": "Subtask", "task": "Task", "bug": "Bug"}
    # Merged is a label on Done items, not a status.
    assert c["labels"]["merged"] == "agentlab-merged"
    assert c["labels"]["unmerged"] == "agentlab-unmerged"
    assert SECRET not in json.dumps(c) and EMAIL not in json.dumps(c)


def test_every_problem_is_listed_at_once_and_blocks_saving():
    statuses = [s for s in STATUSES if s["name"] != "In Review"]
    columns = [c for c in COLUMNS if c["name"] != "Done"]
    perms = dict(PERMS, LINK_ISSUES={"havePermission": False})
    fake = FakeJira(**{
        "/rest/api/3/project/LEDG/statuses": [{"name": "Story", "statuses": statuses}],
        "/rest/agile/1.0/board/7/configuration": {"columnConfig": {"columns": columns}},
        "/rest/api/3/mypermissions": {"permissions": perms},
        "/rest/agile/1.0/board": {"values": [{"id": 7, "name": "LEDG board", "type": "scrum"}]},
    })
    connection = jn.connect_jira(fake, "LEDG")
    problems = [text for ok, text in connection.checks if ok is False]
    assert not connection.ready
    assert any("waiting for a person" in p for p in problems)
    assert any("'Done' is on no column" in p for p in problems)
    assert any("scrum board" in p for p in problems)
    assert any("LINK_ISSUES" in p for p in problems)


def test_a_sub_task_type_is_required_under_a_story():
    types = [kind("10", "Epic"), kind("11", "Story"), kind("12", "Task"), kind("13", "Bug")]
    found, checks = jn.match_types(types)
    assert found["subtask"] is None
    assert [text for ok, text in checks if ok is False] == [
        "No sub-task type for Sub-tasks: None of its work types fit (it has: Bug, Epic, Story, "
        "Task). Name one with --subtask-type."]


def test_task_and_bug_are_optional():
    found, checks = jn.match_types([kind("10", "Epic"), kind("11", "Story"),
                                    kind("14", "Sub-task", True)])
    assert found["subtask"]["name"] == "Sub-task" and found["task"] is None and found["bug"] is None
    assert all(ok is None for ok, _ in checks) and len(checks) == 2


def test_a_renamed_only_sub_task_type_is_still_found():
    found, checks = jn.match_types([kind("10", "Epic"), kind("11", "Story"),
                                    kind("14", "Piece of work", True)])
    assert found["subtask"]["name"] == "Piece of work"
    assert not [text for ok, text in checks if ok is False]


def test_names_given_on_the_command_line_win():
    places, checks = jn.match_statuses(STATUSES + [status("9", "Waiting on a human")],
                                       {"in_review": "Waiting on a human"})
    assert checks == [] and places["in_review"]["name"] == "Waiting on a human"
    found, _ = jn.match_types(TYPES + [kind("16", "Dev Task", True)], {"subtask": "Dev Task"})
    assert found["subtask"]["name"] == "Dev Task"


def test_a_team_managed_space_is_a_warning_not_a_stop():
    fake = FakeJira(**{"/rest/api/3/project/LEDG": {"id": "100", "key": "LEDG",
                                                    "style": "next-gen"}})
    connection = jn.connect_jira(fake, "LEDG")
    assert connection.ready
    assert any(ok is None and "team-managed" in text for ok, text in connection.checks)


def test_a_team_managed_board_is_a_warning_not_a_stop():
    fake = FakeJira(**{"/rest/agile/1.0/board": {"values": [{"id": 7, "name": "B",
                                                             "type": "simple"}]}})
    connection = jn.connect_jira(fake, "LEDG")
    assert connection.ready
    assert any(ok is None and "sprints are off" in text for ok, text in connection.checks)


def test_several_boards_need_a_choice():
    boards = [{"id": 7, "name": "A", "type": "kanban"}, {"id": 8, "name": "B", "type": "kanban"}]
    with pytest.raises(jn.JiraConnectError, match="--board"):
        jn.choose_board(boards, None)
    assert jn.choose_board(boards, 8)["name"] == "B"
    with pytest.raises(jn.JiraConnectError, match="no board"):
        jn.choose_board([], None)


def test_errors_never_carry_the_credentials(monkeypatch):
    def refuse(request, timeout):
        assert request.get_header("Authorization").startswith("Basic ")
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(jc.urllib.request, "urlopen", refuse)
    client = jc.JiraClient(SITE, EMAIL, SECRET)
    with pytest.raises(jc.JiraAPIError) as caught:
        client.myself()
    assert "JIRA_API_TOKEN" in str(caught.value)
    assert SECRET not in str(caught.value) and EMAIL not in str(caught.value)
    assert SECRET not in repr(client)


def test_the_client_wants_https():
    with pytest.raises(ValueError, match="https"):
        jc.JiraClient("http://example.atlassian.net", EMAIL, SECRET)


def test_project_config_reads_and_checks_the_jira_keys():
    config = parse_project_config({"jira_url": "https://example.atlassian.net/",
                                   "jira_project": "LEDG"})
    assert (config.jira_url, config.jira_project) == ("https://example.atlassian.net", "LEDG")
    with pytest.raises(ExecutionRefused, match="jira_url"):
        parse_project_config({"jira_url": "example.atlassian.net"})
    with pytest.raises(ExecutionRefused, match="jira_project"):
        parse_project_config({"jira_project": "ledg"})


def test_settings_read_the_credentials_and_name_what_is_missing():
    settings = load_settings({"JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": SECRET})
    assert settings.jira_credentials() == (EMAIL, SECRET)
    with pytest.raises(ExecutionRefused, match="JIRA_API_TOKEN is not set"):
        load_settings({"JIRA_EMAIL": EMAIL}).jira_credentials()
    with pytest.raises(ExecutionRefused, match="JIRA_EMAIL"):
        load_settings({"JIRA_EMAIL": "not-an-email"})


def test_agents_never_get_the_jira_credentials():
    from agent_env import build_agent_env  # pyright: ignore[reportMissingImports]  (scripts/ is on the test path)
    env = build_agent_env({"JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": SECRET, "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


def test_jira_connect_saves_only_a_ready_space(tmp_path, monkeypatch, capsys):
    (tmp_path / ".agent-lab.toml").write_text(
        'jira_url = "https://example.atlassian.net"\njira_project = "LEDG"\n', encoding="utf-8")
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", SECRET)
    runs = tmp_path / "runs"
    from orchestrator.cli.commands import jira as jira_command
    monkeypatch.setattr(jira_command, "JiraClient", lambda url, email, token: FakeJira())
    monkeypatch.setattr(cli, "settle_project", lambda args: setattr(args, "repo_path", tmp_path))

    assert cli.main(["jira", "connect", "--runs-dir", str(runs)]) == cli.EXIT_OK
    saved = json.loads((runs / "jira.json").read_text(encoding="utf-8"))
    assert saved["project"]["key"] == "LEDG" and SECRET not in json.dumps(saved)
    assert "Nothing was written to Jira." in capsys.readouterr().out

    (runs / "jira.json").unlink()
    broken = FakeJira(**{"/rest/agile/1.0/board": {"values": [{"id": 7, "name": "B",
                                                               "type": "scrum"}]}})
    monkeypatch.setattr(jira_command, "JiraClient", lambda url, email, token: broken)
    assert cli.main(["jira", "connect", "--runs-dir", str(runs)]) == cli.EXIT_REFUSED
    assert not (runs / "jira.json").exists()
    assert "Not saved" in capsys.readouterr().out
