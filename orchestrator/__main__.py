"""``python -m orchestrator``: start the command line (see ``orchestrator/cli``)."""

from orchestrator.cli.commands import main

if __name__ == "__main__":
    raise SystemExit(main())
