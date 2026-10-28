"""The run's integration branch: every finished task is merged into it (M7.1).

Each task works on its own branch in its own worktree. As a task's last step,
once every check has passed and its work is committed, that commit is merged
into the run's branch, ``agent/issue-<n>-<run>``, kept checked out in a worktree
of its own. A task counts as done only once this has worked, so the next task,
which starts from the run's branch, always has everything that went before.

The merge is one of two kinds:

* **Fast-forward**: the task started from the branch's current head, so the
  branch simply moves to the task's commit. The result is exactly the tree the
  task's own checks passed, so nothing is run again. One task at a time, every
  merge is of this kind.
* **A real merge**: another task was merged since this one started (tasks run
  side by side, M7.3). The two changes may each pass alone and fail together,
  so the full test suite runs on the merged result before the branch keeps it.

A conflict, or a merged result whose suite fails, leaves the branch as it was
and ends the task failed, with what went wrong. Its retry starts from the
branch's new head, so its builder sees the other work. Every branch is kept.

Only one merge happens at a time: the merge holds a lock on the run, and the
next task waits for it.

``runs/<run>/integration.json`` records the branch, its worktree and every
merge. Nothing is pushed, and nothing reaches your own checkout's branches.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.execution import checks as tc
from orchestrator.execution.worktrees import create_worktree
from orchestrator.records.run_registry import (
    locked, run_directory, utc_now, write_json_atomic,
)

INTEGRATION_FILE = "integration.json"
LOCK_FOLDER = "integration"
LOCK_MARGIN_SECONDS = 600  # beyond the suite's own limit, for git itself


class MergeProblem(Exception):
    """The task's work could not be merged into the run's branch; the branch is unchanged."""

    def __init__(self, reason: str, kind: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason, self.kind, self.detail = reason, kind, detail or {}


@dataclass(frozen=True, slots=True)
class Merged:
    branch: str
    kind: str            # fast-forward, merge, or already (the commit was in the branch)
    head: str            # the branch's head after the merge
    suite: dict[str, Any] | None = None  # the suite run on a real merge

    def describe(self) -> dict[str, Any]:
        return {"branch": self.branch, "kind": self.kind, "head": self.head,
                **({"suite": self.suite} if self.suite else {})}


def integration_path(runs_dir: Path, run_id: str) -> Path:
    return run_directory(Path(runs_dir), run_id) / INTEGRATION_FILE


def load_integration(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    """The run's branch record, or None before the first merge (or for a run from before M7.1)."""
    try:
        data = json.loads(integration_path(runs_dir, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("branch") else None


def integration_head(runs_dir: Path, run_id: str, repo_path: Path) -> str | None:
    """Where the run's branch is now, read from Git (the record could be behind)."""
    data = load_integration(runs_dir, run_id)
    if data is None:
        return None
    try:
        return tc._git(Path(repo_path), "rev-parse", "--verify",
                       f"refs/heads/{data['branch']}^{{commit}}").strip()
    except tc.CheckError:
        return None


def _git(worktree: Path, *args: str, timeout: int = 120) -> str:
    """git as the orchestrator (its own name, no signing); errors name the git command."""
    name, email = tc.COMMITTER
    try:
        return tc._git(worktree, "-c", f"user.name={name}", "-c", f"user.email={email}",
                       "-c", "commit.gpgsign=false", *args, timeout=timeout)
    except tc.CheckError as exc:
        detail = str(exc).split(" failed: ", 1)[-1]
        raise tc.CheckError(f"git {args[0]} failed: {detail}") from None


def _open_branch(runs_dir: Path, run_id: str, issue_number: int, repo_path: Path,
                 base_commit: str, worktree_root: Path | None) -> dict[str, Any]:
    data = load_integration(runs_dir, run_id)
    if data is not None:
        if not Path(data["worktree_path"]).is_dir():
            raise MergeProblem(
                f"The run's branch {data['branch']} has lost its worktree "
                f"({data['worktree_path']}); restore it with git worktree add, or remove "
                f"{integration_path(runs_dir, run_id)} to start the branch again.", "error")
        return data
    # The first merge of the run: the branch starts where the task started.
    branch, worktree = create_worktree(repo_path, issue_number, run_id, base_ref=base_commit,
                                       worktree_root=worktree_root)
    data = {"branch": branch, "worktree_path": str(worktree), "base_commit": base_commit,
            "created_at": utc_now(), "merges": []}
    write_json_atomic(integration_path(runs_dir, run_id), data)
    return data


def merge_task(
    run_id: str,
    task_id: str,
    commit: str,
    *,
    issue_number: int,
    base_commit: str,
    runs_dir: Path,
    repo_path: Path,
    worktree_root: Path | None,
    test_command: tuple[str, ...],
    test_timeout: int,
    env: dict[str, str] | None,
    title: str = "",
) -> Merged:
    """Merge a finished task's commit into the run's branch, or raise MergeProblem."""
    lock_dir = run_directory(Path(runs_dir), run_id) / LOCK_FOLDER
    with locked(lock_dir, timeout=test_timeout + LOCK_MARGIN_SECONDS):
        data = _open_branch(runs_dir, run_id, issue_number, repo_path, base_commit,
                            worktree_root)
        worktree, branch = Path(data["worktree_path"]), data["branch"]
        if tc.changed_paths(worktree):
            raise MergeProblem(
                f"The run's branch worktree {worktree} has uncommitted changes, so nothing was "
                "merged into it. Someone changed files there; commit or discard them.", "error")
        before = tc.head_commit(worktree)
        suite = None
        if tc.is_ancestor(repo_path, commit, before):
            kind = "already"
        elif tc.is_ancestor(repo_path, before, commit):
            kind = "fast-forward"
            _git(worktree, "merge", "--ff-only", commit)
        else:
            kind = "merge"
            message = f"Merge {task_id}{': ' + title if title else ''} into the run's branch"
            try:
                _git(worktree, "merge", "--no-ff", "--no-verify", "-m", message, commit)
            except tc.CheckError:
                conflicted = [p for p in _git(worktree, "diff", "--name-only",
                                              "--diff-filter=U").splitlines() if p.strip()]
                try:
                    _git(worktree, "merge", "--abort")
                except tc.CheckError:
                    _git(worktree, "reset", "--hard", before)
                files = ", ".join(conflicted) or "the files git named"
                raise MergeProblem(
                    f"{task_id}'s work conflicts with work merged into the run's branch since "
                    f"it started ({files}). The branch was left as it was.",
                    "conflict", {"files": conflicted, "branch_head": before}) from None
            # Everything on the run's branch since it was made: this task's work and the
            # work merged before it. The orchestrator's tests run only if any of it touched them.
            touched = [p for p in _git(worktree, "diff", "--name-only", str(data["base_commit"]),
                                       "HEAD").splitlines() if p.strip()]
            result = tc.run_suite(worktree, command=test_command, timeout=test_timeout, env=env,
                                  touched=touched)
            suite = result.summary()
            if not result.passed:
                _git(worktree, "reset", "--hard", before)
                what = (f"did not finish within {test_timeout} s" if result.timed_out
                        else "fails")
                which = "the full test suite"
                if result.scope == "project":
                    which = "the project's tests"
                    what = "fail" if what == "fails" else what
                raise MergeProblem(
                    f"{task_id}'s work passes on its own, but merged with the work already on "
                    f"the run's branch {which} {what}. The branch was left as it "
                    "was.", "suite", {"suite": suite, "branch_head": before})
        head = tc.head_commit(worktree)
        data["head"] = head
        data["merges"] = [*(data.get("merges") or []),
                          {"task": task_id, "commit": commit, "kind": kind, "head": head,
                           "at": utc_now()}]
        write_json_atomic(integration_path(runs_dir, run_id), data)
        return Merged(branch, kind, head, suite)


# --- showing it (M7.6) -----------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _took(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return ""
    return f" in {seconds:.0f} s" if seconds < 90 else f" in {round(seconds / 60)} min"


def _suite_words(suite: dict[str, Any]) -> str:
    """M7.8: a suite may cover the project's tests only."""
    return "the project's tests" if suite.get("scope") == "project" else "the full suite"


def describe_merge(merge: Any) -> str | None:
    """One line for a task record's ``merge``: how its work reached the run's branch, or not."""
    if not isinstance(merge, dict):
        return None
    problem = merge.get("problem")
    if problem == "conflict":
        files = ", ".join(map(str, merge.get("files") or [])) or "files git named"
        return f"not merged: conflicts with work merged before it ({files})"
    if problem == "suite":
        suite = _as_dict(merge.get("suite"))
        what = "did not finish" if suite.get("timed_out") else "failed"
        return (f"not merged: {_suite_words(suite)} {what} on the merged result"
                f"{_took(suite.get('seconds'))}")
    if problem:
        return "not merged: the merge itself failed"
    kind = merge.get("kind")
    if kind == "fast-forward":
        return "merged: fast-forward (nothing else was merged since it started, so no new suite run)"
    if kind == "merge":
        suite = _as_dict(merge.get("suite"))
        return (f"merged: real merge, {_suite_words(suite)} passed on the merged result"
                f"{_took(suite.get('seconds'))}")
    if kind == "already":
        return "merged: its commit was already on the run's branch"
    return None


KIND_WORDS = {"fast-forward": "fast-forward", "merge": "real merge", "already": "already there"}


def branch_line(data: dict[str, Any] | None) -> str | None:
    """``Run branch: <name> at <head> · merges: T1 fast-forward, T3 real merge``, or None."""
    if not data:
        return None
    head = str(data.get("head") or data.get("base_commit") or "")[:12]
    merges = [m for m in data.get("merges") or [] if isinstance(m, dict)]
    told = ", ".join(f"{m.get('task')} {KIND_WORDS.get(str(m.get('kind')), m.get('kind'))}"
                     for m in merges) or "none yet"
    return f"Run branch: {data['branch']} at {head} · merges: {told}"
