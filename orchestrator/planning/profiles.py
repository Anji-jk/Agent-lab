"""How much model, effort and budget a task of a given size gets.

The planner labels each task small, medium or large. This module turns that
label into concrete run settings, so spending is decided by one table that a
person can read and change, not by the AI.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.execution.roles import BUILDER, REVIEWER, ROLE_NAMES, TESTER
from orchestrator.planning.plan_file import COMPLEXITIES


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


# --- the three agents of one task ------------------------------------------
# The profile above is the whole task's allowance. Each agent gets a share of
# it: the builder does most of the work, the tester less, the reviewer reads
# one diff. These numbers are starting guesses, to be tuned from real runs.

MIN_ROLE_TURNS = 5

# role -> (share of the turn limit, share of the spending cap)
ROLE_SHARES: dict[str, tuple[float, float]] = {
    BUILDER: (1.0, 0.60),
    TESTER: (0.6, 0.25),
    REVIEWER: (0.4, 0.15),
}

assert set(ROLE_SHARES) == set(ROLE_NAMES), "every role needs a share"


@dataclass(frozen=True, slots=True)
class RoleSettings:
    role: str
    model: str
    effort: str
    max_turns: int
    timeout_seconds: int
    max_budget_usd: float

    def describe(self) -> str:
        return (
            f"{self.role}: {self.model}, {self.effort} effort, "
            f"{self.max_turns} turns, ${self.max_budget_usd:.2f}"
        )


def role_settings(profile: RunProfile, role: str) -> RoleSettings:
    """What one agent of a task may use, taken from the task's profile."""
    try:
        turn_share, budget_share = ROLE_SHARES[role]
    except KeyError:
        raise ValueError(f"Unknown role: {role!r}") from None
    return RoleSettings(
        role=role,
        model=profile.model,
        effort=profile.effort,
        max_turns=max(MIN_ROLE_TURNS, round(profile.max_turns * turn_share)),
        timeout_seconds=profile.timeout_seconds,
        max_budget_usd=round(profile.max_budget_usd * budget_share, 4),
    )
