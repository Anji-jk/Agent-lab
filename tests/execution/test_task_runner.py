"""The task pipeline: the good path, broken tasks, ordering, the suite's Python, one branch per run, timeouts."""

import json
import os
import sys
from pathlib import Path

import pytest

from orchestrator.execution import checks as tc
from orchestrator.execution import task_runner as tr
from orchestrator.errors import ExecutionRefused
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.records.task_registry import list_tasks, load_task
from task_runner_support import (
    RUN_ID,
    add_task,
    assert_main_untouched,
    git,
    orchestrator_steps,
    prepare,
    run,
    task,
    use_note,
)


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


def test_a_suite_that_times_out_says_so_and_is_not_retried(world, agents, monkeypatch):
    from orchestrator.execution import retry as rt
    monkeypatch.setattr(tr, "TEST_TIMEOUT_SECONDS", 1)
    record = run(world, agents, test_command=(sys.executable, "-c", "import time; time.sleep(30)"))
    assert record["status"] == "failed" and record["findings"]["suite_timed_out"] is True
    assert any("did not finish within 1 s: it timed out, and no test was reported failing" in r
               for r in record["reasons"])
    assert not any(r == "The full test suite fails." for r in record["reasons"])
    assert "timed out; a retry would not make them faster" in (rt.retry_problem(record) or "")


def test_the_test_time_limit_can_be_set(monkeypatch):
    monkeypatch.delenv(tr.TEST_TIMEOUT_VARIABLE, raising=False)
    assert tr.test_timeout_seconds() == tr.TEST_TIMEOUT_SECONDS == 1200
    monkeypatch.setenv(tr.TEST_TIMEOUT_VARIABLE, "3600")
    assert tr.test_timeout_seconds() == 3600
    for bad in ("ten", "59", "86401"):
        monkeypatch.setenv(tr.TEST_TIMEOUT_VARIABLE, bad)
        with pytest.raises(ExecutionRefused, match="AGENT_LAB_TEST_TIMEOUT"):
            tr.test_timeout_seconds()


def test_the_test_time_limit_reaches_the_suite(world, agents, monkeypatch):
    monkeypatch.setenv(tr.TEST_TIMEOUT_VARIABLE, "90")
    record = run(world, agents)
    started = [e for e in orchestrator_steps(world) if e["step"] == "suite_started"]
    assert started[0]["timeout_seconds"] == 90 and record["findings"]["test_timeout_seconds"] == 90
