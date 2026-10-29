"""Where the agent runner is and where it writes a run's files."""

from __future__ import annotations

from pathlib import Path

from orchestrator.settings import active_project, load_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RUNNER_SCRIPT = PROJECT_ROOT / "scripts" / "claude_runner.py"


def runner_run_directory(run_id: str, runner_script: Path = DEFAULT_RUNNER_SCRIPT) -> Path:
    """Where the runner will write ``run_id``'s artifacts: the project's ``.agent-lab/agents``.

    ``AGENT_LAB_AGENT_RUNS_DIR`` moves them. Without a project (a library call, a test) they go
    beside the runner script, in ``runs/``. The runner is told this folder (``--runs-dir``),
    so the two never have to work it out the same way.
    """
    return agent_runs_root(runner_script) / run_id


def agent_runs_root(runner_script: Path = DEFAULT_RUNNER_SCRIPT) -> Path:
    override = load_settings().agent_runs_dir
    if override:
        return override.resolve()
    project = active_project()
    if project is not None:
        return project.agents_dir
    return Path(runner_script).resolve().parent.parent / "runs"
