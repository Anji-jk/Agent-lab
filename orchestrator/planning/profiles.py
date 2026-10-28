"""How much model, effort and budget a task of a given size gets.

The planner labels each task small, medium or large. This module turns that
label into concrete run settings, so spending is decided by one table that a
person can read and change, not by the AI.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.roles import ROLE_NAMES
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


PROFILES_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "profiles.toml"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _bad(why: str) -> ValueError:
    return ValueError(f"{PROFILES_FILE.name}: {why}.")


def _number(table: dict, key: str, kind: type, where: str, positive: bool = True):
    value = table.get(key)
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    if not ok or (positive and value <= 0):
        raise _bad(f"{where}.{key} must be a positive {'whole ' if kind is int else ''}number")
    return value


def load_profiles(path: Path = PROFILES_FILE) -> tuple[dict[str, RunProfile], int, dict[str, tuple[float, float]]]:
    """(profiles, the least turns an agent gets, each role's (turn share, budget share))."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path.name} could not be read: {exc}") from None
    tables = data.get("profiles") or {}
    if set(tables) != set(COMPLEXITIES):
        raise _bad(f"profiles must be exactly {', '.join(COMPLEXITIES)}")
    profiles = {}
    for name, table in tables.items():
        where = f"profiles.{name}"
        if not isinstance(table.get("model"), str) or not table["model"].strip():
            raise _bad(f"{where}.model must be a model alias")
        if table.get("effort") not in EFFORTS:
            raise _bad(f"{where}.effort must be one of {', '.join(EFFORTS)}")
        profiles[name] = RunProfile(
            name, table["model"], table["effort"],
            _number(table, "max_turns", int, where), _number(table, "timeout_seconds", int, where),
            float(_number(table, "max_budget_usd", float, where)))
    shares_table = data.get("shares") or {}
    if set(shares_table) != set(ROLE_NAMES):
        raise _bad(f"shares must be exactly {', '.join(ROLE_NAMES)}")
    shares = {}
    for role, table in shares_table.items():
        where = f"shares.{role}"
        shares[role] = (float(_number(table, "turns", float, where)),
                        float(_number(table, "budget", float, where)))
    minimum = data.get("min_role_turns")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        raise _bad("min_role_turns must be a positive whole number")
    return profiles, minimum, shares


PROFILES, MIN_ROLE_TURNS, ROLE_SHARES = load_profiles()


def profile_for(complexity: str) -> RunProfile:
    try:
        return PROFILES[complexity]
    except KeyError:
        raise ValueError(f"Unknown complexity: {complexity!r}") from None


# --- the three agents of one task ------------------------------------------
# The profile above is the whole task's allowance; each agent gets a share of it
# (config/profiles.toml: ROLE_SHARES maps a role to (turn share, spending share)).


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
