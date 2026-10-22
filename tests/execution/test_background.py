"""M3.8: starting a task's pipeline in the background."""

import json
import os
import subprocess
import sys

import pytest

import orchestrator.cli.commands as cli
from orchestrator.errors import ExecutionRefused
from orchestrator.execution import background as bg
from orchestrator.planning.plan_file import save_task_plan
from orchestrator.records.task_registry import save_task, update_task

RUN = "run-bg"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def plan_task(task_id, depends=()):
    return {"id": task_id, "title": f"Task {task_id}", "description": "d", "depends_on": list(depends),
            "expected_outcome": "o", "files": ["README.md"], "verification": [],
            "acceptance_criteria": ["it works"], "complexity": "small", "parallel_safe": False}


@pytest.fixture
def world(git_repo, tmp_path):
    runs = tmp_path / "orchestration"
    base = git(git_repo, "rev-parse", "HEAD")
    save_task_plan(runs, RUN, {
        "issue": {"repository": "Anji-jk/Agent-lab", "number": 5, "title": "t", "description": "x", "url": "u"},
        "config": {"repository": "Anji-jk/Agent-lab", "base_ref": "main", "base_commit": base, "model": None,
                   "timeout_seconds": 60, "run_id": RUN, "branch": "b", "worktree_path": "w"},
        "summary": "s",
        "tasks": [plan_task("T1"), plan_task("T2", ["T1"])],
        "planner": {"run_id": f"{RUN}-plan"},
    })
    return {"repo": git_repo, "runs": runs, "worktrees": tmp_path / "wt"}


class FakeProcess:
    """Stands in for the background process; can write the task record as it 'starts'."""

    def __init__(self, world, task_id="T1", *, writes_record_after: int | None = 1,
                 exits_with=None, log_text=""):
        self.world, self.task_id = world, task_id
        self.writes_record_after, self.exits_with, self.log_text = writes_record_after, exits_with, log_text
        self.pid, self.polls, self.started = 4242, 0, None

    def __call__(self, command, log_file, cwd):
        self.started = {"command": command, "log": log_file, "cwd": cwd}
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(self.log_text, encoding="utf-8")
        return self

    def poll(self):
        self.polls += 1
        if self.writes_record_after is not None and self.polls >= self.writes_record_after:
            try:
                save_task(self.world["runs"], RUN, self.task_id, {"title": "x"})
            except FileExistsError:
                pass
        return self.exits_with


def start(world, popen, task_id="T1", **kw):
    return bg.start_task_in_background(
        RUN, task_id, plans_dir=world["runs"], runs_dir=world["runs"], repo_path=world["repo"],
        worktree_root=world["worktrees"], popen=popen, sleep=lambda _seconds: None, **kw)


# --- what is started -----------------------------------------------------------------------


def test_it_starts_the_ordinary_run_task_command_and_returns_once_the_record_exists(world):
    fake = FakeProcess(world)
    started = start(world, fake)
    assert fake.started is not None
    command = fake.started["command"]
    # The project's Python, not whichever one started this (M3.8: system Python had no pytest).
    assert command[:6] == [bg.project_python(), "-m", "orchestrator", "run-task", RUN, "T1"]
    assert "--background" not in command  # the child runs in the foreground: no loop
    assert command[command.index("--runs-dir") + 1] == str(world["runs"].resolve())
    assert command[command.index("--worktree-root") + 1] == str(world["worktrees"].resolve())
    assert fake.started["cwd"] == bg.PROJECT_ROOT  # where `-m orchestrator` can be imported
    assert started.record_seen and started.pid == 4242
    assert started.log == bg.log_path(world["runs"], RUN, "T1")


def test_the_process_id_and_command_are_recorded_for_later_steps(world):
    fake = FakeProcess(world)
    started = start(world, fake)
    launch = json.loads(started.launch.read_text(encoding="utf-8"))
    assert fake.started is not None
    assert launch["pid"] == 4242 and launch["command"] == fake.started["command"]
    assert launch["log"] == str(started.log) and launch["started_at"]


# --- what is refused before anything starts ----------------------------------------------------


def test_a_task_that_already_ran_is_refused_before_starting(world):
    save_task(world["runs"], RUN, "T1", {"title": "x"})
    update_task(world["runs"], RUN, "T1", {"status": "building"})
    fake = FakeProcess(world)
    with pytest.raises(ExecutionRefused, match="already run .it is building"):
        start(world, fake)
    assert fake.started is None


