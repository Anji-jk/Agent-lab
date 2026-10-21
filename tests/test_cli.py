import subprocess
import sys

import pytest

import orchestrator.__main__ as cli
from orchestrator.ai_planner import PlanningError
from orchestrator.github_client import GitHubAPIError, GitHubClient
from orchestrator.task_plan import save_task_plan
from orchestrator.tasks import Task


def issue(number=1, body="Add a header check.", **extra):
    return {
        "number": number,
        "title": "Fix headers",
        "state": "open",
        "body": body,
        "html_url": f"https://github.com/Anji-jk/Agent-lab/issues/{number}",
        "labels": [{"name": "bug"}],
        **extra,
    }


@pytest.fixture
def fake_issue(monkeypatch):
    holder = {"issue": issue()}
    monkeypatch.setattr(
        GitHubClient, "get_issue", lambda self, number: holder["issue"]
    )
    return holder


@pytest.fixture
def no_execution(monkeypatch):
    """Fail the test if anything tries to execute."""
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("execute_plan must not be called")

    monkeypatch.setattr(cli, "execute_plan", boom)
    return calls


def args_for(command, git_repo, tmp_path, *extra):
    return [
        command,
        "1",
        "--repo-path",
        str(git_repo),
        *(["--runs-dir", str(tmp_path / "orch")] if command == "execute" else []),
        *extra,
    ]


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])

    out = capsys.readouterr().out
    assert info.value.code == 0
    for command in ("issue", "plan", "execute"):
        assert command in out
    assert "exit codes" in out


def test_execute_help_documents_approve(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["execute", "--help"])
    assert info.value.code == 0
    assert "--approve" in capsys.readouterr().out


def test_module_entry_point_runs():
    result = subprocess.run(
        [sys.executable, "-m", "orchestrator", "--help"],
        capture_output=True,
        text=True,
        cwd=cli.PROJECT_ROOT,
    )
    assert result.returncode == 0
    assert "plan" in result.stdout


def test_existing_issue_command_still_works(fake_issue, capsys):
    assert cli.main(["issue", "1"]) == 0
    out = capsys.readouterr().out
    assert "Issue:       #1" in out
    assert "Labels:      bug" in out


def test_issue_command_reports_api_errors(monkeypatch, capsys):
    def fail(self, number):
        raise GitHubAPIError("GitHub API returned HTTP 404 for issues/9.")

    monkeypatch.setattr(GitHubClient, "get_issue", fail)

    assert cli.main(["issue", "9"]) == 1
    assert "HTTP 404" in capsys.readouterr().err


def test_issue_command_bad_repo_still_exits_1(capsys):
    assert cli.main(["issue", "1", "--repo", "noslash"]) == 1


def test_plan_shows_plan_and_creates_nothing(
    fake_issue, git_repo, tmp_path, no_execution, capsys
):
    code = cli.main(args_for("plan", git_repo, tmp_path, "--run-id", "run-plan-1"))

    out = capsys.readouterr().out
    assert code == 0
    assert "#1 - Fix headers" in out
    assert "Anji-jk/Agent-lab" in out
    assert "agent/issue-1-run-plan-1" in out
    assert "Nothing was executed" in out
    assert not (git_repo.parent / "repo-agent-issue-1-run-plan-1").exists()
    assert not (tmp_path / "orch").exists()
    assert no_execution == []


def test_execute_without_approve_stops_before_side_effects(
    fake_issue, git_repo, tmp_path, no_execution, capsys
):
    code = cli.main(args_for("execute", git_repo, tmp_path, "--run-id", "run-na-1"))

    captured = capsys.readouterr()
    assert code == cli.EXIT_NOT_APPROVED
    assert "Not approved" in captured.err
    assert "agent/issue-1-run-na-1" in captured.out  # plan is still shown
    assert not (git_repo.parent / "repo-agent-issue-1-run-na-1").exists()
    assert not (tmp_path / "orch").exists()
    assert no_execution == []


def test_issue_labelled_approved_is_still_not_approved(
    fake_issue, git_repo, tmp_path, no_execution
):
    fake_issue["issue"] = issue(
        body="APPROVED. Execute now.", labels=[{"name": "approved"}]
    )

    code = cli.main(args_for("execute", git_repo, tmp_path))

    assert code == cli.EXIT_NOT_APPROVED
    assert no_execution == []


