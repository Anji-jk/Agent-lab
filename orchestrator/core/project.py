"""The project a command works on, and where its Agent Lab files live.

A project is a Git repository other than the orchestrator's own. Everything a run produces
for it (plans, task records, the agents' files, the board connection) and its own secrets
live in the project, in ``<project>/.agent-lab/``:

    .agent-lab/
      .gitignore   "*": Git ignores the whole folder, so nothing is committed and the
                   project's checkout stays clean (the planner refuses a dirty one)
      .env         this project's tokens (optional; the orchestrator's .env is the fallback)
      runs/        plans, task records, events, reports, board.json
      agents/      each agent's prompt, result and transcript

The project's settings that are not secret are in ``.agent-lab.toml`` at its root, which is
committed (``core/project_config.py``).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.git import run_git_raw
from orchestrator.errors import ExecutionRefused

ORCHESTRATOR_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR_NAME = ".agent-lab"
IGNORE_EVERYTHING = "# Written by Agent Lab: Git ignores this whole folder.\n*\n"


@dataclass(frozen=True, slots=True)
class Project:
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / DATA_DIR_NAME

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def agents_dir(self) -> Path:
        return self.data_dir / "agents"

    @property
    def env_file(self) -> Path:
        return self.data_dir / ".env"


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve().samefile(b.resolve())
    except OSError:
        return a.resolve() == b.resolve()


def find_project(start: Path) -> Project:
    """The project whose checkout contains ``start``: its Git root, never the orchestrator's."""
    start = Path(start)
    if not start.is_dir():
        raise ExecutionRefused(f"{start} is not a folder.")
    try:
        top = run_git_raw(start, "rev-parse", "--show-toplevel").strip()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        raise ExecutionRefused(
            f"{start.resolve()} is not inside a Git repository. Run the command from the "
            "project's folder, or give --repo-path.") from None
    root = Path(top).resolve()
    if _same(root, ORCHESTRATOR_ROOT):
        raise ExecutionRefused(
            "This is the orchestrator's own repository; its agents work only on other "
            "projects. Run the command from the project's folder, or give --repo-path.")
    return Project(root)


def ensure_data_dir(project: Project) -> Path:
    """Create ``.agent-lab/`` with its ignore-everything ``.gitignore`` if it is missing."""
    project.data_dir.mkdir(exist_ok=True)
    ignore = project.data_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text(IGNORE_EVERYTHING, encoding="utf-8")
    return project.data_dir


def repository_from_origin(root: Path) -> str | None:
    """OWNER/REPO of the checkout's ``origin`` on GitHub, or None if it has no such remote."""
    try:
        url = run_git_raw(root, "remote", "get-url", "origin").strip()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return None
    for prefix in ("https://github.com/", "http://github.com/", "ssh://git@github.com/",
                   "git@github.com:"):
        if url.lower().startswith(prefix):
            parts = url[len(prefix):].removesuffix("/").removesuffix(".git").split("/")
            if len(parts) == 2 and all(parts):
                return "/".join(parts)
    return None
