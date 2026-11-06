"""Sample inputs for every prompt. tests/prompts/expected/ holds what each one rendered to
when the prompts still lived in Python strings (R5): moving them to Markdown must not change
a word. Run this file to rewrite the reference files (only when the wording is meant to change).
"""

from pathlib import Path
from types import SimpleNamespace

from orchestrator.execution import agents as ta
from orchestrator.jira.story import Epic, Story, WorkItem
from orchestrator.planning.ai_planner import build_planner_prompt
from orchestrator.planning.story_planner import build_story_planner_prompt

EXPECTED = Path(__file__).parent / "expected"

ISSUE = {"number": 7, "title": "Add a header check",
         "description": "The tool should check the CSV header.\n\nAcceptance criteria\n- It checks."}
TASK = {"id": "T2", "title": "Check the header", "description": "Reject files without a header.",
        "expected_outcome": "A clear error for an empty file.", "files": ["src/a.py", "src/b.py"],
        "acceptance_criteria": ["An empty file is rejected.", "The message names the file."]}
BARE_TASK = {"id": "T1", "title": "t", "acceptance_criteria": []}
FEEDBACK = "Attempt 1 failed.\n- The full test suite failed.\nThe end of the output:\n1 failed"
EARLIER = "The worktree already holds: src/a.py (changed)."
LONG_ISSUE = {**ISSUE, "description": "word " * 3000}
PLAN = SimpleNamespace(
    task=SimpleNamespace(repository="Anji-jk/Agent-lab", issue_number=7, title="Add a header check",
                         description="Check the header.\n\nAcceptance criteria\n- It checks."),
    base_ref="main",
)
LONG_PLAN = SimpleNamespace(
    task=SimpleNamespace(repository="o/r", issue_number=9, title="Long", description="x" * 13_000),
    base_ref="main",
)


def _item(key, summary, description, *, done=False, blocked_by=()):
    return WorkItem(key=key, type="Subtask", summary=summary, description=description,
                    status="Done" if done else "To Do", done=done, labels=("cli",),
                    priority="Medium", url=f"https://x/browse/{key}",
                    acceptance_criteria="- It works.", blocked_by=blocked_by)


STORY = Story(
    WorkItem(key="KAN-10", type="Feature", summary="Export to CSV",
             description="Users want a spreadsheet.\n\nAcceptance Criteria:\n- A CSV file.",
             status="To Do", done=False, labels=(), priority="High", url="https://x/browse/KAN-10"),
    Epic("KAN-9", "Reports"),
    (_item("KAN-11", "Formatter", "Rows.\n\nAcceptance Criteria:\n- Quoted commas."),
     _item("KAN-12", "Command", "ledger export\n\nAcceptance Criteria:\n- Prints rows.",
           blocked_by=("KAN-11",)),
     _item("KAN-8", "Spike", "Done before.", done=True)),
)

# A sub-task of a Jira story plan, as jira plan stores it, and what the agents are shown.
STORY_PLAN = {
    "source": {"kind": "jira", "key": "KAN-10", "url": "https://x/browse/KAN-10",
               "epic": {"key": "KAN-9", "summary": "Reports"}},
    "issue": {"repository": None, "number": None, "key": "KAN-10",
              "title": "Export ledger entries to CSV",
              "description": "Users want their entries in a spreadsheet."},
    "tasks": [{"id": "T1"}, {"id": "T2"}, {"id": "T3"}],
}
STORY_TASK = {
    "id": "T2", "jira_key": "KAN-12", "title": "Add the export command",
    "description": "ledger export --out file.csv", "depends_on": ["T1"],
    "dependency_reasons": {"T1": "The command calls the CSV formatter that KAN-11 adds."},
    "expected_outcome": "ledger export --out f.csv writes the file and prints the row count.",
    "files": ["src/ledger/__main__.py", "tests/test_cli.py"],
    "verification": ["python -m pytest -q tests/test_cli.py"],
    "acceptance_criteria": ["Command writes the file and prints how many rows."],
    "doc_target": None,
    "context": {"read_first": ["CLAUDE.md", "src/ledger/__main__.py"],
                "notes": "main() sends every command except add to _list; dispatch by command."},
}
FINISHED = [{"id": "T1", "jira_key": "KAN-11", "title": "Write CSV formatter for entries",
             "commit": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
             "summary": "Added src/ledger/export.py with to_csv(rows) using the csv module.",
             "files": ["src/ledger/export.py", "tests/test_export.py"]}]
AGENT_STORY = ta.story_of(STORY_PLAN)

CASES = {
    "builder": lambda: ta.builder_prompt(ISSUE, TASK),
    "builder_retry_continue": lambda: ta.builder_prompt(ISSUE, TASK, FEEDBACK, EARLIER,
                                                        "tests/test_calc.py"),
    "builder_long_issue_bare_task": lambda: ta.builder_prompt(LONG_ISSUE, BARE_TASK),
    "tester": lambda: ta.tester_prompt(ISSUE, TASK, ["src/a.py", "src/b.py"]),
    "tester_continue": lambda: ta.tester_prompt(ISSUE, TASK, [], EARLIER, "tests/test_calc.py"),
    "reviewer": lambda: ta.reviewer_prompt(ISSUE, TASK, "diff --git a/x b/x\n+1", ["suite passed", "2 files"]),
    "reviewer_huge_diff": lambda: ta.reviewer_prompt(ISSUE, TASK, "d" * 200_000, ["suite passed"]),
    "reviewer_no_facts": lambda: ta.reviewer_prompt(ISSUE, BARE_TASK, "", []),
    "builder_story": lambda: ta.builder_prompt(ISSUE, STORY_TASK, story=AGENT_STORY, finished=FINISHED),
    "tester_story": lambda: ta.tester_prompt(ISSUE, STORY_TASK, ["src/ledger/__main__.py"],
                                             story=AGENT_STORY),
    "reviewer_story": lambda: ta.reviewer_prompt(ISSUE, STORY_TASK, "diff --git a/x b/x\n+1",
                                                 ["suite passed"], story=AGENT_STORY),
    "command_rules": lambda: ta.command_rules("tests/test_calc.py"),
    "planner": lambda: build_planner_prompt(PLAN, ["It checks.", "It reports."], "src/a.py\ntests/t.py"),
    "planner_no_criteria_long": lambda: build_planner_prompt(LONG_PLAN, [], "ctx"),
    "story_planner": lambda: build_story_planner_prompt(STORY, base_ref="main", docs_dir="docs",
                                                        context="ledger/cli.py\nREADME.md"),
}


def write_expected() -> None:
    EXPECTED.mkdir(exist_ok=True)
    for name, render in CASES.items():
        (EXPECTED / f"{name}.txt").write_text(render(), encoding="utf-8", newline="")


if __name__ == "__main__":
    write_expected()
