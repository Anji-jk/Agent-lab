"""M7.3: run-plan runs the tasks that may run together side by side."""

import json
import os
import threading

import pytest

from orchestrator.errors import ExecutionRefused
from orchestrator.execution.integration import integration_path
from orchestrator.records.task_registry import TASK_RECORD_NAME, list_tasks, load_task, save_task, task_directory
from orchestrator.scheduling import run_plan as rp
from orchestrator.scheduling import task_process as tp
from orchestrator.scheduling.plan_progress import compute_progress
from run_plan_support import no_process, per_task, stopped, work
from task_runner_support import RUN_ID, git, task  # pyright: ignore[reportMissingImports]


# --- what may start together (no files, no processes) ----------------------------------------------


def plan_of(*tasks):
    return {"tasks": [{"title": t["id"], "depends_on": [], "parallel_safe": False, **t}
                      for t in tasks]}


def decide(plan, records=None, active=(), slots=3, alive=()):
    records = records or {}
    progress = compute_progress("r", plan, records, lambda t, r: (t in alive, "its process"))
    safe = {t["id"]: t["parallel_safe"] for t in plan["tasks"]}
    return rp.next_actions(progress, records, set(active), safe, slots)


def starts(decision):
    return [f"{action} {task_id}" for action, task_id in decision.start]


def safe(task_id, **kw):
    return {"id": task_id, "parallel_safe": True, **kw}


def alone(task_id, **kw):
    return {"id": task_id, "parallel_safe": False, **kw}


def test_independent_parallel_safe_tasks_all_start():
    got = decide(plan_of(safe("T1"), safe("T2"), safe("T3")))
    assert starts(got) == ["run T1", "run T2", "run T3"]


def test_no_more_than_the_ceiling_start_and_the_rest_wait():
    plan = plan_of(*(safe(f"T{n}") for n in range(1, 6)))
    assert starts(decide(plan, slots=3)) == ["run T1", "run T2", "run T3"]
    assert starts(decide(plan, slots=2)) == ["run T1", "run T2"]
    # Two already run: one more fits.
    got = decide(plan, {"T1": {"status": "building"}, "T2": {"status": "building"}},
                 active={"T1", "T2"}, slots=3, alive={"T1", "T2"})
    assert starts(got) == ["run T3"]


def test_a_task_that_is_not_parallel_safe_runs_by_itself():
    assert starts(decide(plan_of(alone("T1"), safe("T2")))) == ["run T1"]


def test_a_sequential_task_is_not_passed_over_by_the_ones_after_it():
    got = decide(plan_of(safe("T1"), alone("T2"), safe("T3")))
    assert starts(got) == ["run T1"]  # T2 waits for T1; T3 waits behind T2


def test_nothing_joins_a_task_running_by_itself():
    plan = plan_of(alone("T1"), safe("T2"))
    got = decide(plan, {"T1": {"status": "building"}}, active={"T1"}, alive={"T1"})
    assert got.start == () and got.stop is None  # wait for T1


def test_a_task_waits_for_the_ones_it_depends_on():
    got = decide(plan_of(safe("T1"), safe("T2", depends_on=["T1"]), safe("T3")))
    assert starts(got) == ["run T1", "run T3"]


def test_a_task_started_here_is_not_started_again_before_its_record_exists():
    got = decide(plan_of(safe("T1"), safe("T2")), active={"T1"})
    assert starts(got) == ["run T2"]


def test_interrupted_tasks_continue_before_new_ones_start():
    plan = plan_of(safe("T1"), safe("T2"))
    got = decide(plan, {"T1": {"status": "testing"}})  # no live process: interrupted
    assert starts(got) == ["continue T1", "run T2"]


def test_a_task_a_person_must_look_at_starts_nothing_more():
    plan = plan_of(safe("T1"), safe("T2"), safe("T3"))
    records = {"T1": {"status": "needs_review", "reasons": ["the reviewer found a problem"]},
               "T2": {"status": "building"}}
    got = decide(plan, records, active={"T2"}, alive={"T2"})
    assert got.start == () and "T1 needs review" in (got.stop or "")


