"""AI planning with a fake planning agent: Claude is never launched."""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import git
from orchestrator import runner_adapter
from orchestrator.ai_planner import (
    PlanningError,
    build_planner_prompt,
    extract_acceptance_criteria,
    generate_ai_plan,
    repository_context,
)
from orchestrator.executor import ExecutionRefused
from orchestrator.planning import build_plan
from orchestrator.runner_adapter import RunnerOutcome
from orchestrator.task_plan import load_task_plan, plan_path
from orchestrator.tasks import Task

GOOD_PLAN = {
    "summary": "Add a check, then test it.",
    "tasks": [
        {
            "id": "T1",
            "title": "Add header check",
            "description": "Validate CSV headers.",
            "depends_on": [],
            "expected_outcome": "Duplicate headers are rejected.",
            "files": ["src/csv_summary.py"],
            "verification": ["python -m pytest"],
        },
        {
            "id": "T2",
            "title": "Test it",
            "description": "Add tests.",
            "depends_on": ["T1"],
            "expected_outcome": "Tests cover duplicates.",
            "files": ["tests/test_csv_summary.py"],
            "verification": [],
        },
    ],
}

BODY = """Make the summarizer reject bad CSVs.

## Acceptance criteria
- [ ] Duplicate headers raise an error
- [x] Existing tests still pass
1. Error message names the column

## Notes
- not a criterion
"""


def make_task(body=BODY, number=4):
    return Task.from_issue(
        "Anji-jk/Agent-lab",
        {
            "number": number,
            "title": "Reject duplicate headers",
            "body": body,
            "html_url": f"https://github.com/Anji-jk/Agent-lab/issues/{number}",
        },
    )


class FakePlanner:
    """Stands in for the runner; optionally misbehaves."""

    def __init__(self, text=None, outcome="success", error=None, side_effect=None):
        self.text = json.dumps(GOOD_PLAN) if text is None else text
        self.outcome = outcome
        self.error = error
        self.side_effect = side_effect
        self.calls = []
        self.prompts = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.prompts.append(Path(kwargs["prompt_file"]).read_text(encoding="utf-8"))
        if self.side_effect:
            self.side_effect(kwargs)
        result = {"status": "succeeded", "total_cost_usd": 0.05, "num_turns": 4,
                  "duration_seconds": 9.5, "result_text": self.text}
        return RunnerOutcome(self.outcome, 0, self.error, result)


@pytest.fixture
def runner_script(tmp_path):
    script = tmp_path / "project" / "scripts" / "claude_runner.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stand-in", encoding="utf-8")
    return script


@pytest.fixture
def plans_dir(tmp_path):
    return tmp_path / "plans"


@pytest.fixture
def execution_plan(git_repo):
    return build_plan(
        make_task(),
        git_repo,
        base_ref="main",
        timeout_seconds=300,
        model="claude-test-model",
        max_turns=7,
        run_id="run-ai-1",
    )


def generate(execution_plan, plans_dir, runner_script, planner):
    return generate_ai_plan(
        execution_plan,
        plans_dir=plans_dir,
        runner_script=runner_script,
        invoke_runner=planner,
    )


def repo_state(repo):
    return {
        "status": git(repo, "status", "--porcelain"),
        "head": git(repo, "rev-parse", "HEAD"),
        "branches": git(repo, "branch", "--list"),
        "worktrees": git(repo, "worktree", "list", "--porcelain"),
    }


# --- success -----------------------------------------------------------------


def test_successful_plan_is_stored_and_returned(
    execution_plan, plans_dir, runner_script
):
    planner = FakePlanner()

    record = generate(execution_plan, plans_dir, runner_script, planner)

    assert [t["id"] for t in record["tasks"]] == ["T1", "T2"]
    assert record["tasks"][1]["depends_on"] == ["T1"]
    assert record["summary"] == GOOD_PLAN["summary"]
    assert record["issue"]["number"] == 4
    assert load_task_plan(plans_dir, "run-ai-1") == record
    assert plan_path(plans_dir, "run-ai-1").is_file()


