"""The checks before a story is planned. A fake Jira and a small Git repository stand in."""

import os
import subprocess
import sys

import pytest

import orchestrator.cli.commands as cli
from orchestrator.cli.commands import jira as jira_command
from orchestrator.core.project_config import ProjectConfig
from orchestrator.execution import story_check as sc
from orchestrator.jira import story as js
from test_jira_story import SITE, STORY, SUBTASKS, FakeJira, adf, blocked_by, item

PASS = (sys.executable, "-c", "print('3 passed')")
FAIL = (sys.executable, "-c", "print('1 failed'); raise SystemExit(1)")
NONE = (sys.executable, "-c", "raise SystemExit(5)")


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "CLAUDE.md").write_text("# P\n\n## Project structure\nx\n\n## Commands\nx\n\n"
                                    "## Rules\nx\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def story(subtasks=SUBTASKS, story_item=STORY):
    return js.read_story(FakeJira(story=story_item, subtasks=subtasks), story_item["key"], "SKM")


def by_what(result):
    return {check.what: check for check in result.checks}


def run(repo, the_story=None, command=PASS, **options):
    return sc.run_story_checks(the_story or story(), repo, ProjectConfig(test_command=command),
                               env=dict(os.environ), **options)


def test_a_ready_story_and_project(repo):
    result = run(repo)
    assert result.ready, [check for check in result.checks if check.status != "ok"]
    checks = by_what(result)
    assert checks["SKM-9 has 3 sub-tasks"].status == "ok"
    assert checks["Every open sub-task has acceptance criteria"].status == "ok"
    assert checks["Jira's Blocks links do not loop"].detail == "1 link between the sub-tasks"
    assert checks["The project's tests pass on the base branch"].status == "ok"
    assert "Ready to plan." in sc.format_story_check(result)


def test_missing_criteria_fail_and_name_the_sub_tasks(repo):
    subtasks = [*SUBTASKS[:2], item("SKM-12", "x", description=adf("Description: no criteria"))]
    result = run(repo, story(subtasks))
    failed = [check.what for check in result.failed]
    assert failed == ["Every open sub-task has acceptance criteria"]
    assert "Missing in SKM-12" in result.failed[0].detail
    text = sc.format_story_check(result)
    assert "FAIL Every open sub-task has acceptance criteria" in text
    assert "1 check failed; nothing was planned." in text


def test_done_sub_tasks_are_left_out(repo):
    others = [subtask for subtask in SUBTASKS if subtask["key"] != "SKM-10"]
    subtasks = [item("SKM-10", "Design", status=("Done", "done")), *others]
    result = run(repo, story(subtasks))
    assert result.ready
    assert by_what(result)["2 open"].detail == "Done in Jira and left out: SKM-10"


def test_nothing_open_or_no_sub_tasks_fails(repo):
    done = [item("SKM-10", "x", status=("Done", "done"))]
    assert [check.what for check in run(repo, story(done)).failed] == [
        "Some sub-tasks are still open"]
    lonely = item("SKM-30", "Lonely", kind="Story", subtask=False, level=0, subtasks=[])
    assert [check.what for check in run(repo, story([], lonely)).failed] == ["SKM-30 has sub-tasks"]


def test_text_that_forbids_the_work_fails(repo):
    subtasks = [item("SKM-10", "x", description=adf("Do not modify any files.",
                                                    "Acceptance Criteria: y"))]
    failed = run(repo, story(subtasks)).failed
    assert failed[0].what == "The text does not forbid the work"
    assert "SKM-10 forbids modifications" in failed[0].detail


def test_looping_links_fail(repo):
    subtasks = [item("SKM-10", "a", description=adf("Acceptance Criteria: y"),
                     links=[blocked_by("SKM-11")]),
                item("SKM-11", "b", description=adf("Acceptance Criteria: y"),
                     links=[blocked_by("SKM-10")])]
    failed = run(repo, story(subtasks)).failed
    assert failed[0].what == "Jira's Blocks links do not loop"
    assert "SKM-10, SKM-11" in failed[0].detail


def test_loop_report_excludes_items_merely_waiting_on_a_cycle(repo):
    subtasks = [
        item("SKM-10", "a", description=adf("Acceptance Criteria: y"),
             links=[blocked_by("SKM-11")]),
        item("SKM-11", "b", description=adf("Acceptance Criteria: y"),
             links=[blocked_by("SKM-10")]),
        item("SKM-12", "c", description=adf("Acceptance Criteria: y"),
             links=[blocked_by("SKM-10")]),
    ]
    failed = run(repo, story(subtasks)).failed
    assert failed[0].what == "Jira's Blocks links do not loop"
    assert "SKM-10, SKM-11" in failed[0].detail
    assert "SKM-12" not in failed[0].detail


def test_uncommitted_changes_fail_and_skip_the_tests(repo):
    (repo / "new.py").write_text("x = 1\n")
    result = run(repo)
    assert [check.what for check in result.failed] == ["No uncommitted changes"]
    assert "first: new.py" in result.failed[0].detail
    assert by_what(result)["The project's tests pass on the base branch"].status == "skipped"


def test_a_changed_file_is_named_in_full(repo):
    (repo / "CLAUDE.md").write_text("changed\n", encoding="utf-8")
    assert "(first: CLAUDE.md)" in run(repo).failed[0].detail


def test_not_on_the_base_branch_fails(repo):
    git(repo, "checkout", "-q", "-b", "other")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a")
    assert [check.what for check in run(repo).failed] == ["On main"]
    assert run(repo, base_ref="other").ready
    missing = run(repo, base_ref="nope").failed
    assert [check.what for check in missing] == ["The base nope exists"]


def test_a_detached_head_is_not_the_named_base_branch(repo):
    git(repo, "checkout", "-q", "--detach", "HEAD")
    assert [check.what for check in run(repo).failed] == ["On main"]


def test_invalid_base_refs_are_reported_without_invoking_git_with_them(repo, monkeypatch):
    result = run(repo, base_ref="--upload-pack=bad", run_tests=False)
    assert [check.what for check in result.failed] == ["The base reference is valid"]
    assert by_what(result)["The project's tests pass on the base branch"].status == "skipped"


def test_failing_tests_fail(repo):
    result = run(repo, command=FAIL)
    assert [check.what for check in result.failed] == [
        "The project's tests pass on the base branch"]
    assert "exit code 1: 1 failed" in result.failed[0].detail


def test_no_tests_is_only_a_warning(repo):
    pytest_none = (NONE[0], "-m", "pytest", "-q", "--co", "-k", "nothing_matches_this_name")
    result = run(repo, command=pytest_none)
    assert result.ready
    assert by_what(result)["The project has tests"].status == "warning"


def test_tests_can_be_skipped(repo):
    result = run(repo, command=FAIL, run_tests=False)
    assert result.ready
    assert by_what(result)["The project's tests pass on the base branch"].status == "skipped"


def test_missing_claude_md_sections_are_warnings_only(repo):
    (repo / "CLAUDE.md").write_text("# P\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "short")
    result = run(repo)
    assert result.ready
    warnings = [check for check in result.checks if check.status == "warning"]
    assert len(warnings) == 4 and all(check.group == "Project" for check in warnings)
    assert "--   no Project structure section" in sc.format_story_check(result)


def test_jira_check_command(repo, monkeypatch, capsys):
    (repo / ".agent-lab.toml").write_text(
        f'jira_url = "{SITE}"\njira_project = "SKM"\n'
        f'test_command = ["{sys.executable.replace(chr(92), "/")}", "-c", "pass"]\n',
        encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "config")
    monkeypatch.setenv("JIRA_EMAIL", "a@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-token")
    monkeypatch.setattr(jira_command, "JiraClient", lambda url, email, token: FakeJira())

    assert cli.main(["jira", "check", "SKM-9", "--repo-path", str(repo)]) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert output.startswith("Checks before planning SKM-9")
    assert "Ready to plan." in output and "secret-token" not in output

    (repo / "dirty.txt").write_text("x")
    assert cli.main(["jira", "check", "SKM-9", "--repo-path", str(repo)]) == cli.EXIT_REFUSED
    assert "FAIL No uncommitted changes" in capsys.readouterr().out