def test_a_task_running_in_another_process_stops_the_plan_from_starting_more():
    plan = plan_of(safe("T1"), safe("T2"))
    got = decide(plan, {"T1": {"status": "building"}}, alive={"T1"})
    assert got.start == () and "T1 is running in another process" in (got.stop or "")


def test_a_finished_plan_is_complete():
    plan = plan_of(safe("T1"))
    assert decide(plan, {"T1": {"status": "done"}}).complete



# --- the ceiling ---------------------------------------------------------------------------------


@pytest.mark.parametrize("given, expected", [(None, 3), ("", 3), ("1", 1), ("2", 2), ("8", 8)])
def test_the_ceiling_is_the_setting_or_three(given, expected):
    environ = {} if given is None else {rp.MAX_PARALLEL_VARIABLE: given}
    assert rp.max_parallel_setting(environ=environ) == expected


@pytest.mark.parametrize("given", ["0", "9", "-1", "two", "2.5"])
def test_a_ceiling_that_is_not_a_number_from_one_to_eight_is_refused_with_the_fix(given):
    with pytest.raises(ExecutionRefused, match="AGENT_LAB_MAX_PARALLEL.*whole number from 1 to 8"):
        rp.max_parallel_setting(environ={rp.MAX_PARALLEL_VARIABLE: given})


def test_one_at_a_time_wins_over_the_setting():
    assert rp.max_parallel_setting(True, {rp.MAX_PARALLEL_VARIABLE: "5"}) == 1


# --- run-plan with stand-in processes -----------------------------------------------------------------


def mark_safe(world, *task_ids):
    path = world["plans"] / RUN_ID / "plan.json"
    data = json.loads(path.read_text())
    for item in data["tasks"]:
        item["parallel_safe"] = item["id"] in task_ids
    path.write_text(json.dumps(data))


def add_safe(world, *ids):
    path = world["plans"] / RUN_ID / "plan.json"
    data = json.loads(path.read_text())
    for task_id in ids:
        data["tasks"].append(task(task_id, f"extra {task_id}", ["it works"],
                                  files=(f"{task_id.lower()}.py",)))
    path.write_text(json.dumps(data))


def put(world, task_id, fields):
    """Write a task's record, creating it the first time (a continued task already has one)."""
    try:
        load_task(world["runs"], RUN_ID, task_id)
    except Exception:  # noqa: BLE001  (no record yet)
        save_task(world["runs"], RUN_ID, task_id, fields)
    else:  # a continued task is written over directly: the real one reopens its record
        path = task_directory(world["runs"], RUN_ID, task_id) / TASK_RECORD_NAME
        record = json.loads(path.read_text())
        record.update(fields)
        record.pop("budget_stop", None) if "budget_stop" not in fields else None
        record.pop("limit_stop", None) if "limit_stop" not in fields else None
        path.write_text(json.dumps(record))


class Stand:
    """A started task that ends after ``polls`` looks and then leaves ``status`` as its record.

    ``extra`` fields (a budget or usage-limit stop) are written into the record with it.
    """

    extra: dict = {}

    def __init__(self, world, log, task_id, action, polls, status="done"):
        self.world, self.log, self.task_id, self.action = world, log, task_id, action
        self.polls, self.status, self.stopped = polls, status, False

    def poll(self):
        self.polls -= 1
        if self.polls > 0:
            return None
        put(self.world, self.task_id, {"status": self.status, "title": "t", **self.extra})
        self.log.append(("end", self.task_id))
        return tp.Ended(0)

    def stop(self):
        self.stopped = True


def launcher(world, log, how_long, status=None):
    """Starts stand-ins; ``how_long`` is each task's number of looks, ``status`` its result."""
    def launch(action, task_id):
        log.append(("start", task_id))
        return Stand(world, log, task_id, action, how_long.get(task_id, 1),
                     (status or {}).get(task_id, "done"))
    return launch


