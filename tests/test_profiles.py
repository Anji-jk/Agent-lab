import pytest

from orchestrator.profiles import PROFILES, profile_for
from orchestrator.task_plan import COMPLEXITIES


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
