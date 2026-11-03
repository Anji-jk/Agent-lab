"""What a project gives Agent Lab to work from: its CLAUDE.md and its docs folder.

Nothing here stops a run. Every agent reads the project's CLAUDE.md, and the planner also has
the project's file list (``git ls-files``), so a project with less in it still works: the
planner just searches more. This check says what is there and what is missing, so a person can
fill the gaps when they want to.

What helps most in CLAUDE.md (a ## or ### heading containing the word is enough, so "Rules for
the code" and "Instructions for agents" both count):

* structure     which folders the project has and what each holds
* commands      how to run the project and its tests
* rules         instructions every agent follows (the words "rule" and "instruction" both count)

The docs folder is optional. When ``docs_dir`` in ``.agent-lab.toml`` names one that exists, the
planner reads it before searching the code, and feature docs are written there after a story.
Without it, the planner uses the file list and no feature doc is written.

Nothing here writes anything, calls Jira or GitHub, or starts an agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.project_config import ProjectConfig

CLAUDE_FILE_NAME = "CLAUDE.md"
SECTIONS = (
    ("Project structure", ("structure",), "which folders there are and what each holds"),
    ("Commands", ("command",), "how to run the project and its tests"),
    ("Rules", ("rule", "instruction"), "instructions every agent follows"),
)
_HEADING = re.compile(r"^#{2,3}[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


@dataclass(frozen=True, slots=True)
class Finding:
    found: bool
    what: str


@dataclass(frozen=True, slots=True)
class ProjectCheck:
    root: Path
    findings: tuple[Finding, ...]
    docs_dir: str | None

    @property
    def gaps(self) -> list[str]:
        return [finding.what for finding in self.findings if not finding.found]


def section_headings(text: str) -> list[str]:
    """The ## and ### headings of a Markdown text, leaving out those inside code blocks."""
    headings: list[str] = []
    in_code = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            continue
        if not in_code:
            match = _HEADING.match(line)
            if match:
                headings.append(match.group(1).strip())
    return headings


def missing_sections(headings: list[str]) -> list[str]:
    """The parts no heading names (letter case does not matter)."""
    found = [heading.casefold() for heading in headings]
    return [name for name, words, _ in SECTIONS
            if not any(word in heading for heading in found for word in words)]


def _read_claude_md(root: Path) -> tuple[str | None, Finding]:
    path = root / CLAUDE_FILE_NAME
    if not path.is_file():
        return None, Finding(False, f"no {CLAUDE_FILE_NAME}: agents get no description of the "
                                    "project; the planner works from the file list alone")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return None, Finding(False, f"{CLAUDE_FILE_NAME} could not be read "
                                    f"({type(exc).__name__}); agents will not see it either")
    return text, Finding(True, f"{CLAUDE_FILE_NAME} found")


def _section_findings(text: str | None) -> list[Finding]:
    if text is None:
        return []
    headings = section_headings(text)
    results: list[Finding] = []
    for name, words, purpose in SECTIONS:
        if any(word in heading.casefold() for heading in headings for word in words):
            results.append(Finding(True, f"{name}: {purpose}"))
        else:
            results.append(Finding(False, f"no {name} section: {purpose}"))
    return results


def _docs_finding(root: Path, config: ProjectConfig) -> tuple[str | None, Finding]:
    if config.docs_dir is None:
        return None, Finding(False, "no docs folder (docs_dir in .agent-lab.toml): the planner uses the "
                                    "file list, and no feature doc is written")
    folder = root / config.docs_dir
    if folder.is_dir():
        return config.docs_dir, Finding(True, f"docs folder {config.docs_dir}/ found")
    return None, Finding(False, f"docs folder {config.docs_dir}/ (docs_dir in .agent-lab.toml) does not "
                                "exist: it is not used until it does")


def check_project(root: Path, config: ProjectConfig) -> ProjectCheck:
    text, claude = _read_claude_md(root)
    findings = [claude, *_section_findings(text)]
    docs_dir, docs = _docs_finding(root, config)
    findings.append(docs)
    return ProjectCheck(root=root, findings=tuple(findings), docs_dir=docs_dir)


def format_project_check(result: ProjectCheck) -> str:
    lines = [f"Project: {result.root}"]
    for finding in result.findings:
        label = "ok" if finding.found else "--"
        lines.append(f"  {label:<3} {finding.what}")
    return "\n".join(lines)
