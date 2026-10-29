"""How much model, effort and budget a task of a given size gets.

The planner labels each task small, medium or large. This module turns that
label into concrete run settings, so spending is decided by one table that a
person can read and change, not by the AI.

``config/profiles.toml`` is the default for every project. A project may change any field of
a size in its own ``.agent-lab.toml`` (``[profiles.small]`` and so on); the shares of each
role and the least turns an agent gets stay the orchestrator's.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.project_config import CONFIG_FILE_NAME, load_project_config
from orchestrator.core.roles import ROLE_NAMES
from orchestrator.planning.plan_file import COMPLEXITIES
from orchestrator.settings import active_project


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


def _bad(why: str, source: str = PROFILES_FILE.name) -> ValueError:
    return ValueError(f"{source}: {why}.")


def _number(table: dict, key: str, kind: type, where: str, positive: bool = True,
            source: str = PROFILES_FILE.name):
    value = table.get(key)
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    if not ok or (positive and value <= 0):
        raise _bad(f"{where}.{key} must be a positive {'whole ' if kind is int else ''}number",
                   source)
    return value


PROFILE_FIELDS = ("model", "effort", "max_turns", "timeout_seconds", "max_budget_usd")


def _profile(name: str, table: dict, source: str = PROFILES_FILE.name) -> RunProfile:
    where = f"profiles.{name}"
    if not isinstance(table.get("model"), str) or not table["model"].strip():
        raise _bad(f"{where}.model must be a model alias", source)
    if table.get("effort") not in EFFORTS:
        raise _bad(f"{where}.effort must be one of {', '.join(EFFORTS)}", source)
    return RunProfile(
        name, table["model"], table["effort"],
        _number(table, "max_turns", int, where, source=source),
        _number(table, "timeout_seconds", int, where, source=source),
        float(_number(table, "max_budget_usd", float, where, source=source)))


def load_profiles(path: Path = PROFILES_FILE) -> tuple[dict[str, RunProfile], int, dict[str, tuple[float, float]]]:
    """(profiles, the least turns an agent gets, each role's (turn share, budget share))."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path.name} could not be read: {exc}") from None
    tables = data.get("profiles") or {}
    if set(tables) != set(COMPLEXITIES):
        raise _bad(f"profiles must be exactly {', '.join(COMPLEXITIES)}")
    profiles = {name: _profile(name, table) for name, table in tables.items()}
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


def with_overrides(base: RunProfile, overrides: object) -> RunProfile:
    """``base`` with the fields a project's ``[profiles.<size>]`` changes, checked the same way."""
    if not isinstance(overrides, dict):
        raise _bad(f"profiles.{base.complexity} must be a table", CONFIG_FILE_NAME)
    unknown = sorted(set(overrides) - set(PROFILE_FIELDS))
    if unknown:
        raise _bad(f"profiles.{base.complexity} has unknown fields: {', '.join(unknown)}",
                   CONFIG_FILE_NAME)
    table = {field: getattr(base, field) for field in PROFILE_FIELDS} | overrides
    return _profile(base.complexity, table, CONFIG_FILE_NAME)


def project_profiles() -> dict[str, object]:
    """The active project's ``[profiles]`` changes, checked to name only known sizes."""
    project = active_project()
    if project is None:
        return {}
    changes = load_project_config(project.root).profiles or {}
    unknown = sorted(set(changes) - set(COMPLEXITIES))
    if unknown:
        raise _bad(f"profiles has unknown sizes: {', '.join(unknown)} "
                   f"(known: {', '.join(COMPLEXITIES)})", CONFIG_FILE_NAME)
    return changes


def profile_for(complexity: str) -> RunProfile:
    try:
        base = PROFILES[complexity]
    except KeyError:
        raise ValueError(f"Unknown complexity: {complexity!r}") from None
    changes = project_profiles().get(complexity)
    return base if changes is None else with_overrides(base, changes)


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
