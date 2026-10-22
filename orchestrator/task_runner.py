"""Run one planned task from plan to a verified local commit.

For the task it creates a worktree and then runs three agents in turn:

    builder  -> makes the change
    tester   -> writes tests for each acceptance criterion (only under tests/)
    reviewer -> reads the diff and judges each criterion (cannot write)

Between agents the orchestrator checks the facts for itself (``task_checks``):
which files changed, whether the full suite and the tester's named tests pass.
``task_agents.decide`` turns agent reports plus those facts into the status.
Only a task that is ``done`` gets a commit, made by the orchestrator, on the
task's own branch in the task's own worktree. Nothing is pushed or merged.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator import runner_adapter, task_agents as ta, task_checks as tc
from orchestrator.executor import ExecutionRefused
from orchestrator.profiles import RoleSettings, profile_for, role_settings
from orchestrator.roles import BUILDER, REVIEWER, ROLE_NAMES, TESTER, TEST_DIR, role
from orchestrator.run_registry import RunRegistryError
from orchestrator.task_plan import load_task_plan
from orchestrator.task_registry import load_task, save_task, update_task
from orchestrator.worktrees import create_worktree

TEST_DIRS = (f"{TEST_DIR}/",)
# The full suite took 496 s in a worktree outside OneDrive on Windows (M2.8).
TEST_TIMEOUT_SECONDS = 1200
CREDENTIAL_VARIABLES = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
)
AGENT_METRIC_FIELDS = (
    "status", "duration_seconds", "total_cost_usd", "input_tokens",
    "output_tokens", "num_turns", "model", "session_id",
)
MAX_RUN_ID_FOR_TASK = 55  # the worktree run ID adds "-<task id>" and must stay <= 64


class TaskNotReady(ExecutionRefused):
    """The task cannot start yet (a dependency is not done, no common base)."""


@dataclass(frozen=True, slots=True)
class TaskContext:
    plan: dict[str, Any]
    task: dict[str, Any]
    base_commit: str
    settings: dict[str, RoleSettings]


def check_environment(runner_script: Path) -> dict[str, str]:
    """Environment for running tests: the agents' allowlist minus credentials."""
    env = {
        key: value
        for key, value in runner_adapter.load_agent_env(runner_script).items()
        if key.upper() not in CREDENTIAL_VARIABLES
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def find_task(plan: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in plan["tasks"]:
        if task["id"] == task_id:
            return task
    known = ", ".join(task["id"] for task in plan["tasks"])
    raise ExecutionRefused(f"The plan has no task {task_id!r} (it has: {known}).")


def prepare(
    run_id: str,
    task_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
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

    commits: list[str] = []
    for dependency in task["depends_on"]:
        try:
            record = load_task(runs_dir, run_id, dependency)
        except RunRegistryError:
            raise TaskNotReady(f"{dependency} has not been run yet; {task_id} depends on it.") from None
        if record["status"] != "done":
            raise TaskNotReady(
                f"{dependency} is {record['status']}, not done; {task_id} depends on it."
            )
        commits.append(record["commit"])

    base = plan["config"]["base_commit"]
    if commits:
        base = tc.pick_base_commit(repo_path, commits)
        if base is None:
            raise TaskNotReady(
                f"{task_id} depends on tasks whose work is on separate branches. "
                "Combining them needs a merge, which a later stage provides."
            )
    if not tc.commit_exists(repo_path, base):
        raise ExecutionRefused(f"Base commit {base[:12]} does not exist in {repo_path}.")

    profile = profile_for(task["complexity"])
    return TaskContext(plan, task, base, {name: role_settings(profile, name) for name in ROLE_NAMES})


def _agent_metrics(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in AGENT_METRIC_FIELDS if key in result}


def _total_cost(agents: dict[str, dict[str, Any]]) -> float:
    return round(
        sum(a["total_cost_usd"] for a in agents.values()
            if isinstance(a.get("total_cost_usd"), (int, float))),
        6,
    )


class _Pipeline:
    def __init__(self, ctx: TaskContext, run_id: str, runs_dir: Path, worktree: Path,
                 runner_script: Path, invoke_runner, test_command: tuple[str, ...], env):
        self.ctx, self.run_id, self.runs_dir = ctx, run_id, runs_dir
        self.worktree, self.runner_script = worktree, runner_script
        self.invoke_runner, self.test_command, self.env = invoke_runner, test_command, env
        self.task = ctx.task
        self.criteria = ctx.task["acceptance_criteria"]
        self.findings = ta.Findings(criteria_count=len(self.criteria))
        self.agents: dict[str, dict[str, Any]] = {}
        self.checks: dict[str, Any] = {}
        self.after_build: dict[str, str] = {}

    # -- helpers --
    def set_status(self, status: str, **extra: Any) -> None:
        update_task(self.runs_dir, self.run_id, self.task["id"], {"status": status, **extra})

    def call(self, name: str, prompt: str) -> runner_adapter.RunnerOutcome:
        settings = self.ctx.settings[name]
        with tempfile.TemporaryDirectory(prefix="agent-lab-task-") as scratch:
            prompt_file = Path(scratch) / f"{name}-prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            outcome = self.invoke_runner(
                run_id=f"{self.run_id}-{self.task['id']}-{name}",
                worktree_path=self.worktree,
                prompt_file=prompt_file,
                timeout_seconds=settings.timeout_seconds,
                model=settings.model,
                max_turns=settings.max_turns,
                effort=settings.effort,
                max_budget_usd=settings.max_budget_usd,
                keep_result_text=True,
                runner_script=self.runner_script,
                role=name,
                **role(name).runner_options(),
            )
        self.agents[name] = {
            "outcome": outcome.outcome,
            **_agent_metrics(outcome.result),
        }
        if outcome.outcome != "success":
            self.findings.infrastructure.append(
                f"The {name} {outcome.outcome.replace('_', ' ')}: "
                f"{outcome.error or 'no detail reported'}"
            )
        return outcome

    def report(self, name: str, outcome, parser):
        try:
            return parser(outcome.result.get("result_text"))
        except ta.ReportError as exc:
            self.findings.infrastructure.append(f"The {name}'s report was unusable: {exc}")
            return None

    def suite(self) -> tc.CommandResult:
        return tc.run_suite(self.worktree, command=self.test_command,
                            timeout=TEST_TIMEOUT_SECONDS, env=self.env)

    # -- stages --
    def build(self) -> bool:
        f = self.findings
        self.set_status("building")
        outcome = self.call(BUILDER, ta.builder_prompt(self.ctx.plan["issue"], self.task))
        if outcome.outcome != "success":
            return False
        report = self.report(BUILDER, outcome, ta.parse_builder)
        if report is None:
            return False
        f.builder_ok = True
        f.builder_blocked = (report.blocker or "no reason given") if report.blocked else ""
        # The worktree started clean, so every changed file is the builder's.
        self.after_build = tc.snapshot(self.worktree)
        changed = sorted(self.after_build)
        f.nothing_changed = not changed
        f.scope_violations = tc.scope_violations(changed, self.task["files"], TEST_DIRS)
        self.checks["builder_changed"] = changed
        return not ta.problems(f)

    def run_tests(self) -> bool:
        f = self.findings
        self.set_status("testing")
        changed_by_builder = self.checks["builder_changed"]
        outcome = self.call(
            TESTER, ta.tester_prompt(self.ctx.plan["issue"], self.task, changed_by_builder)
        )
        if outcome.outcome != "success":
            return False
        report = self.report(
            TESTER, outcome, lambda text: ta.parse_tester(text, len(self.criteria))
        )
        if report is None:
            return False
        f.tester_ok = True

        now = tc.snapshot(self.worktree)
        wrote = tc.changed_between(self.after_build, now)
        f.tester_wrote_outside_tests = tc.outside(wrote, TEST_DIRS)
        self.checks["tester_changed"] = wrote
        f.weakened_tests = tc.removed_test_lines(self.worktree, tc.changed_paths(self.worktree), TEST_DIRS)

        f.uncovered = dict(report.uncovered)
        f.suspected_defects = list(report.suspected_defects)
        f.tester_notes = list(report.notes)
        f.unnamed = [
            n for n in range(1, len(self.criteria) + 1)
            if n not in report.coverage and n not in report.uncovered
        ]
        named = list(dict.fromkeys(t for tests in report.coverage.values() for t in tests))
        f.invalid_test_ids = [t for t in named if not tc.valid_test_id(t, TEST_DIRS)]
        valid = [t for t in named if t not in f.invalid_test_ids]
        f.no_tests = not named

        suite = self.suite()
        f.suite_passed = suite.passed
        self.checks["suite"] = suite.summary()
        if valid:
            result = tc.run_named_tests(self.worktree, valid, command=self.test_command,
                                        timeout=TEST_TIMEOUT_SECONDS, env=self.env)
            self.checks["named_tests"] = result.summary()
            # pytest exit 4 = a named file/test does not exist; 5 = nothing collected.
            if result.exit_code in (4, 5):
                f.missing_tests = valid
            f.named_passed = result.passed
        else:
            f.named_passed = False if named else None
        return not ta.problems(f)

    def review(self) -> bool:
        f = self.findings
        self.set_status("reviewing")
        suite, named = self.checks.get("suite", {}), self.checks.get("named_tests", {})
        facts = [
            f"Files changed: {', '.join(tc.changed_paths(self.worktree)) or '(none)'}",
            f"Full test suite: {'passed' if f.suite_passed else 'FAILED'} "
            f"(exit {suite.get('exit_code')})",
            f"Tests named by the tester: {'passed' if f.named_passed else 'FAILED'} "
            f"(exit {named.get('exit_code')})",
            f"Criteria the tester could not automate: {len(f.uncovered)}",
        ]
        diff = tc.diff_text(self.worktree)
        outcome = self.call(
            REVIEWER, ta.reviewer_prompt(self.ctx.plan["issue"], self.task, diff, facts)
        )
        if outcome.outcome != "success":
            return False
        report = self.report(
            REVIEWER, outcome, lambda text: ta.parse_reviewer(text, len(self.criteria))
        )
        if report is None:
            return False
        f.reviewer_ok = True
        f.reviewer_verdict = report.verdict
        f.unmet = [n for n in range(1, len(self.criteria) + 1) if not report.met.get(n, False)]
        f.reviewer_problems = list(report.problems)
        f.reviewer_notes = list(report.notes)
        return True


def _commit_message(plan: dict[str, Any], task: dict[str, Any], run_id: str) -> str:
    issue = plan["issue"]
    criteria = "\n".join(f"- {c}" for c in task["acceptance_criteria"])
    return (
        f"{task['id']}: {task['title']}\n\n"
        f"Issue #{issue['number']}, run {run_id}.\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        "Built, tested and reviewed by separate agents; verified by the orchestrator."
    )


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
    test_command: tuple[str, ...] = tc.DEFAULT_TEST_COMMAND,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one task end to end and return its final record."""
    ctx = prepare(run_id, task_id, plans_dir=plans_dir, runs_dir=runs_dir, repo_path=repo_path)
    task, plan = ctx.task, ctx.plan
    if not Path(runner_script).is_file():
        raise ExecutionRefused(f"Runner script not found: {runner_script}")

    env = check_environment(runner_script) if env is None else env
    save_task(runs_dir, run_id, task_id, {
        "title": task["title"],
        "complexity": task["complexity"],
        "depends_on": task["depends_on"],
        "base_commit": ctx.base_commit,
        "roles": {name: s.describe() for name, s in ctx.settings.items()},
    })

    try:
        branch, worktree = create_worktree(
            repo_path, plan["issue"]["number"], f"{run_id}-{task_id.lower()}",
            base_ref=ctx.base_commit, worktree_root=worktree_root,
        )
    except Exception as exc:
        return update_task(runs_dir, run_id, task_id, {
            "status": "failed", "reasons": [f"The worktree could not be created: {exc}"],
        })
    update_task(runs_dir, run_id, task_id, {"branch": branch, "worktree_path": str(worktree)})

    pipeline = _Pipeline(ctx, run_id, runs_dir, worktree, Path(runner_script),
                         invoke_runner, test_command, env)
    try:
        if pipeline.build() and pipeline.run_tests():
            pipeline.review()
        status, reasons = ta.decide(pipeline.findings)
        commit = None
        if status == ta.DONE:
            commit = tc.commit_all(worktree, _commit_message(plan, task, run_id))
            if tc.changed_paths(worktree):
                status, reasons = ta.NEEDS_REVIEW, ["Files remained uncommitted after the commit."]
                commit = None
    except Exception as exc:  # a crash must leave a record, not a stuck "building"
        status, reasons, commit = ta.NEEDS_REVIEW, [f"The pipeline stopped: {type(exc).__name__}: {exc}"], None

    current = load_task(runs_dir, run_id, task_id)["status"]
    if current == "pending":
        status = "failed"
    return update_task(runs_dir, run_id, task_id, {
        "status": status,
        "reasons": reasons,
        "commit": commit,
        "agents": pipeline.agents,
        "checks": pipeline.checks,
        "findings": {k: getattr(pipeline.findings, k) for k in pipeline.findings.__dataclass_fields__},
        "cost_usd": _total_cost(pipeline.agents),
    })
