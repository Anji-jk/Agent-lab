"""The checks before a story is planned: is the story ready, and is the project?

Each check says ok, FAIL, a warning or skipped, with a short reason. A failed check stops the
planning, since the plan (and the money spent on it) would be wasted; a warning only reports
something that helps when filled in and never stops anything.

Story    it has open sub-tasks; each has acceptance criteria; its text does not forbid the work;
         Jira's Blocks links between the sub-tasks do not loop
Checkout the base branch exists; the checkout has no uncommitted changes and is on that branch,
         so the plan matches what the run starts from
Project  CLAUDE.md has its structure, commands and rules; the docs folder exists (warnings)
Tests    the project's tests pass on the base branch (a task cannot be judged on tests that
         already fail); a project with no tests only gets a warning

Nothing here writes anything, calls Jira or starts an agent. It only reads Git, and runs the
project's own test command.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.project_check import check_project
from orchestrator.core.project_config import ProjectConfig
from orchestrator.execution import checks as tc
from orchestrator.execution import runner_adapter
from orchestrator.execution.task_runner import check_environment, test_timeout_seconds
from orchestrator.execution.worktrees import (
    WorktreeError, resolve_repository_root, run_git, validate_base_ref,
)
from orchestrator.jira.story import Story, WorkItem
from orchestrator.planning.safety import PROHIBITION_PATTERNS

OK, FAILED, WARNING, SKIPPED = "ok", "failed", "warning", "skipped"
LABELS = {OK: "ok", FAILED: "FAIL", WARNING: "--", SKIPPED: "skip"}
GROUPS = ("Story", "Checkout", "Project", "Tests")
NO_TESTS_EXIT_CODE = 5


@dataclass(frozen=True, slots=True)
class Check:
    group: str
    status: str
    what: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StoryCheck:
    story_key: str
    checks: tuple[Check, ...]

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if check.status == FAILED]

    @property
    def ready(self) -> bool:
        return not self.failed


def open_subtasks(story: Story) -> list[WorkItem]:
    return [item for item in story.subtasks if not item.done]


def _loops(story: Story) -> list[str]:
    """Find open sub-tasks that belong to a cycle of Jira Blocks links."""
    waiting = {item.key: set(item.blocked_by) for item in open_subtasks(story)}
    for dependencies in waiting.values():
        dependencies.intersection_update(waiting)

    cyclic: set[str] = set()
    for start in waiting:
        pending = list(waiting[start])
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == start:
                cyclic.add(start)
                break
            if current not in visited:
                visited.add(current)
                pending.extend(waiting[current] - visited)
    return sorted(cyclic)


def check_story(story: Story) -> list[Check]:
    """The story and its sub-tasks, as Jira has them."""
    key = story.story.key
    if not story.subtasks:
        return [Check("Story", FAILED, f"{key} has sub-tasks",
                      "It has none, so there is nothing to plan.")]
    found = [Check("Story", OK, f"{key} has {len(story.subtasks)} sub-task"
                   f"{'s' if len(story.subtasks) != 1 else ''}")]
    open_items = open_subtasks(story)
    done = [item.key for item in story.subtasks if item.done]
    if not open_items:
        found.append(Check("Story", FAILED, "Some sub-tasks are still open",
                           "Every sub-task is Done in Jira; nothing is left to do."))
        return found
    found.append(Check("Story", OK, f"{len(open_items)} open",
                       f"Done in Jira and left out: {', '.join(done)}" if done else ""))

    missing = [item.key for item in open_items if not item.acceptance_criteria]
    found.append(
        Check("Story", FAILED, "Every open sub-task has acceptance criteria",
              f"Missing in {', '.join(missing)}. Add an \"Acceptance criteria:\" part to their "
              "descriptions in Jira.") if missing else
        Check("Story", OK, "Every open sub-task has acceptance criteria"))

    reasons: list[str] = []
    for item in (story.story, *open_items):
        text = f"{item.summary}\n{item.description}"
        for reason, pattern in PROHIBITION_PATTERNS:
            match = pattern.search(text)
            if match:
                reasons.append(f"{item.key} {reason}: {match.group(0).strip()!r}")
    found.append(
        Check("Story", FAILED, "The text does not forbid the work", "; ".join(reasons))
        if reasons else Check("Story", OK, "The text does not forbid the work"))

    looping = _loops(story)
    links = sum(len(item.blocked_by) for item in open_items)
    found.append(
        Check("Story", FAILED, "Jira's Blocks links do not loop",
              f"These wait for each other: {', '.join(looping)}.") if looping else
        Check("Story", OK, "Jira's Blocks links do not loop",
              f"{links} link{'s' if links != 1 else ''} between the sub-tasks"
              if links else "no links; the planner finds the order from the descriptions"))
    return found


def check_checkout(root: Path, base_ref: str) -> list[Check]:
    """A clean checkout on the base branch: the plan must match what the run starts from."""
    try:
        top = resolve_repository_root(root)
    except WorktreeError as exc:
        return [Check("Checkout", FAILED, "The folder is a Git repository", str(exc))]

    try:
        base_ref = validate_base_ref(base_ref)
    except ValueError as exc:
        return [Check("Checkout", FAILED, "The base reference is valid", str(exc))]
    try:
        base = run_git(top, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired, WorktreeError):
        return [Check("Checkout", FAILED, f"The base {base_ref} exists",
                      f"{base_ref!r} is not a branch or commit here (--base names another).")]

    try:
        changes = run_git(top, "status", "--porcelain").splitlines()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired, WorktreeError) as exc:
        return [Check("Checkout", FAILED, "Git checkout status can be read", str(exc))]
    found = [Check("Checkout", OK, f"The base {base_ref} exists", base[:12])]
    found.append(
        Check("Checkout", FAILED, "No uncommitted changes",
              f"{len(changes)} changed or new file{'s' if len(changes) != 1 else ''} "
              f"(first: {changes[0][2:].strip()}). Commit or stash them first.") if changes else
        Check("Checkout", OK, "No uncommitted changes"))

    try:
        head = run_git(top, "rev-parse", "HEAD")
        branch = run_git(top, "branch", "--show-current")
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired, WorktreeError) as exc:
        found.append(Check("Checkout", FAILED, f"On {base_ref}", str(exc)))
        return found
    matches_base = head == base and branch == base_ref.removeprefix("refs/heads/")
    found.append(
        Check("Checkout", FAILED, f"On {base_ref}",
              f"The checkout is on {branch or 'detached HEAD'} at {head[:12]}, but {base_ref} "
              f"is {base[:12]}. Check out {base_ref} first.") if not matches_base else
        Check("Checkout", OK, f"On {base_ref}"))
    return found


def check_project_files(root: Path, config: ProjectConfig) -> list[Check]:
    """CLAUDE.md and the docs folder. Warnings only: a project with less still works."""
    result = check_project(root, config)
    return [Check("Project", OK if finding.found else WARNING, finding.what)
            for finding in result.findings]


def check_tests(root: Path, config: ProjectConfig, *, env: dict[str, str] | None = None,
                runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT) -> Check:
    """Run the project's tests where the checkout stands (on the base branch)."""
    what = "The project's tests pass on the base branch"
    python = tc.project_python(root, configured=config.python)
    command = tuple(config.test_command or (python, "-m", "pytest", "-q"))
    env = check_environment(Path(runner_script), python) if env is None else env
    if command[1:3] == ("-m", "pytest"):
        problem = tc.pytest_problem(command[0], env)
        if problem:
            return Check("Tests", FAILED, what,
                         f"pytest is not available for {command[0]}: {problem}. Activate the "
                         f"project's .venv or set {tc.PYTHON_VARIABLE}.")
    result = tc.run_suite(root, command=command, timeout=test_timeout_seconds(config.test_timeout),
                          env=env)
    shown = " ".join(command)
    if result.passed:
        return Check("Tests", OK, what, f"{shown} ({result.seconds:.0f}s)")
    if result.timed_out:
        return Check("Tests", FAILED, what, f"{shown} did not finish in time "
                     "(test_timeout in .agent-lab.toml).")
    if result.exit_code == NO_TESTS_EXIT_CODE and command[1:3] == ("-m", "pytest"):
        return Check("Tests", WARNING, "The project has tests",
                     "pytest found none, so a task's checks have only its own new tests.")
    tail = result.output_tail.strip().splitlines()[-1:] or [""]
    return Check("Tests", FAILED, what,
                 f"{shown} ended with exit code {result.exit_code}: {tail[0][:200]}")


