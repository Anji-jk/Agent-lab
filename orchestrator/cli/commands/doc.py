"""write-doc: write the feature doc of a finished run, or write it again.

run-plan writes it on its own once every task is done; this is for a run that finished before
the doc existed, or for another try. It commits a new file on the run's branch and never
replaces a file.
"""

from __future__ import annotations

import argparse
import sys

from orchestrator.cli.exit_codes import EXIT_OK
from orchestrator.errors import ExecutionRefused
from orchestrator.execution.feature_doc import (
    DocProblem, describe, write_feature_doc,
)
from orchestrator.execution.integration import MergeProblem


def run_write_doc(args: argparse.Namespace) -> int:
    print(f"Writing the feature doc of run {args.run_id}: one read-only documenter agent reads "
          "the run's branch. This calls Claude and may take a few minutes...", file=sys.stderr)
    try:
        doc = write_feature_doc(args.run_id, plans_dir=args.plans_dir, runs_dir=args.runs_dir,
                                repo_path=args.repo_path, model=args.model,
                                timeout=args.timeout, max_turns=args.max_turns,
                                again=args.again)
    except (DocProblem, MergeProblem) as exc:
        raise ExecutionRefused(str(exc)) from None
    print(describe(doc))
    if doc.skipped and not doc.path:
        return EXIT_OK
    print("Nothing was pushed, and your own branches were not changed.")
    return EXIT_OK