def together(log):
    """The most tasks that were running at one time."""
    live, most = set(), 0
    for what, task_id in log:
        live.add(task_id) if what == "start" else live.discard(task_id)
        most = max(most, len(live))
    return most


def go(world, agents, launch, **overrides):
    return work(world, per_task(agents), launch=launch, **overrides)


def test_tasks_that_may_run_together_are_started_before_either_ends(world, with_independent, agents,
                                                                      monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world, "T1", "T3")
    log = []
    result = go(world, agents, launcher(world, log, {"T1": 2, "T3": 3, "T2": 1}), max_parallel=3)

    assert log[:2] == [("start", "T1"), ("start", "T3")]
    assert together(log) == 2
    assert result.complete
    assert [(s.task_id, s.status) for s in result.steps] == [("T1", "done"), ("T3", "done"),
                                                              ("T2", "done")]
    # T2 depends on T1: it started only once T1 had ended.
    assert log.index(("start", "T2")) > log.index(("end", "T1"))


def test_one_at_a_time_runs_them_one_after_the_other(world, with_independent, agents, monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world, "T1", "T3")
    log = []
    result = go(world, agents, launcher(world, log, {}), max_parallel=1)
    assert result.complete and together(log) == 1


def test_tasks_the_plan_does_not_mark_parallel_safe_never_overlap(world, with_independent, agents,
                                                                    monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world)  # none is
    log = []
    result = go(world, agents, launcher(world, log, {"T1": 3, "T3": 3}), max_parallel=3)
    assert result.complete and together(log) == 1


