
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
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
    run_dir = claude_runner.runs_root() / "duplicate-test"
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
        (claude_runner.runs_root() / task_id / "result.json").read_text(
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
    assert (claude_runner.runs_root() / task_id / "result.json").exists()


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
        (claude_runner.runs_root() / task_id / "result.json").read_text(
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


def test_read_only_flags_restrict_tools_and_ignore_settings():
    args = claude_runner.build_claude_args(
        "claude", "PROMPT", max_turns=3, model="opus", read_only=True
    )
    assert args[-1] == "PROMPT"
    assert args[args.index("--tools") + 1] == "Read,Grep,Glob"
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert args[args.index("--disallowedTools") + 2] == "Read(./.agent-lab/**)"
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert "--restricted" in args
    assert "--allowedTools" not in args  # that flag grants, it never restricts
    assert args[args.index("--disallowedTools") + 3].startswith("--")
    assert args[args.index("--model") + 1] == "opus"


def test_the_runner_and_the_orchestrator_name_the_same_data_folder():
    from orchestrator.core.project import DATA_DIR_NAME
    assert claude_runner.AGENT_LAB_DATA_DIR == DATA_DIR_NAME


def test_normal_runs_are_unchanged():
    args = claude_runner.build_claude_args("claude", "PROMPT", max_turns=3)
    assert args == [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--max-turns", "3", "--permission-mode", "default", "PROMPT",
    ]


def test_agent_env_withholds_secrets():
    source = {
        "PATH": "/bin", "Path": "C:\\Windows", "HOME": "/home/me",
        "ANTHROPIC_API_KEY": "needed-for-auth",
        "GITHUB_TOKEN": "secret", "GH_TOKEN": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret", "DATABASE_URL": "secret",
    }
    assert claude_runner.build_agent_env(source) == {
        "PATH": "/bin", "Path": "C:\\Windows", "HOME": "/home/me",
        "ANTHROPIC_API_KEY": "needed-for-auth",
    }


def test_claude_process_gets_the_filtered_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setattr(claude_runner.shutil, "which", lambda _: "claude")
    seen = {}

    def capture(args, **kwargs):
        seen.update(kwargs)
        raise OSError("stop here")

    monkeypatch.setattr(claude_runner.subprocess, "Popen", capture)
    claude_runner.run_task(
        task_id=f"env-{uuid.uuid4().hex[:10]}", prompt="hi", cwd=tmp_path, timeout_seconds=10
    )
    assert "GITHUB_TOKEN" not in seen["env"]
    assert "PATH" in seen["env"]


def test_effort_and_budget_flags_sit_before_the_prompt():
    args = claude_runner.build_claude_args(
        "claude", "PROMPT", max_turns=3, effort="high", max_budget_usd=2.5
    )

    assert args[-1] == "PROMPT"
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--max-budget-usd") + 1] == "2.5"


def test_effort_and_budget_combine_with_read_only():
    args = claude_runner.build_claude_args(
        "claude", "PROMPT", max_turns=3, read_only=True, effort="low", max_budget_usd=1
    )

    assert args[-1] == "PROMPT"
    assert args[args.index("--max-budget-usd") + 1] == "1"
    assert args[args.index("--disallowedTools") + 3].startswith("--")


def test_limits_are_omitted_when_unset():
    args = claude_runner.build_claude_args("claude", "PROMPT", max_turns=3)
    assert "--effort" not in args and "--max-budget-usd" not in args


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"effort": "extreme"}, "effort must be one of"),
        ({"max_budget_usd": 0}, "greater than 0"),
        ({"max_budget_usd": -1}, "greater than 0"),
    ],
)
def test_invalid_limits_are_rejected_before_anything_is_created(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        claude_runner.run_task(
            task_id=f"limits-{uuid.uuid4().hex[:8]}",
            prompt="hi",
            cwd=tmp_path,
            timeout_seconds=10,
            **kwargs,
        )


# --- agent roles: tool and permission flags -------------------------------


ROLE = dict(
    isolated=True,
    tools="Read,Edit,Bash",
    allowed_tools=["Read", "Bash(python -m pytest:*)"],
    disallowed_tools=["mcp__*", "Bash(git commit:*)"],
)


def test_role_flags_sit_before_the_prompt_and_after_p():
    args = claude_runner.build_claude_args("claude", "PROMPT", max_turns=3, **ROLE)

    assert args[:2] == ["claude", "-p"]
    assert args[-1] == "PROMPT"
    assert args[2] == "--restricted"
    assert args[args.index("--tools") + 1] == "Read,Edit,Bash"
    assert args[args.index("--permission-prompts") + 1] == "none"


def test_variadic_flags_are_always_followed_by_a_flag():
    args = claude_runner.build_claude_args("claude", "PROMPT", max_turns=3, **ROLE)

    for flag in ("--allowedTools", "--disallowedTools"):
        values = []
        for part in args[args.index(flag) + 1 :]:
            if part.startswith("--"):
                break
            values.append(part)
        assert values  # at least one rule
        assert "PROMPT" not in values  # the prompt is never swallowed


def test_role_rules_are_passed_whole():
    args = claude_runner.build_claude_args("claude", "PROMPT", max_turns=3, **ROLE)

    allowed = args[args.index("--allowedTools") + 1 : args.index("--disallowedTools")]
    assert allowed == ["Read", "Bash(python -m pytest:*)"]
    denied = args[args.index("--disallowedTools") + 1 : args.index("--permission-prompts")]
    assert denied == ["mcp__*", "Bash(git commit:*)"]


def test_role_flags_combine_with_model_effort_and_budget():
    args = claude_runner.build_claude_args(
        "claude", "PROMPT", max_turns=3, model="sonnet", effort="low",
        max_budget_usd=0.5, **ROLE,
    )

    assert args[:3] == ["claude", "--model", "sonnet"]
    assert args[-1] == "PROMPT"
    assert args[args.index("--effort") + 1] == "low"
    assert args[args.index("--max-budget-usd") + 1] == "0.5"


def test_runs_without_role_options_are_unchanged():
    plain = claude_runner.build_claude_args("claude", "PROMPT", max_turns=3)

    assert "--restricted" not in plain
    assert "--allowedTools" not in plain and "--tools" not in plain


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"allowed_tools": ["--dangerously-skip-permissions"]}, "must not start with '-'"),
        ({"disallowed_tools": ["-x"]}, "must not start with '-'"),
        ({"tools": "Read,--x"}, "must not start with '-'"),
        ({"allowed_tools": [" "]}, "non-empty"),
        ({"read_only": True, "isolated": True}, "cannot be combined"),
        ({"read_only": True, "tools": "Read"}, "cannot be combined"),
    ],
)
def test_bad_role_options_are_rejected_before_anything_is_created(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        claude_runner.run_task(
            task_id=f"role-{uuid.uuid4().hex[:8]}",
            prompt="hi",
            cwd=tmp_path,
            timeout_seconds=10,
            **kwargs,
        )


def test_the_runs_dir_given_by_the_orchestrator_is_where_the_files_go(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_runner.shutil, "which", lambda _: None)
    given = tmp_path / "project" / ".agent-lab" / "agents"
    claude_runner.run_task(task_id="given-dir", prompt="hello", cwd=tmp_path,
                           timeout_seconds=10, runs_dir=given)
    assert (given / "given-dir" / "result.json").is_file()
    assert not (claude_runner.runs_root() / "given-dir").exists()


# --- which model Claude actually ran on ---


class _Progress:
    def observe(self, event):
        pass

    def write(self, force=False):
        pass


def _take(event, result=None):
    result = {"event_count": 0, "session_id": None, **(result or {})}
    claude_runner._take_event(json.dumps(event), result, claude_runner._Stream(), _Progress())
    return result


def test_the_init_event_says_which_model_ran():
    result = _take({"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"},
                   {"model": None, "model_used": None})
    assert result["model_used"] == "claude-sonnet-5-5" and result["model"] is None


def test_other_system_events_and_a_missing_model_leave_it_unset():
    assert _take({"type": "system", "subtype": "status"}, {"model_used": None})["model_used"] is None
    assert _take({"type": "system", "subtype": "init", "model": ""}, {"model_used": None})["model_used"] is None


def test_without_an_init_event_the_models_billed_name_it():
    event = {"modelUsage": {"claude-opus-5-5": {}, "claude-haiku-5-5": {}}}
    assert claude_runner.models_billed(event) == "claude-haiku-5-5, claude-opus-5-5"
    assert claude_runner.models_billed({}) is None and claude_runner.models_billed(None) is None
