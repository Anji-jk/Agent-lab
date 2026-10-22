import functools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import runner_adapter
from orchestrator.runner_adapter import invoke_runner, runner_run_directory

REAL_RUNNER = Path(runner_adapter.DEFAULT_RUNNER_SCRIPT)


@pytest.fixture
def runner_script(tmp_path):
    """A fake runner location; its project root is tmp_path."""
    script = tmp_path / "project" / "scripts" / "claude_runner.py"
    script.parent.mkdir(parents=True)
    return script


def fake_process(runner_script, run_id, *, result=None, raw=None, code=0, stderr=""):
    """Build a process_runner that 'runs' the runner by writing result.json."""
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        run_dir = runner_run_directory(run_id, runner_script)
        if result is not None or raw is not None:
            run_dir.mkdir(parents=True, exist_ok=True)
            text = raw if raw is not None else json.dumps(result)
            (run_dir / "result.json").write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(command, code, stdout=None, stderr=stderr)

    run.calls = calls
    return run


def call(runner_script, tmp_path, process_runner, **overrides):
    options = dict(
        run_id="run-1",
        worktree_path=tmp_path / "wt",
        prompt_file=tmp_path / "prompt.txt",
        timeout_seconds=30,
        runner_script=runner_script,
        process_runner=process_runner,
    )
    options.update(overrides)
    return invoke_runner(**options)


def test_success_maps_status_and_retains_fields(runner_script, tmp_path):
    result = {
        "status": "succeeded",
        "exit_code": 0,
        "duration_seconds": 12.5,
        "total_cost_usd": 0.42,
        "input_tokens": 100,
        "output_tokens": 50,
        "num_turns": 3,
        "started_at": "a",
        "finished_at": "b",
        "result_text": "SECRET MODEL OUTPUT",
        "claude_result_event": {"big": "blob"},
        "command": ["claude", "<prompt omitted>"],
    }
    runner = fake_process(runner_script, "run-1", result=result)

    outcome = call(runner_script, tmp_path, runner)

    assert outcome.outcome == "success"
    assert outcome.process_exit_code == 0
    assert outcome.result["total_cost_usd"] == 0.42
    assert outcome.result["input_tokens"] == 100
    assert outcome.result["duration_seconds"] == 12.5
    for dropped in ("result_text", "claude_result_event", "command"):
        assert dropped not in outcome.result


def test_result_text_is_kept_only_when_requested(runner_script, tmp_path):
    result = {"status": "succeeded", "exit_code": 0, "result_text": "THE ANSWER"}
    runner = fake_process(runner_script, "run-1", result=result)

    default = call(runner_script, tmp_path, runner)
    wanted = call(runner_script, tmp_path, runner, keep_result_text=True)

    assert "result_text" not in default.result
    assert wanted.result["result_text"] == "THE ANSWER"


def test_non_text_result_text_is_ignored(runner_script, tmp_path):
    runner = fake_process(
        runner_script, "run-1", result={"status": "succeeded", "result_text": {"x": 1}}
    )
    outcome = call(runner_script, tmp_path, runner, keep_result_text=True)
    assert "result_text" not in outcome.result


def test_failure(runner_script, tmp_path):
    runner = fake_process(
        runner_script,
        "run-1",
        result={"status": "failed", "exit_code": 1, "error": "boom"},
        code=1,
    )
    outcome = call(runner_script, tmp_path, runner)

    assert outcome.outcome == "failure"
    assert outcome.error == "boom"
    assert outcome.process_exit_code == 1


def test_runner_timeout(runner_script, tmp_path):
    runner = fake_process(
        runner_script,
        "run-1",
        result={"status": "timed_out", "timed_out": True},
        code=1,
    )
    assert call(runner_script, tmp_path, runner).outcome == "timeout"


def test_launch_error_reported_by_runner(runner_script, tmp_path):
    runner = fake_process(
        runner_script,
        "run-1",
        result={"status": "launch_error", "error": "Claude Code CLI was not found on PATH."},
        code=1,
    )
    outcome = call(runner_script, tmp_path, runner)

    assert outcome.outcome == "launch_error"
    assert "not found" in outcome.error


