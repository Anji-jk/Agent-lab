"""The builder of a task is told what the tasks it depends on did, from the orchestrator's records."""

from orchestrator.execution.finished import finished_dependencies
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.planning.plan_file import load_task_plan
from task_runner_support import RUN_ID, git, run


def test_a_dependent_task_is_told_what_its_dependency_did(world, agents):
    first = run(world, agents)
    assert first["status"] == "done", first["reasons"]
    assert first["checks"]["builder_summary"] == "added double"
    assert "## Finished tasks this one depends on" not in agents.prompts["builder"]  # T1: none

    agents.use_triple()
    second = run(world, agents, "T2")
    assert second["status"] == "done", second["reasons"]
    prompt = agents.prompts["builder"]
    assert "## Finished tasks this one depends on" in prompt
    assert f"- T1 double: done, commit {first['commit'][:12]}." in prompt
    assert "  Summary: added double" in prompt
    assert "  Files: calc.py, tests/test_double.py" in prompt
    assert "## Plan" not in prompt  # an Issue task has no story plan block
    assert "--- BEGIN ISSUE #7 ---" in prompt


def test_only_finished_direct_dependencies_count(world, agents):
    plan = load_task_plan(world["plans"], RUN_ID)
    t2 = next(task for task in plan["tasks"] if task["id"] == "T2")
    assert finished_dependencies(world["runs"], RUN_ID, plan, t2) == []  # T1 has not run
    agents.fail["builder"] = RunnerOutcome("timeout", None, "too slow", {})
    assert run(world, agents)["status"] != "done"
    assert finished_dependencies(world["runs"], RUN_ID, plan, t2) == []  # T1 is not done


def test_the_retry_setting_is_kept_in_the_task_record(world, agents):
    (world["repo"] / ".agent-lab.toml").write_text("max_retries = 3\n", encoding="utf-8")
    git(world["repo"], "add", "-A")
    git(world["repo"], "commit", "-q", "-m", "retries")
    record = run(world, agents)
    assert record["max_retries"] == 3