def test_the_ceiling_limits_how_many_run_together(world, with_independent, agents, monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    add_safe(world, "T4", "T5")
    mark_safe(world, "T1", "T3", "T4", "T5")
    log = []
    how_long = {"T1": 2, "T3": 3, "T4": 4, "T5": 2, "T2": 1}
    result = go(world, agents, launcher(world, log, how_long), max_parallel=3)

    assert result.complete and together(log) == 3
    assert [t for what, t in log[:3]] == ["T1", "T3", "T4"]  # T5 waited for a free place
    # T2 is not parallel-safe: it ran by itself, and T5 (after it in plan order) waited for it.
    start_t2 = log.index(("start", "T2"))
    assert {t for what, t in log[:start_t2] if what == "start"} >= {"T1", "T3", "T4"}
    assert [what for what, t in log[start_t2 - 3:start_t2]].count("end") == 3
    assert log.index(("start", "T5")) > log.index(("end", "T2"))


def test_when_one_task_needs_a_person_the_others_finish_and_nothing_new_starts(
        world, with_independent, agents, monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world, "T1", "T3")
    log = []
    result = go(world, agents,
                launcher(world, log, {"T3": 1, "T1": 3}, {"T3": "needs_review"}), max_parallel=3)

    assert not result.complete
    assert "T3 needs review" in stopped(result)
    assert [(s.task_id, s.status) for s in result.steps] == [("T3", "needs_review"), ("T1", "done")]
    assert ("start", "T2") not in log  # T1 finished, but T2 was not started


def test_a_task_that_crashed_stops_the_plan_after_the_others_finish(world, with_independent, agents,
                                                                     monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    monkeypatch.setattr(rp, "SETTLE_SECONDS", 0)
    mark_safe(world, "T1", "T3")
    log = []

    class Crash(Stand):
        def poll(self):
            self.polls -= 1
            if self.polls > 0:
                return None
            save_task(self.world["runs"], RUN_ID, self.task_id, {"status": "testing"})
            self.log.append(("end", self.task_id))
            return tp.Ended(-9)

    def launch(action, task_id):
        log.append(("start", task_id))
        kind = Crash if task_id == "T1" else Stand
        return kind(world, log, task_id, action, 1 if task_id == "T1" else 3)

    result = go(world, agents, launch, max_parallel=3)
    assert [(s.task_id, s.status) for s in result.steps] == [("T3", "done")]
    assert "T1's process ended (exit code -9) while the task was testing" in stopped(result)
    assert ("start", "T2") not in log


def test_a_refusal_is_reported_and_marks_the_result_refused(world, with_independent, agents,
                                                              monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world, "T1", "T3")

    class Refused:
        def __init__(self, task_id):
            self.task_id, self.action, self.stopped = task_id, "run", False

        def poll(self):
            return tp.Ended(4, None, "Run budget: no room left.", refused=True)

        def stop(self):
            self.stopped = True

    result = go(world, agents, lambda action, task_id: Refused(task_id), max_parallel=3)
    assert result.refused and result.steps == []
    assert stopped(result).startswith("T1 was not started: Run budget: no room left.")


def test_ctrl_c_stops_every_running_task(world, with_independent, agents, monkeypatch):
    mark_safe(world, "T1", "T3")
    started = []

    def launch(action, task_id):
        started.append(Stand(world, [], task_id, action, 10 ** 6))
        return started[-1]

    def ctrl_c(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(rp.time, "sleep", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        go(world, agents, launch, max_parallel=3)
    assert [s.task_id for s in started] == ["T1", "T3"] and all(s.stopped for s in started)
    assert not rp.lock_path(world["runs"], RUN_ID).exists()  # the run's lock is released


# --- M7.5: stops while several tasks run ----------------------------------------------------------------


class StandWith(Stand):
    """A Stand whose record also carries ``extra``."""


def launcher_with(world, log, how_long, extra):
    def launch(action, task_id):
        log.append(("start", task_id, action))
        kind = StandWith if task_id in extra else Stand
        stand = kind(world, [], task_id, action, how_long.get(task_id, 1),
                     "needs_review" if task_id in extra else "done")
        if task_id in extra:
            stand.extra = extra[task_id]
        return stand
    return launch


BUDGET = {"budget_stop": True, "reasons": ["Run budget: no room left for the next agent."]}
LIMIT_LATER = {"limit_stop": {"resets_at": "2999-01-01T00:00:00+00:00"},
               "reasons": ["Claude's usage limit."]}
LIMIT_PAST = {"limit_stop": {"resets_at": "2000-01-01T00:00:00+00:00"},
              "reasons": ["Claude's usage limit."]}


def test_a_budget_stop_lets_the_other_task_finish_and_it_continues_next_time(
        world, with_independent, agents, monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world, "T1", "T3")
    log = []
    result = go(world, agents, launcher_with(world, log, {"T3": 1, "T1": 3}, {"T3": BUDGET}),
                max_parallel=3)

    assert not result.complete
    assert "T3 was stopped by the run budget" in stopped(result) and "T3 continues" in stopped(result)
    assert [(s.task_id, s.status) for s in result.steps] == [("T3", "needs_review"), ("T1", "done")]
    assert result.steps[0].budget_stop
    assert not any(t == "T2" for _, t, _ in log)  # nothing new started

    again = []
    go(world, agents, launcher_with(world, again, {}, {}), max_parallel=3)
    assert again[0] == ("start", "T3", "continue")  # before anything new


def test_a_usage_limit_stop_lets_the_other_task_finish_and_waits_for_the_reset(
        world, with_independent, agents, monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world, "T1", "T3")
    log = []
    result = go(world, agents, launcher_with(world, log, {"T3": 1, "T1": 3}, {"T3": LIMIT_LATER}),
                max_parallel=3)

    assert "T3 was stopped by Claude's usage limit" in stopped(result)
    assert [(s.task_id, s.status) for s in result.steps] == [("T3", "needs_review"), ("T1", "done")]
    assert not any(t == "T2" for _, t, _ in log)

    before = []  # the reset has not come: run-plan again starts nothing
    again = go(world, agents, launcher_with(world, before, {}, {}), max_parallel=3)
    assert before == [] and "resets at" in stopped(again)


def test_after_the_reset_the_limit_stopped_task_continues(world, with_independent, agents, monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world, "T1", "T3")
    go(world, agents, launcher_with(world, [], {"T3": 1, "T1": 3}, {"T3": LIMIT_PAST}), max_parallel=3)

    again = []
    go(world, agents, launcher_with(world, again, {}, {}), max_parallel=3)
    assert again[0] == ("start", "T3", "continue")


def test_when_two_tasks_stop_the_first_reason_is_the_one_given(world, with_independent, agents,
                                                                monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    mark_safe(world, "T1", "T3")
    extra = {"T3": BUDGET, "T1": LIMIT_LATER}
    result = go(world, agents, launcher_with(world, [], {"T3": 1, "T1": 2}, extra), max_parallel=3)

    assert "T3 was stopped by the run budget" in stopped(result)
    assert "usage limit" not in stopped(result)
    assert [s.task_id for s in result.steps] == ["T3", "T1"]  # both are in the report


# --- really at the same time ----------------------------------------------------------------------


class Thread:
    """A started task running in a thread of this process, like a child process would."""

    def __init__(self, task_id, action, target):
        self.task_id, self.action, self.error = task_id, action, None

        def body():
            try:
                target()
            except BaseException as exc:  # noqa: BLE001  (reported to run-plan as the exit code)
                self.error = exc

        self.thread = threading.Thread(target=body, daemon=True)
        self.thread.start()

    def poll(self):
        if self.thread.is_alive():
            return None
        return tp.Ended(0 if self.error is None else 1, None, str(self.error or "") or None)

    def stop(self):
        return None


def test_two_tasks_really_run_at_the_same_time_and_are_merged_into_the_run_branch(
        world, with_independent, agents):
    """Both builders wait for each other: this only gets past if the tasks overlap."""
    mark_safe(world, "T1", "T3")
    barrier = threading.Barrier(2, timeout=60)
    lock = threading.Lock()
    waited: set[str] = set()
    inner = per_task(agents)

    def invoke(**kwargs):
        task_id = kwargs["run_id"][len(RUN_ID) + 1:].split("-")[0]
        with lock:
            first = task_id not in waited
            waited.add(task_id)
        if first and task_id in ("T1", "T3"):
            barrier.wait()  # the builder of the other task must be running too
        with lock:  # the stand-in agents share state; the tasks around them do not
            return inner(**kwargs)

    common = dict(plans_dir=world["plans"], runs_dir=world["runs"], repo_path=world["repo"],
                  runner_script=world["script"], invoke_runner=invoke, test_command=None,
                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def launch(action, task_id):
        return Thread(task_id, action, lambda: rp._do(action, RUN_ID, task_id, world["wt"],
                                                      no_process, common))

    result = work(world, invoke, launch=launch, max_parallel=3)

    assert result.complete, result.stopped
    assert sorted(s.task_id for s in result.steps) == ["T1", "T2", "T3"]
    records = list_tasks(world["runs"], RUN_ID)
    assert {t: r["status"] for t, r in records.items()} == {"T1": "done", "T2": "done", "T3": "done"}

    saved = json.loads(integration_path(world["runs"], RUN_ID).read_text())
    kinds = {m["task"]: m["kind"] for m in saved["merges"]}
    first, second = sorted(("T1", "T3"), key=lambda t: [m["task"] for m in saved["merges"]].index(t))
    assert kinds[first] == "fast-forward"
    assert kinds[second] == "merge"  # it started from the old head: merged, and the suite ran on it
    assert records[second]["merge"]["suite"]["exit_code"] == 0
    # The run's branch holds all three tasks' work.
    head = saved["head"]
    for task_id in ("T1", "T2", "T3"):
        assert git(world["repo"], "merge-base", "--is-ancestor", records[task_id]["commit"], head) == ""
    assert git(world["repo"], "rev-parse", "main") == world["base"]  # your own branch is untouched