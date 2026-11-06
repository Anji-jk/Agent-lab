"""A project's own settings come from .agent-lab.toml in its repository (R3)."""

import pytest

from orchestrator.errors import ExecutionRefused
from orchestrator.execution import agents as ta
from orchestrator.execution import task_runner as tr
from orchestrator.core.project_config import (
    CONFIG_FILE_NAME, DEFAULT_TEST_EXAMPLE, ProjectConfig, load_project_config,
)


def write(tmp_path, text):
    (tmp_path / CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
    return tmp_path


def test_without_the_file_everything_is_the_default(tmp_path):
    assert load_project_config(tmp_path) == ProjectConfig()
    assert ProjectConfig().test_example == DEFAULT_TEST_EXAMPLE


def test_every_setting_is_read(tmp_path):
    root = write(tmp_path, 'test_command = ["py", "-m", "pytest"]\npython = "C:/py/python.exe"\n'
                           'test_timeout = 900\ntest_example = "tests/test_a.py"\n')
    config = load_project_config(root)
    assert config == ProjectConfig(("py", "-m", "pytest"), "C:/py/python.exe", 900, "tests/test_a.py")


@pytest.mark.parametrize("text, word", [
    ("test_timeout = 5\n", "test_timeout"),
    ('test_timeout = "900"\n', "test_timeout"),
    ('test_command = "pytest"\n', "test_command"),
    ("test_command = []\n", "test_command"),
    ("python = 3\n", "python"),
    ('test_example = ""\n', "test_example"),
    ("colour = 1\n", "colour"),
    ("this is not toml\n", "could not be read"),
])
def test_a_bad_file_is_refused_with_a_message(tmp_path, text, word):
    with pytest.raises(ExecutionRefused, match=word):
        load_project_config(write(tmp_path, text))


def test_the_command_rules_use_the_projects_example():
    rules = ta.command_rules("tests/test_calc.py")
    assert "`python -m pytest tests/test_calc.py -q`" in rules
    assert "csv" not in ta.COMMAND_RULES
    assert ta.COMMAND_RULES in ta.builder_prompt({"number": 1, "title": "t", "body": ""},
                                                 {"id": "T1", "title": "t", "description": "d",
                                                  "files": [], "acceptance_criteria": ["x"]})


def test_the_timeout_prefers_the_machine_then_the_project(monkeypatch):
    monkeypatch.delenv(tr.TEST_TIMEOUT_VARIABLE, raising=False)
    assert tr.test_timeout_seconds() == tr.TEST_TIMEOUT_SECONDS
    assert tr.test_timeout_seconds(900) == 900
    monkeypatch.setenv(tr.TEST_TIMEOUT_VARIABLE, "300")
    assert tr.test_timeout_seconds(900) == 300


@pytest.mark.parametrize("value, expected", [
    ('"docs"', "docs"),
    ('"docs/"', "docs"),
    ('"./docs"', "docs"),
    ('"project\\\\docs\\\\"', "project/docs"),
    ('" project/docs "', "project/docs"),
])
def test_docs_dir_is_read_with_forward_slashes(tmp_path, value, expected):
    config = load_project_config(write(tmp_path, f"docs_dir = {value}\n"))
    assert getattr(config, "docs_dir") == expected


def test_docs_dir_is_optional(tmp_path):
    config = load_project_config(write(tmp_path, 'repository = "me/ledger"\n'))
    assert getattr(config, "docs_dir") is None


@pytest.mark.parametrize("value", ['""', '"."', '"./"', '"/docs"', '"C:/docs"', '"C:docs"',
                                   '"~/docs"', '"../docs"', '"docs/../.."', "3"])
def test_docs_dir_outside_the_project_is_refused(tmp_path, value):
    with pytest.raises(ExecutionRefused, match="docs_dir must be a folder inside the project"):
        load_project_config(write(tmp_path, f"docs_dir = {value}\n"))


def test_base_branch_is_read(tmp_path):
    assert getattr(load_project_config(write(tmp_path, 'base_branch = " dev "\n')), "base_branch") == "dev"
    assert getattr(load_project_config(write(tmp_path, 'base_branch = "release/2.1"\n')), "base_branch") \
        == "release/2.1"
    assert getattr(ProjectConfig(), "base_branch") is None


@pytest.mark.parametrize("value", ['"-x"', '"two words"', '"a..b"', '""', "3"])
def test_a_bad_base_branch_is_refused(tmp_path, value):
    with pytest.raises(ExecutionRefused, match="base_branch must be a branch name"):
        load_project_config(write(tmp_path, f"base_branch = {value}\n"))


def test_max_retries_is_read(tmp_path):
    assert getattr(load_project_config(write(tmp_path, "max_retries = 3\n")), "max_retries") == 3
    assert getattr(load_project_config(write(tmp_path, "max_retries = 0\n")), "max_retries") == 0
    assert getattr(ProjectConfig(), "max_retries") == 1


@pytest.mark.parametrize("value", ["6", "-1", "true", '"2"', "1.5"])
def test_a_bad_max_retries_is_refused(tmp_path, value):
    with pytest.raises(ExecutionRefused, match="max_retries must be a whole number from 0 to 5"):
        load_project_config(write(tmp_path, f"max_retries = {value}\n"))