def run_story_checks(story: Story, root: Path, config: ProjectConfig, *, base_ref: str = "main",
                     run_tests: bool = True, env: dict[str, str] | None = None) -> StoryCheck:
    """All the checks, in order. The tests run only when the checkout is ready for them."""
    checks = check_story(story)
    checkout = check_checkout(root, base_ref)
    checks += checkout
    checks += check_project_files(root, config)
    if not run_tests:
        checks.append(Check("Tests", SKIPPED, "The project's tests pass on the base branch",
                            "left out (--skip-tests)"))
    elif any(check.status == FAILED for check in checkout):
        checks.append(Check("Tests", SKIPPED, "The project's tests pass on the base branch",
                            "the checkout is not ready"))
    else:
        checks.append(check_tests(root, config, env=env))
    return StoryCheck(story.story.key, tuple(checks))


def format_story_check(result: StoryCheck) -> str:
    lines = [f"Checks before planning {result.story_key}"]
    for group in GROUPS:
        mine = [check for check in result.checks if check.group == group]
        if not mine:
            continue
        lines.append(f"\n{group}")
        for check in mine:
            lines.append(f"  {LABELS[check.status]:<4} {check.what}"
                         + (f"  ({check.detail})" if check.detail and check.status == OK else ""))
            if check.detail and check.status != OK:
                lines.append(f"       {check.detail}")
    failed = result.failed
    warnings = sum(1 for check in result.checks if check.status == WARNING)
    lines.append("")
    lines.append(f"{len(failed)} check{'s' if len(failed) != 1 else ''} failed; nothing was "
                 "planned. Fix the lines marked FAIL and run it again." if failed else
                 "Ready to plan.")
    if warnings:
        lines.append(f"{warnings} warning{'s' if warnings != 1 else ''} (--): they stop nothing; "
                     "filling them in helps the agents.")
    return "\n".join(lines)


__all__ = ["Check", "StoryCheck", "check_checkout", "check_project_files", "check_story",
           "check_tests", "format_story_check", "open_subtasks", "run_story_checks"]
