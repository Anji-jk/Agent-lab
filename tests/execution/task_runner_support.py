"""Shared setup for the task runner tests: plain helpers and the fake agents.

The fixtures that use them are in conftest.py. This file has a unique name because
the test folders have no __init__.py, so pytest tells files apart by name.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.execution import task_runner as tr
from orchestrator.execution.runner_adapter import RunnerOutcome
from orchestrator.planning.plan_file import save_task_plan
from orchestrator.records.task_registry import load_task

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


def make_world(tmp_path):
    """A repository with calc.py and a stored two-task plan (T2 depends on T1)."""
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


def orchestrator_steps(world, task_id="T1"):
    from orchestrator.records.task_events import task_events_path
    path = task_events_path(world["runs"], RUN_ID, task_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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


def prepare(world, task_id, is_alive):
    return tr.prepare(RUN_ID, task_id, plans_dir=world["plans"], runs_dir=world["runs"],
                      repo_path=world["repo"], is_alive=is_alive)


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


def costly(agents, costs):
    """The fake agents, but each role costs what ``costs`` says (small caps: 0.60/0.25/0.15)."""
    def invoke(**kwargs):
        outcome = agents(**kwargs)
        role = agents.calls[-1]["role"]
        return RunnerOutcome(outcome.outcome, outcome.process_exit_code, outcome.error,
                             {**outcome.result, "total_cost_usd": costs.get(role, 0.1)})
    return invoke
