"""A local web page that shows every task of a run and what each agent did.

    python -m orchestrator dashboard [run_id]   ->   http://127.0.0.1:8765

It is a small server built on Python's standard library, so nothing needs
installing. It only reads the files the orchestrator and the runner already
write (plan, task records, progress files, event files) and never changes
anything, so it is safe to open while tasks run.

Safety:
* it listens on the loopback address only, so nobody else on the network can
  reach it -- the events hold whatever the agents read;
* it answers only requests whose Host header names that address, which stops
  a web page in your browser from reaching it through DNS rebinding;
* every run, task and role name is checked before it becomes part of a path;
* the page puts agent text on screen as text, never as HTML, and the Content
  Security Policy forbids inline scripts, so a "<script>" inside a file an
  agent read cannot run.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from orchestrator.monitoring import event_feed

from orchestrator.monitoring import progress

from orchestrator.execution import runner_adapter
from orchestrator.planning.profiles import profile_for, role_settings
from orchestrator.execution.roles import ROLE_NAMES, role
from orchestrator.records.run_registry import RunRegistryError, validate_run_id
from orchestrator.records.task_events import task_events_path
from orchestrator.planning.plan_file import PLAN_FILE_NAME, load_task_plan, plan_path
from orchestrator.records.task_registry import ACTIVE_STATUSES, list_tasks, load_task, validate_task_id

DEFAULT_PORT = 8765
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
}
TABS = (*ROLE_NAMES, "orchestrator")
MAX_RUNS_LISTED = 100

# What each agent waits for, shown on a tab that has not started yet.
WAITS_FOR = {
    "builder": "Starts when the task starts.",
    "tester": "Starts after the builder's changes pass the orchestrator's checks.",
    "reviewer": "Starts after the tester's tests and the full suite pass.",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class NotFound(Exception):
    """The request names something that does not exist or is not allowed."""


def _agent_dict(view: progress.AgentView) -> dict[str, Any]:
    return {
        "role": view.role,
        "state": view.state,
        "status": view.status,
        "turns": view.turns,
        "tokens_read": view.tokens_read,
        "tokens_written": view.tokens_written,
        "last_tool": view.last_tool,
        "heartbeat_age": None if view.heartbeat_age is None else round(view.heartbeat_age, 1),
        "cost_usd": view.cost_usd,
        "stale": view.stale,
        "from_record": view.from_record,
    }


class DashboardData:
    """Everything the page asks for, read from disk on every request."""

    def __init__(self, runs_dir: Path, plans_dir: Path,
                 runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT):
        self.runs_dir = Path(runs_dir)
        self.plans_dir = Path(plans_dir)
        self.runner_script = Path(runner_script)

    # -- the list of runs --
    def runs(self) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for base in {self.plans_dir.resolve(), self.runs_dir.resolve()}:
            if not base.is_dir():
                continue
            for folder in base.iterdir():
                if not folder.is_dir():
                    continue
                try:
                    run_id = validate_run_id(folder.name)
                except ValueError:
                    continue
                has_plan = (folder / PLAN_FILE_NAME).is_file()
                if not has_plan and not (folder / "tasks").is_dir():
                    continue
                entry = found.setdefault(run_id, {"run_id": run_id, "title": "", "issue": None,
                                                  "modified": 0.0})
                entry["modified"] = max(entry["modified"], folder.stat().st_mtime)
                if has_plan and not entry["title"]:
                    try:
                        issue = load_task_plan(self.plans_dir, run_id)["issue"]
                        entry["title"], entry["issue"] = issue.get("title", ""), issue.get("number")
                    except Exception:
                        pass
        runs = sorted(found.values(), key=lambda r: r["modified"], reverse=True)[:MAX_RUNS_LISTED]
        for entry in runs:
            entry["modified"] = datetime.fromtimestamp(entry["modified"], timezone.utc).isoformat()
        return runs

    # -- one run --
    def _plan(self, run_id: str) -> dict[str, Any] | None:
        if not plan_path(self.plans_dir, run_id).is_file():
            return None
        try:
            return load_task_plan(self.plans_dir, run_id)
        except Exception:
            return None

    def run(self, run_id: str) -> dict[str, Any]:
        run_id = validate_run_id(run_id)
        plan = self._plan(run_id)
        records = list_tasks(self.runs_dir, run_id)
        planner = progress.planner_view(run_id, runner_script=self.runner_script)
        if planner is None and plan is not None:  # planned before progress files existed
            planner = progress.agent_view_from_record("planner", plan.get("planner"))
        if plan is None and not records and planner is None:
            raise NotFound(f"No plan or tasks found for run {run_id}.")

        now = datetime.now(timezone.utc)
        planned = plan["tasks"] if plan else []
        order = [t["id"] for t in planned] + [t for t in records if t not in {p["id"] for p in planned}]
        by_id = {t["id"]: t for t in planned}
        tasks = []
        for task_id in order:
            tasks.append(self._task(run_id, task_id, by_id.get(task_id), records.get(task_id),
                                    records, now))

        issue = (plan or {}).get("issue") or {}
        url = issue.get("url")
        return {
            "run_id": run_id,
            "issue": {
                "number": issue.get("number"),
                "title": issue.get("title"),
                "repository": issue.get("repository"),
                "url": url if isinstance(url, str) and url.startswith("https://github.com/") else None,
            },
            "summary": (plan or {}).get("summary", ""),
            "base_commit": ((plan or {}).get("config") or {}).get("base_commit"),
            "planner": _agent_dict(planner) if planner else None,
            "tasks": tasks,
            "active": any(t["active_role"] for t in tasks)
                      or bool(planner and planner.state in ("starting", "running")),
            "cost_usd": round(sum(t["cost_usd"] for t in tasks)
                              + ((planner.cost_usd or 0.0) if planner else 0.0), 6),
            "now": now.isoformat(),
        }

    def _task(self, run_id: str, task_id: str, planned: dict[str, Any] | None,
              record: dict[str, Any] | None, records: dict[str, dict[str, Any]],
              now: datetime) -> dict[str, Any]:
        status = record["status"] if record else "not started"
        depends = (planned or record or {}).get("depends_on") or []
        complexity = (planned or record or {}).get("complexity")
        settings: dict[str, str] = {}
        if record and isinstance(record.get("roles"), dict):
            settings = {k: str(v) for k, v in record["roles"].items()}
        elif complexity:
            try:
                profile = profile_for(complexity)
                settings = {name: role_settings(profile, name).describe() for name in ROLE_NAMES}
            except ValueError:
                settings = {}
        agents = {}
        for name in ROLE_NAMES:
            view = (progress.task_agent_view(run_id, task_id, name, record, self.runner_script, now)
                    if record else progress.agent_view(name, None, now))
            agents[name] = {
                **_agent_dict(view),
                "settings": settings.get(name, ""),
                "purpose": role(name).purpose,
                "waits_for": WAITS_FOR[name],
            }
        active = progress.STAGE_ROLE.get(status) if status in ACTIVE_STATUSES else None
        cost = round(sum(a["cost_usd"] or 0.0 for a in agents.values()), 6)
        return {
            "id": task_id,
            "title": (planned or record or {}).get("title", ""),
            "status": status,
            "depends_on": depends,
            "waiting_for": [d for d in depends if (records.get(d) or {}).get("status") != "done"],
            "complexity": complexity,
            "files": (planned or {}).get("files", []),
            "acceptance_criteria": (planned or {}).get("acceptance_criteria", []),
            "active_role": active,
            "cost_usd": cost,
            "reasons": (record or {}).get("reasons") or [],
            "commit": (record or {}).get("commit"),
            "branch": (record or {}).get("branch"),
            "worktree_path": (record or {}).get("worktree_path"),
            "findings": (record or {}).get("findings") or {},
            "checks": _mask_checks((record or {}).get("checks") or {}),
            "agents": agents,
        }

    # -- events --
    def _source_path(self, run_id: str, source: list[str]) -> tuple[Path, str]:
        run_id = validate_run_id(run_id)
        if source == ["planner"]:
            path = progress.progress_path(f"{run_id}-plan", self.runner_script).with_name(
                event_feed.EVENTS_FILE_NAME)
            return path, "agent"
        if len(source) == 2:
            task_id, tab = source
            try:
                validate_task_id(task_id)
            except ValueError:
                raise NotFound("Unknown task.") from None
            if tab == "orchestrator":
                return task_events_path(self.runs_dir, run_id, task_id), "orchestrator"
            if tab in ROLE_NAMES:
                try:
                    attempt = progress.attempt_of(load_task(self.runs_dir, run_id, task_id))
                except RunRegistryError:
                    attempt = 1
                agent_run = progress.agent_run_id(run_id, task_id, tab, attempt)
                path = progress.progress_path(agent_run, self.runner_script).with_name(
                    event_feed.EVENTS_FILE_NAME)
                return path, "agent"
        raise NotFound("Unknown event source.")

    def items(self, run_id: str, source: list[str]) -> list[event_feed.Item]:
        path, kind = self._source_path(run_id, source)
        if kind == "orchestrator":
            return event_feed.orchestrator_items(path)
        return event_feed.agent_items(path)

    def source_live(self, run_id: str, source: list[str]) -> bool:
        """Whether more events can still arrive for this source."""
        run_id = validate_run_id(run_id)
        if source == ["planner"]:
            view = progress.planner_view(run_id, runner_script=self.runner_script)
            return bool(view and view.state in ("starting", "running"))
        task_id, tab = source
        record = list_tasks(self.runs_dir, run_id).get(task_id)
        if record is None:
            return False
        if tab == "orchestrator":
            return record["status"] in ACTIVE_STATUSES or record["status"] == "pending"
        view = progress.task_agent_view(run_id, task_id, tab, record, self.runner_script,
                                        datetime.now(timezone.utc))
        return view.state in ("starting", "running")

    def events(self, run_id: str, source: list[str], after: int) -> dict[str, Any]:
        return event_feed.feed(self.items(run_id, source), after)

    def event(self, run_id: str, source: list[str], index: int) -> dict[str, Any]:
        items = self.items(run_id, source)
        if not 0 <= index < len(items):
            raise NotFound("No such event.")
        return items[index].to_dict(full=True)


def _mask_checks(checks: dict[str, Any]) -> dict[str, Any]:
    masked = json.loads(json.dumps(checks, default=str))
    for key in ("suite", "named_tests"):
        part = masked.get(key)
        if isinstance(part, dict) and isinstance(part.get("output_tail"), str):
            part["output_tail"] = event_feed.mask_secrets(part["output_tail"])
    return masked


def _int(query: dict[str, list[str]], name: str, default: int = 0) -> int:
    try:
        return max(0, int(query.get(name, [str(default)])[0]))
    except ValueError:
        return default


def make_handler(data: DashboardData, allowed_hosts: frozenset[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentLabDashboard"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            host = (self.headers.get("Host") or "").lower()
            if host not in allowed_hosts:
                self._json(403, {"error": "This dashboard only answers on its local address."})
                return
            url = urlsplit(self.path)
            try:
                if url.path in STATIC_FILES:
                    name, content_type = STATIC_FILES[url.path]
                    self._send(200, (STATIC_DIR / name).read_bytes(), content_type)
                    return
                parts = [unquote(p) for p in url.path.split("/") if p]
                query = parse_qs(url.query)
                if parts[:2] == ["api", "runs"] and len(parts) == 2:
                    self._json(200, {"runs": data.runs()})
                elif parts[:2] == ["api", "run"] and len(parts) == 3:
                    self._json(200, data.run(parts[2]))
                elif parts[:2] == ["api", "events"] and len(parts) >= 4:
                    self._json(200, data.events(parts[2], parts[3:], _int(query, "after")))
                elif parts[:2] == ["api", "event"] and len(parts) >= 5:
                    try:
                        index = int(parts[-1])
                    except ValueError:
                        raise NotFound("No such event.") from None
                    self._json(200, data.event(parts[2], parts[3:-1], index))
                else:
                    raise NotFound("Not found.")
            except NotFound as exc:
                self._json(404, {"error": str(exc)})
            except ValueError as exc:  # an invalid run or task name
                self._json(404, {"error": str(exc) or "Not found."})
            except Exception as exc:  # never let one bad file stop the page
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _refuse(self) -> None:
            self._json(405, {"error": "The dashboard is read-only."})

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is already
    # in use, so a busy port would not be noticed. Elsewhere it only allows
    # restarting quickly after a stop, which is wanted.
    allow_reuse_address = os.name != "nt"


def make_server(data: DashboardData, host: str = "127.0.0.1", port: int = DEFAULT_PORT
                ) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("The dashboard only listens on this computer (127.0.0.1, localhost or ::1).")
    server = _Server((host, port), make_handler(data, frozenset()))
    real_port = server.server_address[1]
    allowed = frozenset({f"127.0.0.1:{real_port}", f"localhost:{real_port}", f"[::1]:{real_port}"})
    server.RequestHandlerClass = make_handler(data, allowed)
    server.daemon_threads = True
    return server


def serve_in_background(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