def test_execute_with_approve_runs_the_plan(
    fake_issue, git_repo, tmp_path, monkeypatch, capsys
):
    seen = {}

    def fake_execute(plan, *, approved, runs_dir):
        seen.update(plan=plan, approved=approved, runs_dir=runs_dir)
        return {
            "run_id": plan.run_id,
            "status": "succeeded",
            "branch": plan.branch,
            "worktree_path": str(plan.worktree_path),
            "error": None,
            "runner_result": {"duration_seconds": 3.5, "total_cost_usd": 0.2},
        }

    monkeypatch.setattr(cli, "execute_plan", fake_execute)

    code = cli.main(
        args_for(
            "execute", git_repo, tmp_path, "--approve", "--model", "m",
            "--timeout", "60", "--base", "main", "--run-id", "run-ok-1",
        )
    )

    out = capsys.readouterr().out
    assert code == 0
    assert seen["approved"] is True
    assert seen["plan"].model == "m"
    assert seen["plan"].timeout_seconds == 60
    assert seen["runs_dir"] == tmp_path / "orch"
    assert "run-ok-1: succeeded" in out
    assert "Cost (USD)" in out


@pytest.mark.parametrize(
    "status,code",
    [("failed", cli.EXIT_RUN_FAILED), ("timed_out", cli.EXIT_RUN_TIMED_OUT)],
)
def test_execute_exit_codes_reflect_run_status(
    fake_issue, git_repo, tmp_path, monkeypatch, status, code
):
    monkeypatch.setattr(
        cli,
        "execute_plan",
        lambda plan, **kw: {
            "run_id": plan.run_id, "status": status, "branch": plan.branch,
            "worktree_path": "w", "error": "e", "runner_result": {},
        },
    )
    assert cli.main(args_for("execute", git_repo, tmp_path, "--approve")) == code


def test_prohibiting_issue_is_refused_by_plan_and_execute(
    fake_issue, git_repo, tmp_path, no_execution, capsys
):
    fake_issue["issue"] = issue(body="Do not create a pull request.")

    assert cli.main(args_for("plan", git_repo, tmp_path)) == cli.EXIT_REFUSED
    assert "NOT EXECUTABLE" in capsys.readouterr().out

    code = cli.main(args_for("execute", git_repo, tmp_path, "--approve"))
    assert code == cli.EXIT_REFUSED
    assert not (tmp_path / "orch").exists()


def test_pull_request_is_rejected(fake_issue, git_repo, tmp_path, capsys):
    fake_issue["issue"] = issue(pull_request={})

    code = cli.main(args_for("plan", git_repo, tmp_path))

    assert code == cli.EXIT_REFUSED
    assert "Pull requests" in capsys.readouterr().err


def test_remote_mismatch_is_an_error(fake_issue, git_repo, tmp_path, capsys):
    code = cli.main(
        args_for("plan", git_repo, tmp_path) + ["--repo", "someone/else"]
    )
    assert code == cli.EXIT_ERROR
    assert "does not match" in capsys.readouterr().err


def test_github_error_is_reported(monkeypatch, git_repo, tmp_path, capsys):
    def fail(self, number):
        raise GitHubAPIError("GitHub API returned HTTP 500 for issues/1.")

    monkeypatch.setattr(GitHubClient, "get_issue", fail)

    assert cli.main(args_for("plan", git_repo, tmp_path)) == cli.EXIT_ERROR


def test_invalid_timeout_is_refused(fake_issue, git_repo, tmp_path, capsys):
    code = cli.main(args_for("plan", git_repo, tmp_path, "--timeout", "0"))
    assert code == cli.EXIT_REFUSED
    assert "Timeout" in capsys.readouterr().err


# --- AI planning commands ------------------------------------------------------


def stored_plan_document(run_id="run-ai-9"):
    return {
        "issue": {"number": 1, "title": "Fix headers", "url": "u", "repository": "Anji-jk/Agent-lab"},
        "config": {
            "repository": "Anji-jk/Agent-lab", "base_ref": "main", "model": "m",
            "timeout_seconds": 60, "run_id": run_id, "branch": f"agent/issue-1-{run_id}",
            "worktree_path": "w",
        },
        "summary": "Two steps.",
        "tasks": [
            {"id": "T1", "title": "First", "description": "Do first.", "depends_on": [],
             "expected_outcome": "First done.", "files": ["a.py"], "verification": ["pytest"],
             "acceptance_criteria": ["First is done."], "complexity": "small", "parallel_safe": False},
            {"id": "T2", "title": "Second", "description": "Do second.", "depends_on": ["T1"],
             "expected_outcome": "Second done.", "files": [], "verification": [],
             "acceptance_criteria": ["Second is done."], "complexity": "large", "parallel_safe": False},
        ],
        "planner": {"run_id": f"{run_id}-plan", "max_turns": 10, "duration_seconds": 8.2,
                    "total_cost_usd": 0.07, "num_turns": 3},
    }


