"""M3.1: the live progress file the runner keeps for every agent."""

import json
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from scripts import claude_runner
from scripts.claude_runner import ProgressTracker, write_json_atomic

ROOT = Path(__file__).resolve().parent.parent


def assistant(message_id, *, tools=(), input_tokens=10, output_tokens=5, session="s-1"):
    content = [{"type": "tool_use", "id": f"{message_id}-{name}", "name": name} for name in tools]
    content = content or [{"type": "text", "text": "thinking"}]
    return {"type": "assistant", "session_id": session, "message": {
        "id": message_id, "content": content,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def tracker(tmp_path, clock=None):
    return ProgressTracker(tmp_path / "progress.json", task_id="t-1", role="builder",
                           clock=clock or Clock())


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- counting ------------------------------------------------------------------


def test_a_reply_split_over_several_events_is_counted_once(tmp_path):
    progress = tracker(tmp_path)
    # Claude streams one reply as one event per content block, each repeating the usage.
    progress.observe(assistant("m1"))
    progress.observe(assistant("m1", tools=["Read"]))
    data = progress.data
    assert data["turns"] == 1
    assert (data["input_tokens"], data["output_tokens"]) == (10, 5)
    assert data["cache_read_input_tokens"] == 100


def test_tokens_add_up_across_replies_and_the_last_tool_is_kept(tmp_path):
    progress = tracker(tmp_path)
    progress.observe({"type": "system", "subtype": "init", "session_id": "s-9"})
    progress.observe(assistant("m1", tools=["Read"], input_tokens=10, output_tokens=5))
    progress.observe({"type": "user"})
    progress.observe(assistant("m2", tools=["Grep", "Edit"], input_tokens=7, output_tokens=3))
    data = progress.data
    assert data["events"] == 4 and data["turns"] == 2
    assert (data["input_tokens"], data["output_tokens"]) == (17, 8)
    assert data["tool_calls"] == 3 and data["last_tool"] == "Edit"
    assert data["session_id"] == "s-1" and data["last_event_type"] == "assistant"


def test_odd_events_do_not_break_counting(tmp_path):
    progress = tracker(tmp_path)
    for event in ({"type": "assistant"}, {"type": "assistant", "message": "x"},
                  {"type": "assistant", "message": {"usage": {"input_tokens": "lots"}}},
                  {"type": "assistant", "message": {"content": [None, {"type": "tool_use"}]}}):
        progress.observe(event)
    data = progress.data
    assert data["events"] == 4 and data["input_tokens"] == 0 and data["last_tool"] is None


def test_finish_uses_claudes_own_totals(tmp_path):
    progress = tracker(tmp_path)
    progress.observe(assistant("m1"))
    progress.finish({"status": "succeeded", "total_cost_usd": 0.0421, "num_turns": 3,
                     "input_tokens": 40, "output_tokens": 30})
    data = progress.data
    assert data["state"] == "finished" and data["status"] == "succeeded"
    assert data["turns"] == 3 and data["input_tokens"] == 40 and data["total_cost_usd"] == 0.0421
    assert data["finished_at"] is not None


# --- writing --------------------------------------------------------------------


def test_writes_are_throttled_and_heartbeats_keep_time(tmp_path):
    clock = Clock()
    progress = tracker(tmp_path, clock)
    assert progress.write() is True                 # first write always lands
    clock.now += 0.2
    assert progress.write() is False                # too soon after the last one
    assert progress.write(force=True) is True       # forced writes always land
    clock.now += 4.0
    assert progress.heartbeat() is False            # written 4 s ago: no heartbeat yet
    clock.now += 1.5
    assert progress.heartbeat() is True             # quiet for 5.5 s: heartbeat
    assert read(tmp_path / "progress.json")["role"] == "builder"


def test_a_locked_file_skips_the_write_instead_of_crashing(tmp_path, monkeypatch):
    def locked(*args):
        raise PermissionError("in use by another process")

    monkeypatch.setattr(claude_runner.os, "replace", locked)
    assert write_json_atomic(tmp_path / "progress.json", {"a": 1}) is False
    assert list(tmp_path.iterdir()) == []           # no temporary file left behind


def test_an_atomic_write_replaces_the_whole_file(tmp_path):
    path = tmp_path / "progress.json"
    assert write_json_atomic(path, {"a": 1})
    assert write_json_atomic(path, {"b": 2})
    assert read(path) == {"b": 2}


def test_bad_role_is_refused_before_anything_is_created(tmp_path):
    task_id = f"role-{uuid.uuid4().hex[:8]}"
    with pytest.raises(ValueError, match="role"):
        claude_runner.run_task(task_id=task_id, prompt="x", cwd=tmp_path, timeout_seconds=5,
                               role="Not A Role!")
    assert not (ROOT / "runs" / task_id).exists()


# --- a whole run with a stand-in for Claude ----------------------------------------------

FAKE_CLAUDE = r"""
import json, sys, time
def say(event):
    print(json.dumps(event), flush=True)
say({"type": "system", "subtype": "init", "session_id": "sess-42"})
say({"type": "assistant", "session_id": "sess-42", "message": {"id": "m1",
     "content": [{"type": "tool_use", "id": "t1", "name": "Read"}],
     "usage": {"input_tokens": 12, "output_tokens": 4}}})
time.sleep(float(sys.argv[1]))
say({"type": "result", "subtype": "success", "session_id": "sess-42", "result": "done",
     "is_error": False, "num_turns": 2, "total_cost_usd": 0.0123,
     "usage": {"input_tokens": 30, "output_tokens": 9}})
"""


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    """Replace only Claude's process; anything else (taskkill) runs for real."""
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    pause = {"seconds": 0.0}
    monkeypatch.setattr(claude_runner.shutil, "which", lambda _: "claude-stand-in")
    original = claude_runner.subprocess.Popen

    def popen(command, *args, **kwargs):
        if isinstance(command, list) and command and command[0] == "claude-stand-in":
            command = [sys.executable, "-u", str(script), str(pause["seconds"])]
        return original(command, *args, **kwargs)

    monkeypatch.setattr(claude_runner.subprocess, "Popen", popen)
    return pause


def test_a_finished_run_leaves_a_complete_progress_file(tmp_path, fake_claude):
    task_id = f"progress-{uuid.uuid4().hex[:8]}"
    result = claude_runner.run_task(task_id=task_id, prompt="x", cwd=tmp_path,
                                    timeout_seconds=60, role="tester")
    assert result["status"] == "succeeded"
    assert result["role"] == "tester"
    data = read(result["progress_file"])
    assert data["state"] == "finished" and data["status"] == "succeeded"
    assert data["role"] == "tester" and data["session_id"] == "sess-42"
    assert data["pid"] == result["pid"] and data["last_tool"] == "Read"
    assert (data["turns"], data["input_tokens"], data["total_cost_usd"]) == (2, 30, 0.0123)


def test_progress_can_be_read_while_the_agent_is_still_working(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setattr(claude_runner, "HEARTBEAT_SECONDS", 0.3)
    fake_claude["seconds"] = 2.5
    task_id = f"progress-{uuid.uuid4().hex[:8]}"
    path = ROOT / "runs" / task_id / "progress.json"
    done = {}
    runner = threading.Thread(target=lambda: done.update(result=claude_runner.run_task(
        task_id=task_id, prompt="x", cwd=tmp_path, timeout_seconds=60, role="builder")))
    runner.start()

    seen = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists():
            try:
                data = read(path)
            except (OSError, json.JSONDecodeError):
                data = None
            if data and data["state"] == "running" and data["last_tool"] == "Read":
                seen = data
                break
        time.sleep(0.05)
    assert seen is not None, "progress never showed the running agent"
    assert seen["turns"] == 1 and seen["input_tokens"] == 12 and seen["status"] is None

    first_beat = seen["heartbeat_at"]
    time.sleep(1.0)  # no events now: only the heartbeat moves
    assert read(path)["heartbeat_at"] > first_beat

    runner.join(timeout=30)
    assert done["result"]["status"] == "succeeded"
    assert read(path)["state"] == "finished"


def test_cli_passes_the_role_through(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(claude_runner, "run_task", lambda **kw: seen.update(kw) or {"status": "succeeded"})
    monkeypatch.setattr(sys, "argv", ["claude_runner.py", "--task-id", "x", "--cwd", str(tmp_path),
                                      "--prompt", "hi", "--role", "reviewer"])
    claude_runner.main()
    assert seen["role"] == "reviewer"