def test_execution_configuration_is_preserved(execution_plan, plans_dir, runner_script):
    record = generate(execution_plan, plans_dir, runner_script, FakePlanner())

    assert record["config"] == {
        "repository": "Anji-jk/Agent-lab",
        "base_ref": "main",
        "base_commit": git(execution_plan.repository_root, "rev-parse", "HEAD"),
        "model": "claude-test-model",
        "timeout_seconds": 300,
        "run_id": "run-ai-1",
        "branch": "agent/issue-4-run-ai-1",
        "worktree_path": str(execution_plan.worktree_path),
    }
    assert record["run_id"] == "run-ai-1"
    assert record["planner"]["max_turns"] == 7
    assert record["planner"]["total_cost_usd"] == 0.05
    assert "result_text" not in record["planner"]


def test_planner_is_launched_read_only_in_the_main_checkout(
    execution_plan, plans_dir, runner_script, git_repo
):
    planner = FakePlanner()
    generate(execution_plan, plans_dir, runner_script, planner)

    (call,) = planner.calls
    assert call["run_id"] == "run-ai-1-plan"  # distinct from the execution run dir
    assert call["worktree_path"] == git_repo.resolve()
    assert call["model"] == "claude-test-model"
    assert call["timeout_seconds"] == 300
    assert call["max_turns"] == 7
    assert call["keep_result_text"] is True
    assert call["read_only"] is True
    assert call["runner_script"] == runner_script


def test_prompt_contains_issue_criteria_and_context(
    execution_plan, plans_dir, runner_script
):
    planner = FakePlanner()
    generate(execution_plan, plans_dir, runner_script, planner)

    prompt = planner.prompts[0]
    assert "Reject duplicate headers" in prompt
    assert "Make the summarizer reject bad CSVs." in prompt
    assert "- Duplicate headers raise an error" in prompt
    assert "- Error message names the column" in prompt
    assert "README.md" in prompt  # repository context
    assert "Do NOT implement anything" in prompt
    assert "untrusted" in prompt


def test_prompt_temp_file_is_removed(execution_plan, plans_dir, runner_script):
    planner = FakePlanner()
    generate(execution_plan, plans_dir, runner_script, planner)
    assert not Path(planner.calls[0]["prompt_file"]).exists()


def test_agent_may_return_prose_and_fences(execution_plan, plans_dir, runner_script):
    text = f"Plan below.\n```json\n{json.dumps(GOOD_PLAN)}\n```"
    record = generate(execution_plan, plans_dir, runner_script, FakePlanner(text))
    assert len(record["tasks"]) == 2


def test_task_count_follows_the_agent_not_a_fixed_number(
    execution_plan, plans_dir, runner_script
):
    one = {"tasks": [GOOD_PLAN["tasks"][0]]}
    record = generate(execution_plan, plans_dir, runner_script, FakePlanner(json.dumps(one)))
    assert len(record["tasks"]) == 1


# --- malformed AI output -----------------------------------------------------


@pytest.mark.parametrize(
    "text,message",
    [
        ("I cannot do that.", "did not contain a JSON object"),
        ("{broken json", "did not contain a JSON object"),
        ("", "no output"),
        ('{"summary": "x"}', "tasks"),
        ('{"tasks": []}', "tasks"),
        (json.dumps({"tasks": [{"id": "T1"}]}), "title"),
        (
            json.dumps({"tasks": [dict(GOOD_PLAN["tasks"][0], depends_on=["T9"])]}),
            "missing task",
        ),
        (
            json.dumps({"tasks": [
                dict(GOOD_PLAN["tasks"][0], id="T1", depends_on=["T2"]),
                dict(GOOD_PLAN["tasks"][0], id="T2", depends_on=["T1"]),
            ]}),
            "cycle",
        ),
        (
            json.dumps({"tasks": [dict(GOOD_PLAN["tasks"][0], files=["/etc/passwd"])]}),
            "repository-relative",
        ),
    ],
)
def test_malformed_output_stores_nothing(
    execution_plan, plans_dir, runner_script, text, message
):
    with pytest.raises(PlanningError, match=message):
        generate(execution_plan, plans_dir, runner_script, FakePlanner(text))

    assert not plans_dir.exists()


