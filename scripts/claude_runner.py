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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def extract_usage(result_event: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize usage metadata from Claude's result event."""
    if not isinstance(result_event, dict):
        result_event = {}

    usage = result_event.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    def number_or_none(value: Any) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value

    return {
        "total_cost_usd": number_or_none(result_event.get("total_cost_usd")),
        "input_tokens": number_or_none(usage.get("input_tokens")),
        "output_tokens": number_or_none(usage.get("output_tokens")),
        "cache_read_input_tokens": number_or_none(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": number_or_none(
            usage.get("cache_creation_input_tokens")
        ),
        "num_turns": number_or_none(result_event.get("num_turns")),
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
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", task_id):
        raise ValueError("Invalid task ID. Use letters, digits, '.', '_' or '-'.")

    if timeout_seconds < 1:
        raise ValueError("Timeout must be at least 1 second.")

    if max_turns < 1:
        raise ValueError("max_turns must be at least 1.")

    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise NotADirectoryError(f"Working directory does not exist: {cwd}")

    project_root = Path(__file__).resolve().parent.parent
    run_dir = project_root / "runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=False)

    events_path = run_dir / "events.jsonl"
    stderr_path = run_dir / "stderr.log"
    result_path = run_dir / "result.json"

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
        "events_file": str(events_path),
        "stderr_file": str(stderr_path),
        "event_count": 0,
        "event_types": {},
        "session_id": None,
        "result_text": None,
        "claude_result_event": None,
        "error": None,
    }

    result.update(extract_usage(None))

    if command is None:
        result.update(
            status="launch_error",
            error="Claude Code CLI was not found on PATH.",
            finished_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_clock, 3),
        )
        write_json(result_path, result)
        return result

    args = [
        command,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
        "--permission-mode",
        "default",
        prompt,
    ]
    if model:
        args[1:1] = ["--model", model]

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
            ) as events_file:
                while not stdout_finished:
                    elapsed = time.monotonic() - started_clock
                    remaining = timeout_seconds - elapsed

                    if remaining <= 0 and process.poll() is None:
                        timed_out = True
                        terminate_process_tree(process)

                    try:
                        line = lines.get(timeout=0.2)
                    except queue.Empty:
                        if process.poll() is not None and not reader.is_alive():
                            break
                        continue

                    if line is None:
                        stdout_finished = True
                        continue

                    # Preserve the original stream for later debugging.
                    events_file.write(line)
                    events_file.flush()

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

                    if event_type == "result":
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

    result.update(extract_usage(result.get("claude_result_event")))


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
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
