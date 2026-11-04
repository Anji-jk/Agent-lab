"""Read a Jira story with its epic and sub-tasks. A fake Jira stands in: no network, no writes."""

import json

import pytest

import orchestrator.cli.commands as cli
from orchestrator.cli.commands import jira as jira_command
from orchestrator.jira import client as jc
from orchestrator.jira import story as js

SITE = "https://example.atlassian.net"


def adf(*lines):
    """A description: one paragraph per line, "- " lines as one bullet list."""
    content, items = [], []
    for line in lines:
        if line.startswith("- "):
            items.append({"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": line[2:]}]}]})
            continue
        if items:
            content.append({"type": "bulletList", "content": items})
            items = []
        content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
    if items:
        content.append({"type": "bulletList", "content": items})
    return {"type": "doc", "version": 1, "content": content}


def blocked_by(key, summary=""):
    return {"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
            "inwardIssue": {"key": key, "fields": {"summary": summary}}}


def blocks(key):
    return {"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
            "outwardIssue": {"key": key, "fields": {"summary": ""}}}


def relates(key):
    return {"type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
            "outwardIssue": {"key": key, "fields": {"summary": ""}}}


def item(key, summary, *, kind="Subtask", subtask=True, level=-1, description=None,
         status=("To Do", "new"), labels=(), priority="Medium", links=(), **more):
    fields = {"summary": summary, "description": description,
              "issuetype": {"name": kind, "subtask": subtask, "hierarchyLevel": level},
              "status": {"name": status[0], "statusCategory": {"key": status[1]}},
              "labels": list(labels), "priority": {"name": priority} if priority else None,
              "issuelinks": list(links), **more}
    return {"key": key, "fields": fields}


STORY = item(
    "SKM-9", "Employee Enrolment Management for Training Batches",
    kind="Story", subtask=False, level=0, priority="High",
    description=adf("Business Context: admins manage who is in each batch.",
                    "Acceptance Criteria:", "- Admins can add and remove employees."),
    parent={"key": "SKM-8", "fields": {"summary": "Training Lifecycle"}},
    subtasks=[{"key": "SKM-10"}, {"key": "SKM-11"}, {"key": "SKM-12"}],
)
SUBTASKS = [
    item("SKM-12", "Develop Completion Status Tracking",
         description=adf("Description: add a status field.",
                         "Acceptance Criteria: Status updates are persisted and shown in the UI.",
                         "Dependencies: Implement Enrolment/Removal Backend Logic.",
                         "Labels: backend, frontend."),
         labels=("backend", "frontend"), links=[blocked_by("SKM-11")]),
    item("SKM-10", "Design Enrolment UI and Batch Selection",
         description=adf("Description: the enrolment screen.",
                         "Acceptance Criteria:",
                         "- UI allows batch selection.",
                         "- Shows the employees enrolled now.",
                         "Dependencies: None."),
         labels=("frontend", "ui-design"), links=[relates("SKM-99")]),
    item("SKM-11", "Implement Enrolment/Removal Backend Logic", priority="High",
         description=adf("Acceptance Criteria:", "- Endpoints update the database.",
                         "- No duplicate UserID/BatchID pair."),
         labels=("backend", "api"), links=[blocks("SKM-12"), blocked_by("SKM-50")]),
]


class FakeJira(jc.JiraClient):
    def __init__(self, story=STORY, subtasks=SUBTASKS):
        super().__init__(SITE, "a@example.com", "token")
        self.items = {story["key"]: story}
        self.subtasks = list(subtasks)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/rest/api/3/search/jql":
            return {"issues": self.subtasks, "isLast": True}
        return self.items[path.rsplit("/", 1)[1]]


def test_the_story_its_epic_and_sub_tasks_are_read():
    story = js.read_story(FakeJira(), "SKM-9", "SKM")
    assert story.story.key == "SKM-9" and story.story.priority == "High"
    assert story.story.url == f"{SITE}/browse/SKM-9"
    assert story.story.acceptance_criteria == "- Admins can add and remove employees."
    assert story.epic == js.Epic("SKM-8", "Training Lifecycle")
    assert [subtask.key for subtask in story.subtasks] == ["SKM-10", "SKM-11", "SKM-12"]


def test_acceptance_criteria_come_from_the_description():
    story = js.read_story(FakeJira(), "SKM-9", "SKM")
    first, second, third = story.subtasks
    assert first.acceptance_criteria == ("- UI allows batch selection.\n"
                                         "- Shows the employees enrolled now.")
    assert second.acceptance_criteria == ("- Endpoints update the database.\n"
                                          "- No duplicate UserID/BatchID pair.")
    assert third.acceptance_criteria == "Status updates are persisted and shown in the UI."
    assert "Dependencies: Implement Enrolment/Removal Backend Logic." in third.description


def test_blocked_by_comes_from_blocks_links_between_sub_tasks():
    story = js.read_story(FakeJira(), "SKM-9", "SKM")
    by_key = {subtask.key: subtask for subtask in story.subtasks}
    assert by_key["SKM-12"].blocked_by == ("SKM-11",)
    assert by_key["SKM-11"].blocked_by == ()
    assert by_key["SKM-10"].blocked_by == ()
    assert [(link.phrase, link.key) for link in by_key["SKM-11"].links] == [
        ("blocks", "SKM-12"), ("is blocked by", "SKM-50")]


