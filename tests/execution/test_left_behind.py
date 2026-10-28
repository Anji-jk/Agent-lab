"""What a stopped task left behind, worked out from files."""

import json
from datetime import datetime, timezone

import pytest

import orchestrator.cli.commands as cli
from orchestrator.execution import checks as tc
from orchestrator.execution.runner_adapter import runner_run_directory
from orchestrator.monitoring.progress import agent_run_id
from orchestrator.records.run_registry import write_json_atomic
from orchestrator.records.task_registry import load_task, save_task, update_task
from orchestrator.execution import left_behind as lb
from orchestrator.execution import liveness

RUN = "run-l"
NOW = datetime(2026, 10, 23, 8, 0, 0, tzinfo=timezone.utc)


# --- what changed in the worktree --------------------------------------------------------------


def test_change_sizes_reports_modified_new_and_deleted_files(git_repo):
    (git_repo / "README.md").write_text("hello\nmore\nlines\n", encoding="utf-8")  # +2
    (git_repo / "new.py").write_text("a\nb\nc\n", encoding="utf-8")
    (git_repo / "gone.txt").write_text("x\ny\n", encoding="utf-8")
    tc._git(git_repo, "add", "gone.txt")
    tc._git(git_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "gone")
    (git_repo / "gone.txt").unlink()
    rows = {path: (kind, added, removed) for path, kind, added, removed in tc.change_sizes(git_repo)}
    assert rows == {
        "README.md": ("modified", 2, 0),
        "new.py": ("new", 3, 0),
        "gone.txt": ("deleted", 0, 2),
    }


def test_change_sizes_does_not_stage_anything(git_repo):
    (git_repo / "new.py").write_text("a\n", encoding="utf-8")
    before = tc._git(git_repo, "status", "--porcelain=v1")
    tc.change_sizes(git_repo)
    assert tc._git(git_repo, "status", "--porcelain=v1") == before == "?? new.py\n"


def test_change_sizes_ignores_noise_and_marks_binary_files(git_repo):
    (git_repo / "__pycache__").mkdir()
    (git_repo / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\0\1")
    (git_repo / "image.bin").write_bytes(b"\0\1\2")
    assert tc.change_sizes(git_repo) == [("image.bin", "new", None, None)]


def test_change_sizes_of_a_clean_worktree_is_empty(git_repo):
    assert tc.change_sizes(git_repo) == []


# --- a stopped task with agent files -------------------------------------------------------------


def say(text, mid="m1"):
    return {"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": text}]}}


def call(tool_id, command, mid="m2"):
    block = {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"id": mid, "content": [block]}}


def answer(tool_id, text, error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": error}
    return {"type": "user", "message": {"content": [block]}}


def write_agent(role, events=None, **progress):
    folder = runner_run_directory(agent_run_id(RUN, "T1", role))
    folder.mkdir(parents=True, exist_ok=True)
    if events is not None:
        (folder / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n",
                                             encoding="utf-8")
    if progress:
        write_json_atomic(folder / "progress.json", progress)


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "orchestration"


def stopped_record(runs, git_repo, final: str | None = "cancelled", **fields):
    """T1 went building -> testing, then was stopped (cancelled, or left at testing)."""
    base = tc.head_commit(git_repo)
    save_task(runs, RUN, "T1", {"title": "Add double", "base_commit": base,
                                "worktree_path": str(git_repo), **fields})
    for step in ("building", "testing", *([final] if final else [])):
        update_task(runs, RUN, "T1", {"status": step})
    return load_task(runs, RUN, "T1")


@pytest.fixture
def stopped(runs, git_repo):
    (git_repo / "calc.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    write_agent("builder", [say("Added double to calc.py."),
                            call("t1", "python -m pytest -q"), answer("t1", "3 passed in 0.2s")],
                state="finished", status="succeeded", turns=9, total_cost_usd=0.0733)
    write_agent("tester", [say("Writing tests for double."),
                           call("t2", "python -m pytest tests/test_double.py -q"),
                           answer("t2", "FAILED tests/test_double.py::test_negative\n1 failed, 1 passed in 0.3s",
                                  error=True),
                           say("The negative case fails; checking calc.py.", mid="m3")],
                state="finished", status="cancelled", turns=7, total_cost_usd=0.0658)
    return stopped_record(runs, git_repo)


