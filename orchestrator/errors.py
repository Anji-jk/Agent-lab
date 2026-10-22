"""Error types that more than one part of the orchestrator raises or catches."""

from __future__ import annotations


class ApprovalRequired(RuntimeError):
    """Execution was requested without explicit approval."""


class ExecutionRefused(RuntimeError):
    """The plan cannot be executed (prohibited by the Issue, bad setup)."""
