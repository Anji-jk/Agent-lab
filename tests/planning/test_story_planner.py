"""jira plan's planner with a fake planning agent."""

import json

import pytest

from conftest import git
from orchestrator.planning.ai_planner import PlanningError
from orchestrator.planning.plan_file import load_task_plan, plan_path
from orchestrator.planning.story_planner import build_story_planner_prompt, generate_story_plan
from story_samples import ANSWER, answer, story, work_item
from test_ai_planner import FakePlanner

RUN = "run-story-1"


@pytest.fixture
def runner_script(tmp_path):
    script = tmp_path / "project" / "scripts" / "claude_runner.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stand-in", encoding="utf-8")
    return script


def plan(git_repo, tmp_path, runner_script, planner, the_story=None, **options):
    return generate_story_plan(the_story or story(), git_repo, plans_dir=tmp_path / "plans",
                               docs_dir="docs", model="m", timeout_seconds=60, max_turns=6,
                               run_id=RUN, runner_script=runner_script, invoke_runner=planner,
                               **options)


def test_a_story_plan_is_stored_waiting_for_approval(git_repo, tmp_path, runner_script):
    planner = FakePlanner(json.dumps(ANSWER))
    record = plan(git_repo, tmp_path, runner_script, planner)
    assert record == load_task_plan(tmp_path / "plans", RUN)
    assert plan_path(tmp_path / "plans", RUN).is_file()
    assert record["source"] == {"kind": "jira", "key": "KAN-10",
                                "url": "https://example.atlassian.net/browse/KAN-10",
                                "epic": {"key": "KAN-9", "summary": "Reports"}}
    assert record["issue"]["key"] == "KAN-10" and record["issue"]["number"] is None
    config = record["config"]
    assert config["branch"] == f"agent/KAN-10-{RUN}"
    assert config["base_commit"] == git(git_repo, "rev-parse", "HEAD")
    assert config["repository"] is None and config["docs_dir"] == "docs"
    assert [task["jira_key"] for task in record["tasks"]] == ["KAN-11", "KAN-12", "KAN-13"]
    assert record["approval"] == {"status": "waiting", "jira_links": [
        {"subtask": "KAN-13", "blocked_by": "KAN-12",
         "reason": "It documents the command KAN-12 adds."}]}
    assert record["skipped_subtasks"] == [{"key": "KAN-14", "status": "Done"}]
    assert record["planner"]["run_id"] == f"{RUN}-plan"
    assert record["planner"]["total_cost_usd"] == 0.05


def test_planner_is_read_only_and_receives_the_story(git_repo, tmp_path, runner_script):
    planner = FakePlanner(json.dumps(ANSWER))
    plan(git_repo, tmp_path, runner_script, planner)
    (call,) = planner.calls
    assert call["read_only"] is True and call["role"] == "planner"
    assert call["run_id"] == f"{RUN}-plan" and call["model"] == "m"
    (prompt,) = planner.prompts
    assert "--- BEGIN STORY KAN-10 ---" in prompt and "Epic: KAN-9 Reports" in prompt
    assert "### KAN-12  Add the export command" in prompt
    assert "Blocked by in Jira: KAN-11" in prompt
    assert "Commas in names are quoted." in prompt
    assert "- KAN-14  Old spike (Done)" in prompt
    assert "Docs folder: docs/" in prompt and "README.md" in prompt


def test_missing_criteria_refuses_before_planner_runs(git_repo, tmp_path, runner_script):
    planner = FakePlanner(json.dumps(ANSWER))
    bare = story(work_item("KAN-11", "a"), work_item("KAN-12", "b", criteria=""))
    with pytest.raises(PlanningError, match="No acceptance criteria in KAN-12"):
        plan(git_repo, tmp_path, runner_script, planner, bare)
    assert planner.calls == []


def test_bad_answer_is_not_stored(git_repo, tmp_path, runner_script):
    planner = FakePlanner(json.dumps(answer(KAN_11={"key": "KAN-99"})))
    with pytest.raises(PlanningError, match="does not fit the story: .*KAN-99"):
        plan(git_repo, tmp_path, runner_script, planner)
    assert not plan_path(tmp_path / "plans", RUN).exists()


def test_dirty_or_changed_checkout_is_refused(git_repo, tmp_path, runner_script):
    (git_repo / "new.txt").write_text("x")
    with pytest.raises(PlanningError, match="uncommitted"):
        plan(git_repo, tmp_path, runner_script, FakePlanner(json.dumps(ANSWER)))
    (git_repo / "new.txt").unlink()

    def writes(_call):
        (git_repo / "planted.txt").write_text("x")

    with pytest.raises(PlanningError, match="Git state changed"):
        plan(git_repo, tmp_path, runner_script,
             FakePlanner(json.dumps(ANSWER), side_effect=writes))
    assert not plan_path(tmp_path / "plans", RUN).exists()


def test_failed_planner_and_non_json_are_refused(git_repo, tmp_path, runner_script):
    with pytest.raises(PlanningError, match="Planner timeout"):
        plan(git_repo, tmp_path, runner_script, FakePlanner("", outcome="timeout"))
    with pytest.raises(PlanningError, match="did not contain a JSON object"):
        plan(git_repo, tmp_path, runner_script, FakePlanner("no json here"))


def test_prompt_without_docs_folder():
    prompt = build_story_planner_prompt(story(), base_ref="main", docs_dir=None, context="a.py")
    assert "Docs folder: (none: this project has no docs folder)" in prompt
