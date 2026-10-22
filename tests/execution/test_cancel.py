"""M3.9: cancelling a running task."""

import json
import subprocess
import sys
import time

import pytest

import orchestrator.cli.commands as cli
from orchestrator.errors import ExecutionRefused
from orchestrator.execution import cancel as cn
from orchestrator.execution.background import launch_path
from orchestrator.execution.runner_adapter import runner_run_directory
from orchestrator.records.run_registry import RunRegistryError, write_json_atomic
from orchestrator.records.task_events import task_events_path
from orchestrator.records.task_registry import load_task, save_task, update_task

RUN, TASK = "run-c", "T1"
OURS = f"C:\\lab\\.venv\\Scripts\\python.exe -m orchestrator run-task {RUN} {TASK} --runs-dir x"


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "orchestration"


def running_task(runs, status="testing", pid=111, launch_pid=None):
    save_task(runs, RUN, TASK, {"title": "t", "pid": pid, "worktree_path": "C:\\wt\\t1"})
    for step in ("building", "testing", "reviewing")[: ("building", "testing", "reviewing").index(status) + 1]:
        update_task(runs, RUN, TASK, {"status": step})
    if launch_pid is not None:
        write_json_atomic(launch_path(runs, RUN, TASK), {"pid": launch_pid})


def agent_progress(role, **data):
    path = runner_run_directory(f"{RUN}-{TASK}-{role}") / "progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class Machine:
    """Pretend processes: pid -> command line. Killing a pid ends its whole tree."""

    def __init__(self, processes, trees=None, stubborn=()):
        self.processes = dict(processes)
        self.trees = trees or {}
        self.stubborn = set(stubborn)
        self.killed = []

    def command_line(self, pid):
        return self.processes.get(pid)

    def kill(self, pid):
        self.killed.append(pid)
        for victim in [pid, *self.trees.get(pid, [])]:
            if victim not in self.stubborn:
                self.processes.pop(victim, None)


def cancel(runs, machine):
    return cn.cancel_task(RUN, TASK, runs_dir=runs, get_command_line=machine.command_line,
                          kill=machine.kill, sleep=lambda s: None)


# --- the normal case ---------------------------------------------------------------------


def test_a_running_task_is_stopped_and_marked_cancelled(runs):
    running_task(runs, pid=111)
    machine = Machine({111: OURS})
    result = cancel(runs, machine)
    assert machine.killed == [111] and result.stopped == [111]
    record = load_task(runs, RUN, TASK)
    assert record["status"] == "cancelled" and record["reasons"][0].startswith("Cancelled by you at")
    assert record["history"][-1]["status"] == "cancelled" and record["cancelled_at"]
    assert result.worktree == "C:\\wt\\t1"
    last = json.loads(task_events_path(runs, RUN, TASK).read_text().splitlines()[-1])
    assert last["step"] == "task_cancelled" and last["stopped"] == [111]


def test_the_windows_launcher_and_the_real_python_are_both_reported_stopped(runs):
    # launch.json holds the .venv launcher (222); it contains the real Python (111).
    running_task(runs, pid=111, launch_pid=222)
    machine = Machine({222: OURS, 111: OURS}, trees={222: [111]})
    result = cancel(runs, machine)
    assert machine.killed == [222]  # one tree kill was enough
    assert result.stopped == [222, 111] and result.already_gone == []


def test_a_running_agent_is_marked_cancelled_too(runs):
    running_task(runs, pid=111)
    path = agent_progress("tester", state="running", pid=333, turns=3)
    result = cancel(runs, Machine({111: OURS}, trees={111: [333]}))
    assert result.agents == ["tester"]
    data = json.loads(path.read_text())
    assert (data["state"], data["status"], data["turns"]) == ("finished", "cancelled", 3)


def test_an_agent_left_running_after_its_pipeline_died_is_stopped(runs):
    running_task(runs, pid=111)
    agent_progress("builder", state="running", pid=333)
    machine = Machine({333: "claude -p --output-format stream-json"})  # 111 is gone
    result = cancel(runs, machine)
    assert machine.killed == [333]
    assert result.already_gone == [111] and result.stopped == [333]
    assert load_task(runs, RUN, TASK)["status"] == "cancelled"


# --- what it leaves alone -------------------------------------------------------------------


def test_a_reused_process_id_is_left_alone(runs):
    running_task(runs, pid=111)
    machine = Machine({111: "C:\\Windows\\notepad.exe"})  # the task died; Windows reused 111
    result = cancel(runs, machine)
    assert machine.killed == [] and result.not_ours == [111]
    assert load_task(runs, RUN, TASK)["status"] == "cancelled"  # nothing is running for it


def test_an_agent_id_now_used_by_another_program_is_left_alone(runs):
    running_task(runs, pid=111)
    agent_progress("builder", state="running", pid=333)
    machine = Machine({333: "C:\\Program Files\\Excel.exe"})
    result = cancel(runs, machine)
    assert machine.killed == [] and result.not_ours == [333]


