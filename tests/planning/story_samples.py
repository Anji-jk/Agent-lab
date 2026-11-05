"""A Jira story like KAN-10 in ledger-cli, and planner answers for it."""

import copy

from orchestrator.jira.story import Epic, Story, WorkItem

SITE = "https://example.atlassian.net/browse"


def work_item(key, summary, *, criteria="- It works.", blocked_by=(), done=False,
              description=None):
    text = description if description is not None else (
        f"{summary}.\n\nAcceptance Criteria:\n\n{criteria}" if criteria else summary)
    return WorkItem(key=key, type="Subtask", summary=summary, description=text,
                    status="Done" if done else "To Do", done=done, labels=("backend",),
                    priority="Medium", url=f"{SITE}/{key}",
                    acceptance_criteria=criteria or None, blocked_by=tuple(blocked_by))


def story(*subtasks):
    the_story = WorkItem(key="KAN-10", type="Feature", summary="Export ledger entries to CSV",
                         description="Users want their entries in a spreadsheet.",
                         status="To Do", done=False, labels=(), priority="High",
                         url=f"{SITE}/KAN-10")
    return Story(the_story, Epic("KAN-9", "Reports"), tuple(subtasks or SUBTASKS))


SUBTASKS = (
    work_item("KAN-11", "Write CSV formatter for entries",
              criteria="- Date, account and amount columns.\n- Commas in names are quoted."),
    work_item("KAN-12", "Add the export command",
              criteria="- Command writes the file and prints how many rows.",
              blocked_by=("KAN-11",)),
    work_item("KAN-13", "Document the export command",
              criteria="- README shows the command and an example."),
    work_item("KAN-14", "Old spike", done=True),
)


def entry(key, **more):
    return {"key": key, "depends_on": [], "dependency_reasons": {}, "files": [f"{key}.py"],
            "complexity": "small", "parallel_safe": False, "expected_outcome": f"{key} done",
            "verification": ["python -m pytest -q"], "doc_target": None,
            "context": {"read_first": ["CLAUDE.md"], "notes": ""}, **more}


ANSWER = {
    "summary": "Formatter first, then the command, then the README.",
    "tasks": [
        entry("KAN-11", files=["ledger/csv_export.py", "tests/test_csv_export.py"],
              doc_target="docs/export.md",
              context={"read_first": ["CLAUDE.md", "ledger/models.py"],
                       "notes": "Use the csv module."}),
        entry("KAN-12", depends_on=["KAN-11"], files=["ledger/cli.py"]),
        entry("KAN-13", depends_on=["KAN-12"],
              dependency_reasons={"KAN-12": "It documents the command KAN-12 adds."},
              files=["README.md"]),
    ],
}


def answer(**changes):
    """ANSWER with entries changed, such as answer(KAN_12={"files": ["src/a.py"]})."""
    copied = copy.deepcopy(ANSWER)
    for name, change in changes.items():
        key = name.replace("_", "-")
        for task in copied["tasks"]:
            if task["key"] == key:
                task.update(change)
    return copied