def test_only_reads_and_asks_for_the_sub_tasks_once():
    fake = FakeJira()
    js.read_story(fake, "skm-9 ", "SKM")
    assert [path for path, _ in fake.calls] == ["/rest/api/3/issue/SKM-9", "/rest/api/3/search/jql"]
    assert fake.calls[1][1]["jql"] == "parent = SKM-9 ORDER BY key ASC"
    assert "subtasks" in fake.calls[0][1]["fields"]


def test_a_story_without_sub_tasks_does_not_search():
    lonely = item("SKM-30", "Lonely", kind="Story", subtask=False, level=0, subtasks=[])
    fake = FakeJira(story=lonely, subtasks=[])
    story = js.read_story(fake, "SKM-30")
    assert story.subtasks == () and story.epic is None
    assert len(fake.calls) == 1
    assert "The story has no sub-tasks." in js.format_story(story)


def test_a_sub_task_or_an_epic_is_refused_with_what_to_use():
    subtask = item("SKM-10", "x", parent={"key": "SKM-9"})
    with pytest.raises(js.StoryError, match="SKM-10 is a sub-task; start from its story SKM-9"):
        js.read_story(FakeJira(story=subtask), "SKM-10")
    epic = item("SKM-8", "x", kind="Epic", subtask=False, level=1)
    with pytest.raises(js.StoryError, match="SKM-8 is an epic"):
        js.read_story(FakeJira(story=epic), "SKM-8")


@pytest.mark.parametrize("key, problem", [
    ("", "is not a Jira key"), ("SKM", "is not a Jira key"), ("SKM-0", "is not a Jira key"),
    ("9", "is not a Jira key"), ("LEDG-3", "not in this project's Jira space SKM"),
])
def test_bad_keys_are_refused_before_any_call(key, problem):
    fake = FakeJira()
    with pytest.raises(js.StoryError, match=problem):
        js.read_story(fake, key, "SKM")
    assert fake.calls == []


def test_sub_tasks_in_done_are_marked_done():
    done = item("SKM-10", "Design", status=("Done", "done"),
                description=adf("Acceptance Criteria: shown"))
    story = js.read_story(FakeJira(subtasks=[done]), "SKM-9")
    assert story.subtasks[0].done is True


@pytest.mark.parametrize("description, expected", [
    ("Acceptance Criteria:\n- a\n- b\nDependencies: none", "- a\n- b"),
    ("**Acceptance criteria:** one line", "one line"),
    ("## Acceptance Criteria\n- a\n## Notes\nx", "- a"),
    ("- Acceptance Criteria: Endpoints work; no duplicates.\n- Dependencies: x",
     "Endpoints work; no duplicates."),
    ("Acceptance Criteria:\n    - a\n    - b\nLabels: qa", "- a\n- b"),
    ("Description: no criteria here", None),
    ("", None),
])
def test_acceptance_criteria_parsing(description, expected):
    assert js.acceptance_criteria(description) == expected


def test_the_summary_names_what_is_missing():
    no_criteria = item("SKM-10", "Design", description=adf("Description: only this"))
    text = js.format_story(js.read_story(FakeJira(subtasks=[no_criteria]), "SKM-9"))
    assert "acceptance criteria: NONE FOUND" in text
    assert "No acceptance criteria found in: SKM-10." in text


def test_the_summary_lists_each_sub_task():
    text = js.format_story(js.read_story(FakeJira(), "SKM-9", "SKM"))
    assert text.splitlines()[0] == "Story:   SKM-9  Employee Enrolment Management for Training Batches"
    assert "Epic:    SKM-8  Training Lifecycle" in text
    assert "Sub-tasks: 3" in text
    assert "      blocked by SKM-11" in text
    assert "      acceptance criteria: 2 lines" in text
    assert "NONE FOUND" not in text


def test_search_follows_the_pages():
    pages = [{"issues": [{"key": "A-1"}], "nextPageToken": "t2"},
             {"issues": [{"key": "A-2"}], "nextPageToken": "t3"},
             {"issues": [{"key": "A-3"}], "isLast": True}]
    seen = []

    class Paged(jc.JiraClient):
        def get(self, path, params=None):
            seen.append(params.get("nextPageToken"))
            return pages[len(seen) - 1]

    found = Paged(SITE, "a@x", "t").search("parent = A-0", ("summary",))
    assert [item["key"] for item in found] == ["A-1", "A-2", "A-3"]
    assert seen == [None, "t2", "t3"]


def test_jira_story_command(tmp_path, monkeypatch, capsys):
    (tmp_path / ".agent-lab.toml").write_text(
        f'jira_url = "{SITE}"\njira_project = "SKM"\n', encoding="utf-8")
    monkeypatch.setenv("JIRA_EMAIL", "a@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-token")
    monkeypatch.setattr(jira_command, "JiraClient", lambda url, email, token: FakeJira())
    monkeypatch.setattr(cli, "settle_project", lambda args: setattr(args, "repo_path", tmp_path))

    assert cli.main(["jira", "story", "SKM-9"]) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert "Sub-tasks: 3" in output and "Nothing was written to Jira or saved." in output
    assert "secret-token" not in output

    assert cli.main(["jira", "story", "SKM-9", "--json"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["epic"]["key"] == "SKM-8"
    assert [subtask["key"] for subtask in data["subtasks"]] == ["SKM-10", "SKM-11", "SKM-12"]
    assert data["subtasks"][2]["blocked_by"] == ["SKM-11"]
    assert list(tmp_path.iterdir()) == [tmp_path / ".agent-lab.toml"]

    assert cli.main(["jira", "story", "LEDG-1"]) == cli.EXIT_REFUSED
    assert "not in this project's Jira space SKM" in capsys.readouterr().err
