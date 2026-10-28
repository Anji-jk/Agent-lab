# Agent Lab

An orchestrator for Claude Code agents. It turns a GitHub Issue into a plan of tasks, runs each task
with a builder, a tester and a reviewer agent in an isolated Git worktree, checks the result itself,
and merges the finished tasks into one branch per run. Nothing is pushed.

The orchestrator works on **other projects**: each one is its own repository (the first is
`csv-summary`, next to this one). Agents never work on the orchestrator's own code.

## Requirements
- Python 3.11+
- Git
- Claude Code CLI

## Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
copy .env.example .env     # then fill in the tokens
```

## Settings

All settings are read in one place, `orchestrator/settings.py`, from the shell or from `.env`
(the shell wins). `.env.example` lists every name with a comment. A value that is not valid stops
the command with a message that names it.

| Setting | Meaning |
| --- | --- |
| `GITHUB_TOKEN`, `AGENT_LAB_PROJECT_TOKEN` | GitHub tokens (Issues; the Project board) |
| `AGENT_LAB_REPOSITORY` | `OWNER/REPO` used when a command is not given `--repo` |
| `AGENT_LAB_MAX_PARALLEL` | how many tasks run together (1 to 8, default 3) |
| `AGENT_LAB_TEST_TIMEOUT` | seconds a test run may take (60 to 86400) |
| `AGENT_LAB_PYTHON`, `AGENT_LAB_WORKTREE_ROOT`, `AGENT_LAB_AGENT_RUNS_DIR` | per-machine paths |

Task sizes (model, effort, turns, timeout, spending cap) and each agent's share are in
`config/profiles.toml`.

## A project

A project is a repository with a `CLAUDE.md` (rules for the agents) and, optionally, a
`.agent-lab.toml`:

```toml
test_command = ["python", "-m", "pytest", "-q"]   # default: <python> -m pytest -q
python = "C:/path/to/python.exe"
test_timeout = 600                                # seconds, 60 to 86400
test_example = "tests/test_csv_summary.py"        # shown to the agents as an example
```

Point the commands at the project with `--repo-path`.

## Commands

`python -m orchestrator <command>`

| Command | What it does |
| --- | --- |
| `issue N` | print an Issue (no AI call) |
| `ai-plan N` | have a read-only agent break the Issue into tasks |
| `show-plan RUN` | show the stored plan (no AI call) |
| `run-plan RUN` | run the plan's tasks, together where it allows, until done or a task needs a person |
| `run-task RUN T1` | run one task; `--retry` for a failed one, `--continue` for a stopped one, `--background` to detach |
| `status RUN` | progress in the terminal (`--watch 2`) |
| `dashboard [RUN]` | progress in the browser, with each agent's events |
| `show-task RUN T1` | one task's full record |
| `report RUN` | the run's summary (`--save` writes `report.md`) |
| `budget RUN` | show, `--set USD` or `--remove` the run's spending limit |
| `cancel RUN T1` | stop a task and everything it started |
| `board connect`, `board sync RUN` | connect a GitHub Project board; put missing tasks on it and move the cards |

Exit codes: 0 success, 1 error, 2 usage, 4 invalid or refused, 7 planning failed, 8 task failed
verification, 9 task needs review.

## Layout

```
orchestrator/
  settings.py        the only module that reads the environment and .env
  core/              small shared helpers: secrets, attempts, roles, git, runner paths
  prompts/           every agent's prompt as a Markdown template (+ loader)
  records/           run and task records, step log
  github/            plain GitHub client: Issues, Project board, publishing
  planning/          plan file, plan model, safety screen, profiles, AI planner
  execution/         the task pipeline (pipeline/), agents, checks, worktrees, merging,
                     budget, cancel, retry, continue
  scheduling/        run-plan, plan progress, run report, board sync
  monitoring/        status, event feed, dashboard
  cli/               parser, exit codes, display, commands/
scripts/             claude_runner.py (headless runner), agent_env.py (what agents may see)
config/profiles.toml task sizes and role shares
tests/               one folder per package
```

Each layer imports only the layers below it: `core`/`settings`/`records` → `github`/`planning`/
`prompts` → `execution` → `scheduling` → `cli`/`monitoring`. `tests/test_layers.py` enforces it.

## Tests

The suite is slow (tests that make Git worktrees). Run it folder by folder:

```
python -m pytest -q tests/execution
python -m pytest -q tests/cli tests/github    # not several folders with a conftest at once
```

`python -m ruff check .` must pass.

## Development policy
Review diffs, run tests, and inspect task results before accepting agent-generated changes.
