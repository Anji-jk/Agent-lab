
import pytest

from orchestrator.github.issue import Task


def sample_issue():
    return {
        "number": 1,
        "title": "Test task",
        "body": "Inspect the project.",
        "html_url": "https://github.com/Anji-jk/Agent-lab/issues/1",
    }


def test_builds_task_from_issue():
    task = Task.from_issue("Anji-jk/Agent-lab", sample_issue())

    assert task.task_id == "issue-1"
    assert task.title == "Test task"
    assert task.description == "Inspect the project."


def test_rejects_missing_title():
    issue = sample_issue()
    issue["title"] = "  "

    with pytest.raises(ValueError, match="title"):
        Task.from_issue("Anji-jk/Agent-lab", issue)


def test_rejects_pull_request():
    issue = sample_issue()
    issue["pull_request"] = {}

    with pytest.raises(ValueError, match="Pull requests"):
        Task.from_issue("Anji-jk/Agent-lab", issue)
