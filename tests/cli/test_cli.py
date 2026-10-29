import subprocess
import sys

import pytest

import orchestrator.cli.commands as cli
from orchestrator.planning.ai_planner import PlanningError
from orchestrator.github.client import GitHubAPIError, GitHubClient
from orchestrator.planning.plan_file import save_task_plan


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
def no_planner(monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the AI planner must not be called")

    monkeypatch.setattr(cli.plan, "generate_ai_plan", boom)
    return calls


def args_for(command, git_repo, tmp_path, *extra):
    return [
        command,
        "1",
        "--repo-path",
        str(git_repo),
        *extra,
    ]


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])

    out = capsys.readouterr().out
    assert info.value.code == 0
    for command in ("init", "issue", "ai-plan", "show-plan", "run-plan", "run-task", "status",
                    "dashboard", "budget", "cancel", "report", "show-task", "board"):
        assert command in out
    assert "exit codes" in out


def test_the_old_single_agent_commands_and_events_are_gone(capsys):
    for command in ("plan", "execute", "events"):
        with pytest.raises(SystemExit) as info:
            cli.main([command, "1"])
        assert info.value.code == cli.EXIT_USAGE
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["board", "publish", "run"])


def test_module_entry_point_runs():
    result = subprocess.run(
        [sys.executable, "-m", "orchestrator", "--help"],
        capture_output=True,
        text=True,
        cwd=cli.PROJECT_ROOT,
    )
    assert result.returncode == 0
    assert "ai-plan" in result.stdout


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


def test_a_prohibiting_issue_is_refused(fake_issue, git_repo, tmp_path, no_planner, capsys):
    fake_issue["issue"] = issue(body="Do not create a pull request.")

    assert cli.main(args_for("ai-plan", git_repo, tmp_path)) == cli.EXIT_REFUSED
    assert "NOT EXECUTABLE" in capsys.readouterr().out


def test_pull_request_is_rejected(fake_issue, git_repo, tmp_path, no_planner, capsys):
    fake_issue["issue"] = issue(pull_request={})

    code = cli.main(args_for("ai-plan", git_repo, tmp_path))

    assert code == cli.EXIT_REFUSED
    assert "Pull requests" in capsys.readouterr().err


def test_remote_mismatch_is_an_error(fake_issue, git_repo, tmp_path, no_planner, capsys):
    code = cli.main(
        args_for("ai-plan", git_repo, tmp_path) + ["--repo", "someone/else"]
    )
    assert code == cli.EXIT_ERROR
    assert "does not match" in capsys.readouterr().err


def test_github_error_is_reported(monkeypatch, git_repo, tmp_path, no_planner, capsys):
    def fail(self, number):
        raise GitHubAPIError("GitHub API returned HTTP 500 for issues/1.")

    monkeypatch.setattr(GitHubClient, "get_issue", fail)

    assert cli.main(args_for("ai-plan", git_repo, tmp_path)) == cli.EXIT_ERROR


def test_invalid_timeout_is_refused(fake_issue, git_repo, tmp_path, no_planner, capsys):
    code = cli.main(args_for("ai-plan", git_repo, tmp_path, "--timeout", "0"))
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
        from orchestrator.planning.plan_file import load_task_plan
        return load_task_plan(plans_dir, plan.run_id)

    monkeypatch.setattr(cli.plan, "generate_ai_plan", fake_generate)

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

    monkeypatch.setattr(cli.plan, "generate_ai_plan", fail)

    assert cli.main(args_for("ai-plan", git_repo, tmp_path)) == cli.EXIT_PLANNING_FAILED
    assert "not a valid plan" in capsys.readouterr().err


