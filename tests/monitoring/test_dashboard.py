"""M3.5: the local dashboard server, and the events and dashboard commands."""

import http.client
import json

import pytest

from orchestrator.execution.runner_adapter import RUNS_DIR_VARIABLE

import orchestrator.cli.commands as cli
from orchestrator.monitoring import dashboard as dash
from orchestrator.records.task_events import task_events_path
from orchestrator.planning.plan_file import save_task_plan
from orchestrator.records.task_registry import save_task, update_task

RUN = "run-9"


def plan_task(task_id, title, depends=()):
    return {"id": task_id, "title": title, "description": "d", "depends_on": list(depends),
            "expected_outcome": "o", "files": ["src/a.py"], "verification": [],
            "acceptance_criteria": ["it works"], "complexity": "small", "parallel_safe": False}


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A run with a plan, T1 being tested, T2 waiting for T1, and event files for both sides."""
    plans = runs = tmp_path / "orchestration"
    script = tmp_path / "lab" / "scripts" / "claude_runner.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    agents = tmp_path / "lab" / "runs"
    monkeypatch.setenv(RUNS_DIR_VARIABLE, str(agents))  # where the agents' files are
    save_task_plan(plans, RUN, {
        "issue": {"repository": "o/r", "number": 3, "title": "Show stats", "description": "x",
                  "url": "https://github.com/o/r/issues/3"},
        "config": {"repository": "o/r", "base_ref": "main", "base_commit": "abc123", "model": None,
                   "timeout_seconds": 60, "run_id": RUN, "branch": "b", "worktree_path": "w"},
        "summary": "Add numeric stats.",
        "tasks": [plan_task("T1", "Add stats"), plan_task("T2", "Document stats", ["T1"])],
        "planner": {"run_id": f"{RUN}-plan", "status": "succeeded", "total_cost_usd": 0.085,
                    "num_turns": 5},
    })
    save_task(runs, RUN, "T1", {"title": "Add stats", "complexity": "small",
                                "roles": {"builder": "builder: sonnet, low effort, 10 turns, $0.60"}})
    update_task(runs, RUN, "T1", {"status": "building"})
    update_task(runs, RUN, "T1", {"status": "testing"})
    jsonl(agents / f"{RUN}-T1-builder" / "events.jsonl", [
        {"type": "system", "subtype": "init", "cwd": "/wt", "model": "sonnet", "tools": ["Read"]},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/wt/src/a.py"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                  "content": "\n".join(f"l{n}" for n in range(300))}]}},
        {"type": "result", "subtype": "success", "num_turns": 2, "total_cost_usd": 0.1},
    ])
    (agents / f"{RUN}-T1-builder" / "progress.json").write_text(json.dumps(
        {"state": "finished", "status": "succeeded", "turns": 2, "total_cost_usd": 0.1}), encoding="utf-8")
    jsonl(agents / f"{RUN}-plan" / "events.jsonl", [
        {"type": "result", "subtype": "success", "num_turns": 5, "total_cost_usd": 0.085}])
    jsonl(task_events_path(runs, RUN, "T1"), [
        {"at": "2026-10-22T10:00:00+00:00", "step": "task_started", "base_commit": "abc123"},
        {"at": "2026-10-22T10:00:01+00:00", "step": "suite_started", "command": ["pytest"]},
    ])
    data = dash.DashboardData(runs, plans, runner_script=script)
    return {"data": data, "runs": runs, "plans": plans, "script": script, "agents": agents}


@pytest.fixture
def server(lab):
    srv = dash.make_server(lab["data"], port=0)
    dash.serve_in_background(srv)
    yield srv
    srv.shutdown()
    srv.server_close()


def request(server, path, *, method="GET", host=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def get_json(server, path):
    response, body = request(server, path)
    return response.status, json.loads(body)


# --- what the page reads ------------------------------------------------------------------


def test_the_page_and_its_files_are_served_with_a_strict_policy(server):
    response, body = request(server, "/")
    assert response.status == 200 and b"Agent Lab Dashboard" in body
    policy = response.getheader("Content-Security-Policy")
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    assert response.getheader("X-Frame-Options") == "DENY"
    assert b"<script>" not in body  # no inline script, so the policy can forbid them
    js, _ = request(server, "/dashboard.js")
    css, _ = request(server, "/dashboard.css")
    assert js.getheader("Content-Type").startswith("text/javascript")
    assert css.getheader("Content-Type").startswith("text/css")


def test_runs_are_listed(server):
    status, body = get_json(server, "/api/runs")
    assert status == 200
    assert body["runs"][0]["run_id"] == RUN and body["runs"][0]["title"] == "Show stats"


def test_a_run_shows_every_task_and_agent(server):
    status, run = get_json(server, f"/api/run/{RUN}")
    assert status == 200 and run["active"] is True
    assert run["issue"]["url"] == "https://github.com/o/r/issues/3"
    # No progress file for this planner: its numbers come from the plan.
    assert run["planner"] is not None and run["planner"]["from_record"] is True
    t1, t2 = run["tasks"]
    assert (t1["id"], t1["status"], t1["active_role"]) == ("T1", "testing", "tester")
    assert t1["agents"]["builder"]["state"] == "finished" and t1["agents"]["builder"]["cost_usd"] == 0.1
    assert t1["agents"]["tester"]["state"] == "not started"
    assert t1["agents"]["builder"]["settings"].startswith("builder: sonnet")
    assert "tests" in t1["agents"]["tester"]["purpose"]
    assert (t2["status"], t2["waiting_for"]) == ("not started", ["T1"])
    assert t2["agents"]["builder"]["settings"]  # from the plan's complexity
    assert run["planner"]["cost_usd"] == 0.085 and run["planner"]["status"] == "succeeded"
    assert run["cost_usd"] == pytest.approx(0.185)  # planner + T1's builder


def test_an_agents_events_come_in_pages(server):
    status, page = get_json(server, f"/api/events/{RUN}/T1/builder")
    assert status == 200 and page["count"] == 3 and page["next"] == 3
    assert [i["kind"] for i in page["items"]] == ["start", "tool", "finish"]
    assert page["items"][1]["truncated"] is True  # 300 lines of output
    _, later = get_json(server, f"/api/events/{RUN}/T1/builder?after=2")
    assert later["from"] == 2 and [i["kind"] for i in later["items"]] == ["finish"]


def test_one_event_can_be_read_in_full(server):
    status, item = get_json(server, f"/api/event/{RUN}/T1/builder/1")
    assert status == 200 and item["truncated"] is False
    assert item["result"]["detail"].count("\n") == 299


def test_the_orchestrator_and_planner_have_events_too(server):
    _, orchestrator = get_json(server, f"/api/events/{RUN}/T1/orchestrator")
    assert [i["title"] for i in orchestrator["items"]] == ["task started from commit abc123", "full test suite"]
    assert orchestrator["items"][1]["pending"] is True and orchestrator["next"] == 1
    _, planner = get_json(server, f"/api/events/{RUN}/planner")
    assert planner["items"][0]["kind"] == "finish"


def test_an_agent_that_has_not_started_has_no_events(server):
    status, page = get_json(server, f"/api/events/{RUN}/T1/reviewer")
    assert status == 200 and page["items"] == []


# --- what it refuses ----------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/run/no-such-run",
    "/api/run/..%2F..%2Fetc",
    f"/api/events/{RUN}/T1/hacker",
    f"/api/events/{RUN}/..%2F..%2Fsecrets/builder",
    f"/api/events/{RUN}/T1/builder/extra/parts",
    "/api/events/..%2Fx/T1/builder",
    f"/api/event/{RUN}/T1/builder/99",
    f"/api/event/{RUN}/T1/builder/notanumber",
    "/etc/passwd",
    "/dashboard_static/dashboard.js",
])
def test_unknown_or_unsafe_paths_are_not_found(server, path):
    status, body = get_json(server, path)
    assert status == 404 and "error" in body


def test_a_request_for_another_host_name_is_refused(server):
    # A web page using DNS rebinding would arrive with its own host name.
    response, _ = request(server, "/api/runs", host=f"evil.example:{server.server_address[1]}")
    assert response.status == 403


def test_localhost_by_name_is_allowed(server):
    response, _ = request(server, "/api/runs", host=f"localhost:{server.server_address[1]}")
    assert response.status == 200


def test_it_cannot_be_written_to(server):
    response, _ = request(server, "/api/runs", method="POST")
    assert response.status == 405


def test_it_will_not_listen_beyond_this_computer(lab):
    with pytest.raises(ValueError, match="only listens on this computer"):
        dash.make_server(lab["data"], host="0.0.0.0", port=0)


def test_a_broken_file_gives_an_error_not_a_crash(lab, server):
    (lab["runs"] / RUN / "tasks" / "T1" / "task.json").write_text("{broken", encoding="utf-8")
    status, body = get_json(server, f"/api/run/{RUN}")
    assert status in (404, 500) and "error" in body
    response, _ = request(server, "/api/runs")  # the server is still up
    assert response.status == 200


# --- the commands --------------------------------------------------------------------------


@pytest.fixture
def cli_lab(lab, monkeypatch):
    original = dash.DashboardData
    monkeypatch.setattr(cli.dash, "DashboardData",
                        lambda runs, plans: original(runs, plans, runner_script=lab["script"]))
    return lab


def run_cli(lab, *args):
    return cli.main([*args, "--runs-dir", str(lab["runs"]), "--plans-dir", str(lab["plans"])])


def test_events_prints_an_agents_story(cli_lab, capsys):
    assert run_cli(cli_lab, "events", RUN, "T1", "--role", "builder") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "started" in out and "Read      src/a.py" in out and "└ ok      300 lines" in out


def test_events_defaults_to_the_orchestrator_and_shows_waiting_steps(cli_lab, capsys):
    run_cli(cli_lab, "events", RUN, "T1")
    out = capsys.readouterr().out
    assert "full test suite" in out and "waiting..." in out


def test_events_follow_stops_once_the_source_is_finished(cli_lab, capsys, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("should not wait"))
    assert run_cli(cli_lab, "events", RUN, "T1", "--role", "builder", "--follow") == cli.EXIT_OK
    assert capsys.readouterr().out.rstrip().endswith("-- finished --")


def test_events_follow_waits_while_the_orchestrator_works(cli_lab, capsys, monkeypatch):
    sleeps = []

    def finish_after_one_wait(seconds):
        sleeps.append(seconds)
        path = task_events_path(cli_lab["runs"], RUN, "T1")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": "2026-10-22T10:09:00+00:00", "step": "suite_finished",
                                "exit_code": 0, "timed_out": False, "seconds": 480.0, "passed": True,
                                "output_tail": "5 passed"}) + "\n")
        for status in ("reviewing", "done"):
            update_task(cli_lab["runs"], RUN, "T1", {"status": status})

    monkeypatch.setattr(cli.time, "sleep", finish_after_one_wait)
    run_cli(cli_lab, "events", RUN, "T1", "--follow")
    out = capsys.readouterr().out
    assert sleeps == [cli.EVENTS_POLL_SECONDS]
    assert "waiting..." not in out  # a step is printed only once its outcome is known
    assert "passed in 480.0s" in out and out.rstrip().endswith("-- finished --")


def test_events_for_an_unknown_task_says_so(cli_lab, capsys):
    run_cli(cli_lab, "events", RUN, "T7", "--role", "builder")
    assert "No events recorded" in capsys.readouterr().out


def test_dashboard_command_prints_its_address_and_stops_on_ctrl_c(cli_lab, capsys, monkeypatch):
    def interrupted(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(dash.ThreadingHTTPServer, "serve_forever", interrupted)
    code = run_cli(cli_lab, "dashboard", RUN, "--port", "0", "--no-browser")
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK and "http://127.0.0.1:" in out and f"#{RUN}" in out


def test_dashboard_on_a_busy_port_explains_itself(cli_lab, capsys, server):
    code = run_cli(cli_lab, "dashboard", "--port", str(server.server_address[1]), "--no-browser")
    assert code == cli.EXIT_ERROR and "could not listen" in capsys.readouterr().err
