"""M5.5: an agent that fails or stops always gets a reason, worked out from its events."""

import json

import pytest

from orchestrator.execution.diagnose import failure_reason
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.records.task_registry import load_task
from task_runner_support import RUN_ID, run  # pyright: ignore[reportMissingImports]

SECRET = "ghp_" + "Q" * 36


def files(tmp_path, events=(), stderr=""):
    events_file, stderr_file = tmp_path / "events.jsonl", tmp_path / "stderr.log"
    events_file.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    stderr_file.write_text(stderr, encoding="utf-8")
    return {"events_file": str(events_file), "stderr_file": str(stderr_file)}


def tool(name):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}


def test_a_given_reason_is_used(tmp_path):
    reason = failure_reason("failure", "Claude's weekly usage limit was reached.", files(tmp_path))
    assert reason.startswith("Claude's weekly usage limit was reached.")


def test_a_result_that_ran_out_of_turns_says_so(tmp_path):
    result = files(tmp_path, [tool("Read"), {"type": "result", "subtype": "error_max_turns",
                                             "is_error": True, "result": ""}])
    reason = failure_reason("failure", None, {**result, "is_error": True, "subtype": "error_max_turns"})
    assert reason.startswith("Claude ran out of turns before it finished")
    assert "2 events recorded, last event result/error_max_turns, last tool Read" in reason


def test_an_error_result_gives_claudes_words(tmp_path):
    result = {**files(tmp_path), "is_error": True, "result_text": "Credit balance is too low"}
    assert failure_reason("failure", None, result).startswith(
        'Claude reported an error: "Credit balance is too low"')


def test_a_timeout_says_how_long(tmp_path):
    reason = failure_reason("timeout", None, {**files(tmp_path), "timed_out": True, "duration_seconds": 900.2})
    assert reason.startswith("It ran out of time after 900 s")


def test_api_retries_are_named(tmp_path):
    events = [{"type": "system", "subtype": "api_retry", "error": "overloaded", "error_status": 529,
               "attempt": 3, "max_retries": 10}]
    reason = failure_reason("failure", None, files(tmp_path, events), exit_code=1)
    assert reason.startswith("Claude's API kept failing (overloaded, HTTP 529; retry 3 of 10)")


def test_a_process_with_no_result_gives_its_exit_code_and_stderr(tmp_path):
    result = files(tmp_path, [tool("Bash")], stderr="boom\nTraceback: something broke\n")
    reason = failure_reason("failure", None, result, exit_code=137)
    assert reason.startswith("Claude stopped without a result: the process exited with code 137")
    assert "stderr ended with: boom | Traceback: something broke" in reason
    assert "last tool Bash" in reason


def test_a_killed_process_names_the_signal(tmp_path):
    reason = failure_reason("failure", None, files(tmp_path), exit_code=-9)
    assert "killed by signal 9" in reason


def test_when_nothing_was_recorded_it_says_so_and_where_to_look(tmp_path):
    reason = failure_reason("failure", None, files(tmp_path), exit_code=0)
    assert reason.startswith("The agent stopped without a result and left no error text")
    assert "no events were recorded" in reason and str(tmp_path / "events.jsonl") in reason
    assert "no detail" not in reason


def test_without_any_files_it_still_gives_a_reason():
    assert failure_reason("failure", None, {}) == "The agent stopped without a result and left no error text."


def test_secrets_are_masked_and_long_text_cut(tmp_path):
    result = files(tmp_path, stderr=f"token={SECRET} " + "x" * 2000)
    reason = failure_reason("failure", None, result, exit_code=1)
    assert SECRET not in reason and len(reason) < 900


def test_a_big_events_file_is_read_from_its_end(tmp_path):
    events = [tool("Read")] * 5000 + [{"type": "result", "subtype": "error_max_turns", "is_error": True}]
    reason = failure_reason("failure", None, files(tmp_path, events))
    assert "ran out of turns" in reason


# --- in the task: the record, the agent entry and the reasons all carry it ------------------------


@pytest.mark.parametrize("role", ["builder", "tester", "reviewer"])
def test_a_failed_agent_leaves_a_reason_everywhere(world, agents, tmp_path, role):
    result = files(tmp_path, [tool("Read")], stderr="it broke\n")
    agents.fail[role] = RunnerOutcome("failure", 1, None, {"status": "failed", **result})
    record = run(world, agents)
    reasons = " ".join(record["reasons"])
    assert f"The {role} failure: Claude stopped without a result: the process exited with code 1" in reasons
    assert "no detail" not in reasons
    assert "it broke" in record["agents"][role]["error"]
    assert "it broke" in " ".join(load_task(world["runs"], RUN_ID, "T1")["reasons"])


def test_a_limit_is_found_in_the_events_when_the_result_lacks_it(tmp_path):
    # Like T5's reviewer on run 165041: the old runner kept no rate_limit, only the events.
    events = [{"type": "rate_limit_event", "rate_limit_info": {
                  "status": "rejected", "rateLimitType": "seven_day", "resetsAt": 1793012400}},
              {"type": "result", "subtype": "success", "is_error": True,
               "result": "You've hit your weekly limit · resets 4:30pm (Asia/Kolkata)"}]
    result = {**files(tmp_path, events), "is_error": True,
              "result_text": "You've hit your weekly limit · resets 4:30pm (Asia/Kolkata)"}
    reason = failure_reason("failure", None, result)
    assert reason.startswith("Claude's weekly usage limit was reached; it resets at 2026-10-26 11:00 UTC.")