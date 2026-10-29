"""Run one planned task from plan to a verified local commit.

For the task it creates a worktree and then runs three agents in turn:

    builder  -> makes the change
    tester   -> writes tests for each acceptance criterion (only under tests/)
    reviewer -> reads the diff and judges each criterion (cannot write)

Between agents the orchestrator checks the facts for itself (``task_checks``):
which files changed, whether the full suite and the tester's named tests pass.
``task_agents.decide`` turns agent reports plus those facts into the status.
Only a task that is ``done`` gets a commit, made by the orchestrator, on the
task's own branch in the task's own worktree, and then merged into the run's
branch (``integration``). Nothing is pushed.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.execution import runner_adapter

from orchestrator.execution import agents as ta
from orchestrator.execution.integration import MergeProblem, integration_head, merge_task

from orchestrator.execution import checks as tc
from orchestrator.execution.budget import (
    BudgetExhausted, budget_problem,
)
from orchestrator.errors import ExecutionRefused
from orchestrator.planning.profiles import RoleSettings, profile_for, role_settings
from orchestrator.execution.roles import BUILDER, REVIEWER, ROLE_NAMES, TESTER
from orchestrator.records.run_registry import RunRegistryError, locked, run_directory, utc_now
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.records.task_events import NullLog, TaskLog
from orchestrator.records.task_registry import (
    ACTIVE_STATUSES, list_tasks, load_task, save_task, update_task,
)
from orchestrator.execution.liveness import process_liveness
from orchestrator.execution.pipeline.base import (  # noqa: F401
    AGENT_METRIC_FIELDS, TEST_DIRS, PipelineBase, _agent_metrics, _command_fields, limit_reason,
)
from orchestrator.execution.pipeline.build import BuildStage
from orchestrator.execution.pipeline.finish import commit_message
from orchestrator.execution.pipeline.review import ReviewStage
from orchestrator.execution.pipeline.test_stage import TestingStage
from orchestrator.core.project_config import DEFAULT_TEST_EXAMPLE, load_project_config
from orchestrator.execution.worktrees import WorktreeError, create_worktree
from orchestrator.settings import TEST_TIMEOUT, load_settings

# The full suite took 496 s in a worktree outside OneDrive on Windows.
TEST_TIMEOUT_SECONDS = 1200
# Set this to give the tests longer on a slow machine: seconds, 60 to 86400.
TEST_TIMEOUT_VARIABLE = TEST_TIMEOUT
CREDENTIAL_VARIABLES = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
)
MAX_RUN_ID_FOR_TASK = 52  # the worktree run ID adds "-<task id>" and a retry's "-r2", and must stay <= 64
# (task ID, its record) -> (is its process still running, why we think so)
IsAlive = Callable[[str, dict[str, Any]], tuple[bool, str]]


class TaskNotReady(ExecutionRefused):
    """The task cannot start yet (a dependency is not done, no common base)."""


@dataclass(frozen=True, slots=True)
class TaskContext:
    plan: dict[str, Any]
    task: dict[str, Any]
    base_commit: str
    settings: dict[str, RoleSettings]


def test_timeout_seconds(configured: int | None = None) -> int:
    """How long the orchestrator's own test runs may take.

    AGENT_LAB_TEST_TIMEOUT (this machine) wins, then the project's ``test_timeout``, then 1200.
    """
    return load_settings().test_timeout or configured or TEST_TIMEOUT_SECONDS


def check_environment(runner_script: Path, python: str) -> dict[str, str]:
    """Environment for running tests: the agents' allowlist minus credentials."""
    env = {
        key: value
        for key, value in runner_adapter.load_agent_env(runner_script).items()
        if key.upper() not in CREDENTIAL_VARIABLES
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return tc.with_python_on_path(env, python)


def find_task(plan: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in plan["tasks"]:
        if task["id"] == task_id:
            return task
    known = ", ".join(task["id"] for task in plan["tasks"])
    raise ExecutionRefused(f"The plan has no task {task_id!r} (it has: {known}).")


def run_head(repo_path: Path, run_id: str, records: dict[str, dict[str, Any]], plan_base: str,
             runs_dir: Path | None = None) -> str:
    """The commit the run's next task starts from.

    Every task of a run starts from the newest finished work, so the run's
    commits form one chain on one branch and need no merge: the head is the
    finished commit that contains all the others. Before anything is finished
    it is the commit the plan was made from. A task that depends on others
    starts from a commit containing theirs, because the head contains every
    finished task's commit.
    """
    if runs_dir is not None:
        head = integration_head(runs_dir, run_id, repo_path)
        if head is not None:  # the run's branch holds every merged task
            return head
    finished = [(task_id, record["commit"]) for task_id, record in sorted(records.items())
                if record.get("status") == "done" and record.get("commit")]
    if not finished:
        return plan_base
    for task_id, commit in finished:
        if not tc.commit_exists(repo_path, commit):
            raise ExecutionRefused(
                f"{task_id}'s commit {commit[:12]} does not exist in {repo_path}; the run's "
                "work cannot be continued from it."
            )
    head = tc.pick_base_commit(repo_path, [commit for _, commit in finished])
    if head is None:
        listed = ", ".join(f"{task_id} at {commit[:12]}" for task_id, commit in finished)
        raise TaskNotReady(
            f"The finished work of run {run_id} is on separate branches ({listed}), "
            "from before a run's tasks shared one branch. Combining them needs a merge, "
            "which a later stage provides; plan again for a new run."
        )
    return head


def check_no_other_task_running(
    task_id: str, records: dict[str, dict[str, Any]], is_alive: IsAlive,
    together: Callable[[str], bool] | None = None,
) -> None:
    """Refuse while another task of the run is still running, unless the two may run together.

    ``together(other)`` says whether this task and ``other`` may run side by side: both
    marked parallel-safe in the plan, which the planner only allows for tasks that
    are not ordered and share no file. Without it, a task runs alone.

    A task whose process has gone (a closed window) does not count: it is not running.
    """
    for other, record in sorted(records.items()):
        if other == task_id or record.get("status") not in ACTIVE_STATUSES | {"pending"}:
            continue
        alive, why = is_alive(other, record)
        if alive and not (together and together(other)):
            raise TaskNotReady(
                f"{other} is still running ({why}). {task_id} and {other} are not both marked "
                f"parallel-safe in the plan, so they run one at a time; wait for {other} to "
                "finish or cancel it."
            )


def prepare(
    run_id: str,
    task_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    is_alive: IsAlive | None = None,
) -> TaskContext:
    """Everything that must be true before anything is created."""
    plan = load_task_plan(plans_dir, run_id)
    if not plan["issue"].get("description"):
        raise ExecutionRefused(
            "This plan does not contain the Issue text (it was made by an older "
            "version). Generate it again with ai-plan."
        )
    if len(run_id) > MAX_RUN_ID_FOR_TASK:
        raise ExecutionRefused(f"Run ID is too long to add a task suffix (max {MAX_RUN_ID_FOR_TASK}).")
    task = find_task(plan, task_id)

    for dependency in task["depends_on"]:
        try:
            record = load_task(runs_dir, run_id, dependency)
        except RunRegistryError:
            raise TaskNotReady(f"{dependency} has not been run yet; {task_id} depends on it.") from None
        if record["status"] != "done":
            raise TaskNotReady(
                f"{dependency} is {record['status']}, not done; {task_id} depends on it."
            )

    records = list_tasks(runs_dir, run_id)
    if is_alive is None:
        is_alive = process_liveness(runs_dir, run_id)
    safe = {t["id"]: bool(t.get("parallel_safe")) for t in plan["tasks"]}
    check_no_other_task_running(
        task_id, records, is_alive,
        together=lambda other: safe.get(task_id, False) and safe.get(other, False))

    base = plan["config"]["base_commit"]
    if not tc.commit_exists(repo_path, base):
        raise ExecutionRefused(f"Base commit {base[:12]} does not exist in {repo_path}.")
    base = run_head(repo_path, run_id, records, base, runs_dir)

    profile = profile_for(task["complexity"])
    return TaskContext(plan, task, base, {name: role_settings(profile, name) for name in ROLE_NAMES})




def _total_cost(agents: dict[str, dict[str, Any]]) -> float:
    return round(
        sum(a["total_cost_usd"] for a in agents.values()
            if isinstance(a.get("total_cost_usd"), (int, float))),
        6,
    )




GIT_LOCK_FOLDER = "git-lock"   # runs/<run>/git-lock: tasks side by side take turns at git
GIT_LOCK_SECONDS = 300


def git_lock(runs_dir: Path, run_id: str):
    """One task at a time creates its worktree or commits; the work itself runs side by side.

    Git takes its own locks, but two tasks making a worktree or a commit in the same
    instant can still fail on them ("Unable to create ... .lock"). Taking turns costs
    a second or two against tasks that run for many minutes.
    """
    return locked(run_directory(Path(runs_dir), run_id) / GIT_LOCK_FOLDER,
                  timeout=GIT_LOCK_SECONDS)


def _liveness(runs_dir: Path, run_id: str, runner_script: Path) -> IsAlive:
    """Whether another task's process is still running."""
    return process_liveness(runs_dir, run_id, runner_script=Path(runner_script))


class _Pipeline(BuildStage, TestingStage, ReviewStage, PipelineBase):
    """One task's run, stage by stage (the stages are in ``execution/pipeline``)."""

    def __init__(self, ctx: TaskContext, run_id: str, runs_dir: Path, worktree: Path,
                 runner_script: Path, invoke_runner, test_command: tuple[str, ...], env,
                 log: TaskLog | NullLog | None = None, attempt: int = 1,
                 feedback: str | None = None, earlier: str | None = None,
                 is_alive: IsAlive | None = None, test_example: str = DEFAULT_TEST_EXAMPLE,
                 python: str | None = None):
        self.ctx, self.run_id, self.runs_dir = ctx, run_id, runs_dir
        self.is_alive = is_alive or _liveness(runs_dir, run_id, runner_script)
        self.attempt, self.feedback, self.earlier = attempt, feedback, earlier
        self.test_example = test_example
        self.python = python  # the project's: first on the agents' PATH
        self.reused: dict[str, dict[str, Any]] = {}  # agents' results kept from the stopped attempt
        self.test_timeout = TEST_TIMEOUT_SECONDS
        self.budget_stopped = False
        self.limit_stop: dict[str, Any] | None = None  # Claude's usage limit refused an agent
        self.log = log or NullLog()
        self.worktree, self.runner_script = worktree, runner_script
        self.invoke_runner, self.test_command, self.env = invoke_runner, test_command, env
        self.task = ctx.task
        self.criteria = ctx.task["acceptance_criteria"]
        self.findings = ta.Findings(criteria_count=len(self.criteria))
        self.agents: dict[str, dict[str, Any]] = {}
        self.checks: dict[str, Any] = {}
        self.after_build: dict[str, str] = {}






def _merge(run_id: str, task: dict[str, Any], commit: str, *, plan: dict[str, Any],
           ctx: TaskContext, runs_dir: Path, repo_path: Path, worktree_root: Path | None,
           test_command: tuple[str, ...], test_timeout: int, env, log, status: str,
           reasons: list[str]) -> tuple[str, list[str], dict[str, Any]]:
    """Merge a done task into the run's branch: (status, reasons, what happened)."""
    log.emit("merge_started", commit=commit)
    try:
        merged = merge_task(run_id, task["id"], commit, issue_number=int(plan["issue"]["number"]),
                            base_commit=ctx.base_commit, runs_dir=runs_dir, repo_path=repo_path,
                            worktree_root=worktree_root, test_command=test_command,
                            test_timeout=test_timeout, env=env, title=task.get("title", ""))
    except MergeProblem as exc:
        log.emit("merge_failed", kind=exc.kind, reason=exc.reason)
        # A conflict or a failing merged suite is the task's to fix: its retry starts from
        # the branch's new head and sees the other work. Anything else needs a person.
        status = ta.FAILED if exc.kind in ("conflict", "suite") else ta.NEEDS_REVIEW
        return status, [exc.reason], {"problem": exc.kind, **exc.detail}
    except (tc.CheckError, WorktreeError, FileExistsError, OSError, RunRegistryError) as exc:
        log.emit("merge_failed", kind="error", reason=str(exc))
        return ta.NEEDS_REVIEW, [f"The work was committed on its own branch, but merging it "
                                 f"into the run's branch failed: {exc}"], {"problem": "error"}
    log.emit("merged", **merged.describe())
    return status, reasons, merged.describe()


def run_task(
    run_id: str,
    task_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    worktree_root: Path | None = None,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    invoke_runner=runner_adapter.invoke_runner,
    test_command: tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
    attempt: int = 1,
    feedback: str | None = None,
    previous_attempts: list[dict[str, Any]] | None = None,
    continue_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one task end to end and return its final record."""
    ctx = prepare(run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path)
    task, plan = ctx.task, ctx.plan
    start = (continue_from or {}).get("stage", "building")
    # With the run's branch a task that started earlier is merged into it, so tasks
    # run side by side may finish in any order; only a run from before that has one chain.
    if (continue_from and continue_from["base_commit"] != ctx.base_commit
            and integration_head(runs_dir, run_id, repo_path) is None):
        raise TaskNotReady(
            f"{task_id} started from {continue_from['base_commit'][:12]}, but the run has "
            f"moved on to {ctx.base_commit[:12]} since (another task finished). Continuing "
            "it would split the run's branch."
        )
    if not Path(runner_script).is_file():
        raise ExecutionRefused(f"Runner script not found: {runner_script}")
    config = load_project_config(repo_path)
    test_timeout = test_timeout_seconds(config.test_timeout)
    first = {"building": BUILDER, "testing": TESTER, "reviewing": REVIEWER}[start]
    alive = _liveness(runs_dir, run_id, Path(runner_script))
    problem = budget_problem(runs_dir, run_id, first, ctx.settings[first].max_budget_usd,
                             Path(runner_script), task_id, alive)
    if problem:  # refused before anything is created: the task stays as it was
        raise BudgetExhausted(problem)

    python = tc.project_python(repo_path, configured=config.python)
    env = check_environment(runner_script, python) if env is None else env
    if test_command is None:
        test_command = config.test_command or (python, "-m", "pytest", "-q")
    # Before anything is created or paid for: a suite that cannot start fails every task.
    if tuple(test_command[1:3]) == ("-m", "pytest"):
        problem = tc.pytest_problem(test_command[0], env)
        if problem:
            raise ExecutionRefused(
                f"pytest is not available for {test_command[0]}: {problem}. Activate the "
                f"project's .venv or set {tc.PYTHON_VARIABLE} to a Python that has pytest."
            )
    log = TaskLog(runs_dir, run_id, task_id)
    save_task(runs_dir, run_id, task_id, {
        "status": start if continue_from else "pending",
        "title": task["title"],
        "complexity": task["complexity"],
        "depends_on": task["depends_on"],
        # A continued task keeps the base its worktree was made from, not the run's head now.
        "base_commit": (continue_from or {}).get("base_commit", ctx.base_commit),
        "roles": {name: s.describe() for name, s in ctx.settings.items()},
        # The process running this pipeline, for cancel and for spotting one that died.
        "pid": os.getpid(),
        **({"attempt": attempt, "previous_attempts": previous_attempts or []} if attempt > 1 else {}),
        **({"continued_at": start} if continue_from else {}),
    })
    log.emit("task_started", base_commit=ctx.base_commit, title=task["title"], pid=os.getpid(),
             **({"attempt": attempt} if attempt > 1 else {}))

    try:
        if continue_from:  # the stopped attempt's worktree, files and all
            branch, worktree = continue_from["branch"], Path(continue_from["worktree_path"])
            if not worktree.is_dir():
                raise FileNotFoundError(f"The worktree {worktree} no longer exists.")
        else:
            with git_lock(runs_dir, run_id):
                branch, worktree = create_worktree(
                    repo_path, plan["issue"]["number"],
                    f"{run_id}-{task_id.lower()}" + (f"-r{attempt}" if attempt > 1 else ""),
                    base_ref=ctx.base_commit, worktree_root=worktree_root,
                )
    except Exception as exc:
        log.emit("worktree_failed", error=str(exc))
        log.emit("task_finished", status="failed", cost_usd=0)
        return update_task(runs_dir, run_id, task_id, {
            "status": "failed", "reasons": [f"The worktree could not be created: {exc}"],
        })
    update_task(runs_dir, run_id, task_id, {"branch": branch, "worktree_path": str(worktree)})
    log.emit("worktree_created", branch=branch, path=str(worktree))

    merging = False
    pipeline = _Pipeline(ctx, run_id, runs_dir, worktree, Path(runner_script),
                         invoke_runner, test_command, env, log, attempt, feedback,
                         (continue_from or {}).get("earlier"), alive, config.test_example,
                         python=python)
    pipeline.test_timeout = test_timeout
    try:
        if start == "building":
            go = (pipeline.build() and pipeline.checkpoint("testing")
                  and pipeline.run_tests() and pipeline.checkpoint("reviewing"))
        else:
            pipeline.restore(continue_from["checkpoint"])  # type: ignore[index]
            pipeline.show_kept()
            go = start == "reviewing" or (pipeline.run_tests() and pipeline.checkpoint("reviewing"))
        if go:
            pipeline.review()
        status, reasons = ta.decide(pipeline.findings)
        log.emit("decided", status=status, reasons=reasons)
        commit, merge = None, None
        if status == ta.DONE:
            with git_lock(runs_dir, run_id):
                commit = tc.commit_all(worktree, commit_message(plan, task, run_id))
            if tc.changed_paths(worktree):
                status, reasons = ta.NEEDS_REVIEW, ["Files remained uncommitted after the commit."]
                commit = None
                log.emit("commit_incomplete", reasons=reasons)
            else:
                log.emit("committed", commit=commit, branch=branch)
                # status shows "merging" meanwhile: a real merge runs the full suite,
                # and a merge may first wait for another task's.
                update_task(runs_dir, run_id, task_id, {"merging": utc_now()})
                merging = True
                status, reasons, merge = _merge(run_id, task, commit, plan=plan, ctx=ctx,
                                                runs_dir=runs_dir, repo_path=repo_path,
                                                worktree_root=worktree_root,
                                                test_command=test_command,
                                                test_timeout=test_timeout, env=env, log=log,
                                                status=status, reasons=reasons)
    except Exception as exc:  # a crash must leave a record, not a stuck "building"
        status, reasons, commit = ta.NEEDS_REVIEW, [f"The pipeline stopped: {type(exc).__name__}: {exc}"], None
        merge = None
        log.emit("pipeline_crashed", error=f"{type(exc).__name__}: {exc}")

    current = load_task(runs_dir, run_id, task_id)["status"]
    if current == "pending":
        status = "failed"
    log.emit("task_finished", status=status, cost_usd=_total_cost(pipeline.agents))
    return update_task(runs_dir, run_id, task_id, {
        "status": status,
        "reasons": reasons,
        "commit": commit,
        "agents": pipeline.agents,
        **({"reused_agents": pipeline.reused} if pipeline.reused else {}),
        **({"budget_stop": True} if pipeline.budget_stopped else {}),
        **({"limit_stop": pipeline.limit_stop} if pipeline.limit_stop else {}),
        **({"merge": merge} if merge else {}),
        **({"merging": None} if merging else {}),
        "checks": pipeline.checks,
        "findings": {k: getattr(pipeline.findings, k) for k in pipeline.findings.__dataclass_fields__},
        "cost_usd": _total_cost(pipeline.agents),
    })
