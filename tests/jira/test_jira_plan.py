"""jira plan, show-plan and run-plan on a story plan."""

import functools
import json
import sys

import pytest

import orchestrator.cli.commands as cli
from orchestrator.cli.commands import jira as jira_command
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.planning.story_planner import generate_story_plan
from orchestrator.records import project_db as pdb
from test_jira_check import git
from test_jira_story import SITE, FakeJira

RUN = "run-20261104-120000-abc123"


def entry(key, **more):
    return {"key": key, "depends_on": [], "dependency_reasons": {}, "files": [f"{key}.py"],
            "complexity": "small", "parallel_safe": False, "expected_outcome": "done",
            "verification": [], "doc_target": None, **more}


ANSWER = {"summary": "UI, backend, then status.", "tasks": [
    entry("SKM-10"),
    entry("SKM-11", depends_on=["SKM-10"],
          dependency_reasons={"SKM-10": "The endpoints serve the screen SKM-10 designs."}),
    entry("SKM-12", depends_on=["SKM-11"]),
]}


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


class FakePlanner:
    def __init__(self, text):
        self.text, self.calls = text, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return RunnerOutcome("success", 0, None, {"status": "succeeded", "total_cost_usd": 0.3,
                                                  "num_turns": 5, "result_text": self.text})


def setup_project(repo, monkeypatch, tmp_path, answer=ANSWER):
    (repo / ".agent-lab.toml").write_text(
        f'jira_url = "{SITE}"\njira_project = "SKM"\n'
        f'test_command = ["{sys.executable.replace(chr(92), "/")}", "-c", "pass"]\n',
        encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "config")
    monkeypatch.setenv("JIRA_EMAIL", "a@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-token")
    monkeypatch.setattr(jira_command, "JiraClient", lambda url, email, token: FakeJira())
    script = tmp_path / "runner" / "claude_runner.py"
    script.parent.mkdir()
    script.write_text("# stand-in", encoding="utf-8")
    planner = FakePlanner(json.dumps(answer))
    monkeypatch.setattr(jira_command, "generate_story_plan", functools.partial(
        generate_story_plan, runner_script=script, invoke_runner=planner))
    return planner


def test_jira_plan_saves_displays_and_refuses_execution(repo, monkeypatch, tmp_path, capsys):
    planner = setup_project(repo, monkeypatch, tmp_path)
    assert cli.main(["jira", "plan", "SKM-9", "--run-id", RUN,
                     "--repo-path", str(repo)]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert f"Planning story SKM-9 with a read-only AI agent (run {RUN})" in captured.err
    out = captured.out
    assert out.startswith("Plan for story SKM-9 - Employee Enrolment Management")
    assert "T1  SKM-10  Design Enrolment UI and Batch Selection" in out
    assert "Jira changes after approval (1):\n  + SKM-11 is blocked by SKM-10" in out
    assert "Nothing was written to Jira, and nothing was run." in out
    assert "secret-token" not in out + captured.err
    assert planner.calls[0]["read_only"] is True and planner.calls[0]["model"] == "opus"

    plan_file = repo / ".agent-lab" / "runs" / RUN / "plan.json"
    import json as json_module
    stored = json_module.loads(plan_file.read_text(encoding="utf-8"))
    assert stored["approval"]["status"] == "waiting"
    (run,) = pdb.query(pdb.db_path(plan_file.parents[1]), "SELECT * FROM runs")
    assert (run["item"], run["status"], run["tasks_total"]) == ("SKM-9", "planned", 3)

    assert cli.main(["show-plan", RUN, "--repo-path", str(repo)]) == cli.EXIT_OK
    assert "Status:      waiting for approval" in capsys.readouterr().out
    assert cli.main(["run-plan", RUN, "--repo-path", str(repo)]) == cli.EXIT_REFUSED
    assert "is waiting for approval" in capsys.readouterr().err
    assert cli.main(["run-task", RUN, "T1", "--repo-path", str(repo)]) == cli.EXIT_REFUSED


def test_a_plan_that_does_not_fit_is_refused(repo, monkeypatch, tmp_path, capsys):
    bad = {"tasks": [entry("SKM-10"), entry("SKM-12")]}
    setup_project(repo, monkeypatch, tmp_path, bad)
    assert cli.main(["jira", "plan", "SKM-9", "--run-id", RUN,
                     "--repo-path", str(repo)]) == cli.EXIT_PLANNING_FAILED
    assert "SKM-11 has no entry in the planner's answer" in capsys.readouterr().err
    assert not (repo / ".agent-lab" / "runs" / RUN / "plan.json").exists()


def test_the_base_comes_from_base_branch(repo, monkeypatch, tmp_path, capsys):
    planner = setup_project(repo, monkeypatch, tmp_path)
    with (repo / ".agent-lab.toml").open("a", encoding="utf-8") as config:
        config.write('base_branch = "dev"\n')
    git(repo, "commit", "-q", "-am", "base dev")
    git(repo, "checkout", "-q", "-b", "dev")
    assert cli.main(["jira", "plan", "SKM-9", "--run-id", RUN,
                     "--repo-path", str(repo)]) == cli.EXIT_OK
    assert "(from dev at " in capsys.readouterr().out
    assert len(planner.calls) == 1

    # --base still wins: main is not checked out, so planning from it is refused
    assert cli.main(["jira", "check", "SKM-9", "--base", "main", "--skip-tests",
                     "--repo-path", str(repo)]) == cli.EXIT_REFUSED
    assert "FAIL On main" in capsys.readouterr().out
    assert cli.main(["jira", "check", "SKM-9", "--skip-tests",
                     "--repo-path", str(repo)]) == cli.EXIT_OK
    assert "ok   On dev" in capsys.readouterr().out
