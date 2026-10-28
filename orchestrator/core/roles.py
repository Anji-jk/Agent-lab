"""The names of the agents of a task. What each may do is decided in ``execution.roles``."""

from __future__ import annotations

BUILDER = "builder"
TESTER = "tester"
REVIEWER = "reviewer"
ROLE_NAMES = (BUILDER, TESTER, REVIEWER)
