"""The project check only reports: what CLAUDE.md and the docs folder give the agents, and what
is missing. It never stops anything."""

import pytest

from orchestrator.core.project_check import (
    CLAUDE_FILE_NAME, check_project, format_project_check, missing_sections, section_headings,
)
from orchestrator.core.project_config import ProjectConfig

GOOD = """\
# Ledger

## Project structure
- src/ledger/: the package
- tests/: one file per module
- docs/: one doc per feature

## Commands
python -m pytest -q

## Rules for the code
- Small functions.
"""


def project(tmp_path, claude_md=GOOD, docs=True):
    if claude_md is not None:
        (tmp_path / CLAUDE_FILE_NAME).write_text(claude_md, encoding="utf-8")
    if docs:
        (tmp_path / "docs").mkdir()
    return tmp_path


def test_a_complete_project_has_no_gaps(tmp_path):
    result = check_project(project(tmp_path), ProjectConfig(docs_dir="docs"))
    assert result.gaps == []
    assert result.docs_dir == "docs"
    assert len(result.findings) == 5  # CLAUDE.md, three sections, the docs folder


def test_without_claude_md_the_planner_falls_back_to_the_file_list(tmp_path):
    result = check_project(project(tmp_path, claude_md=None), ProjectConfig(docs_dir="docs"))
    assert result.gaps == [f"no {CLAUDE_FILE_NAME}: agents get no description of the project; "
                           "the planner works from the file list alone"]
    assert result.docs_dir == "docs"  # the docs are still used


def test_each_missing_section_is_its_own_gap(tmp_path):
    text = "# Ledger\n\n## Project structure\n- src/\n"
    result = check_project(project(tmp_path, claude_md=text), ProjectConfig(docs_dir="docs"))
    assert len(result.gaps) == 2
    assert result.gaps[0].startswith("no Commands section")
    assert result.gaps[1].startswith("no Rules section")


def test_no_docs_folder_is_fine(tmp_path):
    result = check_project(project(tmp_path, docs=False), ProjectConfig())
    assert result.docs_dir is None
    assert result.gaps == ["no docs folder (docs_dir in .agent-lab.toml): the planner uses the "
                           "file list, and no feature doc is written"]


def test_a_docs_dir_that_does_not_exist_yet_is_reported(tmp_path):
    result = check_project(project(tmp_path, docs=False), ProjectConfig(docs_dir="docs"))
    assert result.docs_dir is None
    assert result.gaps == ["docs folder docs/ (docs_dir in .agent-lab.toml) does not exist yet: "
                           "the planner has no docs to read, and the first feature doc makes the "
                           "folder on the run's branch"]


def test_a_nested_docs_folder_works(tmp_path):
    root = project(tmp_path, docs=False)
    (root / "project" / "docs").mkdir(parents=True)
    result = check_project(root, ProjectConfig(docs_dir="project/docs"))
    assert result.gaps == [] and result.docs_dir == "project/docs"


def test_headings_inside_code_blocks_do_not_count():
    assert section_headings("## Commands\n\n```\n## Rules\n```\n") == ["Commands"]


@pytest.mark.parametrize("headings", [
    ["Project structure", "Commands", "Rules"],
    ["Folder structure", "Useful commands", "Instructions for agents"],
    ["STRUCTURE", "commands", "Engineering rules"],
    ["Structure and layout", "Commands", "Agent instructions"],
])
def test_a_heading_counts_when_it_contains_the_word(headings):
    assert missing_sections(headings) == []


def test_every_section_is_missing_without_headings():
    assert missing_sections([]) == ["Project structure", "Commands", "Rules"]
    assert missing_sections(["Purpose", "Environment"]) == ["Project structure", "Commands", "Rules"]


def test_level_three_headings_count_and_trailing_hashes_are_ignored():
    assert section_headings("### Commands ##\n#### Deeper\n# Title\n") == ["Commands"]


def test_the_report_marks_what_is_there_and_what_is_missing(tmp_path):
    result = check_project(project(tmp_path, docs=False), ProjectConfig())
    text = format_project_check(result)
    assert text.splitlines()[0] == f"Project: {tmp_path}"
    assert "  ok  CLAUDE.md found" in text
    assert "  ok  Commands: how to run the project and its tests" in text
    assert "  --  no docs folder" in text
