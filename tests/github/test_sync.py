"""The cards follow the records; run-plan puts the tasks on the board when it starts."""

import argparse
import json
from typing import Any

import pytest

import orchestrator.cli.commands as cli
from orchestrator.github import client as github_client
from orchestrator.github import publish as pub
from orchestrator.scheduling import board_sync as sy
from orchestrator.github.client import GitHubClient
from orchestrator.github.projects import ProjectClient
from orchestrator.records.task_registry import save_task, update_task
from github_fakes import (  # pyright: ignore[reportMissingImports]
    PARENT, RUN, STATUS_FIELD, FakeIssues, FakeProject, make_lab,
)

T1_ITEM, T2_ITEM, PARENT_ITEM = 501, 502, 500 + (8000 + PARENT) % 100


@pytest.fixture
def lab(tmp_path):
    lab = make_lab(tmp_path)
    fakes: Any = (FakeIssues(), FakeProject())  # stand-ins for the real clients
    pub.publish(RUN, plans_dir=lab["plans"], runs_dir=lab["runs"], issues=fakes[0],
                project=fakes[1], write=True, pause=0)
    return lab


def gone(pid):
    return None


def alive_t1(pid):
    return f"python -m orchestrator run-plan {RUN}" if pid == 4242 else None


def sync(lab, issues=None, project=None, **kw):
    issues, project = issues or FakeIssues(), project or FakeProject()
    real: Any = (issues, project)  # stand-ins for the real clients
    kw.setdefault("get_command_line", gone)
    kw.setdefault("pause", 0)
    result = sy.sync_board(RUN, plans_dir=lab["plans"], runs_dir=lab["runs"], issues=real[0],
                           project=real[1], **kw)
    return result, issues, project


def record(lab, task_id, *statuses, **extra):
    save_task(lab["runs"], RUN, task_id, {"status": "pending", **extra})
    for status in statuses:
        update_task(lab["runs"], RUN, task_id, {"status": status})


# --- where each card goes ---------------------------------------------------------------------------


def test_a_new_run_puts_every_card_in_todo(lab):
    result, _, project = sync(lab)
    assert project.moves() == [(T1_ITEM, "o1"), (T2_ITEM, "o1"), (PARENT_ITEM, "o1")]
    assert result.moved == ["T1 -> Todo", "T2 -> Todo", f"#{PARENT} -> Todo"]
    assert all(c[2] == STATUS_FIELD for c in project.calls if c[0] == "move")


def test_nothing_is_sent_when_nothing_changed(lab):
    sync(lab)
    result, issues, project = sync(lab)
    assert project.calls == [] and result.summary() == "Board: up to date"


def test_the_task_run_plan_is_starting_goes_to_in_progress(lab):
    sync(lab)
    result, _, project = sync(lab, starting="T1")
    assert project.moves() == [(T1_ITEM, "o2"), (PARENT_ITEM, "o2")]


def test_a_running_task_is_in_progress(lab):
    record(lab, "T1", "building", "testing", pid=4242)
    _, _, project = sync(lab, get_command_line=alive_t1)
    assert (T1_ITEM, "o2") in project.moves() and (PARENT_ITEM, "o2") in project.moves()


