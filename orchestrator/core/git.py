"""The one place the orchestrator starts ``git``.

Callers turn the errors into their own (a failed check, a worktree problem) and word them as
they need; how the process is started, and its text decoded, is the same for all of them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git_raw(root: Path, *args: str, timeout: int = 60, stdin: str | None = None) -> str:
    """``git -C root args`` and its stdout exactly as printed.

    Raises ``subprocess.CalledProcessError`` when git fails, and ``OSError`` or
    ``subprocess.TimeoutExpired`` when it cannot run or takes too long.
    """
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=stdin,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    ).stdout
