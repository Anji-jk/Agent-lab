"""Turn raw event files into readable items, for ``events`` and the dashboard.

Two sources, one item shape:

* an agent's ``runs/<agent run id>/events.jsonl``: Claude's stream exactly as
  it arrived, with ``event_times.txt`` beside it giving each line's time;
* a task's orchestrator log, ``<task directory>/events.jsonl`` (``task_events``).

An item is one thing that happened: the agent said something, called a tool
(paired with that tool's result), finished; or the orchestrator ran a step
(paired with its outcome, so "full suite" shows "running" until it ends).

Everything here only reads. Text from these files is untrusted -- it holds
whatever the agent read -- so strings that look like secrets are masked and
long output is shortened. Masking is best effort; the sandbox (M6) is the
real protection.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HEAD_LINES = 40
TAIL_LINES = 40
MAX_DETAIL_CHARS = 16_000
MAX_FULL_CHARS = 2_000_000
MAX_TITLE = 160
EVENTS_FILE_NAME = "events.jsonl"
EVENT_TIMES_FILE_NAME = "event_times.txt"

_SECRET_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{16,}=*"),
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(['\"]?)([^\s'\"]{6,})"
)
# System events that repeat what other events already show, or carry nothing readable.
_QUIET_SYSTEM = frozenset({"thinking_tokens", "permission_denied", "task_started", "task_updated",
                           "background_tasks_changed", "status", "hook_started", "hook_response"})
_BACKGROUNDED = re.compile(r"(?i)moved to the background")
_BLOCKED = re.compile(
    r"(?i)permission|not allowed|denied|requires approval|was blocked|haven't granted"
)
MASK = "[masked]"


def mask_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)} {MASK}" if m.lastindex else MASK, text)
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", text)


def shorten(text: str, *, full: bool = False) -> tuple[str, bool]:
    """``text`` cut to its first and last lines unless ``full``; True if anything was cut."""
    if full:
        return (text, False) if len(text) <= MAX_FULL_CHARS else (text[:MAX_FULL_CHARS], True)
    cut = False
    lines = text.split("\n")
    if len(lines) > HEAD_LINES + TAIL_LINES:
        hidden = len(lines) - HEAD_LINES - TAIL_LINES
        lines = [*lines[:HEAD_LINES], f"... {hidden} lines hidden ...", *lines[-TAIL_LINES:]]
        cut = True
    text = "\n".join(lines)
    if len(text) > MAX_DETAIL_CHARS:
        half = MAX_DETAIL_CHARS // 2
        text = f"{text[:half]}\n... {len(text) - MAX_DETAIL_CHARS} characters hidden ...\n{text[-half:]}"
        cut = True
    return text, cut


def _one_line(text: str, limit: int = MAX_TITLE) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return _one_line(lines[-1]) if lines else ""


@dataclass
class Item:
    index: int
    kind: str  # start, say, think, tool, finish, step, error
    title: str
    detail: str = ""
    at: str | None = None
    tool: str | None = None
    status: str | None = None  # for steps: ok, error, warn
    result: dict[str, Any] | None = None  # tool or step outcome; None while waiting
    awaits_result: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        return self.awaits_result and self.result is None

    def to_dict(self, *, full: bool = False) -> dict[str, Any]:
        detail, cut = shorten(mask_secrets(self.detail), full=full)
        data: dict[str, Any] = {
            "index": self.index,
            "kind": self.kind,
            "title": mask_secrets(self.title),
            "detail": detail,
            "truncated": cut,
            "at": self.at,
            "tool": self.tool,
            "status": self.status,
            "pending": self.pending,
            "result": None,
        }
        if self.result is not None:
            result_detail, result_cut = shorten(mask_secrets(self.result.get("detail", "")), full=full)
            data["result"] = {
                "status": self.result.get("status", "ok"),
                "summary": mask_secrets(self.result.get("summary", "")),
                "detail": result_detail,
                "truncated": result_cut,
                "at": self.result.get("at"),
            }
            data["truncated"] = data["truncated"] or result_cut
        return data


def stable_index(items: list[Item]) -> int:
    """Items before this index will not change any more; a reader resumes from it."""
    return next((item.index for item in items if item.pending), len(items))


def feed(items: list[Item], after: int = 0) -> dict[str, Any]:
    after = max(0, min(after, len(items)))
    return {
        "items": [item.to_dict() for item in items[after:]],
        "from": after,
        "next": stable_index(items),
        "count": len(items),
    }


# --- reading files -----------------------------------------------------------------------


def _read_lines(path: Path) -> list[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _json_lines(path: Path) -> list[tuple[int, dict[str, Any]]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    for number, line in enumerate(_read_lines(path)):
        try:
            value = json.loads(line)
        except ValueError:
            continue  # a line still being written, or not JSON at all
        if isinstance(value, dict):
            rows.append((number, value))
    return rows


# --- an agent's Claude stream -------------------------------------------------------------


class _PathTrimmer:
    """Show paths inside the agent's working folder relative to it."""

    def __init__(self) -> None:
        self.root: str | None = None

    def set_root(self, cwd: object) -> None:
        if isinstance(cwd, str) and cwd:
            self.root = cwd.replace("\\", "/").rstrip("/") + "/"

    def in_text(self, text: str) -> str:
        """``text`` with the folder's path, written either way round, taken out."""
        if not self.root:
            return text
        for root in {self.root, self.root.replace("/", "\\")}:
            text = re.sub(re.escape(root), "", text, flags=re.IGNORECASE)
        return text

    def __call__(self, value: object) -> str:
        text = str(value)
        if self.root:
            normal = text.replace("\\", "/")
            if normal.lower().startswith(self.root.lower()):
                return normal[len(self.root):]
        return text


