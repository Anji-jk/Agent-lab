"""What the builder, tester and reviewer are told, how their reports are read,
and how the task's final status is decided.

Reports are claims. ``decide`` combines them with the facts the orchestrator
established itself (``task_checks``) and never trusts a claim over a fact.

Statuses that come out of ``decide``:

* ``done``         every check passed.
* ``failed``       a check caught a problem: tests fail, a criterion is unmet,
                   files outside the plan changed, an existing test was weakened.
* ``needs_review`` the run could not be judged: an agent crashed, timed out,
                   or gave no usable report. A person (or a retry) must look.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.project_config import DEFAULT_TEST_EXAMPLE
from orchestrator.prompts.loader import render

DONE = "done"
FAILED = "failed"
NEEDS_REVIEW = "needs_review"

# A prompt travels as a command-line argument on Windows (about 32k limit).
MAX_PROMPT_CHARS = 28_000
MAX_ISSUE_CHARS = 6_000
MAX_TEXT_FIELD = 600
MAX_LIST_ITEMS = 40

# --- prompts ----------------------------------------------------------------


def _bullets(items: list[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1)) or "(none)"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def _task_block(issue: dict, task: dict) -> str:
    return render(
        "shared/task_block",
        issue_number=issue.get("number"),
        issue_title=issue.get("title", ""),
        issue_description=_cut(issue.get("description") or "", MAX_ISSUE_CHARS),
        task_id=task["id"],
        task_title=task["title"],
        task_description=task.get("description", ""),
        expected_outcome=task.get("expected_outcome", ""),
        files=", ".join(task.get("files") or []) or "(not specified)",
        criteria=_bullets(task["acceptance_criteria"]),
    )


def command_rules(test_example: str = DEFAULT_TEST_EXAMPLE) -> str:
    """How the builder and tester may run commands (prompts/shared/command_rules.md).

    One block for both, so the two cannot drift apart: when they did, an agent ran the whole
    suite and waited about 8 minutes for it. ``test_example`` is the project's own example of
    naming test files.
    """
    return render("shared/command_rules", test_example=test_example)


COMMAND_RULES = command_rules()


def _earlier_block(earlier: str | None) -> str:
    """What a continued task's worktree already holds."""
    return f"\nAbout this worktree:\n{earlier}\n" if earlier else ""


def builder_prompt(issue: dict, task: dict, feedback: str | None = None,
                   earlier: str | None = None, test_example: str = DEFAULT_TEST_EXAMPLE) -> str:
    return render(
        "builder",
        task_block=_task_block(issue, task),
        feedback_block=f"\nProblems found by an earlier attempt:\n{feedback}\n" if feedback else "",
        earlier_block=_earlier_block(earlier),
        test_example=test_example,
    )


def tester_prompt(issue: dict, task: dict, changed_files: list[str],
                  earlier: str | None = None, test_example: str = DEFAULT_TEST_EXAMPLE) -> str:
    return render(
        "tester",
        task_block=_task_block(issue, task),
        changed_files=", ".join(changed_files) or "(none)",
        earlier_block=_earlier_block(earlier),
        test_example=test_example,
    )


def reviewer_prompt(
    issue: dict,
    task: dict,
    diff: str,
    facts: list[str],
) -> str:
    fields = {"task_block": _task_block(issue, task), "facts": _bullets(facts)}
    # What the prompt is without the diff, to see how much of the diff fits. 2 is the length
    # of the "{}" the diff used to replace and 20 leaves room for the truncation marker.
    room = MAX_PROMPT_CHARS - len(render("reviewer", diff="", **fields)) - 2 - 20
    return render("reviewer", diff=_cut(diff, max(room, 1000)), **fields)


# --- reading reports -----------------------------------------------------------


class ReportError(ValueError):
    """An agent's final message was not the JSON report it was asked for."""


def extract_json(text: object) -> dict:
    """The last top-level JSON object in ``text`` (fences and prose tolerated)."""
    if not isinstance(text, str) or not text.strip():
        raise ReportError("The agent returned no text.")
    decoder = json.JSONDecoder()
    found: dict | None = None
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            found = value
        index = end
    if found is None:
        raise ReportError("No JSON report found in the agent's reply.")
    return found


