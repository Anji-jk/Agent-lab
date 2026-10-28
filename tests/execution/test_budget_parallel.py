"""M7.4: the run budget counts what agents running in other tasks may still spend."""

import threading

import pytest

from orchestrator.execution import budget as bg
from orchestrator.execution import task_runner as tr
from orchestrator.records.task_registry import load_task, save_task
from task_runner_support import RUN_ID, run  # pyright: ignore[reportMissingImports]

ALIVE = lambda task_id, record: (True, "its process is running")  # noqa: E731
GONE = lambda task_id, record: (False, "its process has gone")  # noqa: E731


def reserved(world, task_id, role, cap, status="building", attempt=1):
    save_task(world["runs"], RUN_ID, task_id, {
        "status": status,
        "reservation": {"role": role, "cap_usd": cap, "attempt": attempt, "at": "2026-10-27T00:00:00+00:00"},
    })


def problem(world, role, cap, task_id="T3", alive=ALIVE):
    return bg.budget_problem(world["runs"], RUN_ID, role, cap, world["script"], task_id, alive)


# --- the rule ---------------------------------------------------------------------------------------


def test_an_agent_running_elsewhere_keeps_its_whole_cap_back(with_independent):
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 1.00)
    reserved(world, "T1", "builder", 0.60)

    assert problem(world, "tester", 0.25) is None  # 0.60 + 0.25 fits in 1.00
    text = problem(world, "builder", 0.60)  # 0.60 + 0.60 does not
    assert text is not None
    assert "$0.6000 more may still be spent by agents running in other tasks (T1's builder)" in text
    assert "the builder may spend up to $0.60" in text
    assert "run run-plan again" in text and f"budget {RUN_ID} --set" in text


def test_only_what_the_running_agent_has_not_spent_yet_is_kept_back(with_independent, monkeypatch):
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 1.00)
    reserved(world, "T1", "builder", 0.60)
    # T1's builder has cost $0.50 so far: that is already in "spent", only $0.10 can still come.
    monkeypatch.setattr(bg, "_agent_cost", lambda run_id, task_id, role, attempt, script:
                        0.50 if (task_id, role) == ("T1", "builder") else None)

    assert bg.reserved_elsewhere(world["runs"], RUN_ID, "T3", ALIVE, world["script"]) == [
        ("T1", "builder", 0.10)]
    # spent 0.50 + 0.10 kept back + 0.25 = 0.85 fits; counting the cap twice (1.10 + ...) would not.
    assert problem(world, "tester", 0.25) is None


def test_a_task_whose_process_has_gone_keeps_nothing_back(with_independent):
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 1.00)
    reserved(world, "T1", "builder", 0.60)
    assert problem(world, "builder", 0.60, alive=GONE) is None


def test_a_task_does_not_count_its_own_reservation(with_independent):
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 1.00)
    reserved(world, "T1", "builder", 0.60)
    assert problem(world, "builder", 0.60, task_id="T1") is None


def test_without_the_task_and_liveness_the_old_rule_applies(with_independent):
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 1.00)
    reserved(world, "T1", "builder", 0.60)
    assert bg.budget_problem(world["runs"], RUN_ID, "builder", 0.60, world["script"]) is None
    only = bg.budget_problem(world["runs"], RUN_ID, "builder", 1.20, world["script"])
    assert only is not None and "running in other tasks" not in only


# --- reserving ----------------------------------------------------------------------------------------


def started(world, task_id, status="building"):
    save_task(world["runs"], RUN_ID, task_id, {"status": status})


def reserve(world, task_id, role, cap):
    return bg.reserve_agent(world["runs"], RUN_ID, task_id, role, 1, cap, ALIVE, world["script"])


def test_a_reservation_is_written_to_the_record_and_released(with_independent):
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 1.00)
    started(world, "T1")

    assert reserve(world, "T1", "builder", 0.60) is None
    assert load_task(world["runs"], RUN_ID, "T1")["reservation"]["cap_usd"] == 0.60
    bg.release_agent(world["runs"], RUN_ID, "T1")
    assert load_task(world["runs"], RUN_ID, "T1")["reservation"] is None


def test_the_second_task_is_refused_while_the_first_agent_runs_and_fits_after(with_independent):
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 1.00)
    started(world, "T1")
    started(world, "T3")

    assert reserve(world, "T1", "builder", 0.60) is None
    refused = reserve(world, "T3", "builder", 0.60)
    assert refused is not None and "T1's builder" in refused
    assert "reservation" not in load_task(world["runs"], RUN_ID, "T3")  # nothing was reserved

    bg.release_agent(world["runs"], RUN_ID, "T1")
    assert reserve(world, "T3", "builder", 0.60) is None


def test_with_no_budget_nothing_is_reserved(with_independent):
    world = with_independent
    started(world, "T1")
    assert reserve(world, "T1", "builder", 0.60) is None
    assert "reservation" not in load_task(world["runs"], RUN_ID, "T1")
    bg.release_agent(world["runs"], RUN_ID, "T1")  # harmless


def test_releasing_a_task_with_no_record_is_harmless(world):
    bg.release_agent(world["runs"], RUN_ID, "T9")


def test_two_tasks_starting_at_the_same_moment_cannot_both_have_the_money(with_independent):
    """The budget fits one builder ($0.60) but not two: exactly one may start."""
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 0.70)
    started(world, "T1")
    started(world, "T3")
    results = {}
    barrier = threading.Barrier(2, timeout=30)

    def attempt(task_id):
        barrier.wait()
        results[task_id] = reserve(world, task_id, "builder", 0.60)

    threads = [threading.Thread(target=attempt, args=(t,)) for t in ("T1", "T3")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(r is None for r in results.values()) == [False, True]
    assert sum("reservation" in load_task(world["runs"], RUN_ID, t) and
               load_task(world["runs"], RUN_ID, t)["reservation"] is not None
               for t in ("T1", "T3")) == 1


# --- in a task ------------------------------------------------------------------------------------------


def test_an_agent_has_its_cap_reserved_while_it_runs_and_not_after(world, agents):
    bg.set_budget(world["runs"], RUN_ID, 5.00)
    seen = []

    def watch(**kwargs):
        record = load_task(world["runs"], RUN_ID, "T1")
        seen.append((record["status"], (record.get("reservation") or {}).get("role")))
        return agents(**kwargs)

    record = run(world, agents, invoke_runner=watch)

    assert record["status"] == "done", record["reasons"]
    assert seen == [("building", "builder"), ("testing", "tester"), ("reviewing", "reviewer")]
    assert load_task(world["runs"], RUN_ID, "T1")["reservation"] is None


def test_a_task_that_crashes_in_an_agent_still_releases_it(world, agents):
    bg.set_budget(world["runs"], RUN_ID, 5.00)

    def boom(**kwargs):
        raise RuntimeError("the runner blew up")

    record = run(world, agents, invoke_runner=boom)
    assert record["status"] != "done"
    assert load_task(world["runs"], RUN_ID, "T1")["reservation"] is None


def test_a_task_is_refused_before_anything_when_the_money_is_kept_for_others(with_independent, agents,
                                                                           monkeypatch):
    world = with_independent
    bg.set_budget(world["runs"], RUN_ID, 1.00)
    reserved(world, "T1", "builder", 0.60)
    monkeypatch.setattr(tr, "_liveness", lambda runs_dir, run_id, script: ALIVE)

    with pytest.raises(bg.BudgetExhausted, match=r"running in other tasks \(T1's builder\)"):
        run(world, agents, task_id="T3")
    assert agents.calls == []
    assert not (world["wt"]).exists() or list(world["wt"].iterdir()) == []  # no worktree was made
