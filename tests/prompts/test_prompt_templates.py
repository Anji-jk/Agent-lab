"""R5: the Markdown prompts say exactly what the Python strings said, and the loader is strict."""

import pytest

from orchestrator.prompts import loader
from prompt_samples import CASES, EXPECTED  # pyright: ignore[reportMissingImports]

TEMPLATES = ("builder", "tester", "reviewer", "planner",
             "shared/command_rules", "shared/task_block", "shared/untrusted_note")


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_rendered_prompt_equals_the_reference_saved_before_the_move(name):
    assert CASES[name]() == (EXPECTED / f"{name}.txt").read_text(encoding="utf-8")


def test_every_reference_has_a_case_and_back():
    assert {p.stem for p in EXPECTED.glob("*.txt")} == set(CASES)


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_exists_and_has_no_stray_dollar(name):
    text = loader.template_text(name)
    assert text.endswith("\n") and "{{include" not in text
    loader.fields_of(name)  # a bad "$" would raise


def test_the_agent_files_name_the_shared_files_they_include():
    assert "{{include shared/command_rules}}" in (loader.PROMPTS_DIR / "builder.md").read_text(encoding="utf-8")
    assert "{{include shared/command_rules}}" in (loader.PROMPTS_DIR / "tester.md").read_text(encoding="utf-8")
    assert "command_rules" not in (loader.PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")


def test_a_missing_field_is_refused():
    with pytest.raises(loader.PromptError, match="missing .*task_block"):
        loader.render("builder", feedback_block="", earlier_block="", test_example="x")


def test_an_unknown_field_is_refused():
    with pytest.raises(loader.PromptError, match="not in the template: surprise"):
        loader.render("shared/untrusted_note", surprise="x")


def test_an_unknown_template_is_refused():
    with pytest.raises(loader.PromptError, match="No prompt template"):
        loader.render("nobody")


def test_braces_and_dollars_in_the_values_are_left_alone():
    text = loader.render("shared/command_rules", test_example='tests/{a}.py $HOME')
    assert "`python -m pytest tests/{a}.py $HOME -q`" in text


def test_the_markdown_files_are_package_data():
    import tomllib
    from pathlib import Path
    config = tomllib.loads((Path(loader.PROMPTS_DIR).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "prompts/**/*.md" in config["tool"]["setuptools"]["package-data"]["orchestrator"]
    assert "prompts/*.md" in config["tool"]["setuptools"]["package-data"]["orchestrator"]
