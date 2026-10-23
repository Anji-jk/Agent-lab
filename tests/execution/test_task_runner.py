import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.execution import checks as tc
from orchestrator.execution import task_runner as tr
from orchestrator.errors import ExecutionRefused
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.planning.plan_file import save_task_plan
from orchestrator.records.task_registry import list_tasks, load_task

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
        self.passed_roles: list = []  # the role argument each agent was started with

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
        self.passed_roles.append(kwargs.get("role"))
        self.calls.append({**{k: v for k, v in kwargs.items() if k != "prompt_file"}, "role": name})
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


def test_each_agent_is_started_with_its_role_for_progress(world, agents):
    run(world, agents)
    assert agents.passed_roles == ["builder", "tester", "reviewer"]


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


def test_tester_notes_are_recorded_but_do_not_fail_the_task(world, agents):
    # The broken-plan M2.8 run stopped on a tester remark about test speed.
    agents.behaviour["tester"] = lambda cwd: (
        agents.test_double(cwd) and json.dumps({"coverage": [
            {"criterion": 1, "tests": ["tests/test_double.py::test_positive"]},
            {"criterion": 2, "tests": ["tests/test_double.py::test_negative"]}],
            "notes": ["the full suite is slow"]}))
    record = run(world, agents)
    assert record["status"] == "done"
    assert record["findings"]["tester_notes"] == ["the full suite is slow"]


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
    python_folder = str(Path(tc.project_python()).parent)  # put first on PATH (M3.8)
    assert env == {"PATH": python_folder + os.pathsep + "/bin", "HOME": "/h",
                   "PYTHONDONTWRITEBYTECODE": "1"}


# --- M3.3: the orchestrator's own event log -----------------------------------------


