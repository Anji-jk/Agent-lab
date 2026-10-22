"""M3.4: turning raw event files into readable items."""

import json

import pytest

from orchestrator.monitoring import event_feed as ef

CWD = "C:\\Users\\me\\agent-worktrees\\wt-t1"

# Claude Code's stream-json, shaped as it arrives: one content block per event, a reply's
# blocks sharing one message id, and tool results coming back as "user" events.
STREAM = [
    {"type": "system", "subtype": "init", "session_id": "s1", "cwd": CWD, "model": "claude-sonnet",
     "permissionMode": "default", "tools": ["Read", "Edit", "Bash"]},
    {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "text", "text": "I'll read the summarizer first."}]}},
    {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "t1", "name": "Read",
         "input": {"file_path": CWD + "\\src\\csv_summary.py"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "line 1\nline 2\nline 3"}]}},
    {"type": "assistant", "message": {"id": "m2", "content": [
        {"type": "tool_use", "id": "t2", "name": "Edit",
         "input": {"file_path": CWD + "\\src\\csv_summary.py", "old_string": "a", "new_string": "b"}}]}},
    # The same block again (Claude repeats blocks when a reply is streamed in parts).
    {"type": "assistant", "message": {"id": "m2", "content": [
        {"type": "tool_use", "id": "t2", "name": "Edit",
         "input": {"file_path": CWD + "\\src\\csv_summary.py", "old_string": "a", "new_string": "b"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "File updated."}]}]}},
    {"type": "assistant", "message": {"id": "m3", "content": [
        {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "sed -i 's/x/y/' a.py"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t3", "is_error": True,
         "content": "Claude requested permissions to use Bash, but you haven't granted it yet."}]}},
    {"type": "assistant", "message": {"id": "m4", "content": [
        {"type": "tool_use", "id": "t4", "name": "Bash",
         "input": {"command": "python -m pytest tests/test_csv_summary.py -q", "description": "run tests"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t4", "content": "....\n\n4 passed in 0.31s\n"}]}},
    {"type": "assistant", "message": {"id": "m5", "content": [
        {"type": "thinking", "thinking": "All good."},
        {"type": "text", "text": "{\"summary\": \"done\"}"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 5,
     "total_cost_usd": 0.0421, "duration_ms": 31646, "result": "{\"summary\": \"done\"}"},
]


def write_stream(folder, events, times=True, broken_tail=False):
    folder.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in events]
    text = "\n".join(lines) + "\n"
    if broken_tail:
        text += '{"type": "assist'  # a line the runner is still writing
    (folder / "events.jsonl").write_text(text, encoding="utf-8")
    if times:
        stamps = [f"2026-10-22T10:05:{n:02d}+00:00" for n in range(len(lines))]
        (folder / "event_times.txt").write_text("\n".join(stamps) + "\n", encoding="utf-8")
    return folder / "events.jsonl"


@pytest.fixture
def items(tmp_path):
    return ef.agent_items(write_stream(tmp_path / "agent", STREAM))


def test_the_stream_becomes_one_item_per_action(items):
    assert [(i.kind, i.tool) for i in items] == [
        ("start", None), ("say", None), ("tool", "Read"), ("tool", "Edit"),
        ("tool", "Bash"), ("tool", "Bash"), ("think", None), ("say", None), ("finish", None)]


def test_tool_calls_are_paired_with_their_results(items):
    read, edit, blocked, pytest_run = items[2:6]
    assert read.title == "src/csv_summary.py"  # relative to the agent's folder
    assert read.result["status"] == "ok" and read.result["summary"] == "3 lines"
    assert edit.detail == "--- before\na\n+++ after\nb"
    assert edit.result["summary"] == "File updated."
    assert blocked.result["status"] == "blocked"
    assert pytest_run.title == "python -m pytest tests/test_csv_summary.py -q"
    assert pytest_run.result["summary"] == "4 passed in 0.31s"  # Bash: the last line
    assert "# run tests" in pytest_run.detail


def test_start_and_finish_lines(items):
    assert items[0].title == "session started · claude-sonnet · 3 tools"
    assert "permission mode: default" in items[0].detail
    assert items[-1].title == "succeeded · 5 turns · $0.0421 total · 31.6s"


def test_each_item_has_the_time_its_line_arrived(items):
    assert items[0].at == "2026-10-22T10:05:00+00:00"
    assert items[2].at == "2026-10-22T10:05:02+00:00"
    assert items[2].result["at"] == "2026-10-22T10:05:03+00:00"


def test_runs_without_a_times_file_still_read(tmp_path):
    items = ef.agent_items(write_stream(tmp_path / "old", STREAM, times=False))
    assert len(items) == 9 and all(i.at is None for i in items)


def test_a_half_written_last_line_is_skipped(tmp_path):
    items = ef.agent_items(write_stream(tmp_path / "live", STREAM[:3], broken_tail=True))
    assert [i.kind for i in items] == ["start", "say", "tool"]


def test_a_missing_file_gives_no_items(tmp_path):
    assert ef.agent_items(tmp_path / "nothing" / "events.jsonl") == []


def test_odd_events_are_ignored(tmp_path):
    odd = [{"type": "assistant"}, {"type": "assistant", "message": "x"},
           {"type": "user", "message": {"content": [None, {"type": "tool_result"}]}},
           {"type": "stream_event", "event": {}}, [1, 2], "text"]
    path = tmp_path / "odd" / "events.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join(json.dumps(e) for e in odd), encoding="utf-8")
    items = ef.agent_items(path)
    assert [i.title for i in items] == ["result of an earlier call"]


# Shaped like a real builder run on Windows (M3.2): the full suite outlived Bash's 120 s
# wait and went to the background, Claude finished, and the suite ending woke it again.
REAL_SHAPES = [
    {"type": "system", "subtype": "init", "cwd": CWD, "model": "claude-sonnet-5-5", "tools": ["Read", "Bash"],
     "claude_code_version": "2.9.0"},
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50},
    {"type": "assistant", "timestamp": "2026-10-22T10:07:24.899Z", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python src/csv_summary.py x.csv"}}]}},
    {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "tool_use_id": "t1"},
    {"type": "user", "timestamp": "2026-10-22T10:07:24.924Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
         "content": "Permission for this tool use was denied. It requires approval, and this session has no approval surface."}]}},
    {"type": "assistant", "timestamp": "2026-10-22T10:07:27.000Z", "message": {"id": "m2", "content": [
        {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": CWD + "\\src\\a.py", "old_string": "x", "new_string": "y"}}]}},
    {"type": "user", "timestamp": "2026-10-22T10:07:27.100Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": f"The file {CWD}\\src\\a.py has been updated successfully."}]}},
    {"type": "assistant", "timestamp": "2026-10-22T10:07:37.133Z", "message": {"id": "m3", "content": [
        {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "python -m pytest -q"}}]}},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    {"type": "system", "subtype": "task_started", "task_id": "bg1", "tool_use_id": "t3"},
    {"type": "tool_progress", "tool_use_id": "t3-heartbeat-0", "parent_tool_use_id": "t3", "elapsed_time_seconds": 30},
    {"type": "system", "subtype": "task_updated", "task_id": "bg1", "patch": {"is_backgrounded": True}},
    {"type": "user", "timestamp": "2026-10-22T10:09:40.150Z", "tool_use_result": {"backgroundTaskId": "bg1"},
     "message": {"content": [{"type": "tool_result", "tool_use_id": "t3",
                              "content": "Command did not complete within its 120s timeout and was moved to the background (ID: bg1)."}]}},
    {"type": "result", "subtype": "success", "num_turns": 15, "total_cost_usd": 0.1053728, "duration_ms": 165345},
    {"type": "system", "subtype": "task_notification", "task_id": "bg1", "status": "completed",
     "summary": 'Background command "python -m pytest -q" completed (exit code 0)'},
    {"type": "system", "subtype": "background_tasks_changed", "tasks": []},
    {"type": "system", "subtype": "init", "cwd": CWD, "model": "claude-sonnet-5-5", "tools": ["Read", "Bash"]},
    {"type": "assistant", "timestamp": "2026-10-22T10:16:04.862Z", "message": {"id": "m4", "content": [
        {"type": "text", "text": "{\"summary\": \"done\"}"}]}},
    {"type": "result", "subtype": "success", "num_turns": 1, "total_cost_usd": 0.1141872, "duration_ms": 3662},
]


def test_a_run_that_woke_up_again_reads_as_it_happened(tmp_path):
    items = ef.agent_items(write_stream(tmp_path / "real", REAL_SHAPES, times=False))
    assert [(i.kind, i.tool or i.title) for i in items] == [
        ("start", "session started · claude-sonnet-5-5 · 2 tools"),
        ("tool", "Bash"), ("tool", "Edit"), ("tool", "Bash"),
        ("finish", "succeeded · 15 turns · $0.1054 total · 165.3s"),
        ("step", 'background command completed: Background command "python -m pytest -q" completed (exit code 0)'),
        ("start", "session woke up again"),
        ("say", '{"summary": "done"}'),
        ("finish", "succeeded · 1 turns · $0.1142 total · 3.7s"),
    ]
    denied, edit, suite = items[1:4]
    assert denied.result["status"] == "blocked"
    assert edit.result["summary"] == "The file src\\a.py has been updated successfully."  # folder taken out
    assert suite.result["status"] == "background"
    assert suite.result["summary"].startswith("still running, moved to the background")


def test_claudes_own_timestamps_are_used(tmp_path):
    items = ef.agent_items(write_stream(tmp_path / "real", REAL_SHAPES, times=False))
    assert items[1].at == "2026-10-22T10:07:24.899Z"
    assert items[1].result["at"] == "2026-10-22T10:07:24.924Z"
    assert items[0].at is None  # init has no timestamp, and this run has no times file


def test_a_rate_limit_that_bites_is_shown(tmp_path):
    stream = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}]
    (item,) = ef.agent_items(write_stream(tmp_path / "rl", stream))
    assert item.kind == "error" and "rate limited" in item.title


# --- the cursor ---------------------------------------------------------------------------


def test_the_cursor_stops_at_a_tool_still_waiting_for_its_result(tmp_path):
    folder = tmp_path / "agent"
    waiting = ef.agent_items(write_stream(folder, STREAM[:3]))
    assert waiting[2].pending and ef.stable_index(waiting) == 2
    page = ef.feed(waiting, after=0)
    assert page["next"] == 2 and page["count"] == 3 and len(page["items"]) == 3

    done = ef.agent_items(write_stream(folder, STREAM[:4]))
    page = ef.feed(done, after=2)  # resume where the last page said
    assert page["from"] == 2 and page["next"] == 3
    assert page["items"][0]["result"]["summary"] == "3 lines"


def test_after_beyond_the_end_is_clamped(items):
    page = ef.feed(items, after=999)
    assert page["from"] == len(items) and page["items"] == []


# --- masking and shortening ----------------------------------------------------------------


@pytest.mark.parametrize("secret", [
    "ghp_" + "a" * 36, "github_pat_" + "b" * 30, "sk-ant-api03-" + "c" * 30,
    "sk-" + "d" * 40, "AKIAABCDEFGHIJKLMNOP",
])
def test_secret_looking_strings_are_masked(secret):
    masked = ef.mask_secrets(f"value {secret} end")
    assert secret not in masked and ef.MASK in masked and masked.startswith("value ")


def test_secret_assignments_and_bearer_tokens_are_masked():
    text = "GITHUB_TOKEN=abc123xyz789\napi_key: 'qwerty12345'\nAuthorization: Bearer abcdefghijklmnopqrstu"
    masked = ef.mask_secrets(text)
    assert "abc123xyz789" not in masked and "qwerty12345" not in masked
    assert "abcdefghijklmnopqrstu" not in masked
    assert "GITHUB_TOKEN=" in masked and "Bearer" in masked


def test_ordinary_text_is_not_masked():
    text = "def test_token_parser():\n    assert tokens == 3  # 4 passed in 0.3s"
    assert ef.mask_secrets(text) == text


def test_masking_applies_to_what_the_page_receives(tmp_path):
    stream = [{"type": "assistant", "message": {"id": "m", "content": [
        {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "cat .env"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "ANTHROPIC_API_KEY=sk-ant-" + "x" * 40}]}}]
    (item,) = ef.agent_items(write_stream(tmp_path / "a", stream))
    data = item.to_dict()
    assert "x" * 40 not in json.dumps(data)


def test_long_output_keeps_its_first_and_last_lines():
    text = "\n".join(f"line {n}" for n in range(200))
    short, cut = ef.shorten(text)
    assert cut and "line 0" in short and "line 199" in short and "line 100" not in short
    assert "120 lines hidden" in short
    whole, cut = ef.shorten(text, full=True)
    assert whole == text and not cut


def test_one_huge_line_is_cut_too():
    short, cut = ef.shorten("x" * 50_000)
    assert cut and len(short) < 20_000


# --- the orchestrator's log ----------------------------------------------------------------


def write_log(tmp_path, entries):
    path = tmp_path / "task" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"at": f"2026-10-22T10:00:{n:02d}+00:00", **e})
                              for n, e in enumerate(entries)) + "\n", encoding="utf-8")
    return path