def test_success_with_nonzero_exit_is_a_failure(runner_script, tmp_path):
    runner = fake_process(
        runner_script, "run-1", result={"status": "succeeded"}, code=3
    )
    assert call(runner_script, tmp_path, runner).outcome == "failure"


def test_unknown_status_is_a_failure(runner_script, tmp_path):
    runner = fake_process(
        runner_script, "run-1", result={"status": "weird"}, code=0
    )
    assert call(runner_script, tmp_path, runner).outcome == "failure"


def test_missing_result_file(runner_script, tmp_path):
    runner = fake_process(runner_script, "run-1", code=1)
    outcome = call(runner_script, tmp_path, runner)

    assert outcome.outcome == "launch_error"
    assert "missing" in outcome.error


@pytest.mark.parametrize("raw", ["{broken", "[]", '"text"', "{}", '{"status": 5}'])
def test_malformed_result_file(runner_script, tmp_path, raw):
    runner = fake_process(runner_script, "run-1", raw=raw, code=0)
    outcome = call(runner_script, tmp_path, runner)

    assert outcome.outcome == "launch_error"
    assert outcome.result == {}


def test_usage_error_exit_code_2_reports_runner_message(runner_script, tmp_path):
    runner = fake_process(
        runner_script,
        "run-1",
        code=2,
        stderr="usage: ...\nclaude_runner.py: error: Working directory does not exist",
    )
    outcome = call(runner_script, tmp_path, runner)

    assert outcome.outcome == "launch_error"
    assert "rejected its arguments" in outcome.error
    assert "Working directory" in outcome.error


def test_outer_timeout(runner_script, tmp_path):
    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    outcome = call(runner_script, tmp_path, hang)
    assert outcome.outcome == "timeout"


def test_cannot_start_python(runner_script, tmp_path):
    def broken(command, **kwargs):
        raise FileNotFoundError("nope")

    outcome = call(runner_script, tmp_path, broken)
    assert outcome.outcome == "launch_error"
    assert "FileNotFoundError" in outcome.error


def test_command_uses_runner_cli_and_keeps_prompt_out(runner_script, tmp_path):
    runner = fake_process(
        runner_script, "run-1", result={"status": "succeeded"}, code=0
    )
    call(
        runner_script,
        tmp_path,
        runner,
        model="claude-opus-5-5",
        max_turns=7,
        timeout_seconds=60,
    )

    command, kwargs = runner.calls[0]
    assert command[0] == sys.executable
    assert command[1] == str(runner_script)
    pairs = dict(zip(command[2::2], command[3::2]))
    assert pairs == {
        "--task-id": "run-1",
        "--cwd": str(tmp_path / "wt"),
        "--prompt-file": str(tmp_path / "prompt.txt"),
        "--timeout": "60",
        "--model": "claude-opus-5-5",
        "--max-turns": "7",
    }
    assert "--prompt" not in command
    assert kwargs["stdout"] == subprocess.DEVNULL  # runner's copy of output is dropped
    assert kwargs["timeout"] > 60


def test_optional_flags_are_omitted(runner_script, tmp_path):
    runner = fake_process(
        runner_script, "run-1", result={"status": "succeeded"}, code=0
    )
    call(runner_script, tmp_path, runner)

    command, _ = runner.calls[0]
    assert "--model" not in command
    assert "--max-turns" not in command
    assert "--read-only" not in command
    assert "--effort" not in command and "--max-budget-usd" not in command


def test_effort_and_budget_are_passed_to_the_runner(runner_script, tmp_path):
    runner = fake_process(
        runner_script, "run-1", result={"status": "succeeded"}, code=0
    )
    call(runner_script, tmp_path, runner, effort="medium", max_budget_usd=3.0)

    command, _ = runner.calls[0]
    pairs = dict(zip(command[2::2], command[3::2]))
    assert pairs["--effort"] == "medium"
    assert pairs["--max-budget-usd"] == "3"


