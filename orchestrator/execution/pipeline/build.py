"""The builder: run it, then check what it changed."""

from __future__ import annotations

from orchestrator.execution.pipeline.base import TEST_DIRS
from orchestrator.execution import agents as ta
from orchestrator.execution import checks as tc
from orchestrator.execution.roles import BUILDER



class BuildStage:
    """The builder: run it, then check what it changed."""

    # -- stages --
    def build(self) -> bool:
        f = self.findings
        self.reused.pop(BUILDER, None)  # this stage's agent is run again, not reused
        self.set_status("building")
        outcome = self.call(BUILDER, ta.builder_prompt(self.ctx.plan["issue"], self.task, self.feedback,
                                                       self.earlier, self.test_example))
        if outcome.outcome != "success":
            return False
        report = self.report(BUILDER, outcome, ta.parse_builder)
        if report is None:
            return False
        f.builder_ok = True
        f.builder_blocked = (report.blocker or "no reason given") if report.blocked else ""
        # The worktree started clean, so every changed file is the builder's.
        self.after_build = tc.snapshot(self.worktree)
        changed = sorted(self.after_build)
        f.nothing_changed = not changed
        f.scope_violations = tc.scope_violations(changed, self.task["files"], TEST_DIRS)
        self.checks["builder_changed"] = changed
        self.log.emit("builder_checked", changed=changed, scope_violations=f.scope_violations,
                      nothing_changed=f.nothing_changed, blocked=f.builder_blocked)
        return self.stopped()