# --- planner failures --------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,message",
    [
        ("failure", "Planner failure"),
        ("timeout", "Planner timeout"),
        ("launch_error", "Planner launch error"),
    ],
)
def test_planner_failures_store_nothing(
    execution_plan, plans_dir, runner_script, outcome, message
):
    planner = FakePlanner(outcome=outcome, error="it broke")

    with pytest.raises(PlanningError, match=message) as info:
        generate(execution_plan, plans_dir, runner_script, planner)

    assert "it broke" in str(info.value)
    assert not plans_dir.exists()


# --- planning side effects ---------------------------------------------------


def test_planning_has_no_side_effects(
    execution_plan, plans_dir, runner_script, git_repo, tmp_path
):
    before = repo_state(git_repo)

    generate(execution_plan, plans_dir, runner_script, FakePlanner())

    assert repo_state(git_repo) == before
    assert not git(git_repo, "branch", "--list", "agent/*")
    assert not execution_plan.worktree_path.exists()
    # Only the plan file: no orchestration.json (run record), no prompt, no lock.
    assert sorted(p.name for p in (plans_dir / "run-ai-1").iterdir()) == ["plan.json"]
    assert sorted(p.name for p in plans_dir.iterdir()) == ["run-ai-1"]
    # No sibling worktree directory (those are named "repo-agent-...").
    assert not [p for p in git_repo.parent.iterdir() if p.name.startswith("repo-")]


def test_a_later_execution_is_not_blocked_by_the_plan(
    execution_plan, plans_dir, runner_script
):
    from orchestrator.run_registry import save_run

    generate(execution_plan, plans_dir, runner_script, FakePlanner())

    # The same run ID can still be used to create the execution record...
    save_run(plans_dir, "run-ai-1", {"status": "queued"})
    # ...and the planner's runner directory differs from the execution's.
    assert runner_adapter.runner_run_directory("run-ai-1-plan", runner_script) != (
        runner_adapter.runner_run_directory("run-ai-1", runner_script)
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda repo: (repo / "README.md").write_text("changed\n", encoding="utf-8"),
        lambda repo: (repo / "new_file.py").write_text("x", encoding="utf-8"),
        lambda repo: git(repo, "branch", "sneaky"),
        lambda repo: git(repo, "commit", "--allow-empty", "-m", "sneaky"),
    ],
    ids=["edits-file", "creates-file", "creates-branch", "commits"],
)
def test_planner_that_changes_the_repository_is_caught(
    execution_plan, plans_dir, runner_script, git_repo, mutate
):
    planner = FakePlanner(side_effect=lambda kwargs: mutate(git_repo))

    with pytest.raises(PlanningError, match="Git state changed"):
        generate(execution_plan, plans_dir, runner_script, planner)

    assert not plans_dir.exists()  # the plan is not stored


def test_planner_that_creates_a_worktree_is_caught(
    execution_plan, plans_dir, runner_script, git_repo
):
    planner = FakePlanner(
        side_effect=lambda kwargs: git(
            git_repo, "worktree", "add", "-b", "x", str(git_repo.parent / "wt-x"), "main"
        )
    )
    with pytest.raises(PlanningError, match="Git state changed"):
        generate(execution_plan, plans_dir, runner_script, planner)


# --- refusals and pre-flight (no agent launched) -----------------------------


def test_prohibiting_issue_is_refused_without_launching(
    git_repo, plans_dir, runner_script
):
    plan = build_plan(
        make_task(body="Do not modify files."), git_repo, run_id="run-ai-2"
    )
    planner = FakePlanner()

    with pytest.raises(ExecutionRefused, match="prohibits"):
        generate(plan, plans_dir, runner_script, planner)

    assert planner.calls == []
    assert not plans_dir.exists()


