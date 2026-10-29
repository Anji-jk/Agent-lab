"""The project's own settings: ``.agent-lab.toml`` in the root of the repository it works on.

The orchestrator and the project it works on are separate repositories. What differs per
project (how its tests run, which Python, how long they may take, an example test command
shown to the agents) is read from this file. A missing file means the defaults.

    test_command = ["python", "-m", "pytest", "-q"]   # run as given, in the worktree
    python = "C:/path/to/python.exe"                   # runs the tests and the agents' `python`
    test_timeout = 1200                                # seconds, 60 to 86400
    test_example = "tests/test_calc.py tests/test_io.py"
    repository = "OWNER/REPO"                          # its GitHub repository

    [profiles.small]                                   # optional: change a task size
    model = "opus"                                     # (config/profiles.toml is the default)
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from orchestrator.errors import ExecutionRefused

CONFIG_FILE_NAME = ".agent-lab.toml"
DEFAULT_TEST_EXAMPLE = "tests/test_example.py"
TIMEOUT_RANGE = (60, 86400)
KNOWN_KEYS = frozenset({"test_command", "python", "test_timeout", "test_example", "repository",
                        "profiles"})


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    test_command: tuple[str, ...] | None = None
    python: str | None = None
    test_timeout: int | None = None
    test_example: str = DEFAULT_TEST_EXAMPLE
    repository: str | None = None
    profiles: dict[str, object] | None = None  # checked by planning/profiles.py, which knows them


def _bad(name: str, why: str) -> ExecutionRefused:
    return ExecutionRefused(f"{CONFIG_FILE_NAME}: {name} {why}.")


def parse_project_config(data: dict[str, object]) -> ProjectConfig:
    unknown = sorted(set(data) - KNOWN_KEYS)
    if unknown:
        raise _bad(", ".join(unknown), "is not a known setting")
    command = data.get("test_command")
    if command is not None and not (
        isinstance(command, list) and command and all(isinstance(c, str) and c for c in command)
    ):
        raise _bad("test_command", "must be a list of strings")
    python = data.get("python")
    if python is not None and not (isinstance(python, str) and python.strip()):
        raise _bad("python", "must be a path")
    timeout = data.get("test_timeout")
    if timeout is not None and not (
        isinstance(timeout, int) and not isinstance(timeout, bool)
        and TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]
    ):
        raise _bad("test_timeout", f"must be a whole number of seconds from {TIMEOUT_RANGE[0]} "
                                   f"to {TIMEOUT_RANGE[1]}")
    example = data.get("test_example", DEFAULT_TEST_EXAMPLE)
    if not (isinstance(example, str) and example.strip()):
        raise _bad("test_example", "must be text")
    repository = data.get("repository")
    if repository is not None and not (
        isinstance(repository, str) and repository.strip().count("/") == 1
        and all(repository.strip().split("/"))
    ):
        raise _bad("repository", "must be OWNER/REPO")
    profiles = data.get("profiles")
    if profiles is not None and not isinstance(profiles, dict):
        raise _bad("profiles", "must be a table, such as [profiles.small]")
    return ProjectConfig(
        tuple(command) if command else None,  # type: ignore[arg-type]
        python.strip() if isinstance(python, str) else None,
        timeout,  # type: ignore[arg-type]
        example.strip(),
        repository.strip() if isinstance(repository, str) else None,
        profiles,  # type: ignore[arg-type]
    )


def load_project_config(repo_path: Path) -> ProjectConfig:
    path = Path(repo_path) / CONFIG_FILE_NAME
    if not path.is_file():
        return ProjectConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ExecutionRefused(f"{CONFIG_FILE_NAME} could not be read: {exc}") from None
    return parse_project_config(data)
