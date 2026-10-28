"""The tester and the checks that follow it: the full suite and the tester's named tests."""

from __future__ import annotations

from orchestrator.execution.pipeline.base import TEST_DIRS, _command_fields
from orchestrator.execution import agents as ta
from orchestrator.execution import checks as tc
from orchestrator.execution.roles import TESTER



class TestingStage:
    """The tester and the checks that follow it: the full suite and the tester's named tests."""

    def suite(self) -> tc.CommandResult:
        self.log.emit("suite_started", command=list(self.test_command),
                      timeout_seconds=self.test_timeout)
        result = tc.run_suite(self.worktree, command=self.test_command,
                              timeout=self.test_timeout, env=self.env)
        self.log.emit("suite_finished", **_command_fields(result))
        return result

    def run_tests(self) -> bool:
        f = self.findings
        self.reused.pop(TESTER, None)
        f.missing_tests = []  # a continued task may carry the stopped attempt's
        self.set_status("testing")
        changed_by_builder = self.checks["builder_changed"]
        outcome = self.call(
            TESTER, ta.tester_prompt(self.ctx.plan["issue"], self.task, changed_by_builder,
                                     self.earlier, self.test_example)
        )
        if outcome.outcome != "success":
            return False
        report = self.report(
            TESTER, outcome, lambda text: ta.parse_tester(text, len(self.criteria))
        )
        if report is None:
            return False
        f.tester_ok = True

        now = tc.snapshot(self.worktree)
        wrote = tc.changed_between(self.after_build, now)
        f.tester_wrote_outside_tests = tc.outside(wrote, TEST_DIRS)
        self.checks["tester_changed"] = wrote
        f.weakened_tests = tc.removed_test_lines(self.worktree, tc.changed_paths(self.worktree), TEST_DIRS)

        f.uncovered = dict(report.uncovered)
        f.suspected_defects = list(report.suspected_defects)
        f.tester_notes = list(report.notes)
        f.unnamed = [
            n for n in range(1, len(self.criteria) + 1)
            if n not in report.coverage and n not in report.uncovered
        ]
        named = list(dict.fromkeys(t for tests in report.coverage.values() for t in tests))
        f.invalid_test_ids = [t for t in named if not tc.valid_test_id(t, TEST_DIRS)]
        valid = [t for t in named if t not in f.invalid_test_ids]
        f.no_tests = not named
        self.log.emit("tester_checked", changed=wrote, outside_tests=f.tester_wrote_outside_tests,
                      weakened_tests=f.weakened_tests, named_tests=named,
                      invalid_test_ids=f.invalid_test_ids, suspected_defects=f.suspected_defects)

        suite = self.suite()
        f.suite_passed = suite.passed
        f.suite_timed_out = suite.timed_out
        f.test_timeout_seconds = self.test_timeout
        self.checks["suite"] = suite.summary()
        if valid:
            self.log.emit("named_tests_started", tests=valid)
            result = tc.run_named_tests(self.worktree, valid, command=self.test_command,
                                        timeout=self.test_timeout, env=self.env)
            self.log.emit("named_tests_finished", **_command_fields(result))
            self.checks["named_tests"] = result.summary()
            # pytest exit 4 = a named file/test does not exist; 5 = nothing collected.
            if result.exit_code in (4, 5):
                f.missing_tests = valid
            f.named_passed = result.passed
            f.named_timed_out = result.timed_out
        else:
            f.named_passed = False if named else None
        return self.stopped()
