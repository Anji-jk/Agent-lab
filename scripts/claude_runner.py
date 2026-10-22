from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_env import AGENT_ENV_ALLOWLIST, build_agent_env  # noqa: F401  (re-exported)


# Where agent run folders go. Normally runs/ in the project; the variable lets the
# test suite send them to a temporary folder instead (tests/conftest.py). The
# orchestrator reads the same variable (runner_adapter), so both always agree.
RUNS_DIR_VARIABLE = "AGENT_LAB_AGENT_RUNS_DIR"


def runs_root() -> Path:
    override = os.environ.get(RUNS_DIR_VARIABLE)
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent / "runs"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# Claude can answer more than once in one run: when a command it started is moved to
# the background (Bash gives up waiting after 120 s), Claude ends its turn, and the
# command finishing wakes the session for another turn with its own "result" event.
# Each result's total_cost_usd and modelUsage are running totals for the session, but
# its num_turns and usage cover only that last part. Seen on Windows, M3.2.
_MODEL_USAGE_FIELDS = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "cache_creation_input_tokens": "cacheCreationInputTokens",
}


def _number_or_none(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _token_totals(results: list[dict[str, Any]]) -> dict[str, int | float | None]:
    last = results[-1] if results else {}
    model_usage = last.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        # Running totals per model: add the models up.
        totals: dict[str, int | float | None] = {}
        for field, key in _MODEL_USAGE_FIELDS.items():
            values = [_number_or_none(m.get(key)) for m in model_usage.values() if isinstance(m, dict)]
            numbers = [v for v in values if v is not None]
            totals[field] = sum(numbers) if numbers else None
        if any(v is not None for v in totals.values()):
            return totals
    # No running totals: each result's usage covers its own part, so add them up.
    totals = {}
    for field in _MODEL_USAGE_FIELDS:
        numbers = [
            n for n in (_number_or_none((r.get("usage") or {}).get(field)) for r in results
                        if isinstance(r.get("usage"), dict))
            if n is not None
        ]
        totals[field] = sum(numbers) if numbers else None
    return totals


def extract_usage(
    result_event: dict[str, Any] | None,
    all_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize usage metadata from Claude's result event(s).

    ``result_event`` is the last result; ``all_results`` every result of the run,
    in order. Turns and tokens are totals for the whole run; cost is the last
    result's, which is already a running total.
    """
    if not isinstance(result_event, dict):
        result_event = {}
    results = [r for r in (all_results or [result_event]) if isinstance(r, dict) and r]
    turns = [n for n in (_number_or_none(r.get("num_turns")) for r in results) if n is not None]

    return {
        "total_cost_usd": _number_or_none(result_event.get("total_cost_usd")),
        **_token_totals(results),
        "num_turns": sum(turns) if turns else None,
        "is_error": (
            result_event.get("is_error")
            if isinstance(result_event.get("is_error"), bool)
            else None
        ),
        "subtype": (
            result_event.get("subtype")
            if isinstance(result_event.get("subtype"), str)
            else None
        ),
    }


# --- live progress -----------------------------------------------------------
# While an agent runs, progress.json next to result.json says what it is doing.
# It is rewritten as events arrive (at most every MIN_WRITE_SECONDS) and at
# least every HEARTBEAT_SECONDS while the runner is alive, so a reader can tell
# "busy in a long tool call" (heartbeat moving, no new events) from "dead"
# (heartbeat stopped). Writing it is best effort and never stops a run.

PROGRESS_FILE_NAME = "progress.json"
EVENT_TIMES_FILE_NAME = "event_times.txt"
HEARTBEAT_SECONDS = 5.0
MIN_WRITE_SECONDS = 0.5
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
ROLE_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def write_json_atomic(path: Path, data: dict[str, Any]) -> bool:
    """Write via a temporary file and swap it in, so a reader never sees half a file.

    On Windows the swap can fail while a reader holds the file open; that write
    is skipped (False) and the next one tries again.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class ProgressTracker:
    """What one agent is doing, rebuilt from Claude's event stream as it arrives."""

    def __init__(
        self,
        path: Path,
        *,
        task_id: str,
        role: str | None,
        clock=time.monotonic,
    ) -> None:
        self.path = path
        self.clock = clock
        now = utc_now()
        self.data: dict[str, Any] = {
            "task_id": task_id,
            "role": role,
            "state": "starting",  # starting -> running -> finished
            "status": None,  # the runner's final status once finished
            "pid": None,
            "session_id": None,
            "started_at": now,
            "heartbeat_at": now,
            "last_event_at": None,
            "last_event_type": None,
            "events": 0,
            "turns": 0,
            "tool_calls": 0,
            "last_tool": None,
            **{field: 0 for field in TOKEN_FIELDS},
            "total_cost_usd": None,
            "finished_at": None,
        }
        self._usage: dict[str, dict[str, int]] = {}  # message id -> its usage
        self._tool_ids: set[str] = set()
        self._last_write: float | None = None

    def started(self, pid: int) -> None:
        self.data["pid"] = pid
        self.data["state"] = "running"

    def observe(self, event: dict[str, Any]) -> None:
        data = self.data
        data["events"] += 1
        data["last_event_at"] = utc_now()
        data["last_event_type"] = str(event.get("type", "unknown"))[:40]
        session_id = event.get("session_id")
        if isinstance(session_id, str):
            data["session_id"] = session_id
        if event.get("type") != "assistant":
            return

        # Each value is read once into a variable and then checked, so a type
        # checker can follow the checks (it cannot narrow a repeated .get()).
        raw_message = event.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        raw_id = message.get("id")
        message_id = raw_id if isinstance(raw_id, str) else f"event-{data['events']}"
        raw_usage = message.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        # One reply arrives as several events with the same message id and
        # the same usage, so usage is kept per message, never added per event.
        self._usage[message_id] = {field: _count(usage.get(field)) for field in TOKEN_FIELDS}

        raw_content = message.get("content")
        content: list[Any] = raw_content if isinstance(raw_content, list) else []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            raw_tool_id = block.get("id")
            tool_id = raw_tool_id if isinstance(raw_tool_id, str) else f"tool-{len(self._tool_ids)}"
            if tool_id in self._tool_ids:
                continue
            self._tool_ids.add(tool_id)
            name = block.get("name")
            if isinstance(name, str):
                data["last_tool"] = name[:80]

        data["turns"] = len(self._usage)
        data["tool_calls"] = len(self._tool_ids)
        for field in TOKEN_FIELDS:
            data[field] = sum(entry[field] for entry in self._usage.values())

    def finish(self, result: dict[str, Any]) -> None:
        data = self.data
        data["state"] = "finished"
        data["status"] = result.get("status")
        data["finished_at"] = utc_now()
        if isinstance(result.get("total_cost_usd"), (int, float)):
            data["total_cost_usd"] = result["total_cost_usd"]
        # Claude's own totals, when it sent them, replace the running count.
        if isinstance(result.get("num_turns"), int):
            data["turns"] = result["num_turns"]
        for field in TOKEN_FIELDS:
            if isinstance(result.get(field), int):
                data[field] = result[field]

    def write(self, *, force: bool = False) -> bool:
        """Write now if forced, or if MIN_WRITE_SECONDS have passed since the last write."""
        now = self.clock()
        if not force and self._last_write is not None and now - self._last_write < MIN_WRITE_SECONDS:
            return False
        return self._write(now)

    def heartbeat(self) -> bool:
        """Write if nothing has been written for HEARTBEAT_SECONDS."""
        now = self.clock()
        if self._last_write is not None and now - self._last_write < HEARTBEAT_SECONDS:
            return False
        return self._write(now)

    def _write(self, now: float) -> bool:
        self._last_write = now
        self.data["heartbeat_at"] = utc_now()
        return write_json_atomic(self.path, self.data)


# Flags that make a run read-only no matter what the user's or project's
# settings allow. Inserted right after "-p" so the variadic
# --disallowedTools value is followed by a flag, never by the prompt.
READ_ONLY_FLAGS = (
    "--restricted",  # ignore user/project settings and their allow rules
    "--tools",
    "Read,Grep,Glob",  # the only built-in tools that exist in this run
    "--disallowedTools",
    "mcp__*",  # --tools does not cover MCP tools
    "--permission-prompts",
    "none",  # deny anything that would need approval
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def validate_tool_patterns(patterns: Sequence[str], label: str) -> list[str]:
    """Tool names and permission rules, e.g. ``Edit`` or ``Bash(git diff:*)``.

    A pattern starting with ``-`` would be read by Claude Code as another
    flag, so it is refused here rather than passed on.
    """
    checked: list[str] = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"{label} entries must be non-empty text.")
        if pattern.startswith("-"):
            raise ValueError(f"{label} entries must not start with '-': {pattern!r}")
        checked.append(pattern)
    return checked


def role_flags(
    isolated: bool,
    tools: str | None,
    allowed_tools: Sequence[str],
    disallowed_tools: Sequence[str],
) -> list[str]:
    """Permission flags for a working agent (builder, tester).

    ``--tools`` decides which built-in tools exist; ``--allowedTools``
    pre-approves some uses of them; ``--disallowedTools`` removes others.
    ``--restricted`` makes your own settings irrelevant and
    ``--permission-prompts none`` denies whatever is not pre-approved, since
    nobody is there to answer a prompt. The two variadic flags are always
    followed by another flag, never by the prompt.
    """
    flags: list[str] = []
    if isolated:
        flags.append("--restricted")
    if tools:
        flags += ["--tools", tools]
    if allowed_tools:
        flags += ["--allowedTools", *allowed_tools]
    if disallowed_tools:
        flags += ["--disallowedTools", *disallowed_tools]
    if isolated:
        flags += ["--permission-prompts", "none"]
    return flags


def build_claude_args(
    command: str,
    prompt: str,
    *,
    max_turns: int,
    model: str | None = None,
    read_only: bool = False,
    effort: str | None = None,
    max_budget_usd: float | None = None,
    isolated: bool = False,
    tools: str | None = None,
    allowed_tools: Sequence[str] = (),
    disallowed_tools: Sequence[str] = (),
) -> list[str]:
    args = [
        command, "-p", "--output-format", "stream-json", "--verbose",
        "--max-turns", str(max_turns), "--permission-mode", "default", prompt,
    ]
    # Each takes exactly one value, so they are safe in front of the prompt.
    limits: list[str] = []
    if effort:
        limits += ["--effort", effort]
    if max_budget_usd is not None:
        limits += ["--max-budget-usd", f"{max_budget_usd:g}"]
    args[-1:-1] = limits
    if read_only:
        args[2:2] = READ_ONLY_FLAGS
    else:
        args[2:2] = role_flags(isolated, tools, allowed_tools, disallowed_tools)
    if model:
        args[1:1] = ["--model", model]
    return args

def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop the process and its descendants on Windows."""
    if process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        process.kill()

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_task(
    *,
    task_id: str,
    prompt: str,
    cwd: Path,
    timeout_seconds: int,
    model: str | None = None,
    max_turns: int = 5,
    read_only: bool = False,
    effort: str | None = None,
    max_budget_usd: float | None = None,
    isolated: bool = False,
    tools: str | None = None,
    allowed_tools: Sequence[str] = (),
    disallowed_tools: Sequence[str] = (),
    role: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", task_id):
        raise ValueError("Invalid task ID. Use letters, digits, '.', '_' or '-'.")

    if timeout_seconds < 1:
        raise ValueError("Timeout must be at least 1 second.")

    if max_turns < 1:
        raise ValueError("max_turns must be at least 1.")

    if effort is not None and effort not in EFFORT_LEVELS:
        raise ValueError(f"effort must be one of: {', '.join(EFFORT_LEVELS)}.")

    if max_budget_usd is not None and not max_budget_usd > 0:
        raise ValueError("max_budget_usd must be greater than 0.")

    allowed_tools = validate_tool_patterns(allowed_tools, "allowed_tools")
    disallowed_tools = validate_tool_patterns(disallowed_tools, "disallowed_tools")
    if tools is not None:
        validate_tool_patterns(tools.split(","), "tools")
    if read_only and (isolated or tools or allowed_tools or disallowed_tools):
        raise ValueError("read_only cannot be combined with custom tool settings.")
    if role is not None and not ROLE_PATTERN.fullmatch(role):
        raise ValueError("role must be a short lowercase name, such as 'builder'.")

    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise NotADirectoryError(f"Working directory does not exist: {cwd}")

    run_dir = runs_root() / task_id
    run_dir.mkdir(parents=True, exist_ok=False)

    events_path = run_dir / "events.jsonl"
    # One UTC time per line of events.jsonl, in the same order: Claude's events carry
    # no timestamps, and the raw stream is kept exactly as Claude sent it.
    times_path = run_dir / EVENT_TIMES_FILE_NAME
    stderr_path = run_dir / "stderr.log"
    result_path = run_dir / "result.json"
    progress = ProgressTracker(run_dir / PROGRESS_FILE_NAME, task_id=task_id, role=role)
    progress.write(force=True)

    command = shutil.which("claude")
    started_at = utc_now()
    started_clock = time.monotonic()

    result: dict[str, Any] = {
        "task_id": task_id,
        "status": "starting",
        "cwd": str(cwd),
        "started_at": started_at,
        "finished_at": None,
        "duration_seconds": None,
        "pid": None,
        "exit_code": None,
        "timed_out": False,
        "timeout_seconds": timeout_seconds,
        "model": model,
        "max_turns": max_turns,
        "read_only": read_only,
        "effort": effort,
        "max_budget_usd": max_budget_usd,
        "isolated": isolated,
        "tools": tools,
        "allowed_tools": list(allowed_tools),
        "disallowed_tools": list(disallowed_tools),
        "role": role,
        "events_file": str(events_path),
        "event_times_file": str(times_path),
        "stderr_file": str(stderr_path),
        "progress_file": str(progress.path),
        "event_count": 0,
        "event_types": {},
        "session_id": None,
        "result_text": None,
        "claude_result_event": None,
        "error": None,
    }

    result.update(extract_usage(None))
    result_events: list[dict[str, Any]] = []

    if command is None:
        result.update(
            status="launch_error",
            error="Claude Code CLI was not found on PATH.",
            finished_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_clock, 3),
        )
        progress.finish(result)
        progress.write(force=True)
        write_json(result_path, result)
        return result

    args = build_claude_args(
        command,
        prompt,
        max_turns=max_turns,
        model=model,
        read_only=read_only,
        effort=effort,
        max_budget_usd=max_budget_usd,
        isolated=isolated,
        tools=tools,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
    )

    # Keep the prompt out of the recorded command for privacy.
    result["command"] = [
        part if part != prompt else "<prompt omitted>" for part in args
    ]

    lines: queue.Queue[str | None] = queue.Queue()
    process: subprocess.Popen[str] | None = None
    event_types: Counter[str] = Counter()
    parse_errors = 0
    timed_out = False
    reader_error: str | None = None

    try:
        with stderr_path.open("w", encoding="utf-8", errors="replace") as stderr_file:
            process = subprocess.Popen(
                args,
                cwd=str(cwd),
                env=build_agent_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )

            result["pid"] = process.pid
            result["status"] = "running"
            progress.started(process.pid)
            progress.write(force=True)

            assert process.stdout is not None
            stdout = process.stdout

            def read_stdout() -> None:
                nonlocal reader_error
                try:
                    for line in stdout:
                        lines.put(line)
                except Exception as exc:
                    reader_error = f"{type(exc).__name__}: {exc}"
                finally:
                    lines.put(None)

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()

            stdout_finished = False
            with events_path.open(
                "w", encoding="utf-8", errors="replace"
            ) as events_file, times_path.open("w", encoding="utf-8") as times_file:
                while not stdout_finished:
                    elapsed = time.monotonic() - started_clock
                    remaining = timeout_seconds - elapsed

                    if remaining <= 0 and process.poll() is None:
                        timed_out = True
                        terminate_process_tree(process)

                    try:
                        line = lines.get(timeout=0.2)
                    except queue.Empty:
                        progress.heartbeat()
                        if process.poll() is not None and not reader.is_alive():
                            break
                        continue

                    if line is None:
                        stdout_finished = True
                        continue

                    # Preserve the original stream for later debugging.
                    events_file.write(line)
                    events_file.flush()
                    times_file.write(utc_now() + "\n")
                    times_file.flush()

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue

                    if not isinstance(event, dict):
                        parse_errors += 1
                        continue

                    result["event_count"] += 1
                    event_type = str(event.get("type", "unknown"))
                    event_types[event_type] += 1

                    if event.get("session_id"):
                        result["session_id"] = event["session_id"]

                    progress.observe(event)
                    progress.write()

                    if event_type == "result":
                        result_events.append(event)
                        result["claude_result_event"] = event
                        result["result_text"] = event.get("result")

            reader.join(timeout=2)
            result["exit_code"] = process.wait()

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if process is not None and process.poll() is None:
            terminate_process_tree(process)
        if process is not None:
            result["exit_code"] = process.poll()

    result["timed_out"] = timed_out
    result["event_types"] = dict(event_types)
    result["malformed_json_lines"] = parse_errors

    if reader_error:
        result["reader_error"] = reader_error

    result.update(extract_usage(result.get("claude_result_event"), result_events))
    result["result_count"] = len(result_events)  # more than 1: the session woke up again


    if timed_out:
        result["status"] = "timed_out"
    elif result["error"]:
        result["status"] = "failed"
    elif result.get("is_error") is True:
        result["status"] = "failed"
    elif result["exit_code"] == 0:
        result["status"] = "succeeded"
    else:
        result["status"] = "failed"

    result["finished_at"] = utc_now()
    result["duration_seconds"] = round(time.monotonic() - started_clock, 3)

    progress.finish(result)
    progress.write(force=True)
    write_json(result_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Claude Code and capture its JSONL output."
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file", type=Path)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Restrict Claude to Read/Grep/Glob and ignore settings allow rules.",
    )
    parser.add_argument("--effort", choices=EFFORT_LEVELS)
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        help="Stop the run once estimated spend reaches this amount.",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Ignore your settings and deny anything not pre-approved.",
    )
    parser.add_argument("--tools", help="Comma-separated built-in tools that exist.")
    parser.add_argument("--role", help="Who this agent is (builder, tester, ...), for progress.")
    parser.add_argument(
        "--allow", action="append", default=[], metavar="RULE",
        help="Pre-approve a tool or rule such as 'Bash(git diff:*)' (repeatable).",
    )
    parser.add_argument(
        "--deny", action="append", default=[], metavar="RULE",
        help="Remove a tool or rule (repeatable).",
    )
    args = parser.parse_args()

    prompt = (
        args.prompt_file.read_text(encoding="utf-8")
        if args.prompt_file
        else args.prompt
    )
    assert prompt is not None

    try:
        result = run_task(
            task_id=args.task_id,
            prompt=prompt,
            cwd=args.cwd,
            timeout_seconds=args.timeout,
            model=args.model,
            max_turns=args.max_turns,
            read_only=args.read_only,
            effort=args.effort,
            max_budget_usd=args.max_budget_usd,
            isolated=args.isolated,
            tools=args.tools,
            allowed_tools=args.allow,
            disallowed_tools=args.deny,
            role=args.role,
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
