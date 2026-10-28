"""Prompts as Markdown templates.

Every agent's prompt is a file in this folder. A template uses named fields (``$task_block``,
``$criteria``) and may include a shared part on a line of its own: ``{{include shared/name}}``.
Rendering fails if a field is missing or not in the template, so a renamed field cannot send an
agent a half-empty prompt. Which parts a prompt gets (the feedback of a retry, the note of a
continue) is decided in code; only the text lives here.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent
_INCLUDE = re.compile(r"^\{\{include ([a-z_/]+)\}\}\n", re.MULTILINE)
MAX_INCLUDE_DEPTH = 3


class PromptError(ValueError):
    """A template is missing, or the fields given do not match the ones it names."""


def _read(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        raise PromptError(f"No prompt template named {name!r} ({path}).") from None


def template_text(name: str, depth: int = 0) -> str:
    """The template with its includes filled in."""
    if depth > MAX_INCLUDE_DEPTH:
        raise PromptError(f"Prompt template {name!r} includes too deeply.")
    return _INCLUDE.sub(lambda m: template_text(m.group(1), depth + 1), _read(name))


def fields_of(name: str) -> set[str]:
    """The field names a template uses."""
    return {m.group("named") or m.group("braced")
            for m in Template.pattern.finditer(template_text(name))
            if m.group("named") or m.group("braced")}


def render(name: str, **fields: object) -> str:
    wanted = fields_of(name)
    given = set(fields)
    if wanted != given:
        parts = []
        if wanted - given:
            parts.append("missing " + ", ".join(sorted(wanted - given)))
        if given - wanted:
            parts.append("not in the template: " + ", ".join(sorted(given - wanted)))
        raise PromptError(f"Prompt {name!r}: {'; '.join(parts)}.")
    return Template(template_text(name)).substitute({k: str(v) for k, v in fields.items()})
