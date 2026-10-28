"""Sample inputs for every prompt. tests/prompts/expected/ holds what each one rendered to
when the prompts still lived in Python strings (R5): moving them to Markdown must not change
a word. Run this file to rewrite the reference files (only when the wording is meant to change).
"""

from pathlib import Path
from types import SimpleNamespace

from orchestrator.execution import agents as ta
from orchestrator.planning.ai_planner import build_planner_prompt

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
    "command_rules": lambda: ta.command_rules("tests/test_calc.py"),
    "planner": lambda: build_planner_prompt(PLAN, ["It checks.", "It reports."], "src/a.py\ntests/t.py"),
    "planner_no_criteria_long": lambda: build_planner_prompt(LONG_PLAN, [], "ctx"),
}


def write_expected() -> None:
    EXPECTED.mkdir(exist_ok=True)
    for name, render in CASES.items():
        (EXPECTED / f"{name}.txt").write_text(render(), encoding="utf-8", newline="")


if __name__ == "__main__":
    write_expected()
