# Agent Lab

A learning project for building a Claude Code agent orchestration system.

## Requirements
- Python 3.11+
- Git
- Claude Code CLI

## Setup
Create and activate a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Use the installed Python version if Python 3.11 is unavailable.
Run the application
```
python src/csv_summary.py data/sample.csv
```

Run tests

```
python -m pytest -v
```

## Development policy
Review diffs, run tests, and inspect task results before accepting
agent-generated changes.