def test_a_task_that_ended_on_its_own_is_just_marked(runs):
    running_task(runs, pid=111)
    result = cancel(runs, Machine({}))
    assert result.stopped == [] and result.already_gone == [111]
    assert load_task(runs, RUN, TASK)["status"] == "cancelled"


# --- what it refuses ---------------------------------------------------------------------------


@pytest.mark.parametrize("final", ["done", "failed", "needs_review", "cancelled"])
def test_a_finished_task_cannot_be_cancelled(runs, final):
    running_task(runs, status="reviewing")
    update_task(runs, RUN, TASK, {"status": final if final != "needs_review" else "needs_review"})
    with pytest.raises(ExecutionRefused, match=f"already finished .{final}"):
        cancel(runs, Machine({111: OURS}))


def test_a_task_that_never_started_is_an_error(runs):
    with pytest.raises(RunRegistryError):
        cancel(runs, Machine({}))


def test_a_process_that_will_not_stop_leaves_the_task_unchanged(runs):
    running_task(runs, pid=111)
    ticks = iter(range(100))
    machine = Machine({111: OURS}, stubborn=[111])
    with pytest.raises(ExecutionRefused, match="111 did not stop.*not marked cancelled"):
        cn.cancel_task(RUN, TASK, runs_dir=runs, get_command_line=machine.command_line,
                       kill=machine.kill, sleep=lambda s: None, clock=lambda: next(ticks))
    assert load_task(runs, RUN, TASK)["status"] == "testing"


def test_a_task_that_finished_just_before_the_cancel_is_reported(runs):
    running_task(runs, status="reviewing", pid=111)

    def finishes_as_it_dies(pid):
        update_task(runs, RUN, TASK, {"status": "done"})  # it wrote "done" first
        machine.processes.pop(pid, None)

    machine = Machine({111: OURS})
    with pytest.raises(ExecutionRefused, match=r"finished \(done\) just before"):
        cn.cancel_task(RUN, TASK, runs_dir=runs, get_command_line=machine.command_line,
                       kill=finishes_as_it_dies, sleep=lambda s: None)


def test_whose_process_it_is_comes_from_the_command_line():
    assert cn.is_task_process(OURS, RUN, TASK)
    assert not cn.is_task_process(OURS, RUN, "T2")
    assert not cn.is_task_process(OURS.replace(RUN, "run-other"), RUN, TASK)
    assert not cn.is_task_process("python -m orchestrator status run-c T1", RUN, TASK)
    assert cn.is_agent_process("C:\\Users\\a\\.local\\bin\\claude.exe -p")


# --- real processes -----------------------------------------------------------------------------

TREE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def test_a_real_process_and_its_child_are_both_stopped(runs):
    # A stand-in for the pipeline: its arguments look like the task's, and it has a child.
    parent = subprocess.Popen([sys.executable, "-c", TREE, "orchestrator", "run-task", RUN, TASK],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        running_task(runs, pid=parent.pid)
        assert cn.command_line(child_pid) is not None
        result = cn.cancel_task(RUN, TASK, runs_dir=runs)
        assert result.stopped == [parent.pid]
        parent.wait(timeout=10)
        deadline = time.monotonic() + 10
        while cn.command_line(child_pid) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert cn.command_line(child_pid) is None  # the child went too
        assert load_task(runs, RUN, TASK)["status"] == "cancelled"
    finally:
        if parent.poll() is None:
            parent.kill()


def test_command_line_of_a_process_that_does_not_exist_is_none():
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True)
    assert cn.command_line(int(finished.stdout)) is None


# --- the command ---------------------------------------------------------------------------------


def test_cancel_command_says_what_it_stopped_and_where_the_worktree_is(runs, monkeypatch, capsys):
    running_task(runs, pid=111)
    machine = Machine({111: OURS})
    monkeypatch.setattr(cn, "command_line", machine.command_line)
    monkeypatch.setattr(cn, "kill_tree", machine.kill)
    monkeypatch.setattr(cli, "cancel_task", lambda *a, **k: cn.cancel_task(
        *a, **k, get_command_line=machine.command_line, kill=machine.kill, sleep=lambda s: None))
    code = cli.main(["cancel", RUN, TASK, "--runs-dir", str(runs)])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert f"Cancelled {TASK} of run {RUN}." in out and "Stopped:  process 111" in out
    assert 'git worktree remove --force "C:\\wt\\t1"' in out


def test_cancelling_a_finished_task_is_a_refusal(runs, capsys):
    running_task(runs, status="reviewing")
    update_task(runs, RUN, TASK, {"status": "done"})
    code = cli.main(["cancel", RUN, TASK, "--runs-dir", str(runs)])
    assert code == cli.EXIT_REFUSED and "already finished (done)" in capsys.readouterr().err