def test_read_only_is_passed_to_the_runner(runner_script, tmp_path):
    runner = fake_process(runner_script, "run-1", result={"status": "succeeded"}, code=0)
    call(runner_script, tmp_path, runner, read_only=True)
    command, _ = runner.calls[0]
    assert "--read-only" in command


def test_runner_directory_follows_the_runner_script(runner_script):
    assert runner_run_directory("run-1", runner_script) == (
        runner_script.parent.parent / "runs" / "run-1"
    )


def copy_real_runner(script):
    """Copy the runner and the sibling modules it imports."""
    for name in ("claude_runner.py", "agent_env.py"):
        shutil.copy(REAL_RUNNER.parent / name, script.parent / name)


def test_real_runner_cli_contract_without_claude(tmp_path):
    """Drive the real scripts/claude_runner.py with Claude hidden from PATH.

    The runner is copied into tmp_path so its runs/ directory is isolated.
    """
    script = tmp_path / "project" / "scripts" / "claude_runner.py"
    script.parent.mkdir(parents=True)
    copy_real_runner(script)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello", encoding="utf-8")
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {**os.environ, "PATH": str(empty_bin)}

    outcome = invoke_runner(
        run_id="run-real-1",
        worktree_path=worktree,
        prompt_file=prompt,
        timeout_seconds=10,
        runner_script=script,
        process_runner=functools.partial(subprocess.run, env=env),
    )

    assert outcome.outcome == "launch_error"
    assert "not found on PATH" in outcome.error
    assert outcome.result["status"] == "launch_error"
    assert (script.parent.parent / "runs" / "run-real-1" / "result.json").exists()


def test_real_runner_rejects_missing_worktree_with_usage_error(tmp_path):
    script = tmp_path / "project" / "scripts" / "claude_runner.py"
    script.parent.mkdir(parents=True)
    copy_real_runner(script)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello", encoding="utf-8")

    outcome = invoke_runner(
        run_id="run-real-2",
        worktree_path=tmp_path / "does-not-exist",
        prompt_file=prompt,
        timeout_seconds=10,
        runner_script=script,
    )

    assert outcome.outcome == "launch_error"
    assert outcome.process_exit_code == 2


def test_role_options_are_passed_to_the_runner(runner_script, tmp_path):
    runner = fake_process(runner_script, "run-1", result={"status": "succeeded"}, code=0)
    call(
        runner_script, tmp_path, runner,
        isolated=True, tools="Read,Edit",
        allowed_tools=["Read", "Bash(git diff:*)"], disallowed_tools=["mcp__*"],
    )

    command, _ = runner.calls[0]
    assert "--isolated" in command
    assert command[command.index("--tools") + 1] == "Read,Edit"
    allowed = [command[i + 1] for i, part in enumerate(command) if part == "--allow"]
    assert allowed == ["Read", "Bash(git diff:*)"]
    denied = [command[i + 1] for i, part in enumerate(command) if part == "--deny"]
    assert denied == ["mcp__*"]


def test_role_options_are_omitted_by_default(runner_script, tmp_path):
    runner = fake_process(runner_script, "run-1", result={"status": "succeeded"}, code=0)
    call(runner_script, tmp_path, runner)

    command, _ = runner.calls[0]
    for flag in ("--isolated", "--tools", "--allow", "--deny"):
        assert flag not in command


def test_agent_env_loads_in_a_fresh_interpreter_without_scripts_on_the_path():
    """The CLI is not run under pytest, which adds scripts/ to the path."""
    code = (
        "import os; os.environ['GITHUB_TOKEN']='secret'; "
        "from orchestrator.runner_adapter import load_agent_env; "
        "env = load_agent_env(); "
        "print('GITHUB_TOKEN' in env, 'PATH' in {k.upper() for k in env})"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=runner_adapter.PROJECT_ROOT,
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr[-400:]
    assert done.stdout.split() == ["False", "True"]


def test_role_is_passed_to_the_runner(runner_script, tmp_path):
    runner = fake_process(runner_script, "run-1", result={"status": "succeeded"}, code=0)
    call(runner_script, tmp_path, runner, role="tester")
    command, _ = runner.calls[0]
    assert command[command.index("--role") + 1] == "tester"
