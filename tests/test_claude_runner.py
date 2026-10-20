
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import claude_runner


def test_rejects_invalid_task_id(tmp_path):
    with pytest.raises(ValueError, match="Invalid task ID"):
        claude_runner.run_task(
            task_id="../bad",
            prompt="hello",
            cwd=tmp_path,
            timeout_seconds=10,
        )


def test_rejects_missing_working_directory(tmp_path):
    with pytest.raises(NotADirectoryError):
        claude_runner.run_task(
            task_id="missing-cwd",
            prompt="hello",
            cwd=tmp_path / "missing",
            timeout_seconds=10,
        )


def test_rejects_duplicate_run_directory(tmp_path):
    run_dir = ROOT / "runs" / "duplicate-test"
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        with pytest.raises(FileExistsError):
            claude_runner.run_task(
                task_id="duplicate-test",
                prompt="hello",
                cwd=tmp_path,
                timeout_seconds=10,
            )
    finally:
        try:
            run_dir.rmdir()
        except OSError:
            pass


def test_cli_missing_saves_structured_result(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_runner.shutil, "which", lambda _: None)

    task_id = f"cli-missing-{uuid.uuid4().hex[:10]}"
    result = claude_runner.run_task(
        task_id=task_id,
        prompt="hello",
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert result["status"] == "launch_error"
    assert result["exit_code"] is None

    saved = json.loads(
        (ROOT / "runs" / task_id / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["status"] == "launch_error"


def test_launch_exception_saves_failure_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_runner.shutil, "which", lambda _: "claude"
    )

    def fail_to_start(*args, **kwargs):
        raise OSError("simulated launch failure")

    monkeypatch.setattr(
        claude_runner.subprocess, "Popen", fail_to_start
    )

    task_id = f"launch-exception-{uuid.uuid4().hex[:10]}"
    result = claude_runner.run_task(
        task_id=task_id,
        prompt="hello",
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert result["status"] == "failed"
    assert "simulated launch failure" in result["error"]
    assert (ROOT / "runs" / task_id / "result.json").exists()


import os
import time


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree test")
def test_timeout_terminates_process_and_saves_result(tmp_path, monkeypatch):
    import uuid

    task_id = f"timeout-{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(
        claude_runner.shutil, "which", lambda _: sys.executable
    )

    original_popen = claude_runner.subprocess.Popen

    def start_sleeping_process(*args, **kwargs):
        command = args[0]

        # Replace only the runner's target command.
        # Let taskkill and other subprocesses use the real Popen.
        if (
            isinstance(command, (list, tuple))
            and command
            and str(command[0]).lower() == str(sys.executable).lower()
        ):
            return original_popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('started', flush=True); time.sleep(30)",
                ],
                **kwargs,
            )

        return original_popen(*args, **kwargs)

    monkeypatch.setattr(
        claude_runner.subprocess, "Popen", start_sleeping_process
    )

    started = time.monotonic()
    result = claude_runner.run_task(
        task_id=task_id,
        prompt="unused",
        cwd=tmp_path,
        timeout_seconds=2,
    )
    elapsed = time.monotonic() - started

    assert result["status"] == "timed_out"
    assert result["timed_out"] is True
    assert result["pid"] is not None
    assert elapsed < 15

    saved = json.loads(
        (ROOT / "runs" / task_id / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["status"] == "timed_out"


from scripts.claude_runner import extract_usage


def test_extracts_usage_from_result_event():
    event = {
        "total_cost_usd": 0.054994,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 20,
            "cache_read_input_tokens": 16140,
            "cache_creation_input_tokens": 13294,
        },
        "num_turns": 1,
        "is_error": False,
        "subtype": "success",
    }

    result = extract_usage(event)

    assert result == {
        "total_cost_usd": 0.054994,
        "input_tokens": 2,
        "output_tokens": 20,
        "cache_read_input_tokens": 16140,
        "cache_creation_input_tokens": 13294,
        "num_turns": 1,
        "is_error": False,
        "subtype": "success",
    }


def test_missing_result_event_returns_unknown_values():
    result = extract_usage(None)

    assert all(value is None for value in result.values())


def test_missing_usage_fields_remain_unknown():
    result = extract_usage({"usage": {"output_tokens": 12}})

    assert result["output_tokens"] == 12
    assert result["input_tokens"] is None
    assert result["total_cost_usd"] is None


def test_rejects_invalid_numeric_metadata():
    result = extract_usage({
        "total_cost_usd": "unknown",
        "usage": {
            "input_tokens": True,
            "output_tokens": 10,
        },
        "is_error": "false",
        "subtype": 123,
    })

    assert result["total_cost_usd"] is None
    assert result["input_tokens"] is None
    assert result["output_tokens"] == 10
    assert result["is_error"] is None
    assert result["subtype"] is None
