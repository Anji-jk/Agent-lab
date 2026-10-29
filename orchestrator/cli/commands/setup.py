"""init: get a project ready for Agent Lab. Writes only files that are missing."""

from __future__ import annotations

import argparse

from orchestrator.cli.exit_codes import EXIT_OK
from orchestrator.core.project import Project, ensure_data_dir, repository_from_origin
from orchestrator.core.project_config import CONFIG_FILE_NAME
from orchestrator.errors import ExecutionRefused
from orchestrator.settings import active_project

ENV_TEMPLATE = """\
# This project's own Agent Lab settings. Git ignores this folder, so nothing here is committed.
# A value set here wins over the orchestrator's .env; a variable set in the shell wins over both.
# Leave a line empty to use the orchestrator's value.

# GitHub token for this project's Issues and comments.
GITHUB_TOKEN=
# GitHub token for this project's board (needs the project scope; GITHUB_TOKEN if empty).
AGENT_LAB_PROJECT_TOKEN=
"""

TOML_TEMPLATE = """\
# How Agent Lab works on this project (committed: no secrets here; tokens go in .agent-lab/.env).
repository = "{repository}"

# test_command = ["python", "-m", "pytest", "-q"]   # default: <the project's python> -m pytest -q
# python = "C:/path/to/python.exe"                  # default: this project's .venv
# test_timeout = 600                                # seconds, 60 to 86400
# test_example = "tests/test_example.py"            # shown to the agents as an example

# Change a task size for this project only (config/profiles.toml in Agent Lab is the default):
# [profiles.small]
# model = "sonnet"
# max_budget_usd = 1.0
"""


def init_project(project: Project) -> list[str]:
    """Create what is missing; return a line for each thing done or still to do."""
    lines: list[str] = []
    ignore_existed = (project.data_dir / ".gitignore").exists()
    ensure_data_dir(project)
    if not ignore_existed:
        lines.append("created  .agent-lab/            (Git ignores it: runs, agents' files, .env)")
    if not project.env_file.exists():
        project.env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
        lines.append("created  .agent-lab/.env        fill in this project's tokens")
    config = project.root / CONFIG_FILE_NAME
    if not config.exists():
        repository = repository_from_origin(project.root)
        text = TOML_TEMPLATE.format(repository=repository or "OWNER/REPO")
        config.write_text(text, encoding="utf-8")
        lines.append(f"created  {CONFIG_FILE_NAME}        commit it"
                     + ("" if repository else "; set repository (origin is not on GitHub)"))
    if not lines:
        lines.append("nothing to do: this project is already set up")
    return lines


def run_init(args: argparse.Namespace) -> int:
    project = active_project()
    if project is None:  # main() finds it before any command runs
        raise ExecutionRefused("No project found. Run init in the project's folder.")
    print(f"Project: {project.root}")
    for line in init_project(project):
        print("  " + line)
    return EXIT_OK