def orchestrator_steps(world, task_id="T1"):
    from orchestrator.records.task_events import task_events_path
    path = task_events_path(world["runs"], RUN_ID, task_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_good_task_logs_every_orchestrator_step_in_order(world, agents):
    record = run(world, agents)
    entries = orchestrator_steps(world)
    steps = [e["step"] for e in entries]
    assert steps == [
        "task_started", "worktree_created",
        "stage", "agent_started", "agent_finished", "builder_checked",
        "stage", "agent_started", "agent_finished", "tester_checked",
        "suite_started", "suite_finished", "named_tests_started", "named_tests_finished",
        "stage", "agent_started", "agent_finished", "reviewer_checked",
        "decided", "committed", "task_finished",
    ]
    assert [e["status"] for e in entries if e["step"] == "stage"] == ["building", "testing", "reviewing"]
    assert [e["role"] for e in entries if e["step"] == "agent_started"] == ["builder", "tester", "reviewer"]
    by_step = {e["step"]: e for e in entries}  # the last entry of each step
    assert [e["agent_run_id"] for e in entries if e["step"] == "agent_started"] == [
        f"{RUN_ID}-T1-builder", f"{RUN_ID}-T1-tester", f"{RUN_ID}-T1-reviewer"]
    assert by_step["suite_finished"]["passed"] is True
    assert by_step["committed"]["commit"] == record["commit"]
    assert by_step["task_finished"]["status"] == "done"
    assert all(isinstance(e["at"], str) for e in entries)


def test_a_stopped_task_logs_why(world, agents):
    def sprawling(cwd):
        agents.build_double(cwd)
        (cwd / "README.md").write_text("changed\n")
        return json.dumps({"summary": "s"})

    agents.behaviour["builder"] = sprawling
    run(world, agents)
    entries = orchestrator_steps(world)
    stopped = [e for e in entries if e["step"] == "stage_stopped"]
    assert len(stopped) == 1 and any("README.md" in r for r in stopped[0]["reasons"])
    assert entries[-1]["step"] == "task_finished" and entries[-1]["status"] == "failed"
    assert "suite_started" not in [e["step"] for e in entries]


def test_a_crash_is_logged(world, agents, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(tc, "snapshot", explode)
    run(world, agents)
    steps = [e["step"] for e in orchestrator_steps(world)]
    assert "pipeline_crashed" in steps and steps[-1] == "task_finished"


def test_a_log_that_cannot_be_written_does_not_stop_the_task(world, agents, monkeypatch, tmp_path):
    from orchestrator.records import task_events
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the log's folder should be")
    monkeypatch.setattr(task_events, "task_events_path", lambda *args: blocker / "events.jsonl")
    record = run(world, agents)
    assert record["status"] == "done"


# --- the suite's Python is checked before anything is paid for (M3.8) -----------------------


def test_a_python_without_pytest_is_refused_before_any_agent_runs(world, agents, monkeypatch):
    monkeypatch.setattr(tc, "pytest_problem", lambda python, env=None: "No module named pytest")
    with pytest.raises(ExecutionRefused, match="pytest is not available"):
        run(world, agents)
    assert agents.calls == [] and list_tasks(world["runs"], RUN_ID) == {}


def test_a_test_command_that_is_not_pytest_is_not_checked(world, agents, monkeypatch):
    monkeypatch.setattr(tc, "pytest_problem", lambda python, env=None: pytest.fail("checked"))
    run(world, agents, test_command=(sys.executable, "-c", "pass"))


def test_the_checks_environment_puts_the_projects_python_first_on_path(monkeypatch):
    monkeypatch.setattr(tr.runner_adapter, "load_agent_env", lambda script: {"PATH": "elsewhere"})
    env = tr.check_environment(Path("runner.py"))
    folder = str(Path(tc.project_python()).parent)
    assert env["PATH"].split(os.pathsep) == [folder, "elsewhere"]


# --- M4.2: one branch for the run ------------------------------------------------------------


def add_task(world, new_task):
    path = world["plans"] / RUN_ID / "plan.json"
    data = json.loads(path.read_text())
    data["tasks"].append(new_task)
    path.write_text(json.dumps(data))


def build_note(cwd):
    (cwd / "notes.py").write_text("NOTE = 1\n")
    return json.dumps({"summary": "added a note", "files_changed": ["notes.py"]})


def write_note_tests(cwd):
    (cwd / "tests" / "test_notes.py").write_text("from notes import NOTE\n\n\ndef test_note():\n    assert NOTE == 1\n")
    return json.dumps({"coverage": [{"criterion": 1, "tests": ["tests/test_notes.py::test_note"]}]})


def use_note(agents):
    agents.behaviour = {"builder": build_note, "tester": write_note_tests,
                        "reviewer": agents.review_pass_one}


@pytest.fixture
def with_independent(world):
    add_task(world, task("T3", "note", ["NOTE is 1"], files=("notes.py",)))
    return world


def test_an_independent_task_starts_from_the_last_finished_commit(with_independent, agents):
    world = with_independent
    first = run(world, agents)
    use_note(agents)
    third = run(world, agents, task_id="T3")
    assert third["status"] == "done", third["reasons"]
    assert third["base_commit"] == first["commit"]  # not the plan's base
    worktree = Path(third["worktree_path"])
    assert "def double" in (worktree / "calc.py").read_text()  # it sees T1's work
    assert git(worktree, "rev-list", "--count", f"{first['commit']}..HEAD") == "1"


def test_the_scope_check_judges_only_the_tasks_own_change(with_independent, agents):
    # T3 starts from T1's commit; T1's calc.py change must not count against T3.
    world = with_independent
    run(world, agents)
    use_note(agents)
    third = run(world, agents, task_id="T3")
    assert third["status"] == "done", third["reasons"]
    assert git(world["repo"], "diff", "--name-only", third["base_commit"], third["commit"]) \
        == "notes.py\ntests/test_notes.py"


def test_a_failed_task_leaves_the_head_where_it_was(with_independent, agents):
    world = with_independent
    agents.behaviour["builder"] = lambda cwd: json.dumps({"summary": "did nothing"})
    assert run(world, agents)["status"] == "failed"
    use_note(agents)
    third = run(world, agents, task_id="T3")
    assert third["base_commit"] == world["base"]


def test_a_dependent_task_starts_from_the_head_which_contains_its_dependency(with_independent, agents):
    world = with_independent
    first = run(world, agents)
    use_note(agents)
    third = run(world, agents, task_id="T3")
    agents.use_triple()
    second = run(world, agents, task_id="T2")  # depends on T1 only
    assert second["status"] == "done", second["reasons"]
    assert second["base_commit"] == third["commit"]
    # One chain: base -> T1 -> T3 -> T2, each task adding one commit.
    assert git(world["repo"], "rev-list", "--count", f"{world['base']}..{second['commit']}") == "3"
    assert tc.is_ancestor(world["repo"], first["commit"], second["commit"])


def test_a_task_depending_on_two_independent_tasks_now_runs(with_independent, agents):
    # T4 depends on T1 and T3, which depend on nothing. Before M4.2 their work was on
    # two branches and T4 was refused; now T3 starts from T1, so T3 contains both.
    world = with_independent
    add_task(world, task("T4", "triple", ["triple(2) is 6"], depends=["T1", "T3"]))
    first = run(world, agents)
    use_note(agents)
    third = run(world, agents, task_id="T3")
    agents.use_triple()
    fourth = run(world, agents, task_id="T4")
    assert fourth["status"] == "done", fourth["reasons"]
    assert fourth["base_commit"] == third["commit"]
    worktree = Path(fourth["worktree_path"])
    assert (worktree / "notes.py").exists() and "def double" in (worktree / "calc.py").read_text()
    assert tc.is_ancestor(world["repo"], first["commit"], fourth["commit"])


def test_finished_work_on_separate_branches_is_refused(with_independent, agents):
    # A run made before M4.2, where T1 and T3 both started from the plan's base.
    from orchestrator.records.task_registry import save_task
    world, repo = with_independent, with_independent["repo"]
    commits = {}
    for task_id, name in (("T1", "a.txt"), ("T3", "b.txt")):
        git(repo, "checkout", "-q", "-b", f"side-{task_id}", world["base"])
        (repo / name).write_text("x\n")
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", task_id)
        commits[task_id] = git(repo, "rev-parse", "HEAD")
        save_task(world["runs"], RUN_ID, task_id, {"status": "done", "commit": commits[task_id]})
    git(repo, "checkout", "-q", "main")
    with pytest.raises(tr.TaskNotReady, match=r"separate branches \(T1 at .*, T3 at .*\).*new run"):
        run(world, agents, task_id="T2")
    assert agents.calls == []


def test_a_finished_commit_that_is_missing_is_refused(world, agents):
    from orchestrator.records.task_registry import save_task
    save_task(world["runs"], RUN_ID, "T1", {"status": "done", "commit": "1" * 40})
    with pytest.raises(ExecutionRefused, match="T1's commit 111111111111 does not exist"):
        run(world, agents, task_id="T2")


def prepare(world, task_id, is_alive):
    return tr.prepare(RUN_ID, task_id, plans_dir=world["plans"], runs_dir=world["runs"],
                      repo_path=world["repo"], is_alive=is_alive)


@pytest.mark.parametrize("status", ["pending", "building", "testing", "reviewing"])
def test_a_task_is_refused_while_another_task_of_the_run_is_running(with_independent, status):
    from orchestrator.records.task_registry import save_task
    world = with_independent
    save_task(world["runs"], RUN_ID, "T1", {"status": status})
    seen = []

    def alive(task_id, record):
        seen.append(task_id)
        return True, "process 42 is running it"

    with pytest.raises(tr.TaskNotReady, match="T1 is still running .process 42.*one at a time"):
        prepare(world, "T3", alive)
    assert seen == ["T1"]


def test_a_task_whose_process_has_gone_does_not_block_the_next(with_independent):
    from orchestrator.records.task_registry import save_task
    world = with_independent
    save_task(world["runs"], RUN_ID, "T1", {"status": "building"})
    ctx = prepare(world, "T3", lambda task_id, record: (False, "its process is gone"))
    assert ctx.base_commit == world["base"]  # it left no commit


def test_finished_tasks_are_not_checked_for_a_process(with_independent, agents):
    world = with_independent
    run(world, agents)

    def alive(task_id, record):
        raise AssertionError("a finished task's process was checked")

    assert prepare(world, "T3", alive).task["id"] == "T3"

# --- M4.4: retry once, with the findings -------------------------------------------------------


def wrong_double(cwd):
    with open(cwd / "calc.py", "a") as handle:
        handle.write("\ndef double(x):\n    return x + 2\n")  # double(-1) is 1, not -2
    return json.dumps({"summary": "added double", "files_changed": ["calc.py"]})


def retry(world, agents, task_id="T1", **overrides):
    from orchestrator.execution import retry as rt
    options = dict(plans_dir=world["plans"], runs_dir=world["runs"], repo_path=world["repo"],
                   worktree_root=world["wt"], runner_script=world["script"],
                   invoke_runner=agents, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    options.update(overrides)
    return rt.retry_task(RUN_ID, task_id, **options)


def task_folder(world, task_id="T1"):
    return world["runs"] / RUN_ID / "tasks" / task_id


def test_a_task_that_fails_once_then_passes(world, agents):
    agents.behaviour["builder"] = wrong_double
    first = run(world, agents)
    assert first["status"] == "failed"
    agents.behaviour["builder"] = agents.build_double
    agents.calls.clear()

    second = retry(world, agents)
    assert second["status"] == "done", second["reasons"]
    assert second["attempt"] == 2 and second["commit"]
    # A fresh worktree and branch, from the same head the first attempt started from.
    assert second["base_commit"] == first["base_commit"] == world["base"]
    assert second["branch"] == first["branch"] + "-r2"
    assert second["worktree_path"] != first["worktree_path"]
    assert Path(first["worktree_path"]).is_dir()  # the first attempt's worktree is kept
    # Its own agents, so the first attempt's runner folders stay as they were.
    assert [c["run_id"] for c in agents.calls] == [
        f"{RUN_ID}-T1-r2-builder", f"{RUN_ID}-T1-r2-tester", f"{RUN_ID}-T1-r2-reviewer"]
    # The builder was told what went wrong.
    prompt = agents.prompts["builder"]
    assert "Problems found by an earlier attempt" in prompt
    assert first["reasons"][0][:60] in prompt
    assert "clean checkout" in prompt


def test_each_attempt_keeps_its_own_record_and_cost(world, agents):
    agents.behaviour["builder"] = wrong_double
    first = run(world, agents)
    agents.behaviour["builder"] = agents.build_double
    second = retry(world, agents)
    archived = json.loads((task_folder(world) / "attempt-1" / "task.json").read_text())
    assert archived["status"] == "failed" and archived["cost_usd"] == first["cost_usd"]
    assert (task_folder(world) / "attempt-1" / "events.jsonl").is_file()
    assert second["previous_attempts"] == [{
        "attempt": 1, "status": "failed", "reasons": first["reasons"],
        "cost_usd": first["cost_usd"], "branch": first["branch"],
        "worktree_path": first["worktree_path"], "base_commit": first["base_commit"],
        "finished_at": first["updated_at"]}]
    assert second["cost_usd"] == pytest.approx(0.3)  # this attempt's own three agents
    steps = [e["step"] for e in orchestrator_steps(world)]
    assert steps[0] == "task_started" and steps.count("task_started") == 1  # a fresh log


def test_a_task_that_fails_twice_stays_failed(world, agents):
    from orchestrator.errors import ExecutionRefused as Refused
    agents.behaviour["builder"] = wrong_double
    run(world, agents)
    second = retry(world, agents)
    assert second["status"] == "failed" and second["attempt"] == 2 and second["commit"] is None
    with pytest.raises(Refused, match="already been retried"):
        retry(world, agents)
    assert load_task(world["runs"], RUN_ID, "T1")["status"] == "failed"
    # Its dependent is blocked, as for any failed task.
    with pytest.raises(tr.TaskNotReady, match="T1 is failed, not done"):
        run(world, agents, task_id="T2")


@pytest.mark.parametrize("make,expected", [
    ("done", "only a failed task is retried"),
    ("needs_review", "A person should look"),
    ("blocked", "cannot be done .no API"),
    ("no_worktree", "failed before it had a worktree"),
])
def test_tasks_that_are_not_retried(world, agents, make, expected):
    if make == "needs_review":
        agents.fail["builder"] = RunnerOutcome("failure", 1, "Claude exited with code 1", {})
    elif make == "blocked":
        agents.behaviour["builder"] = lambda cwd: json.dumps({"blocked": True, "blocker": "no API"})
    record = run(world, agents, worktree_root=world["repo"] / "inside") if make == "no_worktree" \
        else run(world, agents)
    calls = len(agents.calls)
    with pytest.raises(ExecutionRefused, match=expected):
        retry(world, agents)
    assert len(agents.calls) == calls  # nothing was paid for
    assert load_task(world["runs"], RUN_ID, "T1") == record  # and nothing moved
    assert not (task_folder(world) / "attempt-1").exists()


def test_a_task_that_never_ran_cannot_be_retried(world, agents):
    from orchestrator.records.run_registry import RunRegistryError
    with pytest.raises(RunRegistryError):
        retry(world, agents)


def test_a_refused_retry_leaves_the_failed_attempt_in_place(world, agents, monkeypatch):
    agents.behaviour["builder"] = wrong_double
    record = run(world, agents)
    monkeypatch.setattr(tc, "pytest_problem", lambda *args, **kwargs: "No module named pytest")
    with pytest.raises(ExecutionRefused):
        retry(world, agents, test_command=None)
    assert load_task(world["runs"], RUN_ID, "T1") == record
    assert not (task_folder(world) / "attempt-1").exists()


def test_a_retry_that_cannot_start_puts_the_first_attempt_back(world, agents, monkeypatch):
    agents.behaviour["builder"] = wrong_double
    record = run(world, agents)

    def explode(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(tr, "run_task", explode)
    with pytest.raises(OSError):
        retry(world, agents)
    assert load_task(world["runs"], RUN_ID, "T1") == record
    assert (task_folder(world) / "events.jsonl").is_file()
    assert not (task_folder(world) / "attempt-1").exists()


def test_the_feedback_names_the_failures_and_hides_secrets():
    from orchestrator.execution import retry as rt
    record = {
        "task_id": "T1", "status": "failed",
        "reasons": ["The full test suite failed.", "Reviewer: double(-1) returns 1"],
        "checks": {
            "suite": {"exit_code": 1, "timed_out": False,
                      "output_tail": "FAILED tests/test_double.py::test_negative\n"
                                     "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
                                     "1 failed, 4 passed"},
            "named_tests": {"exit_code": 0, "timed_out": False, "output_tail": "2 passed"},
        },
    }
    text = rt.retry_feedback(record)
    assert text.startswith("Attempt 1 of this task")
    assert "- The full test suite failed." in text and "- Reviewer: double(-1) returns 1" in text
    assert "The end of the full test suite output:" in text and "1 failed, 4 passed" in text
    assert "tests the tester named" not in text  # it passed, so it is not repeated
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in text


def test_the_feedback_says_when_the_tests_timed_out_and_cuts_long_text():
    from orchestrator.execution import retry as rt
    record = {"task_id": "T1", "status": "failed", "reasons": ["x" * 2000] * 20,
              "checks": {"suite": {"exit_code": None, "timed_out": True, "output_tail": ""}}}
    text = rt.retry_feedback(record)
    assert "(it timed out)" in text and "(no output)" in text
    assert "... and 8 more" in text
    assert max(len(line) for line in text.splitlines()) <= rt.REASON_CHARS + 2


def test_agent_ids_and_the_status_follow_the_attempt(world, agents, monkeypatch):
    from orchestrator.monitoring import progress
    assert progress.agent_run_id("r", "T1", "builder") == "r-T1-builder"
    assert progress.agent_run_id("r", "T1", "builder", 2) == "r-T1-r2-builder"
    assert progress.attempt_of({}) == 1 and progress.attempt_of({"attempt": 2}) == 2
    agents.behaviour["builder"] = wrong_double
    run(world, agents)
    second = retry(world, agents)
    from orchestrator.execution.runner_adapter import runner_run_directory
    folder = runner_run_directory(f"{RUN_ID}-T1-r2-builder", world["script"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "progress.json").write_text(json.dumps(
        {"state": "finished", "status": "succeeded", "turns": 4, "total_cost_usd": 0.5}))
    from datetime import datetime, timezone
    view = progress.task_agent_view(RUN_ID, "T1", "builder", second, world["script"],
                                    datetime.now(timezone.utc))
    assert view.turns == 4 and view.cost_usd == 0.5  # attempt 2's builder, not attempt 1's


def test_show_task_lists_the_earlier_attempt(world, agents, capsys):
    from orchestrator.cli.display import display_task
    agents.behaviour["builder"] = wrong_double
    run(world, agents)
    agents.behaviour["builder"] = agents.build_double
    display_task(retry(world, agents))
    out = capsys.readouterr().out
    assert "Attempt 1: failed, $" in out
    assert "This is attempt 2 (a retry, told what attempt 1 got wrong)." in out

# --- M4.5: continue a stopped task from its stage ----------------------------------------------


class Stop(BaseException):
    """Like a killed process: escapes the pipeline, leaving the record at its stage."""


def gone(task_id, record):
    return False, "its process is no longer running"


def stopping(work=None):
    def behaviour(cwd):
        if work:
            work(cwd)
        raise Stop()
    return behaviour


def stop_at(world, agents, role, work=None):
    """Run T1 until ``role`` starts, then stop it there."""
    real = agents.behaviour[role]
    agents.behaviour[role] = stopping(work)
    with pytest.raises(Stop):
        run(world, agents)
    agents.behaviour[role] = real
    agents.calls.clear()
    return load_task(world["runs"], RUN_ID, "T1")


def resume(world, agents, **overrides):
    from orchestrator.execution import continuing
    options = dict(plans_dir=world["plans"], runs_dir=world["runs"], repo_path=world["repo"],
                   runner_script=world["script"], invoke_runner=agents, is_alive=gone,
                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    options.update(overrides)
    return continuing.continue_task(RUN_ID, "T1", **options)


def test_a_task_interrupted_while_testing_continues_with_a_new_tester(world, agents):
    stopped = stop_at(world, agents, "tester")
    assert stopped["status"] == "testing" and stopped["checkpoint"]["stage"] == "testing"
    record = resume(world, agents)
    assert record["status"] == "done", record["reasons"]
    assert agents.roles == ["tester", "reviewer"]  # the builder's checked result was kept
    assert [c["run_id"] for c in agents.calls] == [f"{RUN_ID}-T1-r2-tester", f"{RUN_ID}-T1-r2-reviewer"]
    # Same worktree and branch; the commit holds the first builder's change.
    assert record["branch"] == stopped["branch"] and record["worktree_path"] == stopped["worktree_path"]
    assert record["attempt"] == 2 and record["continued_at"] == "testing"
    assert [h["status"] for h in record["history"]] == ["testing", "reviewing", "done"]
    assert set(record["reused_agents"]) == {"builder"} and set(record["agents"]) == {"tester", "reviewer"}
    assert "def double" in git(Path(record["worktree_path"]), "show", "HEAD:calc.py")
    assert record["previous_attempts"][0]["status"] == "interrupted"


def test_a_cancelled_task_continues_too(world, agents):
    from orchestrator.records.task_registry import update_task
    stop_at(world, agents, "tester")
    update_task(world["runs"], RUN_ID, "T1", {"status": "cancelled", "reasons": ["Cancelled by you."]})
    record = resume(world, agents)
    assert record["status"] == "done" and agents.roles == ["tester", "reviewer"]
    assert record["previous_attempts"][0]["status"] == "cancelled"
    assert record["previous_attempts"][0]["reasons"] == ["Cancelled by you."]


def test_a_task_stopped_while_reviewing_only_needs_a_new_reviewer(world, agents):
    stopped = stop_at(world, agents, "reviewer")
    assert stopped["checkpoint"]["stage"] == "reviewing"
    record = resume(world, agents)
    assert record["status"] == "done" and agents.roles == ["reviewer"]
    assert set(record["reused_agents"]) == {"builder", "tester"}
    steps = [e["step"] for e in orchestrator_steps(world)]
    assert "suite_started" not in steps  # the tests' results were kept, not run again


def test_files_changed_after_the_tests_ran_mean_the_tests_run_again(world, agents):
    stopped = stop_at(world, agents, "reviewer")
    Path(stopped["worktree_path"], "tests", "test_extra.py").write_text("def test_x():\n    pass\n")
    record = resume(world, agents)
    assert agents.roles == ["tester", "reviewer"] and record["continued_at"] == "testing"


def test_a_task_stopped_while_building_gets_a_builder_told_what_is_there(world, agents):
    stopped = stop_at(world, agents, "builder", work=lambda cwd: (cwd / "calc.py").write_text(
        (cwd / "calc.py").read_text() + "\n" + GOOD_DOUBLE))
    assert "checkpoint" not in stopped
    record = resume(world, agents)
    assert agents.roles == ["builder", "tester", "reviewer"] and record["continued_at"] == "building"
    prompt = agents.prompts["builder"]
    assert "About this worktree" in prompt and "changed files: calc.py" in prompt
    assert "Problems found by an earlier attempt" not in prompt  # not a retry


def test_a_record_from_before_checkpoints_continues_from_the_builder(world, agents):
    from orchestrator.records.task_registry import update_task
    stop_at(world, agents, "tester")
    update_task(world["runs"], RUN_ID, "T1", {"checkpoint": None})
    record = resume(world, agents)
    assert agents.roles == ["builder", "tester", "reviewer"] and record["status"] == "done"


@pytest.mark.parametrize("setup,expected", [
    ("running", "still running"),
    ("failed", "failed. Only a cancelled or interrupted task is continued; a failed one is retried"),
    ("done", "done. Only a cancelled or interrupted task"),
    ("no_worktree", "worktree .* no longer exists"),
])
def test_tasks_that_are_not_continued(world, agents, setup, expected):
    import shutil
    if setup in ("failed", "done"):
        if setup == "failed":
            agents.behaviour["builder"] = wrong_double
        run(world, agents)
    else:
        stopped = stop_at(world, agents, "tester")
        if setup == "no_worktree":
            shutil.rmtree(stopped["worktree_path"])
    record = load_task(world["runs"], RUN_ID, "T1")
    alive = (lambda task_id, rec: (True, "process 7 is running it")) if setup == "running" else gone
    calls = len(agents.calls)
    with pytest.raises(ExecutionRefused, match=expected):
        resume(world, agents, is_alive=alive)
    assert len(agents.calls) == calls and load_task(world["runs"], RUN_ID, "T1") == record
    assert not (task_folder(world) / "attempt-1").exists()


def test_a_task_is_not_continued_once_the_run_has_moved_on(with_independent, agents):
    world = with_independent
    stop_at(world, agents, "tester")  # T1 stopped; its process is gone
    use_note(agents)
    other = run(world, agents, task_id="T3")  # T3 starts from the plan's base and finishes
    assert other["status"] == "done"
    with pytest.raises(tr.TaskNotReady, match="moved on"):
        resume(world, agents)
    assert not (task_folder(world) / "attempt-1").exists()


def test_continuing_does_not_use_up_the_retry(world, agents):
    agents.behaviour["builder"] = wrong_double
    stop_at(world, agents, "tester")
    failed = resume(world, agents)  # attempt 2: the wrong double fails the tests
    assert failed["status"] == "failed" and failed["attempt"] == 2
    agents.behaviour["builder"] = agents.build_double
    third = retry(world, agents)
    assert third["status"] == "done" and third["attempt"] == 3
    assert third["branch"].endswith("-r3")
    assert [p["status"] for p in third["previous_attempts"]] == ["interrupted", "failed"]


def test_run_task_continue_cannot_run_in_the_background(world, monkeypatch, capsys):
    import orchestrator.cli.commands as cli
    monkeypatch.setattr(cli, "verify_remote_repository", lambda *args: None)
    code = cli.main(["run-task", RUN_ID, "T1", "--continue", "--background",
                     "--plans-dir", str(world["plans"]), "--runs-dir", str(world["runs"]),
                     "--repo-path", str(world["repo"])])
    assert code == cli.EXIT_USAGE and "without --background" in capsys.readouterr().err


def test_show_task_says_what_was_kept(world, agents, capsys):
    from orchestrator.cli.display import display_task
    stop_at(world, agents, "tester")
    display_task(resume(world, agents))
    out = capsys.readouterr().out
    assert "-> kept from the stopped attempt" in out
    assert "the stopped attempt 1 continued from testing, in the same worktree" in out


def test_show_task_of_a_stopped_task_does_not_say_its_agents_never_ran(world, agents, capsys):
    from orchestrator.cli.display import display_task
    display_task(stop_at(world, agents, "tester"))
    out = capsys.readouterr().out
    assert "-> not run" not in out and "no result recorded (the task was stopped)" in out
