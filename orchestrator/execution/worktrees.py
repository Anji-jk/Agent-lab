"""Create and clean up isolated Git worktrees for agent tasks.

Safety rules enforced here:
* the main checkout is never removed;
* branches are never deleted automatically;
* uncommitted changes in a worktree are never discarded unless the caller
  passes ``discard_changes=True`` (a separate, explicit decision);
* a failed creation reports what is left behind instead of deleting it.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.records.run_registry import validate_run_id

# Conservative subset of valid ref names: no leading '-', no '..', no spaces.
BASE_REF_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")


class WorktreeError(RuntimeError):
    """Raised when a worktree cannot be created or safely removed."""


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    path: Path
    branch: str | None  # None for a detached HEAD
    head: str | None


def run_git(
    repository_root: Path,
    *args: str,
) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository_root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _git(root: Path, *args: str) -> str:
    """run_git with failures translated to WorktreeError (stderr only)."""
    try:
        return run_git(root, *args)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit code {exc.returncode}"
        raise WorktreeError(f"git {args[0]} failed: {reason}") from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(
            f"git {args[0]} could not run: {type(exc).__name__}"
        ) from None


def resolve_repository_root(repository_root: Path) -> Path:
    """Return the top level of the Git work tree containing the path."""
    repository_root = Path(repository_root)
    if not repository_root.is_dir():
        raise WorktreeError(f"Repository path is not a directory: {repository_root}")
    return Path(_git(repository_root, "rev-parse", "--show-toplevel")).resolve()


def verify_remote_repository(repository_root: Path, repository: str) -> None:
    """Require ``origin`` to point at OWNER/REPO, so an Issue from one
    repository is never executed against a different local checkout."""
    root = resolve_repository_root(repository_root)
    try:
        url = _git(root, "remote", "get-url", "origin")
    except WorktreeError:
        raise WorktreeError(
            f"{root} has no 'origin' remote to match against {repository}."
        ) from None
    normalized = url.removesuffix(".git").rstrip("/").lower()
    if not normalized.endswith(("/" + repository.lower(), ":" + repository.lower())):
        raise WorktreeError(
            f"Local checkout's origin does not match {repository}."
        )


def validate_base_ref(base_ref: str) -> str:
    if (
        not isinstance(base_ref, str)
        or not BASE_REF_PATTERN.fullmatch(base_ref)
        or ".." in base_ref
        or base_ref.endswith(("/", ".lock"))
    ):
        raise ValueError(f"Invalid base reference: {base_ref!r}")
    return base_ref


def validate_issue_number(issue_number: object) -> int:
    if (
        not isinstance(issue_number, int)
        or isinstance(issue_number, bool)
        or issue_number < 1
    ):
        raise ValueError("Issue number must be positive.")
    return issue_number


WORKTREE_ROOT_ENV = "AGENT_LAB_WORKTREE_ROOT"


def default_worktree_root() -> Path:
    """Where task worktrees live: outside the repository and, for most
    people, outside OneDrive, whose sync client slows Git down and can
    lock files. Override with ``AGENT_LAB_WORKTREE_ROOT``."""
    configured = os.environ.get(WORKTREE_ROOT_ENV)
    return Path(configured) if configured else Path.home() / "agent-worktrees"


def plan_worktree(
    repository_root: Path,
    issue_number: int,
    run_id: str,
    worktree_root: Path | None = None,
) -> tuple[Path, str, Path]:
    """Return ``(root, branch, worktree_path)`` without changing anything.

    Without ``worktree_root`` the worktree is a sibling of the repository.
    """
    validate_issue_number(issue_number)
    validate_run_id(run_id)
    root = resolve_repository_root(repository_root)
    branch = f"agent/issue-{issue_number}-{run_id}"
    parent = root.parent
    if worktree_root is not None:
        parent = Path(worktree_root).expanduser().resolve()
        if parent == root or root in parent.parents:
            raise WorktreeError("The worktree folder must be outside the repository.")
    worktree_path = parent / f"{root.name}-{branch.replace('/', '-')}"
    return root, branch, worktree_path


def list_worktrees(repository_root: Path) -> list[WorktreeInfo]:
    """Parse ``git worktree list --porcelain``."""
    output = _git(repository_root, "worktree", "list", "--porcelain")
    entries: list[WorktreeInfo] = []
    fields: dict[str, str] = {}

    def flush() -> None:
        if "worktree" in fields:
            branch = fields.get("branch")
            if branch and branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
            entries.append(
                WorktreeInfo(
                    path=Path(fields["worktree"]).resolve(),
                    branch=branch,
                    head=fields.get("HEAD"),
                )
            )

    for line in output.splitlines():
        if not line.strip():
            flush()
            fields = {}
            continue
        key, _, value = line.partition(" ")
        fields[key] = value
    flush()
    return entries


def create_worktree(
    repository_root: Path,
    issue_number: int,
    run_id: str,
    base_ref: str = "main",
    worktree_root: Path | None = None,
) -> tuple[str, Path]:
    """Create a unique branch and worktree for one run.

    Returns ``(branch, worktree_path)``. Nothing is created unless every
    check passes. If ``git worktree add`` itself fails, the error names any
    leftover state; nothing is deleted automatically.
    """
    validate_base_ref(base_ref)
    root, branch, worktree_path = plan_worktree(
        repository_root, issue_number, run_id, worktree_root
    )

    if worktree_path.exists():
        raise FileExistsError(f"Worktree path already exists: {worktree_path}")

    registered = list_worktrees(root)
    if any(entry.path == worktree_path.resolve() for entry in registered):
        raise FileExistsError(
            f"Worktree path is already registered: {worktree_path}"
        )
    if any(entry.branch == branch for entry in registered):
        raise FileExistsError(f"Branch is already checked out: {branch}")
    if _git(root, "branch", "--list", branch):
        raise FileExistsError(f"Branch already exists: {branch}")

    # Confirm the requested base exists before creating anything.
    _git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")

    try:
        if worktree_root is not None:
            # Created only now, after every check has passed.
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
        run_git(
            root,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            base_ref,
        )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
        leftovers = []
        try:
            if _git(root, "branch", "--list", branch):
                leftovers.append(f"branch {branch}")
        except WorktreeError:
            pass
        if worktree_path.exists():
            leftovers.append(f"path {worktree_path}")
        detail = (
            f" Left in place for manual review: {', '.join(leftovers)}."
            if leftovers
            else ""
        )
        reason = ""
        if isinstance(exc, subprocess.CalledProcessError):
            lines = (exc.stderr or "").strip().splitlines()
            reason = f": {lines[-1]}" if lines else ""
        raise WorktreeError(f"git worktree add failed{reason}.{detail}") from None

    return branch, worktree_path


def remove_worktree(
    repository_root: Path,
    worktree_path: Path,
    branch: str,
    *,
    discard_changes: bool = False,
) -> None:
    """Remove a registered agent worktree. The branch is always kept.

    The path and branch must match a worktree git has registered. Refuses
    the main checkout, unregistered paths, branch mismatches, and (unless
    ``discard_changes`` is set) worktrees with uncommitted changes.
    """
    root = resolve_repository_root(repository_root)
    target = Path(worktree_path).resolve()

    if target == root:
        raise WorktreeError("Refusing to remove the main checkout.")

    entries = list_worktrees(root)
    if entries and target == entries[0].path:
        raise WorktreeError("Refusing to remove the main checkout.")

    match = next((entry for entry in entries if entry.path == target), None)
    if match is None:
        raise WorktreeError(f"Not a registered worktree: {target}")
    if match.branch != branch:
        raise WorktreeError(
            f"Worktree branch mismatch: expected {branch!r}, "
            f"found {match.branch!r}."
        )
    if not branch.startswith("agent/"):
        raise WorktreeError("Refusing to remove a non-agent worktree.")

    if target.exists() and not discard_changes:
        if _git(target, "status", "--porcelain"):
            raise WorktreeError(
                "Worktree has uncommitted changes; review them or pass "
                "discard_changes=True explicitly."
            )

    command = ["worktree", "remove"]
    if discard_changes:
        command.append("--force")
    command.append(str(target))
    _git(root, *command)
