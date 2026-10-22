import pytest

from orchestrator.planning.profiles import PROFILES, ROLE_SHARES, profile_for, role_settings
from orchestrator.execution.roles import BUILDER, REVIEWER, ROLE_NAMES, TESTER
from orchestrator.planning.plan_file import COMPLEXITIES


def test_every_complexity_has_a_profile():
    assert set(PROFILES) == set(COMPLEXITIES)


def test_bigger_tasks_get_more_of_everything():
    small, medium, large = (profile_for(name) for name in ("small", "medium", "large"))

    assert small.max_turns < medium.max_turns < large.max_turns
    assert small.timeout_seconds < medium.timeout_seconds < large.timeout_seconds
    assert small.max_budget_usd < medium.max_budget_usd < large.max_budget_usd


def test_profiles_are_usable_runner_arguments():
    for profile in PROFILES.values():
        assert profile.effort in {"low", "medium", "high", "xhigh", "max"}
        assert profile.model and profile.max_turns >= 1 and profile.timeout_seconds >= 1
        assert profile.max_budget_usd > 0


def test_describe_is_human_readable():
    assert profile_for("medium").describe() == "sonnet, medium effort, 25 turns, 1200s, $3 budget"


def test_unknown_complexity():
    with pytest.raises(ValueError, match="Unknown complexity"):
        profile_for("gigantic")


# --- the three agents of one task ---------------------------------------------


def test_budget_shares_add_up_to_the_task_budget():
    assert sum(budget for _, budget in ROLE_SHARES.values()) == pytest.approx(1.0)
    for profile in PROFILES.values():
        total = sum(role_settings(profile, name).max_budget_usd for name in ROLE_NAMES)
        assert total == pytest.approx(profile.max_budget_usd, rel=1e-3)


def test_every_agent_uses_the_tasks_model_and_effort():
    profile = profile_for("large")
    for name in ROLE_NAMES:
        settings = role_settings(profile, name)
        assert (settings.model, settings.effort) == (profile.model, profile.effort)
        assert settings.timeout_seconds == profile.timeout_seconds


def test_builder_gets_the_most_and_reviewer_the_least():
    for profile in PROFILES.values():
        builder, tester, reviewer = (role_settings(profile, n) for n in (BUILDER, TESTER, REVIEWER))
        assert builder.max_turns >= tester.max_turns >= reviewer.max_turns
        assert builder.max_budget_usd > tester.max_budget_usd > reviewer.max_budget_usd


def test_no_agent_gets_fewer_than_the_minimum_turns():
    settings = role_settings(profile_for("small"), REVIEWER)
    assert settings.max_turns == 5


def test_small_task_numbers():
    profile = profile_for("small")
    assert role_settings(profile, BUILDER).max_turns == 10
    assert role_settings(profile, BUILDER).max_budget_usd == 0.6
    assert role_settings(profile, TESTER).max_turns == 6
    assert role_settings(profile, REVIEWER).max_budget_usd == 0.15


def test_role_settings_describe():
    assert role_settings(profile_for("medium"), TESTER).describe() == (
        "tester: sonnet, medium effort, 15 turns, $0.75"
    )


def test_unknown_role_in_settings():
    with pytest.raises(ValueError, match="Unknown role"):
        role_settings(profile_for("small"), "janitor")
