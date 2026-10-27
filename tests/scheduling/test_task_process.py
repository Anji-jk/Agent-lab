"""M7.2: run-plan runs each task as its own process and watches it."""

import os
import sys
from pathlib import Path

import pytest

from orchestrator.records.task_registry import list_tasks, save_task, update_task
from orchestrator.scheduling import run_plan as rp
from orchestrator.scheduling import task_process as tp
from run_plan_support import per_task, stopped, work
from task_runner_support import RUN_ID, Stop  # pyright: ignore[reportMissingImports]


def launcher(world, **kw):
    return tp.ProcessLauncher(RUN_ID, plans_dir=world["plans"], runs_dir=world["runs"],
                              repo_path=world["repo"], **kw)


class Fake:
    """A started task that ends as told; ``then`` runs when it ends (writes its record)."""

    def __init__(self, task_id, action, ended, then=None, polls=2):
        self.task_id, self.action, self.ended, self.then, self.polls = task_id, action, ended, then, polls
        self.stopped = False

    def poll(self):
        self.polls -= 1
        if self.polls > 0:
            return None
        if self.then:
            self.then()
            self.then = None
        return self.ended

    def stop(self):
        self.stopped = True


def no_sleep(monkeypatch):
    monkeypatch.setattr(tp.time, "sleep", lambda s: None)


# --- the command and its log -----------------------------------------------------------------------


@pytest.mark.parametrize("action, flags", [("run", []), ("retry", ["--retry"]),
                                           ("continue", ["--continue"])])
def test_each_step_is_a_run_task_command(world, action, flags):
    command = launcher(world, worktree_root=world["wt"]).command(action, "T1")
    assert command[1:6] == ["-m", "orchestrator", "run-task", RUN_ID, "T1"]
    assert "--worktree-root" in command and command[-len(flags) or len(command):] == flags


def test_each_step_gets_its_own_log_outside_the_task_folder(world):
    first = launcher(world).log_path("T1")
    first.write_text("x")
    second = launcher(world).log_path("T1")
    assert first.name == "T1-1.log" and second.name == "T1-2.log"
    assert first.parent == world["runs"] / RUN_ID / "run-plan-logs"


def test_the_child_starts_in_the_project_folder_with_its_output_in_the_log(world):
    seen = {}

    class Process:
        pid, returncode = 99, None

        def poll(self):
            return None

    def popen(command, **kwargs):
        seen.update(kwargs, command=command)
        return Process()

    handle = launcher(world, popen=popen)("retry", "T2")
    assert seen["cwd"] == tp.PROJECT_ROOT and seen["command"][-1] == "--retry"
    assert handle.log.exists() and handle.poll() is None


def test_a_real_child_that_refuses_is_read_from_its_log(world, monkeypatch):
    script = "print('Starting...'); print('Error: Run budget: no room left.'); raise SystemExit(4)"
    monkeypatch.setattr(tp.ProcessLauncher, "command",
                        lambda self, action, task_id: [sys.executable, "-c", script])
    ended = tp.wait(launcher(world)("run", "T1"), poll_seconds=0.05)
    assert ended == tp.Ended(4, ended.log, "Run budget: no room left.", refused=True)
    assert "Starting..." in ended.log.read_text()


def test_the_last_error_keeps_its_following_lines(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("Error: old\nok\nError: The background run stopped.\n  line two\n")
    assert tp.last_error(log) == "The background run stopped.\n  line two"
    assert tp.last_error(tmp_path / "missing.log") is None


def test_ctrl_c_while_waiting_stops_the_child():
    fake = Fake("T1", "run", tp.Ended(0), polls=99)

    def ctrl_c(seconds):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        tp.wait(fake, sleep=ctrl_c)
    assert fake.stopped


# --- run-plan with tasks in their own processes ----------------------------------------------------


def test_run_plan_reads_each_finished_child_from_its_record(world, agents, monkeypatch):
    """A child that ends normally: here the task is run when the fake process ends."""
    no_sleep(monkeypatch)
    from orchestrator.scheduling import run_plan as rp
    invoke = per_task(agents)
    started = []

    def launch(action, task_id):
        started.append((action, task_id))
        do = lambda: rp._do(action, RUN_ID, task_id, world["wt"], lambda pid: None, dict(
            plans_dir=world["plans"], runs_dir=world["runs"], repo_path=world["repo"],
            runner_script=world["script"], invoke_runner=invoke, test_command=None,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}))
        return Fake(task_id, action, tp.Ended(0), then=do)

    result = work(world, invoke, launch=launch)
    assert result.complete and started == [("run", "T1"), ("run", "T2")]
    assert [s.status for s in result.steps] == ["done", "done"]