def test_a_task_whose_dependency_is_not_done_is_refused_before_starting(world):
    fake = FakeProcess(world, "T2")
    with pytest.raises(ExecutionRefused, match="T1 has not been run yet"):
        start(world, fake, task_id="T2")
    assert fake.started is None


def test_an_unknown_task_is_refused_before_starting(world):
    fake = FakeProcess(world, "T9")
    with pytest.raises(ExecutionRefused, match="no task 'T9'"):
        start(world, fake, task_id="T9")
    assert fake.started is None


# --- what happens after it starts ---------------------------------------------------------------


def test_a_process_that_dies_at_once_is_reported_with_its_log(world):
    fake = FakeProcess(world, writes_record_after=None, exits_with=1,
                       log_text="Traceback (most recent call last):\nImportError: no module named x\n")
    with pytest.raises(ExecutionRefused) as caught:
        start(world, fake)
    message = str(caught.value)
    assert "stopped at once (exit code 1)" in message and "ImportError: no module named x" in message


def test_a_task_that_finishes_before_the_first_look_is_not_an_error(world):
    fake = FakeProcess(world, writes_record_after=1, exits_with=0)
    assert start(world, fake).record_seen


def test_a_slow_start_returns_and_says_so(world):
    ticks = iter(range(100))
    fake = FakeProcess(world, writes_record_after=None)
    started = start(world, fake, wait_seconds=3, clock=lambda: next(ticks))
    assert not started.record_seen and fake.polls >= 1


# --- the real thing ----------------------------------------------------------------------------


def test_a_detached_process_writes_its_output_to_the_log(tmp_path):
    log = tmp_path / "task" / bg.LOG_FILE_NAME
    process = bg.start_detached([sys.executable, "-c", "print('hello from the background')"], log, tmp_path)
    assert process.wait(timeout=30) == 0
    assert "hello from the background" in log.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="sessions are a POSIX idea")
def test_a_detached_process_is_not_part_of_this_terminals_session(tmp_path):
    getsid = getattr(os, "getsid")  # POSIX only; looked up here so Windows type checkers stay quiet
    log = tmp_path / "task" / bg.LOG_FILE_NAME
    process = bg.start_detached([sys.executable, "-c", "import time; time.sleep(5)"], log, tmp_path)
    try:
        assert getsid(process.pid) != getsid(0)  # Ctrl+C and closing the window miss it
    finally:
        process.kill()
        process.wait()


def test_on_windows_it_starts_without_a_window_and_in_its_own_group(monkeypatch):
    monkeypatch.setattr(bg.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    assert bg.detached_options() == {"creationflags": 0x200 | 0x8000000}


# --- the pipeline records which process runs it --------------------------------------------------


def test_run_task_records_its_process_id(world, monkeypatch):
    from orchestrator.execution import task_runner as tr
    monkeypatch.setattr(tr, "create_worktree", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no worktrees here")))
    script = world["runs"].parent / "claude_runner.py"
    script.write_text("")
    record = tr.run_task(RUN, "T1", plans_dir=world["runs"], runs_dir=world["runs"], repo_path=world["repo"],
                         runner_script=script, env={})
    assert record["pid"] == os.getpid() and record["status"] == "failed"


# --- the command -------------------------------------------------------------------------------


def test_run_task_background_prints_how_to_watch(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "verify_remote_repository", lambda *_args: None)
    seen = {}

    def fake_start(run_id, task_id, **kw):
        seen.update(kw, run_id=run_id, task_id=task_id)
        return bg.Started(pid=777, log=bg.log_path(world["runs"], RUN, "T1"),
                          launch=bg.launch_path(world["runs"], RUN, "T1"), record_seen=True)

    monkeypatch.setattr(cli, "start_task_in_background", fake_start)
    code = cli.main(["run-task", RUN, "T1", "--background", "--plans-dir", str(world["runs"]),
                     "--runs-dir", str(world["runs"]), "--repo-path", str(world["repo"])])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK and seen["task_id"] == "T1"
    assert "in the background (process 777)" in out
    assert f"status {RUN} --watch 2" in out and f"dashboard {RUN}" in out
    assert "keeps running if you close this window" in out


def test_run_task_background_refusal_is_an_error_exit(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "verify_remote_repository", lambda *_args: None)
    save_task(world["runs"], RUN, "T1", {"title": "x"})
    code = cli.main(["run-task", RUN, "T1", "--background", "--plans-dir", str(world["runs"]),
                     "--runs-dir", str(world["runs"]), "--repo-path", str(world["repo"])])
    assert code == cli.EXIT_REFUSED and "has already run" in capsys.readouterr().err