def test_missing_runner_script_launches_nothing(execution_plan, plans_dir, tmp_path):
    planner = FakePlanner()
    with pytest.raises(PlanningError, match="Runner script"):
        generate(execution_plan, plans_dir, tmp_path / "nope.py", planner)
    assert planner.calls == []


def test_existing_plan_is_never_overwritten(execution_plan, plans_dir, runner_script):
    generate(execution_plan, plans_dir, runner_script, FakePlanner())
    original = load_task_plan(plans_dir, "run-ai-1")
    planner = FakePlanner()

    with pytest.raises(FileExistsError, match="already exists"):
        generate(execution_plan, plans_dir, runner_script, planner)

    assert planner.calls == []
    assert load_task_plan(plans_dir, "run-ai-1") == original


def test_existing_planner_artifacts_block_a_rerun(execution_plan, plans_dir, runner_script):
    runner_adapter.runner_run_directory("run-ai-1-plan", runner_script).mkdir(parents=True)
    planner = FakePlanner()

    with pytest.raises(FileExistsError, match="Planner directory"):
        generate(execution_plan, plans_dir, runner_script, planner)

    assert planner.calls == []


# --- helpers -----------------------------------------------------------------


def test_extracts_acceptance_criteria():
    assert extract_acceptance_criteria(BODY) == [
        "Duplicate headers raise an error",
        "Existing tests still pass",
        "Error message names the column",
    ]


@pytest.mark.parametrize(
    "text",
    ["", "No criteria here.", "- a bullet but no heading", "## Notes\n- x"],
)
def test_no_acceptance_criteria(text):
    assert extract_acceptance_criteria(text) == []


@pytest.mark.parametrize(
    "heading", ["Acceptance Criteria:", "**Acceptance criteria**", "### ACCEPTANCE CRITERIA"]
)
def test_heading_variants(heading):
    assert extract_acceptance_criteria(f"Intro\n{heading}\n- one\n- two") == ["one", "two"]


def test_long_description_is_truncated(git_repo):
    plan = build_plan(make_task(body="x" * 50_000), git_repo, run_id="run-long")
    prompt = build_planner_prompt(plan, [], "ctx")

    assert "(description truncated)" in prompt
    assert len(prompt) < 20_000  # stays inside the Windows command-line limit


def test_repository_context_lists_tracked_files_and_truncates(git_repo):
    assert "README.md" in repository_context(git_repo)

    for i in range(200):
        (git_repo / f"file_{i:03}.txt").write_text("x", encoding="utf-8")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-m", "many files")
    context = repository_context(git_repo, limit=300)

    assert context.endswith("(listing truncated)")
    assert len(context) < 400


def test_repository_context_survives_git_failure(tmp_path):
    assert "unavailable" in repository_context(tmp_path / "nope")


def test_dirty_checkout_is_refused_before_launching(execution_plan, plans_dir, runner_script, git_repo):
    (git_repo / "scratch.txt").write_text("uncommitted", encoding="utf-8")
    planner = FakePlanner()
    with pytest.raises(PlanningError, match="uncommitted or untracked"):
        generate(execution_plan, plans_dir, runner_script, planner)
    assert planner.calls == []
    assert not plan_path(plans_dir, "run-ai-1").exists()


def test_checkout_not_at_base_ref_is_refused(execution_plan, plans_dir, runner_script, git_repo):
    git(git_repo, "checkout", "-b", "feature")
    (git_repo / "extra.txt").write_text("x", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "ahead of main")
    planner = FakePlanner()
    with pytest.raises(PlanningError, match="Check out main"):
        generate(execution_plan, plans_dir, runner_script, planner)
    assert planner.calls == []


def test_missing_base_ref_is_refused(git_repo, plans_dir, runner_script):
    plan = build_plan(make_task(), git_repo, base_ref="does-not-exist", run_id="run-x")
    planner = FakePlanner()
    with pytest.raises(PlanningError, match="does not exist"):
        generate(plan, plans_dir, runner_script, planner)
    assert planner.calls == []
