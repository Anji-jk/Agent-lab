"""The project's database: ``.agent-lab/agentlab.db``, an index of the run files.

The files stay exactly as they are and stay the source of truth; this database is built from
them so runs, tasks and costs can be queried in one place. It can be deleted at any time and
rebuilt with ``db rebuild``. What goes in, from which file:

=============  ================================  ============================================
table          one row per                       from
=============  ================================  ============================================
runs           run                               plan.json, integration.json
tasks          task in a run                     plan.json, tasks/<id>/task.json, its log
attempts       try of a task                     tasks/<id>/task.json, tasks/<id>/attempt-<n>/
agent_runs     builder/tester/reviewer/planner   task.json's agents, agents/<id>/result.json,
                                                 agents/<id>/stderr.log (its end, on failure)
merges         task merged into the run branch   integration.json
=============  ================================  ============================================

Live files stay files: the dashboard reads events.jsonl and progress.json while agents work.
Long texts are kept short (the end of a log, not all of it); every row says where the full file
is. A run is refreshed after each of its tasks ends; writing the database is best effort and
never stops a task. When the code's schema is newer than the file's, the file is rebuilt.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from orchestrator.core.attempts import agent_run_id, attempt_of
from orchestrator.records.run_registry import utc_now

DB_FILE_NAME = "agentlab.db"
SCHEMA_VERSION = 1
TAIL_LINES = 30
TAIL_CHARS = 4000
PLAN_FILE = "plan.json"
TASK_FILE = "task.json"
INTEGRATION_FILE = "integration.json"
BOARD_FILE = "board_items.json"
REPORT_FILE = "report.md"
TASK_LOG_FILE = "run-task.log"
RUN_LOG_FOLDER = "run-plan-logs"
RESULT_FILE = "result.json"
STDERR_FILE = "stderr.log"
ROLES = ("builder", "tester", "reviewer")
ENDED = {"done", "failed", "needs_review", "cancelled"}
WORKING = {"pending", "building", "testing", "reviewing"}

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    source_kind TEXT,
    item TEXT,
    repository TEXT,
    title TEXT,
    url TEXT,
    epic TEXT,
    base_ref TEXT,
    base_commit TEXT,
    branch TEXT,
    head_commit TEXT,
    status TEXT,
    tasks_total INTEGER,
    tasks_done INTEGER,
    tasks_failed INTEGER,
    planner_agent_run_id TEXT,
    planner_cost_usd REAL,
    cost_usd REAL,
    created_at TEXT,
    updated_at TEXT,
    plan_path TEXT,
    report_path TEXT
);
CREATE TABLE tasks (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    jira_key TEXT,
    title TEXT,
    status TEXT,
    complexity TEXT,
    parallel_safe INTEGER,
    depends_on TEXT,
    files TEXT,
    acceptance_criteria TEXT,
    attempt INTEGER,
    attempts INTEGER,
    commit_sha TEXT,
    branch TEXT,
    worktree_path TEXT,
    merge_kind TEXT,
    cost_usd REAL,
    reasons TEXT,
    error TEXT,
    log_tail TEXT,
    log_path TEXT,
    github_issue INTEGER,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (run_id, task_id)
);
CREATE TABLE attempts (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT,
    cost_usd REAL,
    reasons TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    record_path TEXT,
    PRIMARY KEY (run_id, task_id, attempt)
);
CREATE TABLE agent_runs (
    agent_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT,
    role TEXT,
    attempt INTEGER,
    outcome TEXT,
    status TEXT,
    model TEXT,
    model_used TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_seconds REAL,
    num_turns INTEGER,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    exit_code INTEGER,
    timed_out INTEGER,
    error TEXT,
    diagnosis TEXT,
    stderr_tail TEXT,
    event_count INTEGER,
    session_id TEXT,
    folder TEXT,
    events_file TEXT,
    stderr_file TEXT
);
CREATE TABLE merges (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    position INTEGER,
    kind TEXT,
    commit_sha TEXT,
    head TEXT,
    merged_at TEXT,
    PRIMARY KEY (run_id, task_id)
);
CREATE INDEX tasks_by_status ON tasks (status);
CREATE INDEX agent_runs_by_run ON agent_runs (run_id, task_id);
"""
TABLES = ("runs", "tasks", "attempts", "agent_runs", "merges")