def _tool_title(name: str, data: dict[str, Any], trim: _PathTrimmer) -> str:
    path = data.get("file_path") or data.get("notebook_path") or data.get("path")
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit") and path:
        title = trim(path)
        if name == "Write" and isinstance(data.get("content"), str):
            title += f" ({len(data['content'].splitlines())} lines)"
        if name == "MultiEdit" and isinstance(data.get("edits"), list):
            title += f" ({len(data['edits'])} edits)"
        return title
    if name == "Bash" and data.get("command"):
        return _one_line(str(data["command"]))
    if name == "Grep" and data.get("pattern"):
        where = trim(data.get("path") or data.get("glob") or "")
        return f"{data['pattern']}" + (f"  in {where}" if where else "")
    if name == "Glob" and data.get("pattern"):
        return str(data["pattern"])
    for key in ("description", "url", "query", "prompt"):
        if isinstance(data.get(key), str):
            return _one_line(data[key])
    return _one_line(json.dumps(data, ensure_ascii=False)) if data else ""


def _tool_detail(name: str, data: dict[str, Any]) -> str:
    if name == "Edit":
        return f"--- before\n{data.get('old_string', '')}\n+++ after\n{data.get('new_string', '')}"
    if name == "MultiEdit" and isinstance(data.get("edits"), list):
        parts = []
        for number, edit in enumerate(data["edits"], 1):
            if isinstance(edit, dict):
                parts.append(f"--- edit {number}, before\n{edit.get('old_string', '')}\n"
                             f"+++ edit {number}, after\n{edit.get('new_string', '')}")
        return "\n\n".join(parts)
    if name == "Write":
        return str(data.get("content", ""))
    if name == "Bash":
        text = str(data.get("command", ""))
        if data.get("description"):
            text += f"\n\n# {data['description']}"
        return text
    return json.dumps(data, ensure_ascii=False, indent=2)


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _result_summary(tool: str | None, text: str, status: str) -> str:
    if status == "background":
        return "still running, moved to the background: " + _one_line(text, 100)
    if status != "ok":
        return _one_line(text) or status
    if not text.strip():
        return "(no output)"
    if tool == "Read":
        return f"{len(text.splitlines())} lines"
    if tool in ("Grep", "Glob"):
        lines = [line for line in text.splitlines() if line.strip()]
        return _one_line(lines[0]) if len(lines) == 1 else f"{len(lines)} lines of matches"
    if tool == "Bash":
        return _last_line(text)
    return _one_line(text)


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


