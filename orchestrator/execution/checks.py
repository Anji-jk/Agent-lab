"""Facts the orchestrator establishes for itself about a task's worktree.

Nothing here asks an agent. After each agent has run, the orchestrator looks
at the files and at Git, and runs the tests with its own hands. An agent's
report ("I only touched the tests", "tests pass") is a claim; these
functions are the check.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from orchestrator.core.git import run_git_raw
from orchestrator.settings import PYTHON, load_settings

DELETED = "<deleted>"

# Two different Pythons. The orchestrator's own (to start itself again, for a background
# task) and the project's (its tests, and the agents' plain `python`). The project's is not
# simply the one running the orchestrator: that one has the orchestrator's packages, not the
# project's.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_VARIABLE = PYTHON
PYTEST_CHECK_TIMEOUT_SECONDS = 60


def _venv_python(root: Path) -> str | None:
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return None


def orchestrator_python() -> str:
    """The orchestrator's own Python: its .venv, else the one running now."""
    return _venv_python(PROJECT_ROOT) or sys.executable


def project_python(root: Path, environ: dict[str, str] | None = None,
                   configured: str | None = None) -> str:
    """The project's Python, the most specific first: its ``.agent-lab.toml`` setting, its
    .venv, then AGENT_LAB_PYTHON (this machine's fallback), then the one running now."""
    if configured:
        return configured
    found = _venv_python(Path(root))
    if found:
        return found
    return load_settings(environ).python or sys.executable


def with_python_on_path(env: dict[str, str], python: str) -> dict[str, str]:
    """``env`` with ``python``'s folder first on PATH, so a plain `python` is that one."""
    env = dict(env)
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    folder = str(Path(python).parent)
    rest = env.get(key, "")
    env[key] = folder + (os.pathsep + rest if rest else "")
    return env


def pytest_problem(python: str, env: dict[str, str] | None = None) -> str | None:
    """Why ``python -m pytest`` cannot run, or None when it can."""
    try:
        completed = subprocess.run(
            [python, "-m", "pytest", "--version"], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PYTEST_CHECK_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{python} could not be started ({type(exc).__name__})"
    if completed.returncode != 0:
        lines = ((completed.stderr or "") + (completed.stdout or "")).strip().splitlines()
        return lines[-1][:300] if lines else f"exit code {completed.returncode}"
    return None


# Files that tools leave behind and that are never part of the work.
_NOISE_DIRS = frozenset({"__pycache__", ".pytest_cache"})

OUTPUT_TAIL_CHARS = 3000
MAX_DIFF_CHARS = 60_000

COMMITTER = ("Agent Lab Orchestrator", "orchestrator@agent-lab.invalid")

_TEST_ID = re.compile(r"[A-Za-z0-9_./-]+\.py(?:::[\w\[\]\-.,:=@ ]+)?")


class CheckError(RuntimeError):
    """Git or the test run could not be inspected."""


def _git(worktree: Path, *args: str, timeout: int = 60, stdin: str | None = None) -> str:
    """Run git in ``worktree`` and return stdout exactly as printed."""
    command = next((a for a in args if not a.startswith("-")), "command")
    try:
        return run_git_raw(worktree, *args, timeout=timeout, stdin=stdin)
    except subprocess.CalledProcessError as exc:
        # Report git's own message, not the "hint:" lines it prints after it.
        lines = [l for l in (exc.stderr or "").strip().splitlines()
                 if l.strip() and not l.startswith("hint:")]
        raise CheckError(
            f"git {command} failed: {lines[0] if lines else exc.returncode}"
        ) from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"git {command} could not run: {type(exc).__name__}") from None


# --- what changed ------------------------------------------------------------


