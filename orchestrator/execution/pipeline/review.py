"""The reviewer, judged against the facts the orchestrator measured."""

from __future__ import annotations

from orchestrator.execution import agents as ta
from orchestrator.execution import checks as tc
from orchestrator.execution.roles import REVIEWER



class ReviewStage:
    """The reviewer, judged against the facts the orchestrator measured."""

    def review(self) -> bool:
        f = self.findings
        self.reused.pop(REVIEWER, None)
        self.set_status("reviewing")
        suite, named = self.checks.get("suite", {}), self.checks.get("named_tests", {})
        facts = [
            f"Files changed: {', '.join(tc.changed_paths(self.worktree)) or '(none)'}",
            f"Full test suite: {'passed' if f.suite_passed else 'FAILED'} "
            f"(exit {suite.get('exit_code')})",
            f"Tests named by the tester: {'passed' if f.named_passed else 'FAILED'} "
            f"(exit {named.get('exit_code')})",
            f"Criteria the tester could not automate: {len(f.uncovered)}",
        ]
        diff = tc.diff_text(self.worktree)
        outcome = self.call(
            REVIEWER, ta.reviewer_prompt(self.ctx.plan["issue"], self.task, diff, facts)
        )
        if outcome.outcome != "success":
            return False
        report = self.report(
            REVIEWER, outcome, lambda text: ta.parse_reviewer(text, len(self.criteria))
        )
        if report is None:
            return False
        f.reviewer_ok = True
        f.reviewer_verdict = report.verdict
        f.unmet = [n for n in range(1, len(self.criteria) + 1) if not report.met.get(n, False)]
        f.reviewer_problems = list(report.problems)
        f.reviewer_notes = list(report.notes)
        self.log.emit("reviewer_checked", verdict=report.verdict, unmet=f.unmet,
                      problems=f.reviewer_problems)
        return True
