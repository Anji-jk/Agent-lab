"""Where the agent runner is and where it writes a run's files."""

from __future__ import annotations

from pathlib import Path

from orchestrator.settings import load_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RUNNER_SCRIPT = PROJECT_ROOT / "scripts" / "claude_runner.py"


def runner_run_directory(run_id: str, runner_script: Path = DEFAULT_RUNNER_SCRIPT) -> Path:
    """Where the runner will write ``run_id``'s artifacts.

    ``AGENT_LAB_AGENT_RUNS_DIR`` (read by scripts/claude_runner.py too) moves them.
    """
    override = load_settings().agent_runs_dir
    if override:
        return override.resolve() / run_id
    return Path(runner_script).resolve().parent.parent / "runs" / run_id