def test_orchestrator_steps_pair_their_start_and_end(tmp_path):
    path = write_log(tmp_path, [
        {"step": "task_started", "base_commit": "4f3a5b548def0000", "title": "Add stats"},
        {"step": "agent_started", "role": "builder", "settings": "builder: sonnet"},
        {"step": "agent_finished", "role": "builder", "outcome": "success", "cost_usd": 0.1142, "seconds": 454.6},
        {"step": "builder_checked", "changed": ["src/a.py"], "scope_violations": [], "nothing_changed": False, "blocked": ""},
        {"step": "suite_started", "command": ["python", "-m", "pytest", "-q"]},
        {"step": "suite_finished", "exit_code": 0, "timed_out": False, "seconds": 554.5, "passed": True,
         "output_tail": "....\n575 passed in 554.5s\n"},
        {"step": "decided", "status": "done", "reasons": ["Every check passed."]},
        {"step": "committed", "commit": "fb222bf6b7407725554f", "branch": "agent/x"},
        {"step": "task_finished", "status": "done", "cost_usd": 0.2362},
    ])
    items = ef.orchestrator_items(path)
    titles = [i.title for i in items]
    assert titles == ["task started from commit 4f3a5b548def", "builder agent",
                      "builder's changes checked: 1 file", "full test suite", "decision: done",
                      "committed fb222bf6b7 on agent/x", "task finished: done · $0.2362"]
    assert items[1].result["summary"] == "success · $0.1142 · 454.6s"
    assert items[3].result["summary"] == "passed in 554.5s · 575 passed in 554.5s"
    assert items[3].result["detail"].startswith("....")
    assert ef.stable_index(items) == len(items)


def test_a_suite_still_running_is_pending(tmp_path):
    path = write_log(tmp_path, [{"step": "suite_started", "command": ["pytest"]}])
    (item,) = ef.orchestrator_items(path)
    assert item.pending and ef.stable_index([item]) == 0


def test_failures_in_the_orchestrator_log_stand_out(tmp_path):
    path = write_log(tmp_path, [
        {"step": "suite_started", "command": ["pytest"]},
        {"step": "suite_finished", "exit_code": None, "timed_out": True, "seconds": 1200.0, "passed": False, "output_tail": ""},
        {"step": "stage_stopped", "reasons": ["The full test suite failed or timed out."]},
        {"step": "task_finished", "status": "failed", "cost_usd": 0.1},
    ])
    suite, stopped, finished = ef.orchestrator_items(path)
    assert suite.result["status"] == "error" and suite.result["summary"] == "timed out in 1200.0s"
    assert stopped.kind == "error" and stopped.title.startswith("stopped: The full test suite")
    assert finished.kind == "error" and finished.status == "error"


def test_unknown_steps_still_show(tmp_path):
    (item,) = ef.orchestrator_items(write_log(tmp_path, [{"step": "something_new", "x": 1}]))
    assert item.title == "something new" and '"x": 1' in item.detail