def test_a_done_task_goes_to_done_and_its_issue_is_closed_once(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    result, issues, project = sync(lab)
    assert (T1_ITEM, "o4") in project.moves() and ("close", 101) in issues.calls
    assert (T2_ITEM, "o1") in project.moves()  # T2 is ready now
    assert (PARENT_ITEM, "o2") in project.moves()  # started, not finished
    assert result.closed == [101]
    _, issues, project = sync(lab)
    assert issues.calls == [] and project.calls == []


def test_a_task_needing_a_person_and_the_tasks_it_blocks(lab):
    record(lab, "T1", "building", "needs_review")
    _, _, project = sync(lab)
    assert (T1_ITEM, "o3") in project.moves()
    assert (T2_ITEM, "o3") in project.moves()  # blocked by T1
    assert (PARENT_ITEM, "o3") in project.moves()  # the plan has stopped


def test_an_interrupted_task_needs_a_person(lab):
    record(lab, "T1", "building", pid=4242)  # its process has gone
    _, _, project = sync(lab)
    assert (T1_ITEM, "o3") in project.moves()


def test_a_finished_plan_puts_its_issue_in_done(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    record(lab, "T2", "building", "testing", "reviewing", "done")
    _, issues, project = sync(lab)
    assert (PARENT_ITEM, "o4") in project.moves()
    assert [c for c in issues.calls if c[0] == "close"] == [("close", 101), ("close", 102)]


@pytest.mark.parametrize("states, place", [
    ({"T1": "ready", "T2": "waiting"}, "todo"),
    ({"T1": "done", "T2": "ready"}, "in_progress"),
])
def test_parent_place_between_tasks(states, place):
    from orchestrator.scheduling.plan_progress import PlanProgress, TaskProgress
    progress = PlanProgress(RUN, tuple(TaskProgress(k, k, v) for k, v in states.items()))
    assert sy.parent_place(progress) == place


# --- GitHub failing never stops anything ---------------------------------------------------------------


def test_a_failed_move_is_reported_and_tried_again_next_time(lab):
    result, _, project = sync(lab, project=FakeProject(fail_moves={T1_ITEM: 1}))
    assert project.moves() == [(T2_ITEM, "o1"), (PARENT_ITEM, "o1")]  # the others still moved
    assert result.problems and result.problems[0].startswith("T1: ")
    assert "could not update" in result.summary()
    _, _, project = sync(lab)
    assert project.moves() == [(T1_ITEM, "o1")]


def test_a_failed_close_is_tried_again_next_time(lab):
    record(lab, "T1", "building", "testing", "reviewing", "done")
    result, _, _ = sync(lab, issues=FakeIssues(fail={"close": 1}))
    assert result.closed == [] and result.problems
    result, issues, _ = sync(lab)
    assert ("close", 101) in issues.calls and result.closed == [101]


def test_tasks_not_on_the_board_yet_are_left_alone(lab):
    path = pub.items_path(lab["runs"], RUN)
    data = json.loads(path.read_text())
    del data["tasks"]["T2"]
    path.write_text(json.dumps(data))
    _, _, project = sync(lab)
    assert [m[0] for m in project.moves()] == [T1_ITEM, PARENT_ITEM]


def test_the_parent_card_is_found_once(lab):
    _, issues, project = sync(lab)
    assert ("get", PARENT) in issues.calls and ("add", 8000 + PARENT) in project.calls
    assert pub.load_parent(lab["runs"], RUN)["item_id"] == PARENT_ITEM
    record(lab, "T1", "building", pid=4242)
    _, issues, project = sync(lab, get_command_line=alive_t1)
    assert ("get", PARENT) not in issues.calls and not [c for c in project.calls if c[0] == "add"]


def test_publishing_again_keeps_the_parent_card(lab):
    sync(lab)
    fakes: Any = (FakeIssues(), FakeProject())
    pub.publish(RUN, plans_dir=lab["plans"], runs_dir=lab["runs"], issues=fakes[0],
                project=fakes[1], write=True, pause=0)
    assert pub.load_parent(lab["runs"], RUN)["item_id"] == PARENT_ITEM


# --- run-plan's board work --------------------------------------------------------------------------


def args_for(lab, **extra):
    return argparse.Namespace(run_id=RUN, plans_dir=lab["plans"], runs_dir=lab["runs"],
                              no_board=False, **extra)


def test_run_plan_puts_the_tasks_on_the_board_when_it_starts(tmp_path, monkeypatch, capsys):
    lab = make_lab(tmp_path)  # planned, nothing on GitHub yet (option B)
    issues, project = FakeIssues(), FakeProject()
    monkeypatch.setattr(cli.board, "_board_clients", lambda runs_dir: (issues, project))
    monkeypatch.setattr(cli.board, "publish", lambda *a, **k: pub.publish(*a, **{**k, "pause": 0}))
    monkeypatch.setattr(cli.board, "sync_board", lambda *a, **k: sy.sync_board(*a, **{**k, "pause": 0}))
    follower = cli.board.BoardFollower(args_for(lab))
    follower.start()
    assert [c[0] for c in issues.calls].count("create") == 2
    out = capsys.readouterr().out
    assert "Board: put 2 task(s) on https://github.com/users/Anji-jk/projects/2" in out
    assert "Board: moved T1 -> Todo, T2 -> Todo" in out
    follower.sync(starting="T1")
    assert (T1_ITEM, "o2") in project.moves()


def test_no_board_and_no_connection_leave_github_alone(lab, monkeypatch):
    called = []
    monkeypatch.setattr(cli.board, "_board_clients", lambda runs_dir: called.append(1))
    skipped = args_for(lab)
    skipped.no_board = True
    assert cli.board.BoardFollower(skipped).clients is None  # --no-board
    (lab["runs"] / "board.json").unlink()
    assert cli.board.BoardFollower(args_for(lab)).clients is None  # no board connected
    assert called == []


def test_a_board_failure_is_a_warning_not_a_stop(lab, monkeypatch, capsys):
    class Broken(FakeIssues):
        def list_sub_issues(self, parent):
            raise github_client.GitHubAPIError("GitHub API returned HTTP 503 for issues.")

    (pub.items_path(lab["runs"], RUN)).unlink()
    monkeypatch.setattr(cli.board, "_board_clients", lambda runs_dir: (Broken(), FakeProject()))
    follower = cli.board.BoardFollower(args_for(lab))
    follower.start()  # does not raise
    err = capsys.readouterr().err
    assert "Board publish failed" in err and f"board sync {RUN} catches up later" in err


def test_board_sync_command(lab, monkeypatch, capsys):
    monkeypatch.setattr(cli.board, "_board_clients", lambda runs_dir: (FakeIssues(), FakeProject()))
    monkeypatch.setattr(cli.board, "publish", lambda *a, **k: pub.publish(*a, **{**k, "pause": 0}))
    monkeypatch.setattr(cli.board, "sync_board", lambda *a, **k: sy.sync_board(*a, **{**k, "pause": 0}))
    code = cli.main(["board", "sync", RUN, "--plans-dir", str(lab["plans"]),
                     "--runs-dir", str(lab["runs"])])
    assert code == cli.EXIT_OK
    assert "Board: moved T1 -> Todo, T2 -> Todo, #7 -> Todo" in capsys.readouterr().out



# --- the requests themselves ------------------------------------------------------------------------


class Captured:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)

        class R:
            headers = None

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return R()


def test_moving_a_card_and_closing_an_issue_send_the_documented_requests(monkeypatch):
    captured = Captured()
    ProjectClient("Anji-jk", 2, token="classic", opener=captured).set_field(501, 11, "o4")
    move = captured.requests[0]
    assert move.get_method() == "PATCH"
    assert move.full_url == "https://api.github.com/users/Anji-jk/projectsV2/2/items/501"
    assert json.loads(move.data) == {"fields": [{"id": 11, "value": "o4"}]}

    monkeypatch.setattr(github_client.urllib.request, "urlopen", captured)
    GitHubClient("Anji-jk/Agent-lab", token="t").close_issue(101)
    close = captured.requests[1]
    assert close.get_method() == "PATCH" and close.full_url.endswith("/repos/Anji-jk/Agent-lab/issues/101")
    assert json.loads(close.data) == {"state": "closed", "state_reason": "completed"}