def test_ai_plan_existing_plan_is_an_error(fake_issue, git_repo, tmp_path, monkeypatch):
    def exists(plan, *, plans_dir):
        raise FileExistsError("A plan already exists for run x.")

    monkeypatch.setattr(cli.plan, "generate_ai_plan", exists)
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

    monkeypatch.setattr(cli.run, "run_task", fake_run_task)
    monkeypatch.setattr(cli.run, "verify_remote_repository", lambda path, repo: seen.update(verified=repo))
    argv = ["run-task", "run-ai-9", "T1", "--plans-dir", str(plans), "--runs-dir", str(tmp_path / "r"),
            "--repo-path", str(git_repo), "--worktree-root", str(tmp_path / "wt")]
    assert cli.main(argv) == code
    out = capsys.readouterr().out
    assert f"Task T1 of run run-ai-9: {status}" in out and "Nothing was pushed, and your own branches were not changed." in out
    assert seen["verified"] == "Anji-jk/Agent-lab" and seen["worktree_root"] == tmp_path / "wt"
    assert seen["plans_dir"] == plans and seen["repo_path"] == git_repo


def test_run_task_refusals_use_the_refusal_code(git_repo, tmp_path, monkeypatch, capsys):
    from orchestrator.execution.task_runner import TaskNotReady

    plans = tmp_path / "plans"
    save_task_plan(plans, "run-ai-9", stored_plan_document())
    monkeypatch.setattr(cli.run, "verify_remote_repository", lambda path, repo: None)

    def not_ready(*args, **kwargs):
        raise TaskNotReady("T1 is failed, not done; T2 depends on it.")

    monkeypatch.setattr(cli.run, "run_task", not_ready)
    code = cli.main(["run-task", "run-ai-9", "T2", "--plans-dir", str(plans), "--repo-path", str(git_repo)])
    assert code == cli.EXIT_REFUSED and "depends on it" in capsys.readouterr().err


def test_run_task_with_no_plan_is_an_error(git_repo, tmp_path, capsys):
    code = cli.main(["run-task", "nope", "T1", "--plans-dir", str(tmp_path), "--repo-path", str(git_repo)])
    assert code == cli.EXIT_ERROR and "No plan found" in capsys.readouterr().err


