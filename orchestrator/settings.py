"""Every setting of the orchestrator, read in one place.

This is the only module of the orchestrator that reads the environment or the ``.env`` file.
Everything else asks ``load_settings()`` for a typed, validated ``Settings`` and uses the field
it needs. ``.env.example`` lists the names.

* A variable set in the shell wins over the project's ``.agent-lab/.env``, which wins over the
  orchestrator's ``.env`` (the fallback for every project).
* The project's GitHub repository comes from its ``.agent-lab.toml`` before
  ``AGENT_LAB_REPOSITORY``, so a value kept for one project never points another one elsewhere.
* Only the names below are taken from ``.env``, and they go into the ``Settings`` object, never
  into the process environment. Other names in the file (``ANTHROPIC_API_KEY`` is on the agents'
  allowlist, ``scripts/agent_env.py``) are left alone on purpose, and neither GitHub token is
  ever handed to an agent.
* A value that is not valid stops the command with a message that names it.
* Values are never printed or logged.

Known gap until the sandbox exists: an agent's Read tool is not limited by path, so an agent
could open ``.env`` by its full path. ``.gitignore`` keeps it out of commits, and worktrees are
made from commits, so it is not copied into them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.project import Project
from orchestrator.core.project_config import load_project_config
from orchestrator.errors import ExecutionRefused

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE_NAME = ".env"
ENV_FILE_VARIABLE = "AGENT_LAB_ENV_FILE"  # another file instead; the tests point it at none
MAX_ENV_FILE_BYTES = 64_000

GITHUB_TOKEN = "GITHUB_TOKEN"
PROJECT_TOKEN = "AGENT_LAB_PROJECT_TOKEN"
REPOSITORY = "AGENT_LAB_REPOSITORY"
PYTHON = "AGENT_LAB_PYTHON"
WORKTREE_ROOT = "AGENT_LAB_WORKTREE_ROOT"
AGENT_RUNS_DIR = "AGENT_LAB_AGENT_RUNS_DIR"
MAX_PARALLEL = "AGENT_LAB_MAX_PARALLEL"
TEST_TIMEOUT = "AGENT_LAB_TEST_TIMEOUT"

# Every name that may come from .env. (AGENT_LAB_ENV_FILE says where .env is, so it cannot.)
SETTING_NAMES = (GITHUB_TOKEN, PROJECT_TOKEN, REPOSITORY, PYTHON, WORKTREE_ROOT, AGENT_RUNS_DIR,
                 MAX_PARALLEL, TEST_TIMEOUT)

DEFAULT_MAX_PARALLEL = 3
MAX_PARALLEL_LIMIT = 8
TEST_TIMEOUT_RANGE = (60, 86400)


@dataclass(frozen=True, slots=True)
class Settings:
    github_token: str | None = None
    project_token: str | None = None  # the board's; github_token is the fallback (project_token_source)
    repository: str | None = None  # OWNER/REPO, for commands run before a plan records it
    python: str | None = None
    worktree_root: Path | None = None
    agent_runs_dir: Path | None = None
    max_parallel: int = DEFAULT_MAX_PARALLEL
    test_timeout: int | None = None  # seconds; None means the project's setting, then the default

    def board_token(self) -> tuple[str | None, str]:
        """(token, the variable it came from) for the Project board."""
        if self.project_token:
            return self.project_token, PROJECT_TOKEN
        if self.github_token:
            return self.github_token, GITHUB_TOKEN
        return None, PROJECT_TOKEN


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if " #" in raw:  # an unquoted value may end with a comment
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def read_env_file(path: Path) -> dict[str, str]:
    """The known names found in a .env file: KEY=VALUE lines, # comments, optional 'export '."""
    try:
        if path.stat().st_size > MAX_ENV_FILE_BYTES:
            return {}
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key in SETTING_NAMES:
            value = _value(raw)
            if value:
                found[key] = value
    return found


def _whole_number(name: str, raw: str, low: int, high: int, advice: str) -> int:
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise ExecutionRefused(
            f"{name} is {raw!r}; it must be a whole number from {low} to {high}{advice}")
    return int(raw)


def _build(values: Mapping[str, str]) -> Settings:
    def get(name: str) -> str | None:
        return (values.get(name) or "").strip() or None

    timeout = get(TEST_TIMEOUT)
    if timeout is not None:
        try:
            seconds = int(timeout)
        except ValueError:
            raise ExecutionRefused(
                f"{TEST_TIMEOUT} must be a whole number of seconds, not {timeout!r}.") from None
        if not TEST_TIMEOUT_RANGE[0] <= seconds <= TEST_TIMEOUT_RANGE[1]:
            raise ExecutionRefused(f"{TEST_TIMEOUT} must be between "
                                   f"{TEST_TIMEOUT_RANGE[0]} and {TEST_TIMEOUT_RANGE[1]} seconds.")
    else:
        seconds = None
    parallel = get(MAX_PARALLEL)
    repository = get(REPOSITORY)
    if repository is not None and repository.count("/") != 1:
        raise ExecutionRefused(f"{REPOSITORY} is {repository!r}; it must be OWNER/REPO.")
    root, runs = get(WORKTREE_ROOT), get(AGENT_RUNS_DIR)
    return Settings(
        github_token=get(GITHUB_TOKEN),
        project_token=get(PROJECT_TOKEN),
        repository=repository,
        python=get(PYTHON),
        worktree_root=Path(root) if root else None,
        agent_runs_dir=Path(runs) if runs else None,
        max_parallel=DEFAULT_MAX_PARALLEL if parallel is None else _whole_number(
            MAX_PARALLEL, parallel, 1, MAX_PARALLEL_LIMIT,
            " (how many tasks run together). Fix it in .env or the window, or use --one-at-a-time."),
        test_timeout=seconds,
    )


_active_project: Project | None = None


def use_project(project: Project | None) -> None:
    """The project this process works on (the command line sets it once), or None."""
    global _active_project
    _active_project = project


def active_project() -> Project | None:
    return _active_project


def load_settings(environ: Mapping[str, str] | None = None, *, env_file: Path | None = None) -> Settings:
    """The settings: the shell (or ``environ``) first, then the .env files, then the defaults.

    With no ``environ`` the real environment is used, and two files are read: the project's
    ``.agent-lab/.env`` (when a project is active) over the orchestrator's ``.env`` (or the
    file named by ``AGENT_LAB_ENV_FILE``). A mapping that is given is taken as the whole
    environment: a file is read only if ``env_file`` is given too.
    """
    given = environ is not None
    environ = os.environ if environ is None else environ
    files: list[Path] = []
    if env_file is not None:
        files.append(env_file)
    elif not given:
        chosen = (environ.get(ENV_FILE_VARIABLE) or "").strip()
        files.append(Path(chosen) if chosen else PROJECT_ROOT / ENV_FILE_NAME)
        if _active_project is not None:
            files.append(_active_project.env_file)
    merged: dict[str, str] = {}
    for path in files:  # later files win
        merged.update(read_env_file(path))
    for name in SETTING_NAMES:
        if (environ.get(name) or "").strip():
            merged[name] = environ[name]
    if not given and _active_project is not None:
        repository = load_project_config(_active_project.root).repository
        if repository:
            merged[REPOSITORY] = repository
    return _build(merged)


def process_environment() -> dict[str, str]:
    """A copy of the environment this process runs in, to start a child process with."""
    return dict(os.environ)
