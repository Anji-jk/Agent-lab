"""R6: the task sizes and role shares are read from config/profiles.toml."""

import pytest

from orchestrator.planning import profiles as pr

GOOD = pr.PROFILES_FILE.read_text(encoding="utf-8")


def test_the_file_holds_the_values_the_code_used_to():
    assert pr.PROFILES["small"] == pr.RunProfile("small", "sonnet", "low", 10, 600, 1.0)
    assert pr.PROFILES["medium"] == pr.RunProfile("medium", "sonnet", "medium", 25, 1200, 3.0)
    assert pr.PROFILES["large"] == pr.RunProfile("large", "opus", "high", 50, 2400, 10.0)
    assert pr.MIN_ROLE_TURNS == 5
    assert pr.ROLE_SHARES == {"builder": (1.0, 0.60), "tester": (0.6, 0.25), "reviewer": (0.4, 0.15)}


def write(tmp_path, text):
    path = tmp_path / "profiles.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("old, new, words", [
    ('effort = "high"', 'effort = "loud"', "effort must be one of"),
    ("max_turns = 50", "max_turns = 0", "max_turns must be a positive whole number"),
    ("max_turns = 50", 'max_turns = "50"', "max_turns"),
    ("max_budget_usd = 10.0", "max_budget_usd = -1", "max_budget_usd"),
    ("[profiles.large]", "[profiles.huge]", "profiles must be exactly"),
    ("[shares.tester]", "[shares.cook]", "shares must be exactly"),
    ("min_role_turns = 5", "min_role_turns = 0", "min_role_turns"),
    ('model = "opus"', 'model = ""', "model"),
])
def test_a_bad_file_is_refused_with_a_message(tmp_path, old, new, words):
    assert old in GOOD
    with pytest.raises(ValueError, match=words):
        pr.load_profiles(write(tmp_path, GOOD.replace(old, new)))


def test_a_missing_or_broken_file_is_refused(tmp_path):
    with pytest.raises(ValueError, match="could not be read"):
        pr.load_profiles(tmp_path / "none.toml")
    with pytest.raises(ValueError, match="could not be read"):
        pr.load_profiles(write(tmp_path, "not toml ["))


def test_a_changed_file_changes_the_role_allowance(tmp_path):
    profiles, minimum, shares = pr.load_profiles(write(tmp_path, GOOD.replace("budget = 0.60", "budget = 0.50")))
    assert shares["builder"] == (1.0, 0.50)
