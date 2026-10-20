"""How much model, effort and budget a task of a given size gets.

The planner labels each task small, medium or large. This module turns that
label into concrete run settings, so spending is decided by one table that a
person can read and change, not by the AI.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.task_plan import COMPLEXITIES


@dataclass(frozen=True, slots=True)
class RunProfile:
    complexity: str
    model: str  # a Claude Code model alias
    effort: str  # low | medium | high | xhigh | max
    max_turns: int
    timeout_seconds: int
    max_budget_usd: float

    def describe(self) -> str:
        return (
            f"{self.model}, {self.effort} effort, {self.max_turns} turns, "
            f"{self.timeout_seconds}s, ${self.max_budget_usd:g} budget"
        )


PROFILES: dict[str, RunProfile] = {
    "small": RunProfile("small", "sonnet", "low", 10, 600, 1.0),
    "medium": RunProfile("medium", "sonnet", "medium", 25, 1200, 3.0),
    "large": RunProfile("large", "opus", "high", 50, 2400, 10.0),
}

assert set(PROFILES) == set(COMPLEXITIES), "every complexity needs a profile"


def profile_for(complexity: str) -> RunProfile:
    try:
        return PROFILES[complexity]
    except KeyError:
        raise ValueError(f"Unknown complexity: {complexity!r}") from None