def _text(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReportError(f"{name} must be text.")
    return value[:MAX_TEXT_FIELD]


def _text_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReportError(f"{name} must be a list of text.")
    return [item[:MAX_TEXT_FIELD] for item in value[:MAX_LIST_ITEMS]]


def _number(value: Any, count: int, name: str) -> int:
    """A 1-based criterion number inside 1..count."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= count:
        raise ReportError(f"{name} must be a criterion number from 1 to {count}.")
    return value


@dataclass(frozen=True, slots=True)
class BuilderReport:
    summary: str
    files_changed: list[str]
    blocked: bool
    blocker: str


@dataclass(frozen=True, slots=True)
class TesterReport:
    summary: str
    coverage: dict[int, list[str]]
    uncovered: dict[int, str]
    suspected_defects: list[str]
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReviewerReport:
    verdict: str
    met: dict[int, bool]
    evidence: dict[int, str]
    problems: list[str]
    notes: list[str] = field(default_factory=list)


def parse_builder(text: object) -> BuilderReport:
    data = extract_json(text)
    blocked = data.get("blocked", False)
    if not isinstance(blocked, bool):
        raise ReportError("blocked must be true or false.")
    return BuilderReport(
        summary=_text(data.get("summary"), "summary"),
        files_changed=_text_list(data.get("files_changed"), "files_changed"),
        blocked=blocked,
        blocker=_text(data.get("blocker"), "blocker"),
    )


def parse_tester(text: object, criteria_count: int) -> TesterReport:
    data = extract_json(text)
    coverage: dict[int, list[str]] = {}
    for item in data.get("coverage") or []:
        if not isinstance(item, dict):
            raise ReportError("Each coverage entry must be an object.")
        number = _number(item.get("criterion"), criteria_count, "coverage criterion")
        coverage.setdefault(number, []).extend(_text_list(item.get("tests"), "tests"))
    uncovered: dict[int, str] = {}
    for item in data.get("uncovered") or []:
        if not isinstance(item, dict):
            raise ReportError("Each uncovered entry must be an object.")
        number = _number(item.get("criterion"), criteria_count, "uncovered criterion")
        uncovered[number] = _text(item.get("reason"), "reason")
    return TesterReport(
        summary=_text(data.get("summary"), "summary"),
        coverage=coverage,
        uncovered=uncovered,
        suspected_defects=_text_list(data.get("suspected_defects"), "suspected_defects"),
        notes=_text_list(data.get("notes"), "notes"),
    )


def parse_reviewer(text: object, criteria_count: int) -> ReviewerReport:
    data = extract_json(text)
    verdict = data.get("verdict")
    if verdict not in ("pass", "fail"):
        raise ReportError('verdict must be "pass" or "fail".')
    met: dict[int, bool] = {}
    evidence: dict[int, str] = {}
    for item in data.get("criteria") or []:
        if not isinstance(item, dict):
            raise ReportError("Each criteria entry must be an object.")
        number = _number(item.get("criterion"), criteria_count, "criterion")
        if not isinstance(item.get("met"), bool):
            raise ReportError("met must be true or false.")
        met[number] = item["met"]
        evidence[number] = _text(item.get("evidence"), "evidence")
    # A reviewer that skips a criterion has not reviewed the task: treat the
    # report as unusable rather than reading silence as "met" or "unmet".
    skipped = [n for n in range(1, criteria_count + 1) if n not in met]
    if skipped:
        raise ReportError(
            "The reviewer did not judge criteria: " + ", ".join(str(n) for n in skipped)
        )
    return ReviewerReport(
        verdict, met, evidence,
        _text_list(data.get("problems"), "problems"),
        _text_list(data.get("notes"), "notes"),
    )


# --- the decision ---------------------------------------------------------------


@dataclass(slots=True)
class Findings:
    """Everything known when the pipeline stops. Fields are None until measured."""

    criteria_count: int
    builder_ok: bool | None = None          # agent ran and returned a usable report
    builder_blocked: str = ""
    nothing_changed: bool = False
    scope_violations: list[str] = field(default_factory=list)
    tester_ok: bool | None = None
    tester_wrote_outside_tests: list[str] = field(default_factory=list)
    uncovered: dict[int, str] = field(default_factory=dict)
    unnamed: list[int] = field(default_factory=list)       # criteria with no test and no reason
    no_tests: bool = False                                  # the tester named no test at all
    invalid_test_ids: list[str] = field(default_factory=list)
    missing_tests: list[str] = field(default_factory=list)  # named but not collected / not found
    weakened_tests: list[str] = field(default_factory=list)
    suspected_defects: list[str] = field(default_factory=list)
    tester_notes: list[str] = field(default_factory=list)       # recorded, never fail
    suite_passed: bool | None = None
    named_passed: bool | None = None
    suite_timed_out: bool = False   # stopped at the time limit: nothing was reported failing
    named_timed_out: bool = False
    test_timeout_seconds: int | None = None
    reviewer_ok: bool | None = None
    reviewer_verdict: str | None = None
    unmet: list[int] = field(default_factory=list)
    reviewer_problems: list[str] = field(default_factory=list)  # blocking
    reviewer_notes: list[str] = field(default_factory=list)     # recorded, never fail
    infrastructure: list[str] = field(default_factory=list)  # what stopped judging


def problems(findings: Findings) -> list[str]:
    """Reasons the task has failed so far. Empty means nothing is wrong yet."""
    f = findings
    reasons: list[str] = []
    if f.builder_blocked:
        reasons.append(f"The builder reported it is blocked: {f.builder_blocked}")
    if f.nothing_changed:
        reasons.append("The builder changed no files.")
    if f.scope_violations:
        reasons.append("Files outside the plan were changed: " + ", ".join(f.scope_violations))
    if f.tester_wrote_outside_tests:
        reasons.append(
            "The tester changed files outside tests/: " + ", ".join(f.tester_wrote_outside_tests)
        )
    if f.weakened_tests:
        reasons.append(
            "Existing tests lost lines (edited or deleted): " + ", ".join(f.weakened_tests)
        )
    if f.no_tests:
        reasons.append("The tester named no tests.")
    if f.invalid_test_ids:
        reasons.append("The tester named invalid test IDs: " + ", ".join(f.invalid_test_ids))
    if f.missing_tests:
        reasons.append("Named tests were not found or did not run: " + ", ".join(f.missing_tests))
    if f.unnamed:
        numbers = ", ".join(str(n) for n in f.unnamed)
        reasons.append(f"No test and no reason given for criteria: {numbers}")
    # `uncovered` (a stated reason why a criterion has no automated test) is
    # recorded but is not a failure: the reviewer must still confirm it is met.
    if f.suspected_defects:
        reasons.append("The tester suspects defects: " + "; ".join(f.suspected_defects))
    if f.suite_passed is False:
        reasons.append(_timed_out("The full test suite", f) if f.suite_timed_out
                       else "The full test suite fails.")
    if f.named_passed is False:
        reasons.append(_timed_out("The tester's named tests", f) if f.named_timed_out
                       else "The tester's named tests fail.")
    if f.reviewer_verdict == "fail":
        reasons.append("The reviewer's verdict is fail.")
    if f.unmet:
        reasons.append("The reviewer found criteria unmet: " + ", ".join(str(n) for n in f.unmet))
    reasons += [f"Reviewer: {problem}" for problem in f.reviewer_problems]
    return reasons


def _timed_out(what: str, f: Findings) -> str:
    limit = f"{f.test_timeout_seconds} s" if f.test_timeout_seconds else "the time limit"
    return (f"{what} did not finish within {limit}: it timed out, and no test was reported "
            "failing. Raise AGENT_LAB_TEST_TIMEOUT or make the tests faster.")


def decide(findings: Findings) -> tuple[str, list[str]]:
    """Final status and the reasons for it. Pure; no I/O."""
    f = findings
    if f.infrastructure:
        return NEEDS_REVIEW, list(f.infrastructure)

    reasons = problems(f)
    if reasons:
        return FAILED, reasons

    # Passing needs positive evidence for every stage, not just no complaints.
    if not (
        f.builder_ok and f.tester_ok and f.reviewer_ok
        and f.suite_passed is True and f.named_passed is True
        and f.reviewer_verdict == "pass"
    ):
        return NEEDS_REVIEW, ["The run ended before every check produced a result."]
    return DONE, ["Every check passed."]
