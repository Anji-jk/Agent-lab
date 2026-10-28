"""The safety screen: Issues that tell the agent not to do what execution involves.

Best effort and deliberately conservative. A match blocks execution; it is never a way to
approve it.
"""

from __future__ import annotations

import re

from orchestrator.github.issue import Task

_NEGATION = r"(?:do not|don't|dont|must not|never|should not|shall not|may not)"
_ARTICLE = r"(?:(?:a|an|any|the|new|git)\s+)*"

# Best-effort, deliberately conservative screen for Issues that tell the
# agent not to do the very things execution involves. A match blocks
# execution; absence of a match is NOT a safety guarantee.
PROHIBITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "forbids execution",
        re.compile(rf"\b{_NEGATION}\s+(?:execute|implement|start)\b", re.I),
    ),
    (
        "forbids modifications",
        re.compile(
            rf"\b{_NEGATION}\s+(?:modify|change|edit|write to|touch)\s+"
            rf"{_ARTICLE}(?:files?|code|repo(?:sitory)?|anything)\b",
            re.I,
        ),
    ),
    (
        "forbids making changes",
        re.compile(rf"\b{_NEGATION}\s+make\s+{_ARTICLE}changes\b", re.I),
    ),
    (
        "forbids branch creation",
        re.compile(
            rf"\b{_NEGATION}\s+(?:create|make|open)\s+{_ARTICLE}branch(?:es)?\b",
            re.I,
        ),
    ),
    (
        "forbids pull requests",
        re.compile(
            rf"\b{_NEGATION}\s+(?:create|open|submit|make|raise)\s+"
            rf"{_ARTICLE}(?:pull[- ]requests?|prs?)\b",
            re.I,
        ),
    ),
    (
        "forbids pull requests",
        re.compile(r"\bno\s+(?:pull[- ]requests?|prs?)\b", re.I),
    ),
    (
        "marks the task as non-executable",
        re.compile(r"\b(?:read[- ]only|analysis[- ]only|plan[- ]only)\s+task\b", re.I),
    ),
)


def find_execution_prohibitions(task: Task) -> list[str]:
    """Return the reasons the Issue text forbids execution (empty if none)."""
    text = f"{task.title}\n{task.description}"
    reasons: list[str] = []
    for reason, pattern in PROHIBITION_PATTERNS:
        match = pattern.search(text)
        if match and reason not in reasons:
            reasons.append(f"{reason}: {match.group(0).strip()!r}")
    return reasons
