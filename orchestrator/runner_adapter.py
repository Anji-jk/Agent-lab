"""Invoke ``scripts/claude_runner.py`` through its command-line interface.

This module never talks to Claude itself and does not parse the JSONL
event stream; the runner does that and records the outcome in
``<runner project root>/runs/<run_id>/result.json``, which is read here.

The runner's stdout (a full copy of its result, including model output) is
discarded rather than captured, and the prompt is only ever passed by file,
so neither ends up in orchestrator logs or records.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNNER_SCRIPT = PROJECT_ROOT / "scripts" / "claude_runner.py"

# Extra seconds beyond the runner's own timeout before we give up on it.
OUTER_TIMEOUT_GRACE = 120

Outcome = Literal["success", "failure", "timeout", "launch_error"]

# Result fields worth keeping in orchestration records. Deliberately
# excludes result_text, claude_result_event and the recorded command.
RETAINED_FIELDS = (
    "status",
    "started_at",
    "finished_at",
    "duration_seconds",
    "exit_code",
    "timed_out",
    "error",
    "model",
    "session_id",
    "num_turns",
    "total_cost_usd",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "is_error",
    "subtype",
    "events_file",
    "stderr_file",
)

ProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class RunnerOutcome:
    outcome: Outcome
    process_exit_code: int | None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)


def runner_run_directory(
    run_id: str,
    runner_script: Path = DEFAULT_RUNNER_SCRIPT,
) -> Path:
    """Where the runner will write ``run_id``'s artifacts."""
    return Path(runner_script).resolve().parent.parent / "runs" / run_id


def _launch_error(message: str, exit_code: int | None = None) -> RunnerOutcome:
    return RunnerOutcome("launch_error", exit_code, error=message)


def read_runner_result(
    result_path: Path,
    keep_result_text: bool = False,
) -> dict[str, Any]:
    """Load ``result.json`` and keep only the retained fields.

    ``result_text`` (the model's final answer) is dropped unless the caller
    needs it, as the planner does; run records never keep it.
    """
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("Runner result file is missing.") from None
    except (OSError, json.JSONDecodeError):
        raise ValueError("Runner result file is unreadable or malformed.") from None
    if not isinstance(data, dict):
        raise ValueError("Runner result file is not a JSON object.")
    if not isinstance(data.get("status"), str):
        raise ValueError("Runner result file has no status.")
    kept = {key: data[key] for key in RETAINED_FIELDS if key in data}
    if keep_result_text and isinstance(data.get("result_text"), str):
        kept["result_text"] = data["result_text"]
    return kept


def invoke_runner(
    *,
    run_id: str,
    worktree_path: Path,
    prompt_file: Path,
    timeout_seconds: int,
    model: str | None = None,
    max_turns: int | None = None,
    keep_result_text: bool = False,
    runner_script: Path = DEFAULT_RUNNER_SCRIPT,
    process_runner: ProcessRunner = subprocess.run,
) -> RunnerOutcome:
    """Run the existing runner for one orchestration run.

    ``run_id`` is passed as the runner's ``--task-id``, so the runner's
    artifacts land in a directory unique to this run.
    """
    command = [
        sys.executable,
        str(runner_script),
        "--task-id",
        run_id,
        "--cwd",
        str(worktree_path),
        "--prompt-file",
        str(prompt_file),
        "--timeout",
        str(timeout_seconds),
    ]
    if model:
        command += ["--model", model]
    if max_turns is not None:
        command += ["--max-turns", str(max_turns)]

    try:
        completed = process_runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds + OUTER_TIMEOUT_GRACE,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RunnerOutcome(
            "timeout",
            None,
            error="Runner did not exit within the timeout plus grace period.",
        )
    except OSError as exc:
        return _launch_error(f"Could not start the runner: {type(exc).__name__}.")

    exit_code = completed.returncode
    result_path = runner_run_directory(run_id, runner_script) / "result.json"

    try:
        result = read_runner_result(result_path, keep_result_text)
    except ValueError as exc:
        if exit_code == 2:
            # argparse usage error: the runner rejected its arguments and
            # never created a result. stderr holds only the usage message.
            stderr = (completed.stderr or "").strip().splitlines()
            reason = stderr[-1][:300] if stderr else "invalid arguments"
            return _launch_error(f"Runner rejected its arguments: {reason}", exit_code)
        return _launch_error(str(exc), exit_code)

    status = result["status"]
    error = result.get("error") if isinstance(result.get("error"), str) else None

    if status == "timed_out":
        return RunnerOutcome("timeout", exit_code, error, result)
    if status == "launch_error":
        return RunnerOutcome("launch_error", exit_code, error, result)
    if status == "succeeded" and exit_code == 0:
        return RunnerOutcome("success", exit_code, error, result)
    if status == "succeeded":
        return RunnerOutcome(
            "failure",
            exit_code,
            "Runner reported success but exited non-zero.",
            result,
        )
    if status == "failed":
        return RunnerOutcome("failure", exit_code, error, result)
    return RunnerOutcome(
        "failure", exit_code, f"Unexpected runner status: {status[:40]!r}.", result
    )
