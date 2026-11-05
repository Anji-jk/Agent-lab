"""Jira's description format (Atlassian Document Format, ADF) as plain text.

Jira Cloud sends every description and comment as an ADF tree: nodes with a ``type`` and
``content``. Agents and the planner read plain text, so this turns the tree into lines close to
Markdown: headings get ``#``, list items ``-`` or ``1.``, code blocks a fence, table rows ``|``.
Formatting such as bold or colour is dropped; links keep their address. A node type this does
not know still gives its text, so nothing written in Jira is lost.
"""

from __future__ import annotations

from typing import Any

_INDENT = "  "


def adf_to_text(document: Any) -> str:
    """The text of an ADF document; "" for None. A plain string (older Jira) is returned as is."""
    if document is None:
        return ""
    if isinstance(document, str):
        return document.strip()
    if not isinstance(document, dict):
        return ""
    lines = _blocks(document.get("content"), depth=0)
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text as an ADF document: one paragraph per line, for a comment Jira will show."""
    paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": line}]}
                  for line in text.splitlines() if line.strip()]
    return {"type": "doc", "version": 1, "content": paragraphs or [{"type": "paragraph"}]}


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [child for child in node.get("content") or [] if isinstance(child, dict)]


def _inline(nodes: Any) -> str:
    """Text, links, mentions and the like inside one paragraph or heading."""
    parts: list[str] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        attrs = node.get("attrs") or {}
        if kind == "text":
            text = str(node.get("text") or "")
            link = next((m.get("attrs", {}).get("href") for m in node.get("marks") or []
                         if isinstance(m, dict) and m.get("type") == "link"), None)
            parts.append(f"{text} ({link})" if link and link != text else text)
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind == "mention":
            parts.append(str(attrs.get("text") or "@someone"))
        elif kind == "emoji":
            parts.append(str(attrs.get("text") or attrs.get("shortName") or ""))
        elif kind in ("inlineCard", "blockCard", "embedCard"):
            parts.append(str(attrs.get("url") or ""))
        elif kind == "date":
            parts.append(str(attrs.get("timestamp") or ""))
        elif kind == "status":
            parts.append(f"[{attrs.get('text') or ''}]")
        else:
            parts.append(_inline(node.get("content")))
    return "".join(parts)


def _blocks(nodes: Any, depth: int) -> list[str]:
    lines: list[str] = []
    for node in nodes or []:
        if isinstance(node, dict):
            lines += _block(node, depth)
    return lines


def _list(node: dict[str, Any], depth: int, ordered: bool) -> list[str]:
    lines: list[str] = []
    start = int((node.get("attrs") or {}).get("order") or 1)
    for number, item in enumerate(_children(node), start=start):
        marker = f"{number}. " if ordered else "- "
        inner = _blocks(item.get("content"), depth + 1)
        first = True
        for line in inner:
            if first and line.strip():
                lines.append(_INDENT * depth + marker + line.strip())
                first = False
            elif line.strip():
                lines.append(line if line.startswith(_INDENT * (depth + 1)) else
                             _INDENT * (depth + 1) + line.strip())
        if first:
            lines.append(_INDENT * depth + marker.rstrip())
    return lines


def _block(node: dict[str, Any], depth: int) -> list[str]:
    kind = node.get("type")
    if kind == "paragraph":
        text = _inline(node.get("content"))
        return [*text.split("\n"), ""] if depth == 0 else text.split("\n")
    if kind == "heading":
        level = int((node.get("attrs") or {}).get("level") or 2)
        return ["#" * max(1, min(level, 6)) + " " + _inline(node.get("content")).strip(), ""]
    if kind == "bulletList":
        return [*_list(node, depth, ordered=False), *([""] if depth == 0 else [])]
    if kind == "orderedList":
        return [*_list(node, depth, ordered=True), *([""] if depth == 0 else [])]
    if kind == "codeBlock":
        text = "".join(str(c.get("text") or "") for c in _children(node))
        return ["```", *text.split("\n"), "```", ""]
    if kind == "blockquote":
        return [("> " + line) if line else ">" for line in _blocks(node.get("content"), 0)]
    if kind == "rule":
        return ["---", ""]
    if kind == "table":
        rows = []
        for row in _children(node):
            cells = [" ".join(line.strip() for line in _blocks(cell.get("content"), 1)
                              if line.strip())
                     for cell in _children(row)]
            rows.append("| " + " | ".join(cells) + " |")
        return [*rows, ""]
    if kind in ("mediaSingle", "mediaGroup", "media"):
        return []
    if kind in ("panel", "expand", "nestedExpand", "layoutSection", "layoutColumn",
                "decisionList", "taskList", "listItem", "taskItem", "decisionItem"):
        title = (node.get("attrs") or {}).get("title")
        inner = _blocks(node.get("content"), depth)
        if kind in ("taskItem", "decisionItem"):
            inner = [_inline(node.get("content"))]
        return ([str(title)] if title else []) + inner
    inner = _blocks(node.get("content"), depth)
    return inner if any(line.strip() for line in inner) else [_inline(node.get("content"))]