def test_a_child_that_refuses_stops_the_plan_with_its_reason(world, agents, monkeypatch):
    no_sleep(monkeypatch)
    launch = lambda action, task_id: Fake(task_id, action, tp.Ended(
        4, Path("T1-1.log"), "Run budget: $0.0000 of $0.10 is spent...", refused=True))
    result = work(world, per_task(agents), launch=launch)
    assert result.refused and result.steps == []
    assert stopped(result) == "T1 was not started: Run budget: $0.0000 of $0.10 is spent..."


def test_a_child_that_dies_before_its_record_stops_the_plan(world, agents, monkeypatch):
    no_sleep(monkeypatch)
    launch = lambda action, task_id: Fake(task_id, action, tp.Ended(
        1, Path("runs/T1-1.log"), "Plan file is unreadable"))
    result = work(world, per_task(agents), launch=launch)
    assert not result.refused and result.steps == []
    assert stopped(result) == ("T1 was not started: Plan file is unreadable Its output: "
                               + str(Path("runs/T1-1.log")))


def test_a_child_that_crashes_part_way_stops_the_plan_and_says_how_to_go_on(world, agents, monkeypatch):
    no_sleep(monkeypatch)
    monkeypatch.setattr(rp, "SETTLE_SECONDS", 0)

    def crash():
        save_task(world["runs"], RUN_ID, "T1", {"status": "pending", "pid": 4242})
        update_task(world["runs"], RUN_ID, "T1", {"status": "building"})
        update_task(world["runs"], RUN_ID, "T1", {"status": "testing"})

    launch = lambda action, task_id: Fake(task_id, action, tp.Ended(
        -9, Path("T1-1.log")), then=crash)
    result = work(world, per_task(agents), launch=launch)

    assert result.steps == []
    assert stopped(result).startswith("T1's process ended (exit code -9) while the task was "
                                      "testing, without finishing it.")
    assert "Run run-plan again: T1 continues from where it stopped." in stopped(result)
    assert "T2" not in list_tasks(world["runs"], RUN_ID)


def test_without_a_launcher_the_step_runs_in_this_process_as_before(world, agents):
    invoke = per_task(agents)
    invoke.behaviours["T1"]["tester"] = lambda cwd: (_ for _ in ()).throw(Stop())
    with pytest.raises(Stop):  # in this process, a stop reaches run-plan itself
        work(world, invoke)


def test_the_cli_runs_each_task_as_its_own_process(world, monkeypatch):
    import orchestrator.cli.commands as cli
    seen = {}

    def fake_work(run_id, **kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(cli, "work_through_plan", fake_work)
    monkeypatch.setattr(cli, "verify_remote_repository", lambda *a: None)
    with pytest.raises(SystemExit):
        cli.main(["run-plan", RUN_ID, "--plans-dir", str(world["plans"]),
                  "--runs-dir", str(world["runs"]), "--repo-path", str(world["repo"]),
                  "--no-board"])
    assert isinstance(seen["launch"], tp.ProcessLauncher)
    assert seen["launch"].worktree_root == seen["worktree_root"]


def test_a_cancel_is_not_taken_for_a_crash(world, monkeypatch):
    """cancel stops the process first and writes "cancelled" just after."""
    reads = iter([{"status": "testing"}, {"status": "testing"}, {"status": "cancelled"}])
    monkeypatch.setattr(rp, "_record", lambda runs_dir, run_id, task_id: next(reads))
    monkeypatch.setattr(rp, "SETTLE_POLL_SECONDS", 0)
    assert rp._settled(world["runs"], RUN_ID, "T1", {"status": "testing"}) == {"status": "cancelled"}


def test_a_crash_is_called_one_once_the_wait_is_over(world, monkeypatch):
    monkeypatch.setattr(rp, "_record", lambda runs_dir, run_id, task_id: {"status": "testing"})
    monkeypatch.setattr(rp, "SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(rp, "SETTLE_POLL_SECONDS", 0.01)
    assert rp._settled(world["runs"], RUN_ID, "T1", {"status": "testing"}) == {"status": "testing"}
