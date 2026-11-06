"""run-plan writes the feature doc once every task is done; a problem with it fails nothing."""

import json

from orchestrator.execution.integration import load_integration
from orchestrator.execution.runner_adapter import RunnerOutcome
from run_plan_support import per_task, work
from task_runner_support import RUN_ID, git  # pyright: ignore[reportMissingImports]

ANSWER = {"what_it_does": "Adds double and triple.", "how_to_use": "`double(2)`",
          "how_it_works": "In calc.py.", "notes": []}


def with_docs(world):
    (world["repo"] / ".agent-lab.toml").write_text('docs_dir = "docs"\n', encoding="utf-8")


def with_documenter(agents, answer=ANSWER):
    tasks, seen = per_task(agents), []

    def invoke(**kwargs):
        if kwargs.get("role") != "documenter":
            return tasks(**kwargs)
        seen.append(kwargs)
        if answer is None:
            return RunnerOutcome("timeout", None, "too slow", {})
        return RunnerOutcome("success", 0, None, {"status": "succeeded", "total_cost_usd": 0.1,
                                                  "result_text": json.dumps(answer)})
    return invoke, seen


def test_the_doc_is_written_when_the_plan_completes(world, agents):
    with_docs(world)
    invoke, seen = with_documenter(agents)
    result = work(world, invoke)
    assert result.complete and result.doc is not None and result.doc_problem is None
    assert result.doc.path == "docs/issue-7-add-helpers.md" and len(seen) == 1
    branch = load_integration(world["runs"], RUN_ID)["branch"]
    assert "Adds double and triple." in git(world["repo"], "show", f"{branch}:{result.doc.path}")

    again = work(world, invoke)  # a complete plan, run again: the doc is not written twice
    assert again.complete and again.doc.skipped.startswith("already written") and len(seen) == 1


def test_no_docs_folder_means_no_doc_and_no_documenter(world, agents):
    invoke, seen = with_documenter(agents)
    result = work(world, invoke)
    assert result.complete and "no docs folder" in result.doc.skipped and seen == []


def test_a_documenter_that_fails_does_not_fail_the_plan(world, agents):
    with_docs(world)
    invoke, seen = with_documenter(agents, answer=None)
    result = work(world, invoke)
    assert result.complete and len(seen) == 2
    assert "timeout" in result.doc.overview_missing