def _money(value: object) -> str:
    return f"${value:.4f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else ""


def agent_items(events_path: Path, times_path: Path | None = None) -> list[Item]:
    """The readable story of one agent, from its runner folder's event files."""
    events_path = Path(events_path)
    if times_path is None:
        times_path = events_path.with_name(EVENT_TIMES_FILE_NAME)
    times = _read_lines(times_path)
    items: list[Item] = []
    tools: dict[str, Item] = {}
    seen: set[tuple[str, str]] = set()
    trim = _PathTrimmer()

    def add(kind: str, title: str, **kwargs: Any) -> Item:
        item = Item(index=len(items), kind=kind, title=title, **kwargs)
        items.append(item)
        return item

    started = False
    for number, event in _json_lines(events_path):
        # Claude stamps its assistant and user events; the runner's times file covers the rest.
        stamp = event.get("timestamp")
        at = stamp if isinstance(stamp, str) and stamp else (
            times[number].strip() if number < len(times) and times[number].strip() else None)
        kind = event.get("type")

        if kind == "system":
            subtype = str(event.get("subtype") or "system")
            if subtype == "init":
                trim.set_root(event.get("cwd"))
                tool_names = _list(event.get("tools"))
                model = event.get("model") or "default model"
                detail = "\n".join(filter(None, [
                    f"model: {model}",
                    f"Claude Code {event.get('claude_code_version')}" if event.get("claude_code_version") else "",
                    f"working folder: {event.get('cwd')}" if event.get("cwd") else "",
                    f"permission mode: {event.get('permissionMode')}" if event.get("permissionMode") else "",
                    "tools: " + ", ".join(map(str, tool_names)) if tool_names else "tools: none",
                ]))
                title = (f"session started · {model} · {len(tool_names)} tools" if not started
                         else "session woke up again")
                add("start", title, detail=detail, at=at)
                started = True
            elif subtype == "task_notification":
                # A command Claude had moved to the background has ended; this wakes the session.
                status = str(event.get("status") or "finished")
                add("step", f"background command {status}: {_one_line(str(event.get('summary') or ''))}",
                    detail=json.dumps(event, ensure_ascii=False, indent=2), at=at,
                    status="ok" if status == "completed" else "error")
            elif subtype not in _QUIET_SYSTEM:
                add("step", subtype.replace("_", " "),
                    detail=json.dumps(event, ensure_ascii=False, indent=2), at=at)
            continue

        if kind == "rate_limit_event":
            info = _dict(event.get("rate_limit_info"))
            if info.get("status") not in (None, "allowed"):
                add("error", f"rate limited ({info.get('rateLimitType', 'limit')}: {info.get('status')})",
                    detail=json.dumps(info, ensure_ascii=False, indent=2), at=at, status="error")
            continue

        if kind == "assistant":
            message = _dict(event.get("message"))
            message_id = str(message.get("id") or f"line-{number}")
            content = _list(message.get("content"))
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_id = str(block.get("id") or f"{message_id}-{len(items)}")
                    if ("tool", tool_id) in seen:
                        continue
                    seen.add(("tool", tool_id))
                    name = str(block.get("name") or "tool")
                    data = _dict(block.get("input"))
                    tools[tool_id] = add("tool", _tool_title(name, data, trim),
                                         detail=_tool_detail(name, data), at=at,
                                         tool=name, awaits_result=True)
                elif block_type in ("text", "thinking"):
                    text = str(block.get("text") if block_type == "text" else block.get("thinking") or "")
                    if not text.strip() or (message_id, text) in seen:
                        continue
                    seen.add((message_id, text))
                    title = _one_line(text)
                    add("say" if block_type == "text" else "think", title,
                        detail=text if text.strip() != title else "", at=at)
            continue

        if kind == "user":
            content = _list(_dict(event.get("message")).get("content"))
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = _result_text(block.get("content"))
                status = "ok"
                if block.get("is_error"):
                    status = "blocked" if _BLOCKED.search(text) else "error"
                elif _BACKGROUNDED.search(text[:300]) or _dict(event.get("tool_use_result")).get("backgroundTaskId"):
                    status = "background"
                owner = tools.get(str(block.get("tool_use_id")))
                result = {"status": status, "detail": text, "at": at,
                          "summary": trim.in_text(_result_summary(owner.tool if owner else None, text, status))}
                if owner is not None and owner.result is None:
                    owner.result = result
                else:
                    add("tool", "result of an earlier call", result=result, at=at)
            continue

        if kind == "result":
            failed = bool(event.get("is_error")) or event.get("subtype") not in (None, "success")
            parts = ["error: " + str(event.get("subtype")) if failed else "succeeded"]
            if isinstance(event.get("num_turns"), int):
                parts.append(f"{event['num_turns']} turns")
            if _money(event.get("total_cost_usd")):
                parts.append(_money(event.get("total_cost_usd")) + " total")
            if isinstance(event.get("duration_ms"), (int, float)):
                parts.append(f"{event['duration_ms'] / 1000:.1f}s")
            add("error" if failed else "finish", " · ".join(parts),
                detail=str(event.get("result") or ""), at=at)
            continue
        # Other event types (stream deltas, rate-limit notices) add nothing readable.

    return items


