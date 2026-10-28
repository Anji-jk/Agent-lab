"""An agent stopped by Claude's usage limit says so, instead of "no detail reported"."""

import json
import sys

import pytest

import scripts.claude_runner as claude_runner
from scripts.claude_runner import error_reason, usage_limit

RESETS = 1793012400  # 2026-10-26T11:00:00Z (4:30pm in Asia/Kolkata)


def limit_event(status="rejected", kind="seven_day", resets=RESETS):
    return {"type": "rate_limit_event",
            "rate_limit_info": {"status": status, "rateLimitType": kind, "resetsAt": resets}}


# --- reading the event ------------------------------------------------------------------------


def test_a_refused_request_is_a_usage_limit():
    assert usage_limit(limit_event()) == {"type": "seven_day", "status": "rejected",
                                          "resets_at": "2026-10-26T11:00:00+00:00"}


@pytest.mark.parametrize("status", ["allowed", "allowed_warning", None])
def test_an_allowed_request_is_not(status):
    assert usage_limit(limit_event(status=status)) is None


def test_odd_events_are_not_limits():
    assert usage_limit({"type": "rate_limit_event"}) is None
    assert usage_limit({"type": "rate_limit_event", "rate_limit_info": "x"}) is None
    assert usage_limit(limit_event(resets="soon"))["resets_at"] is None


# --- the reason given -------------------------------------------------------------------------


def test_a_limit_names_which_limit_and_when_it_resets():
    reason = error_reason({"rate_limit": usage_limit(limit_event()),
                           "result_text": "You've hit your weekly limit · resets 4:30pm (Asia/Kolkata)"})
    assert reason == ("Claude's weekly usage limit was reached; it resets at "
                      "2026-10-26 11:00 UTC. Claude said: \"You've hit your weekly "
                      "limit · resets 4:30pm (Asia/Kolkata)\"")


def test_the_five_hour_limit_is_named_too():
    reason = error_reason({"rate_limit": usage_limit(limit_event(kind="five_hour"))})
    assert reason.startswith("Claude's 5-hour usage limit was reached")


def test_any_other_error_gives_claudes_own_words():
    assert error_reason({"result_text": "API Error: 529 overloaded"}) == \
        "Claude reported an error: API Error: 529 overloaded"
    assert error_reason({"subtype": "error_max_turns"}) == "Claude reported an error (error_max_turns)."
    assert error_reason({}) == "Claude reported an error without saying what it was."


def test_long_error_text_is_cut():
    assert len(error_reason({"result_text": "x" * 1000})) < 340


# --- a whole run with a stand-in for Claude --------------------------------------------------

LIMITED_CLAUDE = r"""
import json
def say(event):
    print(json.dumps(event), flush=True)
say({"type": "system", "subtype": "init", "session_id": "sess-7"})
say({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
     "rateLimitType": "seven_day", "resetsAt": %d}})
say({"type": "result", "subtype": "success", "session_id": "sess-7", "is_error": True,
     "result": "You've hit your weekly limit · resets 4:30pm (Asia/Kolkata)",
     "num_turns": 1, "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0}})
""" % RESETS


@pytest.fixture
def limited_claude(monkeypatch, tmp_path):
    script = tmp_path / "limited_claude.py"
    script.write_text(LIMITED_CLAUDE, encoding="utf-8")
    monkeypatch.setenv(claude_runner.RUNS_DIR_VARIABLE, str(tmp_path / "agent-runs"))
    monkeypatch.setattr(claude_runner.shutil, "which", lambda _: "claude-stand-in")
    original = claude_runner.subprocess.Popen

    def popen(command, *args, **kwargs):
        if isinstance(command, list) and command and command[0] == "claude-stand-in":
            command = [sys.executable, "-u", str(script)]
        return original(command, *args, **kwargs)

    monkeypatch.setattr(claude_runner.subprocess, "Popen", popen)


def test_a_run_refused_by_the_weekly_limit_fails_with_the_reason(tmp_path, limited_claude):
    result = claude_runner.run_task(task_id="limited-1", prompt="x", cwd=tmp_path,
                                    timeout_seconds=60, role="reviewer")
    assert result["status"] == "failed"
    assert result["rate_limit"]["type"] == "seven_day"
    assert result["error"].startswith("Claude's weekly usage limit was reached; it resets at "
                                      "2026-10-26 11:00 UTC.")
    saved = json.loads((tmp_path / "agent-runs" / "limited-1" / "result.json").read_text("utf-8"))
    assert saved["rate_limit"] == result["rate_limit"] and saved["error"] == result["error"]