def test_it_lists_the_files_the_worktree_changed(stopped, git_repo):
    left = lb.left_behind(stopped, now=NOW)
    assert left.files == [lb.FileChange("calc.py", "new", 2, 0)]
    assert left.base_commit == stopped["base_commit"]
    assert left.unverified and left.status == "cancelled"


def test_it_reports_the_stage_the_task_reached(stopped):
    assert lb.left_behind(stopped, now=NOW).stage == "testing"


def test_the_stage_of_a_task_that_never_began_is_none(runs, git_repo):
    save_task(runs, RUN, "T1", {"title": "t", "worktree_path": str(git_repo)})
    update_task(runs, RUN, "T1", {"status": "cancelled"})
    assert lb.left_behind(load_task(runs, RUN, "T1"), now=NOW).stage is None


def test_it_reports_each_agents_last_message_and_last_tests(stopped):
    builder, tester, reviewer = lb.left_behind(stopped, now=NOW).agents
    assert builder.last_message == "Added double to calc.py."
    assert builder.last_tests == lb.LastTests("python -m pytest -q", "passed", "3 passed in 0.2s")
    assert tester.last_message == "The negative case fails; checking calc.py."
    assert tester.last_tests == lb.LastTests(
        "python -m pytest tests/test_double.py -q", "failed", "1 failed, 1 passed in 0.3s")
    assert reviewer.last_message is None and reviewer.last_tests is None
    assert reviewer.line == "reviewer: not started"


def test_a_test_run_that_was_cut_off_has_no_result(runs, git_repo):
    write_agent("tester", [call("t1", "python -m pytest -q")], state="running", status=None, turns=2)
    record = stopped_record(runs, git_repo, final=None)
    tester = lb.left_behind(record, now=NOW).agents[1]
    assert tester.last_tests == lb.LastTests("python -m pytest -q", "not finished", "")


def test_only_pytest_commands_count_as_tests(runs, git_repo):
    write_agent("builder", [call("t1", "git status --short"), answer("t1", "M calc.py")],
                state="running", turns=1)
    record = stopped_record(runs, git_repo, final=None)
    assert lb.left_behind(record, now=NOW).agents[0].last_tests is None


def test_secrets_in_a_message_are_masked_and_long_ones_cut(runs, git_repo):
    long = "Used ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop12345 then " + "word " * 200
    write_agent("builder", [say(long)], state="running", turns=1)
    message = lb.left_behind(stopped_record(runs, git_repo, final=None), now=NOW).agents[0].last_message
    assert message is not None
    assert "sk-ant-abcdefghijklmnop12345" not in message and "[masked]" in message
    assert message is not None and len(message) <= lb.MESSAGE_LIMIT and message.endswith("…")


def test_it_adds_up_what_the_agents_spent(stopped):
    assert lb.left_behind(stopped, now=NOW).spent == pytest.approx(0.1391)


def test_a_missing_worktree_is_reported_not_guessed(runs, tmp_path):
    save_task(runs, RUN, "T1", {"title": "t", "worktree_path": str(tmp_path / "gone")})
    update_task(runs, RUN, "T1", {"status": "cancelled"})
    left = lb.left_behind(load_task(runs, RUN, "T1"), now=NOW)
    assert left.files is None and "no longer exists" in (left.files_note or "")
    assert "Files:       unknown, the worktree" in lb.format_left_behind(left)


def test_a_task_that_never_made_a_worktree_says_so(runs):
    save_task(runs, RUN, "T1", {"title": "t"})
    update_task(runs, RUN, "T1", {"status": "cancelled"})
    assert lb.left_behind(load_task(runs, RUN, "T1"), now=NOW).files_note == "no worktree was created"


