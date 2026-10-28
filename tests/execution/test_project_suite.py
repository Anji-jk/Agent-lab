"""M7.8: a task's suite runs the project's tests, not the orchestrator's own."""

from pathlib import Path

from orchestrator.execution import checks as tc
from orchestrator.execution.integration import describe_merge
from task_runner_support import RUN_ID, orchestrator_steps, run  # pyright: ignore[reportMissingImports]

PYTEST = ("python", "-m", "pytest", "-q")


def tree(tmp_path: Path, *folders: str) -> Path:
    for folder in folders:
        (tmp_path / folder).mkdir(parents=True)
    return tmp_path


def test_project_work_leaves_out_the_orchestrators_test_folders(tmp_path):
    root = tree(tmp_path, "tests/src", "tests/cli", "tests/scheduling")
    extra, scope = tc.suite_scope(root, ["src/csv_summary.py", "tests/src/test_csv_summary.py"])
    assert scope == "project"
    assert extra == ["--ignore=tests/cli", "--ignore=tests/scheduling"]  # only folders that exist


def test_work_on_the_orchestrator_or_a_shared_file_runs_everything(tmp_path):
    root = tree(tmp_path, "tests/cli")
    for touched in (["orchestrator/execution/checks.py"], ["scripts/claude_runner.py"],
                    ["tests/cli/test_cli.py"], ["pyproject.toml"], ["tests/conftest.py"],
                    ["src/a.py", "orchestrator/errors.py"]):
        assert tc.suite_scope(root, touched) == ([], "whole"), touched


def test_unknown_changes_run_everything(tmp_path):
    assert tc.suite_scope(tree(tmp_path, "tests/cli"), None) == ([], "whole")


def test_the_suite_adds_the_ignores_and_says_which_suite_it_was(tmp_path, monkeypatch):
    root = tree(tmp_path, "tests/cli")
    seen = {}

    def fake(command, cwd, *, timeout, env=None):
        seen["command"] = tuple(command)
        return tc.CommandResult(tuple(command), 0, False, "1 passed", 0.5)

    monkeypatch.setattr(tc, "run_command", fake)
    result = tc.run_suite(root, command=PYTEST, timeout=60, touched=["src/a.py"])
    assert seen["command"] == (*PYTEST, "--ignore=tests/cli")
    assert result.passed and result.scope == "project" and result.summary()["scope"] == "project"
    whole = tc.run_suite(root, command=PYTEST, timeout=60, touched=["orchestrator/x.py"])
    assert seen["command"] == PYTEST and whole.scope == "whole"


def test_a_command_that_is_not_pytest_runs_as_given(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(tc, "run_command", lambda command, cwd, **kw: seen.setdefault(
        "result", tc.CommandResult(tuple(command), 0, False, "", 0.1)))
    result = tc.run_suite(tree(tmp_path, "tests/cli"), command=("make", "test"), timeout=60,
                          touched=["src/a.py"])
    assert result.command == ("make", "test") and result.scope is None
    assert "scope" not in result.summary()


def test_a_task_records_that_its_suite_was_the_projects(world, agents):
    record = run(world, agents)
    assert record["status"] == "done", record["reasons"]
    assert record["checks"]["suite"]["scope"] == "project"
    started = [e for e in orchestrator_steps(world) if e["step"] == "suite_started"]
    assert started and started[0]["scope"] == "project"
    assert RUN_ID


def test_the_merge_line_names_the_suite_it_ran():
    project = {"kind": "merge", "suite": {"seconds": 30, "scope": "project"}}
    assert describe_merge(project) == (
        "merged: real merge, the project's tests passed on the merged result in 30 s")
    failed = {"problem": "suite", "suite": {"scope": "project"}}
    assert describe_merge(failed) == "not merged: the project's tests failed on the merged result"
    old = {"kind": "merge", "suite": {"seconds": 30}}  # a record from before M7.8
    assert "the full suite passed" in str(describe_merge(old))
