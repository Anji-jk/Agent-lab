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
