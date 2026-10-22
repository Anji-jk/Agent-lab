import pytest

from orchestrator.execution import roles
from orchestrator.execution.roles import BUILDER, REVIEWER, ROLES, TESTER, role


def test_every_role_has_a_spec():
    assert set(ROLES) == set(roles.ROLE_NAMES) == {BUILDER, TESTER, REVIEWER}


def test_reviewer_is_read_only_and_nothing_else():
    options = role(REVIEWER).runner_options()

    assert options == {"read_only": True}
    assert role(REVIEWER).may_write == ()


@pytest.mark.parametrize("name", [BUILDER, TESTER])
def test_working_roles_are_isolated_and_cannot_change_history(name):
    options = role(name).runner_options()

    assert options["isolated"] is True
    assert "mcp__*" in options["disallowed_tools"]
    for verb in ("commit", "push", "checkout", "reset", "branch", "config"):
        assert f"Bash(git {verb}:*)" in options["disallowed_tools"]
    assert not set(options["allowed_tools"]) & set(options["disallowed_tools"])


def test_tester_may_edit_only_the_test_folder():
    allowed = role(TESTER).allowed

    assert "Edit(tests/**)" in allowed and "Write(tests/**)" in allowed
    assert "Edit" not in allowed and "Write" not in allowed
    assert role(TESTER).may_write == ("tests/",)


def test_builder_may_edit_but_the_tester_is_narrower():
    assert "Edit" in role(BUILDER).allowed
    assert "Edit" not in role(TESTER).allowed


def test_no_flag_value_can_be_mistaken_for_a_flag():
    for spec in ROLES.values():
        for value in (*spec.allowed, *spec.denied, *(spec.tools or "").split(",")):
            assert not value.startswith("-")


def test_unknown_role():
    with pytest.raises(ValueError, match="Unknown role"):
        role("janitor")


@pytest.mark.parametrize("name", [BUILDER, TESTER])
def test_both_shell_tools_are_named_because_only_one_exists_per_machine(name):
    tools = role(name).tools.split(",")

    assert "Bash" in tools and "PowerShell" in tools


@pytest.mark.parametrize("name", [BUILDER, TESTER])
def test_rules_exist_for_both_shells(name):
    spec = role(name)

    for shell in roles.SHELLS:
        assert f"{shell}(python -m pytest:*)" in spec.allowed
        assert f"{shell}(git commit:*)" in spec.denied
        assert f"{shell}(git push:*)" in spec.denied


@pytest.mark.parametrize("name", [BUILDER, TESTER])
def test_no_unrestricted_shell_is_ever_pre_approved(name):
    for rule in role(name).allowed:
        assert rule not in roles.SHELLS  # a bare "Bash"/"PowerShell" would allow everything
