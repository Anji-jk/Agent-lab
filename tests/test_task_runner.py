import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import task_checks as tc
from orchestrator import task_runner as tr
from orchestrator.executor import ExecutionRefused
from orchestrator.runner_adapter import RunnerOutcome
from orchestrator.task_plan import save_task_plan
from orchestrator.task_registry import list_tasks, load_task

RUN_ID = "run1"
GOOD_DOUBLE = "def double(x):\n    return x * 2\n"
GOOD_TRIPLE = "def triple(x):\n    return x * 3\n"

TESTS_DOUBLE = (
    "from calc import double\n\n\n"
    "def test_positive():\n    assert double(2) == 4\n\n\n"
    "def test_negative():\n    assert double(-1) == -2\n"
)
TESTS_TRIPLE = "from calc import triple\n\n\ndef test_triple():\n    assert triple(2) == 6\n"


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def task(task_id, title, criteria, depends=(), files=("calc.py",)):
    return {
        "id": task_id, "title": title, "description": f"Do {title}",
        "depends_on": list(depends), "expected_outcome": "it works", "files": list(files),
        "verification": [], "acceptance_criteria": list(criteria),
        "complexity": "small", "parallel_safe": False,
    }


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "conftest.py").write_text("")
    # Like the real repository: test runs create folders that .gitignore ignores.
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.py[cod]\n")
    (repo / "README.md").write_text("docs\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_add.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    base = git(repo, "rev-parse", "HEAD")

    plans, runs = tmp_path / "plans", tmp_path / "runs"
    save_task_plan(plans, RUN_ID, {
        "issue": {"repository": "o/r", "number": 7, "title": "Add helpers",
                  "url": "u", "description": "Add double and triple."},
        "config": {"repository": "o/r", "base_ref": "main", "base_commit": base,
                   "model": None, "timeout_seconds": 60, "run_id": RUN_ID,
                   "branch": "b", "worktree_path": "w"},
        "summary": "s",
        "tasks": [
            task("T1", "double", ["double(2) is 4", "double(-1) is -2"]),
            task("T2", "triple", ["triple(2) is 6"], depends=["T1"]),
        ],
        "planner": {"run_id": f"{RUN_ID}-plan"},
    })
    return {"repo": repo, "base": base, "plans": plans, "runs": runs,
            "wt": tmp_path / "wt", "script": Path(__file__)}


class FakeAgents:
    """Stands in for the runner. One behaviour per role; records every call."""

    def __init__(self, repo_base_dir):
        self.calls: list[dict] = []
        self.prompts: dict[str, str] = {}
        self.behaviour = {"builder": self.build_double, "tester": self.test_double,
                          "reviewer": self.review_pass}
        self.fail = {}  # role -> RunnerOutcome to return instead

    # -- default behaviours: each returns the agent's final text --
    def build_double(self, cwd):
        with open(cwd / "calc.py", "a") as handle:
            handle.write("\n" + GOOD_DOUBLE)
        return json.dumps({"summary": "added double", "files_changed": ["calc.py"]})

    def test_double(self, cwd):
        (cwd / "tests" / "test_double.py").write_text(TESTS_DOUBLE)
        return json.dumps({"summary": "tests", "coverage": [
            {"criterion": 1, "tests": ["tests/test_double.py::test_positive"]},
            {"criterion": 2, "tests": ["tests/test_double.py::test_negative"]}]})

    def review_pass(self, cwd):
        return json.dumps({"verdict": "pass", "problems": [], "criteria": [
            {"criterion": 1, "met": True, "evidence": "calc.py"},
            {"criterion": 2, "met": True, "evidence": "calc.py"}]})

    def build_triple(self, cwd):
        with open(cwd / "calc.py", "a") as handle:
            handle.write("\n" + GOOD_TRIPLE)
        return json.dumps({"summary": "added triple", "files_changed": ["calc.py"]})

    def test_triple(self, cwd):
        (cwd / "tests" / "test_triple.py").write_text(TESTS_TRIPLE)
        return json.dumps({"coverage": [{"criterion": 1, "tests": ["tests/test_triple.py::test_triple"]}]})

    def review_pass_one(self, cwd):
        return json.dumps({"verdict": "pass", "criteria": [{"criterion": 1, "met": True}]})

    def use_triple(self):
        self.behaviour = {"builder": self.build_triple, "tester": self.test_triple,
                          "reviewer": self.review_pass_one}

    def __call__(self, **kwargs):
        if kwargs.get("read_only"):
            name = "reviewer"
        elif any("tests/**" in rule for rule in kwargs.get("allowed_tools", ())):
            name = "tester"
        else:
            name = "builder"
        self.calls.append({"role": name, **{k: v for k, v in kwargs.items() if k != "prompt_file"}})
        self.prompts[name] = Path(kwargs["prompt_file"]).read_text(encoding="utf-8")
        if name in self.fail:
            return self.fail[name]
        text = self.behaviour[name](Path(kwargs["worktree_path"]))
        return RunnerOutcome("success", 0, None, {
            "status": "succeeded", "result_text": text, "total_cost_usd": 0.1,
            "num_turns": 3, "model": kwargs.get("model")})

    @property
    def roles(self):
        return [call["role"] for call in self.calls]


@pytest.fixture
def agents(world):
    return FakeAgents(world["repo"])


def run(world, agents, task_id="T1", **overrides):
    options = dict(plans_dir=world["plans"], runs_dir=world["runs"], repo_path=world["repo"],
                   worktree_root=world["wt"], runner_script=world["script"],
                   invoke_runner=agents, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    options.update(overrides)
    return tr.run_task(RUN_ID, task_id, **options)


def assert_main_untouched(world):
    assert git(world["repo"], "rev-parse", "HEAD") == world["base"]
    assert git(world["repo"], "status", "--porcelain") == ""
    assert "double" not in (world["repo"] / "calc.py").read_text()


# --- the good path ---------------------------------------------------------------


def test_good_task_ends_as_a_verified_local_commit(world, agents):
    record = run(world, agents)
    assert record["status"] == "done", record["reasons"]
    assert agents.roles == ["builder", "tester", "reviewer"]
    assert [h["status"] for h in record["history"]] == ["pending", "building", "testing", "reviewing", "done"]

    worktree = Path(record["worktree_path"])
    assert git(worktree, "rev-parse", "HEAD") == record["commit"] != world["base"]
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == record["branch"]
    assert tc.changed_paths(worktree) == []
    assert git(worktree, "diff", "--name-only", world["base"], "HEAD").splitlines() == [
        "calc.py", "tests/test_double.py"]
    assert git(worktree, "log", "-1", "--format=%s") == "T1: double"
    assert record["checks"]["suite"]["exit_code"] == 0
    assert record["checks"]["named_tests"]["exit_code"] == 0
    assert record["cost_usd"] == pytest.approx(0.3)
    assert set(record["agents"]) == {"builder", "tester", "reviewer"}
    assert_main_untouched(world)


def test_nothing_is_pushed_or_merged(world, agents):
    run(world, agents)
    assert git(world["repo"], "branch", "--list", "main").strip().endswith("main")
    assert git(world["repo"], "remote") == ""


def test_each_role_gets_its_own_permissions_and_budget(world, agents):
    run(world, agents)
    builder, tester, reviewer = agents.calls
    assert builder["isolated"] and "Edit" in builder["allowed_tools"]
    assert "Bash(git commit:*)" in builder["disallowed_tools"]
    assert tester["isolated"] and "Edit(tests/**)" in tester["allowed_tools"]
    assert "Edit" not in tester["allowed_tools"]
    assert reviewer["read_only"] is True and "isolated" not in reviewer
    for call in agents.calls:
        assert call["model"] == "sonnet" and call["effort"] == "low"
    assert builder["max_budget_usd"] > tester["max_budget_usd"] > reviewer["max_budget_usd"]


def test_prompts_contain_the_issue_task_and_the_diff(world, agents):
    run(world, agents)
    assert "Add double and triple." in agents.prompts["builder"]
    assert "calc.py" in agents.prompts["tester"]
    assert "+def double" in agents.prompts["reviewer"]
    assert "Full test suite: passed" in agents.prompts["reviewer"]


# --- a broken task is caught -------------------------------------------------------


def test_sabotaged_code_is_caught_and_not_committed(world, agents):
    def broken(cwd):
        with open(cwd / "calc.py", "a") as handle:
            handle.write("\ndef double(x):\n    return x + 2\n")
        return json.dumps({"summary": "done", "files_changed": ["calc.py"]})

    agents.behaviour["builder"] = broken
    record = run(world, agents)
    assert record["status"] == "failed"
    assert any("full test suite" in r or "named tests" in r for r in record["reasons"])
    assert "reviewer" not in agents.roles  # stopped before spending on the review
    assert record["commit"] is None
    assert git(Path(record["worktree_path"]), "rev-parse", "HEAD") == world["base"]
    assert_main_untouched(world)


def test_builder_changing_files_outside_the_plan_is_caught(world, agents):
    def sprawling(cwd):
        agents.build_double(cwd)
        (cwd / "README.md").write_text("changed\n")
        return json.dumps({"summary": "s"})

    agents.behaviour["builder"] = sprawling
    record = run(world, agents)
    assert record["status"] == "failed"
    assert any("README.md" in r for r in record["reasons"])
    assert agents.roles == ["builder"]


def test_builder_that_changes_nothing_fails(world, agents):
    agents.behaviour["builder"] = lambda cwd: json.dumps({"summary": "thought hard"})
    record = run(world, agents)
    assert record["status"] == "failed" and "changed no files" in record["reasons"][0]


def test_builder_reporting_blocked_fails(world, agents):
    agents.behaviour["builder"] = lambda cwd: json.dumps({"blocked": True, "blocker": "no API"})
    record = run(world, agents)
    assert record["status"] == "failed" and "no API" in record["reasons"][0]


def test_tester_that_edits_code_is_caught(world, agents):
    def meddling(cwd):
        agents.test_double(cwd)
        (cwd / "calc.py").write_text((cwd / "calc.py").read_text() + "\n# tweak\n")
        return agents.test_double(cwd)

    agents.behaviour["tester"] = meddling
    record = run(world, agents)
    assert record["status"] == "failed"
    assert any("outside tests/" in r and "calc.py" in r for r in record["reasons"])


def test_tester_weakening_an_existing_test_is_caught(world, agents):
    def weakening(cwd):
        (cwd / "tests" / "test_add.py").write_text(
            "from calc import add\n\n\ndef test_add():\n    assert True\n")
        return agents.test_double(cwd)

    agents.behaviour["tester"] = weakening
    record = run(world, agents)
    assert record["status"] == "failed"
    assert any("lost lines" in r and "test_add.py" in r for r in record["reasons"])


def test_tester_naming_tests_that_do_not_exist_is_caught(world, agents):
    agents.behaviour["tester"] = lambda cwd: (
        agents.test_double(cwd) and json.dumps({"coverage": [
            {"criterion": 1, "tests": ["tests/test_ghost.py::test_x"]},
            {"criterion": 2, "tests": ["tests/test_double.py::test_negative"]}]}))
    record = run(world, agents)
    assert record["status"] == "failed"
    assert any("not found" in r for r in record["reasons"])


def test_tester_leaving_a_criterion_without_test_or_reason_is_caught(world, agents):
    agents.behaviour["tester"] = lambda cwd: (
        agents.test_double(cwd) and json.dumps({"coverage": [
            {"criterion": 1, "tests": ["tests/test_double.py::test_positive"]}]}))
    record = run(world, agents)
    assert record["status"] == "failed" and any("No test and no reason" in r for r in record["reasons"])


def test_tester_naming_a_pytest_option_is_refused_not_run(world, agents):
    agents.behaviour["tester"] = lambda cwd: (
        agents.test_double(cwd) and json.dumps({"coverage": [
            {"criterion": 1, "tests": ["--co"]},
            {"criterion": 2, "tests": ["tests/test_double.py::test_negative"]}]}))
    record = run(world, agents)
    assert record["status"] == "failed" and any("invalid test IDs" in r for r in record["reasons"])


def test_criterion_without_automated_test_is_allowed_if_reviewer_confirms(world, agents):
    agents.behaviour["tester"] = lambda cwd: (
        agents.test_double(cwd) and json.dumps({
            "coverage": [{"criterion": 1, "tests": ["tests/test_double.py::test_positive"]}],
            "uncovered": [{"criterion": 2, "reason": "needs a person"}]}))
    assert run(world, agents)["status"] == "done"


def test_reviewer_fail_verdict_blocks_the_commit(world, agents):
    agents.behaviour["reviewer"] = lambda cwd: json.dumps({
        "verdict": "fail", "problems": ["no docs"],
        "criteria": [{"criterion": 1, "met": True}, {"criterion": 2, "met": False}]})
    record = run(world, agents)
    assert record["status"] == "failed" and record["commit"] is None
    assert any("verdict is fail" in r for r in record["reasons"])
    assert any("unmet: 2" in r for r in record["reasons"])
    assert git(Path(record["worktree_path"]), "rev-parse", "HEAD") == world["base"]


def test_reviewer_notes_are_recorded_but_do_not_block_the_commit(world, agents):
    # The second live M2.8 run failed only on two remarks the reviewer itself called minor.
    agents.behaviour["reviewer"] = lambda cwd: json.dumps({
        "verdict": "pass", "problems": [], "notes": ["tests are in a separate file"],
        "criteria": [{"criterion": 1, "met": True}, {"criterion": 2, "met": True}]})
    record = run(world, agents)
    assert record["status"] == "done" and record["commit"]
    assert record["findings"]["reviewer_notes"] == ["tests are in a separate file"]


def test_a_blocking_reviewer_problem_still_fails_the_task(world, agents):
    agents.behaviour["reviewer"] = lambda cwd: json.dumps({
        "verdict": "pass", "problems": ["the message keeps the 'Could not read' prefix"],
        "criteria": [{"criterion": 1, "met": True}, {"criterion": 2, "met": True}]})
    record = run(world, agents)
    assert record["status"] == "failed" and record["commit"] is None
    assert any("prefix" in r for r in record["reasons"])


@pytest.mark.parametrize("criteria", [[], [{"criterion": 1, "met": True}]])
def test_reviewer_that_skips_criteria_is_not_done(world, agents, criteria):
    agents.behaviour["reviewer"] = lambda cwd: json.dumps({
        "verdict": "pass", "criteria": criteria, "problems": []})
    record = run(world, agents)
    assert record["status"] == "needs_review" and record["commit"] is None
    assert "did not judge criteria" in record["reasons"][0]


# --- the run itself breaks -------------------------------------------------------------


@pytest.mark.parametrize("role", ["builder", "tester", "reviewer"])
def test_agent_crash_needs_review(world, agents, role):
    agents.fail[role] = RunnerOutcome("failure", 1, "Claude exited with code 1", {})
    record = run(world, agents)
    assert record["status"] == "needs_review" and record["commit"] is None
    assert role in record["reasons"][0]


def test_agent_timeout_needs_review(world, agents):
    agents.fail["builder"] = RunnerOutcome("timeout", None, "too slow", {})
    assert run(world, agents)["status"] == "needs_review"


@pytest.mark.parametrize("role", ["builder", "tester", "reviewer"])
def test_unreadable_report_needs_review(world, agents, role):
    agents.behaviour[role] = lambda cwd, real=agents.behaviour[role]: (real(cwd), "I am done!")[1]
    record = run(world, agents)
    assert record["status"] == "needs_review" and "unusable" in record["reasons"][0]


def test_unexpected_crash_leaves_a_record_not_a_stuck_task(world, agents, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(tc, "snapshot", explode)
    record = run(world, agents)
    assert record["status"] == "needs_review" and "OSError" in record["reasons"][0]
    assert load_task(world["runs"], RUN_ID, "T1")["status"] == "needs_review"


def test_test_command_that_hangs_is_cut_off(world, agents, monkeypatch):
    monkeypatch.setattr(tr, "TEST_TIMEOUT_SECONDS", 1)
    record = run(world, agents, test_command=(sys.executable, "-c", "import time; time.sleep(30)"))
    assert record["status"] == "failed" and record["checks"]["suite"]["timed_out"]


# --- ordering and refusals -----------------------------------------------------------------


def test_dependent_task_waits_for_its_dependency(world, agents):
    with pytest.raises(tr.TaskNotReady, match="has not been run"):
        run(world, agents, task_id="T2")
    agents.behaviour["builder"] = lambda cwd: json.dumps({"summary": "x"})
    assert run(world, agents)["status"] == "failed"
    with pytest.raises(tr.TaskNotReady, match="failed, not done"):
        run(world, agents, task_id="T2")
    assert list_tasks(world["runs"], RUN_ID).keys() == {"T1"}


def test_dependent_task_starts_from_its_dependencys_commit(world, agents):
    first = run(world, agents)
    assert first["status"] == "done"
    agents.use_triple()
    second = run(world, agents, task_id="T2")
    assert second["status"] == "done", second["reasons"]
    assert second["base_commit"] == first["commit"]
    worktree = Path(second["worktree_path"])
    assert "def double" in (worktree / "calc.py").read_text()
    assert "def triple" in (worktree / "calc.py").read_text()
    assert git(worktree, "rev-list", "--count", f"{first['commit']}..HEAD") == "1"


def test_running_a_task_twice_is_refused(world, agents):
    run(world, agents)
    with pytest.raises(FileExistsError):
        run(world, agents)


def test_unknown_task_is_refused(world, agents):
    with pytest.raises(ExecutionRefused, match="no task 'T9'"):
        run(world, agents, task_id="T9")
    assert agents.calls == []


def test_plan_without_issue_text_is_refused(world, agents):
    path = world["plans"] / RUN_ID / "plan.json"
    data = json.loads(path.read_text())
    data["issue"].pop("description")
    path.write_text(json.dumps(data))
    with pytest.raises(ExecutionRefused, match="Issue text"):
        run(world, agents)


def test_missing_base_commit_is_refused_before_anything_is_created(world, agents):
    path = world["plans"] / RUN_ID / "plan.json"
    data = json.loads(path.read_text())
    data["config"]["base_commit"] = "0" * 40
    path.write_text(json.dumps(data))
    with pytest.raises(ExecutionRefused, match="does not exist"):
        run(world, agents)
    assert not world["runs"].exists() and not world["wt"].exists()


def test_worktree_inside_the_repository_is_recorded_as_failure(world, agents):
    record = run(world, agents, worktree_root=world["repo"] / "inside")
    assert record["status"] == "failed" and "outside the repository" in record["reasons"][0]
    assert agents.calls == []


def test_check_environment_removes_credentials(monkeypatch):
    monkeypatch.setattr(tr.runner_adapter, "load_agent_env", lambda script: {
        "PATH": "/bin", "ANTHROPIC_API_KEY": "secret", "claude_code_oauth_token": "t",
        "CLAUDE_CONFIG_DIR": "/c", "HOME": "/h"})
    env = tr.check_environment(Path("x"))
    assert env == {"PATH": "/bin", "HOME": "/h", "PYTHONDONTWRITEBYTECODE": "1"}