def db_path(runs_dir: Path) -> Path:
    """The database beside the runs folder: ``.agent-lab/agentlab.db`` in a project."""
    return Path(runs_dir).parent / DB_FILE_NAME


def _json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tail(path: Path | str | None) -> str | None:
    """The last lines of a text file, short enough for a cell."""
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_CHARS * 4))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = text.strip().splitlines()[-TAIL_LINES:]
    text = "\n".join(lines)[-TAIL_CHARS:]
    return text or None


def _list(value: Any) -> str:
    return json.dumps(value if isinstance(value, list) else [], ensure_ascii=False)


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag(value: Any) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _task_log(run_dir: Path, task_id: str) -> Path | None:
    """The newest process log of a task: run-plan's, else run-task's."""
    numbered = []
    for path in (run_dir / RUN_LOG_FOLDER).glob(f"{task_id}-*.log"):
        number = path.stem.rsplit("-", 1)[-1]
        if number.isdigit():
            numbered.append((int(number), path))
    if numbered:
        return max(numbered)[1]
    single = run_dir / "tasks" / task_id / TASK_LOG_FILE
    return single if single.is_file() else None


def _attempt_records(task_dir: Path, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Every attempt of a task, oldest first: the archived ones, then the current record."""
    found: dict[int, dict[str, Any]] = {}
    for summary in record.get("previous_attempts") or []:
        if isinstance(summary, dict) and _int(summary.get("attempt")):
            found[summary["attempt"]] = {**summary, "_path": None}
    for folder in task_dir.glob("attempt-*"):
        number = folder.name.split("-", 1)[1]
        archived = _json(folder / TASK_FILE)
        if number.isdigit() and archived:
            found[int(number)] = {**found.get(int(number), {}), **archived,
                                  "_path": str(folder / TASK_FILE)}
    found[attempt_of(record)] = {**record, "_path": str(task_dir / TASK_FILE)}
    return [found[number] for number in sorted(found)]


def _agent_row(run_id: str, task_id: str | None, role: str, attempt: int, agents_dir: Path,
               recorded: dict[str, Any], folder_id: str) -> tuple[Any, ...]:
    folder = agents_dir / folder_id
    result = _json(folder / RESULT_FILE) or {}
    merged = {**result, **{key: value for key, value in recorded.items() if value is not None}}
    outcome = recorded.get("outcome") or result.get("status")
    failed = outcome not in (None, "success") or bool(result.get("error"))
    stderr_file = result.get("stderr_file") or str(folder / STDERR_FILE)
    return (
        folder_id, run_id, task_id, role, attempt, outcome, result.get("status"),
        merged.get("model"), merged.get("model_used"), result.get("started_at"),
        result.get("finished_at"), _number(merged.get("duration_seconds")),
        _int(merged.get("num_turns")), _number(merged.get("total_cost_usd")),
        _int(merged.get("input_tokens")), _int(merged.get("output_tokens")),
        _int(result.get("exit_code")), _flag(result.get("timed_out")), result.get("error"),
        recorded.get("error") if failed else None,
        _tail(stderr_file) if failed else None, _int(result.get("event_count")),
        merged.get("session_id"), str(folder) if folder.is_dir() else None,
        result.get("events_file"), stderr_file if folder.is_dir() else None,
    )


def run_rows(runs_dir: Path, run_id: str, agents_dir: Path,
             plans_dir: Path | None = None) -> dict[str, list[tuple[Any, ...]]]:
    """The rows of every table for one run, read from its files. Raises nothing for gaps.

    In a project the plans are kept with the runs; ``plans_dir`` is for when they are not.
    """
    run_dir = Path(runs_dir) / run_id
    plan_file = Path(plans_dir or runs_dir) / run_id / PLAN_FILE
    plan = _json(plan_file)
    if plan is None:
        return {}
    issue, config = plan.get("issue") or {}, plan.get("config") or {}
    source = plan.get("source") or {"kind": "github", "number": issue.get("number"),
                                    "repository": issue.get("repository")}
    integration = _json(run_dir / INTEGRATION_FILE) or {}
    board = (_json(run_dir / BOARD_FILE) or {}).get("tasks") or {}
    merges = [merge for merge in integration.get("merges") or [] if isinstance(merge, dict)]
    merged_kind = {merge.get("task"): merge.get("kind") for merge in merges}

    rows: dict[str, list[tuple[Any, ...]]] = {name: [] for name in TABLES}
    statuses, run_cost = [], 0.0
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict) or not task.get("id"):
            continue
        task_id = str(task["id"])
        task_dir = run_dir / "tasks" / task_id
        record = _json(task_dir / TASK_FILE)
        if record is None:
            statuses.append("planned")
            rows["tasks"].append((
                run_id, task_id, task.get("jira_key"), task.get("title"), "planned",
                task.get("complexity"), _flag(task.get("parallel_safe")),
                _list(task.get("depends_on")), _list(task.get("files")),
                _list(task.get("acceptance_criteria")), None, 0, None, None, None, None, None,
                "[]", None, None, None, _int((board.get(task_id) or {}).get("number")), None,
                None))
            continue

        attempts = _attempt_records(task_dir, record)
        cost = round(sum(_number(attempt.get("cost_usd")) or 0.0 for attempt in attempts), 6)
        run_cost += cost
        status = record.get("status")
        statuses.append(status)
        reasons = record.get("reasons") if isinstance(record.get("reasons"), list) else []
        unfinished = status in ENDED - {"done"}
        log = _task_log(run_dir, task_id)
        rows["tasks"].append((
            run_id, task_id, task.get("jira_key"), task.get("title"), status,
            task.get("complexity"), _flag(task.get("parallel_safe")),
            _list(task.get("depends_on")), _list(task.get("files")),
            _list(task.get("acceptance_criteria")), attempt_of(record), len(attempts),
            record.get("commit"), record.get("branch"), record.get("worktree_path"),
            merged_kind.get(task_id), cost, _list(reasons),
            str(reasons[0]) if unfinished and reasons else None,
            _tail(log) if unfinished else None, str(log) if log else None,
            _int((board.get(task_id) or {}).get("number")), record.get("created_at"),
            record.get("updated_at")))
        for attempt_record in attempts:
            number = attempt_of(attempt_record)
            attempt_reasons = (attempt_record.get("reasons")
                               if isinstance(attempt_record.get("reasons"), list) else [])
            rows["attempts"].append((
                run_id, task_id, number, attempt_record.get("status"),
                _number(attempt_record.get("cost_usd")), _list(attempt_reasons),
                str(attempt_reasons[0]) if attempt_record.get("status") != "done"
                and attempt_reasons else None,
                attempt_record.get("created_at"), attempt_record.get("updated_at"),
                attempt_record.get("_path")))
            for role, recorded in (attempt_record.get("agents") or {}).items():
                if role not in ROLES or not isinstance(recorded, dict):
                    continue
                agent_attempt = _int(recorded.get("attempt")) or number
                rows["agent_runs"].append(_agent_row(
                    run_id, task_id, role, agent_attempt, agents_dir, recorded,
                    agent_run_id(run_id, task_id, role, agent_attempt)))

    planner = plan.get("planner") or {}
    planner_cost = _number(planner.get("total_cost_usd"))
    if planner.get("run_id"):
        rows["agent_runs"].append(_agent_row(run_id, None, "planner", 1, agents_dir,
                                             {"outcome": "success", **planner},
                                             str(planner["run_id"])))
    rows["agent_runs"] = list({row[0]: row for row in rows["agent_runs"]}.values())
    for position, merge in enumerate(merges, 1):
        rows["merges"].append((run_id, merge.get("task"), position, merge.get("kind"),
                               merge.get("commit"), merge.get("head"), merge.get("at")))
    rows["merges"] = list({(row[0], row[1]): row for row in rows["merges"] if row[1]}.values())

    done = statuses.count("done")
    failed = sum(1 for status in statuses if status in ENDED - {"done"})
    status = ("planned" if all(item == "planned" for item in statuses) else
              "done" if statuses and done == len(statuses) else
              "running" if any(item in WORKING for item in statuses) else
              "stopped" if failed else "partly done")
    report = run_dir / REPORT_FILE
    times = [row[-1] for row in rows["tasks"] if row[-1]]
    rows["runs"].append((
        run_id, source.get("kind"), str(source.get("key") or source.get("number") or ""),
        source.get("repository") or issue.get("repository"), issue.get("title"),
        source.get("url") or issue.get("url"),
        (source.get("epic") or {}).get("key") if isinstance(source.get("epic"), dict) else None,
        config.get("base_ref"), config.get("base_commit"),
        integration.get("branch") or config.get("branch"), integration.get("head"), status,
        len(statuses), done, failed, planner.get("run_id"), planner_cost,
        round(run_cost + (planner_cost or 0.0), 6), plan.get("created_at"),
        max(times) if times else plan.get("created_at"), str(plan_file),
        str(report) if report.is_file() else None))
    return rows


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _schema_version(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and str(row[0]).isdigit() else None


def _create(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.executemany("INSERT INTO meta VALUES (?, ?)",
                           [("schema_version", str(SCHEMA_VERSION)), ("built_at", utc_now())])


def _write_run(connection: sqlite3.Connection, run_id: str,
               rows: dict[str, list[tuple[Any, ...]]]) -> None:
    for table in TABLES:
        connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
    for table, values in rows.items():
        if values:
            marks = ", ".join("?" * len(values[0]))
            connection.executemany(f"INSERT INTO {table} VALUES ({marks})", values)


def _run_ids(plans_dir: Path) -> list[str]:
    if not Path(plans_dir).is_dir():
        return []
    return sorted(path.name for path in Path(plans_dir).iterdir()
                  if (path / PLAN_FILE).is_file())


def rebuild(runs_dir: Path, agents_dir: Path, path: Path | None = None,
            plans_dir: Path | None = None) -> dict[str, int]:
    """Delete the database and fill it again from every run's files. Returns rows per table."""
    path = path or db_path(runs_dir)
    for leftover in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        leftover.unlink(missing_ok=True)
    with closing(_connect(path)) as connection, connection:
        _create(connection)
        for run_id in _run_ids(plans_dir or runs_dir):
            _write_run(connection, run_id, run_rows(runs_dir, run_id, agents_dir, plans_dir))
    return counts(path)


def refresh_run(runs_dir: Path, run_id: str, agents_dir: Path, path: Path | None = None,
                plans_dir: Path | None = None) -> None:
    """Replace one run's rows. A missing or older database is rebuilt from every run."""
    path = path or db_path(runs_dir)
    if path.is_file():
        with closing(_connect(path)) as connection:
            if _schema_version(connection) == SCHEMA_VERSION:
                with connection:
                    _write_run(connection, run_id,
                               run_rows(runs_dir, run_id, agents_dir, plans_dir))
                return
    rebuild(runs_dir, agents_dir, path, plans_dir)


def try_refresh_run(runs_dir: Path, run_id: str, agents_dir: Path,
                    plans_dir: Path | None = None) -> str | None:
    """refresh_run that never raises: the reason it failed, or None."""
    try:
        refresh_run(runs_dir, run_id, agents_dir, plans_dir=plans_dir)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLES}


def query(path: Path, sql: str, parameters: Iterable[Any] = ()) -> list[sqlite3.Row]:
    """Read-only rows, for the summary and for tests."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, tuple(parameters)).fetchall()


__all__ = ["DB_FILE_NAME", "SCHEMA_VERSION", "TABLES", "counts", "db_path", "query", "rebuild",
           "refresh_run", "run_rows", "try_refresh_run"]