def test_the_summary_is_readable_and_marked_unverified(stopped):
    text = lb.format_left_behind(lb.left_behind(stopped, now=NOW))
    lines = text.splitlines()
    assert lines[0] == "Left behind by T1 (unverified: the task was stopped before its checks and review finished)"
    assert "  Stopped at:  testing (the tester)" in lines
    assert any(l.strip().startswith("new       calc.py") and l.endswith("+2") for l in lines)
    assert "      last tests:  python -m pytest -q -> passed (3 passed in 0.2s)" in lines
    assert any("last tests:  python -m pytest tests/test_double.py -q -> failed" in l for l in lines)
    assert "      last said:   The negative case fails; checking calc.py." in lines
    assert "  reviewer: not started" in lines
    assert lines[-1] == "  Spent so far: $0.1391"


def test_a_stop_after_the_agent_had_finished_names_the_checks(runs, git_repo):
    # Seen on the real cancelled run: the tester had succeeded and the orchestrator's
    # full suite (about 11 minutes there) was running when the task was cancelled.
    write_agent("builder", [say("built")], state="finished", status="succeeded", turns=3)
    write_agent("tester", [say("tested")], state="finished", status="succeeded", turns=2)
    text = lb.format_left_behind(lb.left_behind(stopped_record(runs, git_repo), now=NOW))
    assert ("  Stopped at:  testing (the tester had finished; the orchestrator's own checks "
            "were running)") in text.splitlines()


def test_a_long_file_list_is_cut(runs, git_repo):
    for number in range(lb.MAX_FILES_SHOWN + 3):
        (git_repo / f"f{number:02}.py").write_text("x\n", encoding="utf-8")
    left = lb.left_behind(stopped_record(runs, git_repo), now=NOW)
    assert "... and 3 more" in lb.format_left_behind(left)


# --- which tasks count as stopped ------------------------------------------------------------------


def alive(running):
    return lambda task_id, record: (running, "process 9 is running it")


@pytest.mark.parametrize("status,running,expected", [
    ("cancelled", False, True),
    ("building", False, True), ("testing", False, True), ("reviewing", False, True),
    ("building", True, False),
    ("done", False, False), ("failed", False, False), ("needs_review", False, False),
    ("pending", False, False),
])
def test_which_tasks_count_as_stopped(status, running, expected):
    assert lb.is_stopped({"task_id": "T1", "status": status}, alive(running)) is expected


# --- show-task --------------------------------------------------------------------------------------


def test_show_task_prints_what_a_cancelled_task_left(stopped, runs, capsys):
    assert cli.main(["show-task", RUN, "T1", "--runs-dir", str(runs)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Task T1 of run run-l: cancelled" in out
    assert "Left behind by T1" in out and "calc.py" in out and "last said:" in out


def test_show_task_prints_it_for_an_interrupted_task_too(runs, git_repo, monkeypatch, capsys):
    stopped_record(runs, git_repo, final=None, pid=4242)
    monkeypatch.setattr(liveness, "command_line", lambda pid: None)  # its process has gone
    assert cli.main(["show-task", RUN, "T1", "--runs-dir", str(runs)]) == cli.EXIT_OK
    assert "Left behind by T1" in capsys.readouterr().out


def test_show_task_leaves_a_running_task_alone(runs, git_repo, monkeypatch, capsys):
    stopped_record(runs, git_repo, final=None, pid=4242)
    monkeypatch.setattr(liveness, "command_line", lambda pid: f"python -m orchestrator run-task {RUN} T1")
    assert cli.main(["show-task", RUN, "T1", "--runs-dir", str(runs)]) == cli.EXIT_OK
    assert "Left behind" not in capsys.readouterr().out


def test_show_task_of_a_finished_task_has_no_summary(runs, git_repo, capsys):
    stopped_record(runs, git_repo, final="reviewing")
    update_task(runs, RUN, "T1", {"status": "done", "commit": "a" * 40})
    assert cli.main(["show-task", RUN, "T1", "--runs-dir", str(runs)]) == cli.EXIT_OK
    assert "Left behind" not in capsys.readouterr().out