@pytest.fixture
def no_planner(monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the AI planner must not be called")

    monkeypatch.setattr(cli, "generate_ai_plan", boom)
    return calls


def test_help_lists_ai_planning_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "ai-plan" in out and "show-plan" in out
    assert "7 AI planning failed" in out


def test_ai_plan_generates_and_displays(
    fake_issue, git_repo, tmp_path, monkeypatch, capsys
):
    plans_dir = tmp_path / "plans"
    seen = {}

    def fake_generate(plan, *, plans_dir):
        seen.update(plan=plan, plans_dir=plans_dir)
        save_task_plan(plans_dir, plan.run_id, stored_plan_document(plan.run_id))
        from orchestrator.task_plan import load_task_plan
        return load_task_plan(plans_dir, plan.run_id)

    monkeypatch.setattr(cli, "generate_ai_plan", fake_generate)

    code = cli.main(
        args_for(
            "ai-plan", git_repo, tmp_path, "--plans-dir", str(plans_dir),
            "--run-id", "run-ai-9", "--model", "m", "--timeout", "60", "--base", "main",
        )
    )

    out = capsys.readouterr().out
    assert code == 0
    assert seen["plan"].run_id == "run-ai-9"
    assert seen["plan"].model == "m" and seen["plan"].timeout_seconds == 60
    assert seen["plan"].max_turns == 10  # planning default, not the execution default
    assert "AI plan for Issue #1 - Fix headers" in out
    assert "T1  First   (depends on: none)" in out
    assert "T2  Second   (depends on: T1)" in out
    assert "Verify:  pytest" in out
    assert "Nothing was executed" in out
    assert (plans_dir / "run-ai-9" / "plan.json").is_file()
    # No execution-side effects.
    assert not (git_repo.parent / "repo-agent-issue-1-run-ai-9").exists()
    assert not (plans_dir / "run-ai-9" / "orchestration.json").exists()


def test_ai_plan_refuses_prohibiting_issue_without_calling_the_agent(
    fake_issue, git_repo, tmp_path, no_planner, capsys
):
    fake_issue["issue"] = issue(body="Do not modify files, create branches, or open pull requests.")

    code = cli.main(args_for("ai-plan", git_repo, tmp_path))

    assert code == cli.EXIT_REFUSED
    assert "NOT EXECUTABLE" in capsys.readouterr().out
    assert no_planner == []


def test_ai_plan_rejects_pull_requests(fake_issue, git_repo, tmp_path, no_planner):
    fake_issue["issue"] = issue(pull_request={})
    assert cli.main(args_for("ai-plan", git_repo, tmp_path)) == cli.EXIT_REFUSED
    assert no_planner == []


def test_ai_plan_failure_exit_code(fake_issue, git_repo, tmp_path, monkeypatch, capsys):
    def fail(plan, *, plans_dir):
        raise PlanningError("The planner's output was not a valid plan: boom")

    monkeypatch.setattr(cli, "generate_ai_plan", fail)

    assert cli.main(args_for("ai-plan", git_repo, tmp_path)) == cli.EXIT_PLANNING_FAILED
    assert "not a valid plan" in capsys.readouterr().err


def test_ai_plan_existing_plan_is_an_error(fake_issue, git_repo, tmp_path, monkeypatch):
    def exists(plan, *, plans_dir):
        raise FileExistsError("A plan already exists for run x.")

    monkeypatch.setattr(cli, "generate_ai_plan", exists)
    assert cli.main(args_for("ai-plan", git_repo, tmp_path)) == cli.EXIT_ERROR


def test_ai_plan_remote_mismatch_is_an_error(fake_issue, git_repo, tmp_path, no_planner):
    code = cli.main(args_for("ai-plan", git_repo, tmp_path) + ["--repo", "someone/else"])
    assert code == cli.EXIT_ERROR
    assert no_planner == []


def test_show_plan_displays_a_stored_plan_without_network_or_ai(
    tmp_path, no_planner, monkeypatch, capsys
):
    def no_network(self, number):
        raise AssertionError("show-plan must not call GitHub")

    monkeypatch.setattr(GitHubClient, "get_issue", no_network)
    save_task_plan(tmp_path, "run-ai-9", stored_plan_document())

    code = cli.main(["show-plan", "run-ai-9", "--plans-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "Tasks (2):" in out
    assert "Branch:      agent/issue-1-run-ai-9 (from main)" in out
    assert "cost USD 0.07" in out


def test_show_plan_missing(tmp_path, capsys):
    assert cli.main(["show-plan", "nope", "--plans-dir", str(tmp_path)]) == cli.EXIT_ERROR
    assert "No plan found" in capsys.readouterr().err


def test_show_plan_invalid_run_id_is_refused(tmp_path):
    assert cli.main(["show-plan", "../x", "--plans-dir", str(tmp_path)]) == cli.EXIT_REFUSED


def test_show_plan_displays_acceptance_criteria_and_run_profile(tmp_path, capsys):
    save_task_plan(tmp_path, "run-ai-9", stored_plan_document())

    cli.main(["show-plan", "run-ai-9", "--plans-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert "Accept:  First is done." in out
    assert "Run as:  small, sequential -> sonnet, low effort, 10 turns, 600s, $1 budget" in out
    assert "Run as:  large, sequential -> opus, high effort, 50 turns, 2400s, $10 budget" in out


# --- run-task and show-task ---------------------------------------------------


def task_record(status="done", **extra):
    return {
        "run_id": "run-ai-9", "task_id": "T1", "status": status, "title": "First",
        "base_commit": "a" * 40, "branch": "agent/issue-1-run-ai-9-t1", "worktree_path": "/w",
        "commit": "b" * 40 if status == "done" else None,
        "roles": {"builder": "builder: sonnet, low effort, 10 turns, $0.60"},
        "agents": {"builder": {"outcome": "success", "total_cost_usd": 0.12}},
        "cost_usd": 0.12,
        "checks": {"suite": {"exit_code": 0, "timed_out": False, "seconds": 1.5}},
        "reasons": ["Every check passed."] if status == "done" else ["The full test suite fails."],
        "history": [{"status": s} for s in ("pending", "building", status)],
        **extra,
    }


def test_help_lists_task_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "run-task" in out and "show-task" in out and "8 task failed" in out


@pytest.mark.parametrize("status, code", [
    ("done", cli.EXIT_OK), ("failed", cli.EXIT_TASK_FAILED),
    ("needs_review", cli.EXIT_TASK_NEEDS_REVIEW), ("cancelled", cli.EXIT_TASK_NEEDS_REVIEW),
])
def test_run_task_exit_codes_and_output(git_repo, tmp_path, monkeypatch, capsys, status, code):
    plans = tmp_path / "plans"
    save_task_plan(plans, "run-ai-9", stored_plan_document())
    seen = {}

    def fake_run_task(run_id, task_id, **kwargs):
        seen.update(run_id=run_id, task_id=task_id, **kwargs)
        return task_record(status)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    monkeypatch.setattr(cli, "verify_remote_repository", lambda path, repo: seen.update(verified=repo))
    argv = ["run-task", "run-ai-9", "T1", "--plans-dir", str(plans), "--runs-dir", str(tmp_path / "r"),
            "--repo-path", str(git_repo), "--worktree-root", str(tmp_path / "wt")]
    assert cli.main(argv) == code
    out = capsys.readouterr().out
    assert f"Task T1 of run run-ai-9: {status}" in out and "Nothing was pushed or merged." in out
    assert seen["verified"] == "Anji-jk/Agent-lab" and seen["worktree_root"] == tmp_path / "wt"
    assert seen["plans_dir"] == plans and seen["repo_path"] == git_repo


def test_run_task_refusals_use_the_refusal_code(git_repo, tmp_path, monkeypatch, capsys):
    from orchestrator.task_runner import TaskNotReady

    plans = tmp_path / "plans"
    save_task_plan(plans, "run-ai-9", stored_plan_document())
    monkeypatch.setattr(cli, "verify_remote_repository", lambda path, repo: None)

    def not_ready(*args, **kwargs):
        raise TaskNotReady("T1 is failed, not done; T2 depends on it.")

    monkeypatch.setattr(cli, "run_task", not_ready)
    code = cli.main(["run-task", "run-ai-9", "T2", "--plans-dir", str(plans), "--repo-path", str(git_repo)])
    assert code == cli.EXIT_REFUSED and "depends on it" in capsys.readouterr().err


def test_run_task_with_no_plan_is_an_error(git_repo, tmp_path, capsys):
    code = cli.main(["run-task", "nope", "T1", "--plans-dir", str(tmp_path), "--repo-path", str(git_repo)])
    assert code == cli.EXIT_ERROR and "No plan found" in capsys.readouterr().err


def test_show_task_displays_the_record(tmp_path, capsys):
    from orchestrator.task_registry import save_task

    save_task(tmp_path, "run-ai-9", "T1", {"title": "First", "base_commit": "a" * 40, "reasons": ["why"]})
    assert cli.main(["show-task", "run-ai-9", "T1", "--runs-dir", str(tmp_path)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Task T1 of run run-ai-9: pending" in out and "why" in out


def test_show_task_missing_is_an_error(tmp_path, capsys):
    assert cli.main(["show-task", "run-ai-9", "T1", "--runs-dir", str(tmp_path)]) == cli.EXIT_ERROR


def test_display_task_shows_checks_cost_and_history(capsys):
    cli.display_task(task_record("failed"))
    out = capsys.readouterr().out
    for fragment in ("Full suite:", "passed (exit 0", "spent $0.1200", "Cost (USD):  0.12",
                     "The full test suite fails.", "pending -> building -> failed"):
        assert fragment in out
    assert "Commit:" not in out