# --- the orchestrator's own log -----------------------------------------------------------

_PAIRS = {"agent_started": "agent_finished", "suite_started": "suite_finished",
          "named_tests_started": "named_tests_finished"}
_ENDINGS = {end: start for start, end in _PAIRS.items()}


def _lines(values: object) -> str:
    return "\n".join(f"- {value}" for value in values) if isinstance(values, list) else ""


def _command_outcome(entry: dict[str, Any]) -> dict[str, Any]:
    seconds = entry.get("seconds")
    took = f" in {seconds:.1f}s" if isinstance(seconds, (int, float)) else ""
    if entry.get("timed_out"):
        status, summary = "error", f"timed out{took}"
    elif entry.get("passed"):
        status, summary = "ok", f"passed{took}"
    else:
        status, summary = "error", f"failed (exit {entry.get('exit_code')}){took}"
    tail = str(entry.get("output_tail") or "")
    last = _last_line(tail)
    return {"status": status, "summary": f"{summary} · {last}" if last else summary,
            "detail": tail, "at": entry.get("at")}


def _step_item(index: int, entry: dict[str, Any]) -> Item:
    step = str(entry.get("step"))
    at = entry.get("at") if isinstance(entry.get("at"), str) else None
    rest = {k: v for k, v in entry.items() if k not in ("at", "step")}

    def item(kind: str, title: str, detail: str = "", status: str | None = None, **kw: Any) -> Item:
        return Item(index=index, kind=kind, title=title, detail=detail, at=at, status=status,
                    extra={"step": step}, **kw)

    if step == "task_started":
        return item("start", f"task started from commit {str(entry.get('base_commit', ''))[:12]}",
                    str(entry.get("title", "")))
    if step == "worktree_created":
        return item("step", f"worktree ready on {entry.get('branch')}", str(entry.get("path", "")), "ok")
    if step == "worktree_failed":
        return item("error", "the worktree could not be created", str(entry.get("error", "")), "error")
    if step == "stage":
        return item("step", f"stage: {entry.get('status')}")
    if step == "agent_started":
        return item("step", f"{entry.get('role')} agent", str(entry.get("settings", "")),
                    awaits_result=True)
    if step == "suite_started":
        command = " ".join(map(str, entry.get("command") or []))
        return item("step", "full test suite", command, awaits_result=True)
    if step == "named_tests_started":
        tests = entry.get("tests") or []
        return item("step", f"the tester's {_count(len(tests), 'named test')}", _lines(tests),
                    awaits_result=True)
    if step == "builder_checked":
        changed = entry.get("changed") or []
        bad = entry.get("scope_violations") or entry.get("nothing_changed") or entry.get("blocked")
        detail = "changed:\n" + (_lines(changed) or "(nothing)")
        if entry.get("scope_violations"):
            detail += "\noutside the plan:\n" + _lines(entry["scope_violations"])
        if entry.get("blocked"):
            detail += f"\nbuilder says it is blocked: {entry['blocked']}"
        return item("step", f"builder's changes checked: {_count(len(changed), 'file')}", detail,
                    "error" if bad else "ok")
    if step == "tester_checked":
        changed, named = entry.get("changed") or [], entry.get("named_tests") or []
        bad = any(entry.get(k) for k in ("outside_tests", "weakened_tests", "invalid_test_ids",
                                         "suspected_defects"))
        sections = [("changed", changed), ("tests named", named),
                    ("written outside tests/", entry.get("outside_tests")),
                    ("existing tests weakened", entry.get("weakened_tests")),
                    ("invalid test ids", entry.get("invalid_test_ids")),
                    ("suspected defects", entry.get("suspected_defects"))]
        detail = "\n".join(f"{label}:\n{_lines(values)}" for label, values in sections if values)
        return item("step", f"tester's changes checked: {_count(len(changed), 'file')}, "
                            f"{_count(len(named), 'test')} named",
                    detail, "error" if bad else "ok")
    if step == "reviewer_checked":
        bad = entry.get("verdict") != "pass" or entry.get("unmet") or entry.get("problems")
        detail = ""
        if entry.get("unmet"):
            detail += "criteria not met: " + ", ".join(map(str, entry["unmet"])) + "\n"
        if entry.get("problems"):
            detail += "problems:\n" + _lines(entry["problems"])
        return item("step", f"reviewer verdict: {entry.get('verdict')}", detail.strip(),
                    "error" if bad else "ok")
    if step == "stage_stopped":
        reasons = entry.get("reasons") or []
        return item("error", "stopped: " + _one_line(str(reasons[0]) if reasons else "a check failed"),
                    _lines(reasons), "error")
    if step == "report_unusable":
        return item("error", f"the {entry.get('role')}'s report was unusable",
                    str(entry.get("error", "")), "error")
    if step == "decided":
        status = entry.get("status")
        return item("step", f"decision: {status}", _lines(entry.get("reasons")),
                    "ok" if status == "done" else "error")
    if step == "committed":
        return item("step", f"committed {str(entry.get('commit', ''))[:10]} on {entry.get('branch')}",
                    str(entry.get("commit", "")), "ok")
    if step in ("commit_incomplete", "pipeline_crashed"):
        return item("error", step.replace("_", " "),
                    str(entry.get("error") or _lines(entry.get("reasons"))), "error")
    if step == "task_finished":
        status = entry.get("status")
        money = _money(entry.get("cost_usd"))
        return item("finish" if status == "done" else "error",
                    f"task finished: {status}" + (f" · {money}" if money else ""),
                    status="ok" if status == "done" else "error")
    return item("step", step.replace("_", " "), json.dumps(rest, ensure_ascii=False, indent=2))


def orchestrator_items(path: Path) -> list[Item]:
    """The readable story of what the orchestrator did for one task."""
    items: list[Item] = []
    open_steps: dict[tuple[str, str], Item] = {}
    for _, entry in _json_lines(Path(path)):
        step = entry.get("step")
        if not isinstance(step, str):
            continue
        if step in _ENDINGS:
            key = (_ENDINGS[step], str(entry.get("role", "")))
            started = open_steps.pop(key, None)
            if started is not None:
                if step == "agent_finished":
                    ok = entry.get("outcome") == "success"
                    parts = [str(entry.get("outcome", "")).replace("_", " ")]
                    if _money(entry.get("cost_usd")):
                        parts.append(_money(entry.get("cost_usd")))
                    if isinstance(entry.get("seconds"), (int, float)):
                        parts.append(f"{entry['seconds']:.1f}s")
                    started.result = {"status": "ok" if ok else "error", "summary": " · ".join(parts),
                                      "detail": str(entry.get("error") or ""), "at": entry.get("at")}
                else:
                    started.result = _command_outcome(entry)
                continue
        item = _step_item(len(items), entry)
        items.append(item)
        if step in _PAIRS:
            open_steps[(step, str(entry.get("role", "")))] = item
    return items
