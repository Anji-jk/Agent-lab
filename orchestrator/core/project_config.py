"""The project's own settings: ``.agent-lab.toml`` in the root of the repository it works on.

The orchestrator and the project it works on are separate repositories. What differs per
project (how its tests run, which Python, how long they may take, an example test command
shown to the agents) is read from this file. A missing file means the defaults.

    test_command = ["python", "-m", "pytest", "-q"]   # run as given, in the worktree
    python = "C:/path/to/python.exe"                   # runs the tests and the agents' `python`
    test_timeout = 1200                                # seconds, 60 to 86400
    test_example = "tests/test_calc.py tests/test_io.py"
    repository = "OWNER/REPO"                          # its GitHub repository
    jira_url = "https://yourteam.atlassian.net"        # optional: its Jira Cloud site
    jira_project = "LEDG"                              # optional: its Jira space (project key)
    docs_dir = "docs"                                  # optional: its folder of feature docs
    base_branch = "dev"                                # optional: the branch runs start from

    [profiles.small]                                   # optional: change a task size
    model = "opus"                                     # (config/profiles.toml is the default)
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from orchestrator.errors import ExecutionRefused

CONFIG_FILE_NAME = ".agent-lab.toml"
DEFAULT_TEST_EXAMPLE = "tests/test_example.py"
TIMEOUT_RANGE = (60, 86400)
KNOWN_KEYS = frozenset({"test_command", "python", "test_timeout", "test_example", "repository",
                        "profiles", "jira_url", "jira_project", "docs_dir", "base_branch"})
DEFAULT_BASE_BRANCH = "main"
# A branch name: it is given to Git, so no leading "-" and no spaces (checked again by Git).
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")
# A Jira project key: a capital letter, then capitals, digits or _ (Jira's default rule), 2-10 long.
JIRA_KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{1,9}")


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    test_command: tuple[str, ...] | None = None
    python: str | None = None
    test_timeout: int | None = None
    test_example: str = DEFAULT_TEST_EXAMPLE
    repository: str | None = None
    profiles: dict[str, object] | None = None  # checked by planning/profiles.py, which knows them
    jira_url: str | None = None  # https://<site>.atlassian.net, no trailing slash
    jira_project: str | None = None  # the Jira project (space) key, such as LEDG
    docs_dir: str | None = None  # the feature docs folder, relative to the root, with / only
    base_branch: str | None = None  # the branch plans and runs start from; None means main


def _bad(name: str, why: str) -> ExecutionRefused:
    return ExecutionRefused(f"{CONFIG_FILE_NAME}: {name} {why}.")


def _docs_dir(value: object) -> str | None:
    """A folder inside the project, written with forward slashes and no trailing slash.

    It must name a real subfolder: the whole project (".") is refused, because the step that
    writes feature docs may change only this folder.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _bad("docs_dir", "must be a folder inside the project, such as \"docs\"")
    text = value.strip().replace("\\", "/").rstrip("/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if (PurePosixPath(text).is_absolute() or PureWindowsPath(value.strip()).drive
            or text.startswith("~") or ".." in parts or not parts):
        raise _bad("docs_dir", "must be a folder inside the project, such as \"docs\"")
    return "/".join(parts)


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
    jira_url = data.get("jira_url")
    if jira_url is not None:
        if not (isinstance(jira_url, str) and jira_url.strip().startswith("https://")
                and len(jira_url.strip()) > len("https://")):
            raise _bad("jira_url", "must be the site's address, such as https://yourteam.atlassian.net")
        jira_url = jira_url.strip().rstrip("/")
    jira_project = data.get("jira_project")
    if jira_project is not None and not (
        isinstance(jira_project, str) and JIRA_KEY_PATTERN.fullmatch(jira_project.strip())
    ):
        raise _bad("jira_project", "must be a project key in capitals, such as LEDG")
    docs_dir = _docs_dir(data.get("docs_dir"))
    base_branch = data.get("base_branch")
    if base_branch is not None and not (
        isinstance(base_branch, str) and BRANCH_PATTERN.fullmatch(base_branch.strip())
        and ".." not in base_branch
    ):
        raise _bad("base_branch", "must be a branch name, such as \"dev\"")
    return ProjectConfig(
        tuple(command) if command else None,  # type: ignore[arg-type]
        python.strip() if isinstance(python, str) else None,
        timeout,  # type: ignore[arg-type]
        example.strip(),
        repository.strip() if isinstance(repository, str) else None,
        profiles,  # type: ignore[arg-type]
        jira_url,  # type: ignore[arg-type]
        jira_project.strip() if isinstance(jira_project, str) else None,
        docs_dir,
        base_branch.strip() if isinstance(base_branch, str) else None,
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