def test_show_task_displays_the_record(tmp_path, capsys):
    from orchestrator.records.task_registry import save_task

    save_task(tmp_path, "run-ai-9", "T1", {"title": "First", "base_commit": "a" * 40, "reasons": ["why"]})
    assert cli.main(["show-task", "run-ai-9", "T1", "--runs-dir", str(tmp_path)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Task T1 of run run-ai-9: pending" in out and "why" in out


def test_show_task_missing_is_an_error(tmp_path, capsys):
    assert cli.main(["show-task", "run-ai-9", "T1", "--runs-dir", str(tmp_path)]) == cli.EXIT_ERROR


def test_display_task_shows_reviewer_notes(capsys):
    cli.display_task(task_record("done", findings={"reviewer_notes": ["tests in another file"],
                                                   "tester_notes": ["suite is slow"]}))
    out = capsys.readouterr().out
    assert "Reviewer notes (not blocking):" in out and "tests in another file" in out
    assert "Tester notes (not blocking):" in out and "suite is slow" in out


def test_display_task_shows_checks_cost_and_history(capsys):
    cli.display_task(task_record("failed"))
    out = capsys.readouterr().out
    for fragment in ("Full suite:", "passed (exit 0", "spent $0.1200", "Cost (USD):  0.12",
                     "The full test suite fails.", "pending -> building -> failed"):
        assert fragment in out
    assert "Commit:" not in out

# --- the project: found from the current folder, its files in its .agent-lab/ ---


def test_runs_and_plans_default_to_the_projects_folder(git_repo, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.task, "run_report", lambda args: seen.update(vars(args)) or 0)
    monkeypatch.chdir(git_repo)
    assert cli.main(["report", "r"]) == cli.EXIT_OK
    data = git_repo.resolve() / ".agent-lab"
    assert seen["runs_dir"] == seen["plans_dir"] == data / "runs"
    assert seen["repo_path"] == git_repo.resolve()
    assert (data / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"


def test_from_a_subfolder_the_project_is_still_its_root(git_repo, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.plan, "run_ai_plan", lambda args: seen.update(vars(args)) or 0)
    (git_repo / "src").mkdir()
    monkeypatch.chdir(git_repo / "src")
    assert cli.main(["ai-plan", "1"]) == cli.EXIT_OK
    assert seen["repo_path"] == git_repo.resolve()


def test_the_orchestrators_own_repository_is_refused_for_planning(monkeypatch, capsys):
    monkeypatch.setattr(cli.plan, "run_ai_plan", lambda args: pytest.fail("must not plan"))
    monkeypatch.chdir(cli.PROJECT_ROOT)
    assert cli.main(["ai-plan", "1"]) == cli.EXIT_REFUSED
    assert "orchestrator's own repository" in capsys.readouterr().err
    assert cli.main(["ai-plan", "1", "--repo-path", str(cli.PROJECT_ROOT)]) == cli.EXIT_REFUSED


def test_folders_given_on_the_command_line_need_no_project(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.task, "run_report", lambda args: seen.update(vars(args)) or 0)
    monkeypatch.chdir(tmp_path)  # not a Git repository
    assert cli.main(["report", "r", "--runs-dir", str(tmp_path / "runs")]) == cli.EXIT_OK
    assert seen["plans_dir"] == tmp_path / "runs"  # plans are kept with the runs


def test_the_projects_toml_names_the_repository(git_repo, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.plan, "run_ai_plan", lambda args: seen.update(vars(args)) or 0)
    (git_repo / ".agent-lab.toml").write_text('repository = "me/ledger"\n', encoding="utf-8")
    assert cli.main(["ai-plan", "1", "--repo-path", str(git_repo)]) == cli.EXIT_OK
    assert seen["repo"] == "me/ledger"


def test_init_sets_up_a_project_and_only_writes_what_is_missing(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    assert cli.main(["init"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    data = git_repo / ".agent-lab"
    assert "GITHUB_TOKEN=" in (data / ".env").read_text(encoding="utf-8")
    assert 'repository = "Anji-jk/Agent-lab"' in (git_repo / ".agent-lab.toml").read_text(encoding="utf-8")
    assert ".agent-lab/.env" in out and "commit it" in out
    # the toml is the only new thing Git sees: the folder and its .env are ignored
    assert subprocess.run(["git", "-C", str(git_repo), "status", "--porcelain"], capture_output=True,
                          text=True, check=True).stdout.strip() == "?? .agent-lab.toml"

    (data / ".env").write_text("GITHUB_TOKEN=mine\n", encoding="utf-8")
    assert cli.main(["init"]) == cli.EXIT_OK
    assert "nothing to do" in capsys.readouterr().out
    assert (data / ".env").read_text(encoding="utf-8") == "GITHUB_TOKEN=mine\n"


def test_init_is_refused_in_the_orchestrators_own_repository(monkeypatch, capsys):
    monkeypatch.chdir(cli.PROJECT_ROOT)
    assert cli.main(["init"]) == cli.EXIT_REFUSED
    assert not (cli.PROJECT_ROOT / ".agent-lab").exists()


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/me/ledger-cli.git", "me/ledger-cli"),
    ("https://github.com/me/ledger-cli", "me/ledger-cli"),
    ("git@github.com:me/ledger-cli.git", "me/ledger-cli"),
    ("ssh://git@github.com/me/ledger-cli.git", "me/ledger-cli"),
    ("https://gitlab.com/me/ledger-cli.git", None),
])
def test_the_repository_is_read_from_origin(git_repo, url, expected):
    from orchestrator.core.project import repository_from_origin
    subprocess.run(["git", "-C", str(git_repo), "remote", "set-url", "origin", url], check=True)
    assert repository_from_origin(git_repo) == expected