def head_commit(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").strip()


def current_branch(worktree: Path) -> str | None:
    """The checked-out branch, or None for a detached HEAD."""
    try:
        return _git(worktree, "symbolic-ref", "--short", "-q", "HEAD").strip() or None
    except CheckError:
        return None


def is_noise(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(_NOISE_DIRS & set(parts)) or path.endswith(".pyc")


def parse_status(raw: str) -> list[str]:
    """Paths from ``git status --porcelain=v1 -z``; renames list both names."""
    parts = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if ("R" in status or "C" in status) and index < len(parts):
            paths.append(parts[index])
            index += 1
    return paths


def changed_paths(worktree: Path) -> list[str]:
    """Every file that differs from HEAD (including new files), sorted."""
    raw = _git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return sorted({path for path in parse_status(raw) if not is_noise(path)})


MAX_COUNTED_BYTES = 1_000_000


def _tracked_sizes(worktree: Path) -> dict[str, tuple[int | None, int | None]]:
    """Lines added and removed in tracked files against HEAD; None for a binary file."""
    parts = _git(worktree, "diff", "--numstat", "-z", "HEAD").split("\0")
    sizes: dict[str, tuple[int | None, int | None]] = {}
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        fields = entry.split("\t", 2)
        if len(fields) < 3:
            continue
        added, removed, path = fields
        if not path:  # a rename: the old and the new name follow as their own parts
            index += 1
            path = parts[index] if index < len(parts) else ""
            index += 1
        if path:
            sizes[path] = (
                int(added) if added.isdigit() else None,
                int(removed) if removed.isdigit() else None,
            )
    return sizes


def change_sizes(worktree: Path) -> list[tuple[str, str, int | None, int | None]]:
    """(path, "new" | "modified" | "deleted", lines added, lines removed) for every changed file.

    Read-only: it does not stage anything. New files have no entry in git's own
    count, so their lines are counted from the file (None when it is binary or large).
    """
    tracked = _tracked_sizes(worktree)
    rows: list[tuple[str, str, int | None, int | None]] = []
    for path in changed_paths(worktree):
        target = Path(worktree) / path
        if path in tracked:
            added, removed = tracked[path]
            rows.append((path, "modified" if target.exists() else "deleted", added, removed))
            continue
        lines: int | None = None
        try:
            if target.is_file() and target.stat().st_size <= MAX_COUNTED_BYTES:
                data = target.read_bytes()
                if b"\0" not in data:
                    lines = len(data.decode("utf-8", errors="replace").splitlines())
        except OSError:
            pass
        rows.append((path, "new" if target.exists() else "deleted", lines, 0 if lines is not None else None))
    return rows


def snapshot(worktree: Path) -> dict[str, str]:
    """Content fingerprint of every changed file, to compare between agents."""
    result: dict[str, str] = {}
    for path in changed_paths(worktree):
        target = Path(worktree) / path
        try:
            if target.is_symlink():
                result[path] = "link:" + os.readlink(target)
            elif target.is_file():
                result[path] = hashlib.sha256(target.read_bytes()).hexdigest()
            else:
                result[path] = DELETED
        except OSError:
            result[path] = DELETED
    return result


def changed_between(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths an agent created, edited or removed, given snapshots around it."""
    paths = {path for path in after if before.get(path) != after[path]}
    # A file that was changed before and is now back to HEAD vanishes from
    # `after`; that is a change too.
    paths |= {path for path in before if path not in after}
    return sorted(paths)


# --- rules about where changes may be ----------------------------------------


def _normalize(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix().removeprefix("./")
    # An empty path normalizes to "."; keep it empty so "" can mean "anywhere".
    return "" if normalized == "." else normalized


def under(path: str, prefixes: tuple[str, ...] | list[str]) -> bool:
    """Is ``path`` one of ``prefixes`` or inside one that is a folder?"""
    path = _normalize(path)
    for prefix in prefixes:
        prefix = _normalize(prefix)
        if not prefix or path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def outside(paths: list[str], prefixes: tuple[str, ...] | list[str]) -> list[str]:
    return [path for path in paths if not under(path, prefixes)]


def scope_violations(
    paths: list[str], planned_files: list[str], test_dirs: tuple[str, ...]
) -> list[str]:
    """Changed files that the plan did not name and that are not tests.

    A plan that names no files gives nothing to compare against, so nothing
    is reported; the reviewer still sees the whole diff.
    """
    if not planned_files:
        return []
    return outside(paths, [*planned_files, *test_dirs])


_TEST_OR_ASSERT = re.compile(r"^\s*(?:async\s+)?def\s+test|^\s*class\s+Test|^\s*assert\b")


def removed_test_lines(
    worktree: Path, paths: list[str], test_dirs: tuple[str, ...]
) -> list[str]:
    """Existing test files that lost a test function, a test class or an assertion.

    Adding tests is expected, and so is touching an import or a helper in an
    existing test file. Removing or rewriting a test or an ``assert`` is how
    a change makes a failing suite pass, so only those lines are reported.
    """
    candidates = [p for p in paths if under(p, test_dirs)]
    if not candidates:
        return []
    raw = _git(worktree, "-c", "core.quotepath=off", "diff", "HEAD", "-U0",
               "--no-color", "--no-renames", "--", *candidates)
    flagged: set[str] = set()
    current: str | None = None
    in_hunk = False
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            current, in_hunk = None, False
        elif not in_hunk and line.startswith("--- a/"):
            current = line[len("--- a/"):]
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and current and line.startswith("-") and _TEST_OR_ASSERT.match(line[1:]):
            flagged.add(current)
    return sorted(flagged)


# --- staging, diff and commit --------------------------------------------------


def stage_all(worktree: Path) -> None:
    """Stage exactly the files that changed, and nothing else.

    The list comes from ``changed_paths``, so tool leftovers and ignored
    files are never in it. Paths are passed on stdin and read literally, so
    a long list cannot hit the Windows command-line limit and a name such as
    ``a[1].py`` is not read as a pattern. Touches only this worktree's index.
    """
    paths = changed_paths(worktree)
    if not paths:
        return
    _git(worktree, "--literal-pathspecs", "add", "-A",
         "--pathspec-from-file=-", "--pathspec-file-nul", stdin="\0".join(paths))


def diff_text(worktree: Path, limit: int = MAX_DIFF_CHARS) -> str:
    """The staged change as a patch, cut at ``limit`` characters."""
    stage_all(worktree)
    patch = _git(worktree, "diff", "--cached", "HEAD", "--no-color")
    if len(patch) <= limit:
        return patch
    return patch[:limit].rsplit("\n", 1)[0] + "\n... (diff truncated)\n"


def commit_all(worktree: Path, message: str) -> str:
    """Commit the staged change as the orchestrator and return the new SHA.

    ``--no-verify`` keeps repository hooks (code an agent may have been able
    to influence) from running as part of the orchestrator's own commit.
    """
    stage_all(worktree)
    name, email = COMMITTER
    _git(
        worktree,
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        "-c", "commit.gpgsign=false",
        "commit", "--no-verify", "-m", message,
    )
    return head_commit(worktree)


def commit_exists(repository: Path, commit: str) -> bool:
    try:
        _git(repository, "cat-file", "-e", f"{commit}^{{commit}}")
    except CheckError:
        return False
    return True


def is_ancestor(repository: Path, older: str, newer: str) -> bool:
    try:
        _git(repository, "merge-base", "--is-ancestor", older, newer)
    except CheckError:
        return False
    return True


def pick_base_commit(repository: Path, commits: list[str]) -> str | None:
    """The one commit that already contains all the others, if there is one.

    A task that depends on several finished tasks can start from the last of
    them only when the others are its ancestors. Combining separate branches
    needs a merge, which is a later stage's job, so then there is no answer.
    """
    unique = list(dict.fromkeys(commits))
    for candidate in unique:
        if all(is_ancestor(repository, other, candidate) for other in unique):
            return candidate
    return None


# --- running commands ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    output_tail: str
    seconds: float

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> dict:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "seconds": round(self.seconds, 2),
            "output_tail": self.output_tail,
        }


def run_command(
    command: tuple[str, ...] | list[str],
    cwd: Path,
    *,
    timeout: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run a command to completion, keeping only the end of its output."""
    import time

    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if isinstance(exc.stdout, bytes):
            partial = exc.stdout.decode("utf-8", errors="replace")
        elif isinstance(exc.stdout, str):
            partial = exc.stdout
        else:
            partial = ""
        return CommandResult(
            tuple(command), None, True, partial[-OUTPUT_TAIL_CHARS:],
            time.monotonic() - started,
        )
    except OSError as exc:
        return CommandResult(
            tuple(command), None, False, f"Could not start: {type(exc).__name__}",
            time.monotonic() - started,
        )
    output = (completed.stdout or "") + (completed.stderr or "")
    return CommandResult(
        tuple(command), completed.returncode, False, output[-OUTPUT_TAIL_CHARS:],
        time.monotonic() - started,
    )


def valid_test_id(test_id: object, test_dirs: tuple[str, ...]) -> bool:
    """A pytest node ID the tester named, safe to pass to pytest.

    It must be a file inside a test folder (optionally ``::name``), and
    cannot start with ``-`` (pytest would read it as an option) or climb out
    of the repository.
    """
    if not isinstance(test_id, str) or not _TEST_ID.fullmatch(test_id):
        return False
    if test_id.startswith(("-", "/")) or ".." in test_id.split("::")[0].split("/"):
        return False
    return under(test_id.split("::")[0], test_dirs)


def run_suite(
    worktree: Path,
    *,
    command: tuple[str, ...],
    timeout: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    return run_command(command, worktree, timeout=timeout, env=env)


def run_named_tests(
    worktree: Path,
    test_ids: list[str],
    *,
    command: tuple[str, ...],
    timeout: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run only the tests the tester said cover the criteria."""
    return run_command([*command, *test_ids], worktree, timeout=timeout, env=env)
