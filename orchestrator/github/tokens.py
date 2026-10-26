"""The GitHub tokens, from the environment or from the project's .env file (M5.1).

Only two names are taken from ``.env``: ``GITHUB_TOKEN`` (the Issue work) and
``AGENT_LAB_PROJECT_TOKEN`` (the board). Everything else in the file is left
alone on purpose: ``ANTHROPIC_API_KEY`` is on the agents' allowlist
(``scripts/agent_env.py``), so loading every name would pass more to the agents
than intended. Neither GitHub token is on that list, so loading them into this
process does not give them to an agent.

A variable that is already set wins over the file, so a token set in the window
can override ``.env`` for one run. Values are never printed or logged.

Known gap until the M6 sandbox: an agent's Read tool is not limited by path, so
an agent could open this file by its full path. ``.gitignore`` keeps it out of
commits, and worktrees are made from commits, so it is not copied into them.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

TOKEN_NAMES = ("GITHUB_TOKEN", "AGENT_LAB_PROJECT_TOKEN")
ENV_FILE_NAME = ".env"
ENV_FILE_VARIABLE = "AGENT_LAB_ENV_FILE"  # another file instead; the tests point it at none
MAX_ENV_FILE_BYTES = 64_000


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if " #" in raw:  # an unquoted value may end with a comment
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def read_env_file(path: Path) -> dict[str, str]:
    """The token names found in a .env file: KEY=VALUE lines, # comments, optional 'export '."""
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
        if key in TOKEN_NAMES:
            value = _value(raw)
            if value:
                found[key] = value
    return found


def load_tokens(project_root: Path,
                environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Set the GitHub tokens from ``<project>/.env`` where they are not set already.

    Returns the names it set (never the values).
    """
    environ = os.environ if environ is None else environ
    chosen = environ.get(ENV_FILE_VARIABLE, "").strip()
    path = Path(chosen) if chosen else Path(project_root) / ENV_FILE_NAME
    loaded = []
    for key, value in read_env_file(path).items():
        if not environ.get(key, "").strip():
            environ[key] = value
            loaded.append(key)
    return loaded
