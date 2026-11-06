"""The feature doc: the documenter's three sections, the orchestrator's record, one commit."""

import json

import pytest

from orchestrator.execution import feature_doc as fd
from orchestrator.execution.integration import load_integration
from orchestrator.execution.runner_adapter import RunnerOutcome
from task_runner_support import RUN_ID, git, run

ANSWER = {"what_it_does": "Adds `double` and `triple` to calc.",
          "how_to_use": "```python\nfrom calc import double\ndouble(2)  # 4\n```",
          "how_it_works": "Both live in `calc.py` next to `add`.",
          "notes": ["triple has no docstring"]}


class Documenter:
    """A fake documenter: answers, records its calls, and may misbehave."""

    def __init__(self, answers=(ANSWER,), *, write=False):
        self.answers, self.calls, self.prompts, self.write = list(answers), [], [], write

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.prompts.append(open(kwargs["prompt_file"], encoding="utf-8").read())
        if self.write:
            (kwargs["worktree_path"] / "calc.py").write_text("changed\n")
        answer = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if answer == "timeout":
            return RunnerOutcome("timeout", None, "too slow", {"total_cost_usd": 0.05})
        return RunnerOutcome("success", 0, None, {
            "status": "succeeded", "total_cost_usd": 0.2, "num_turns": 4,
            "result_text": answer if isinstance(answer, str) else json.dumps(answer)})


def set_docs(world, docs="docs"):
    (world["repo"] / ".agent-lab.toml").write_text(f'docs_dir = "{docs}"\n', encoding="utf-8")


@pytest.fixture
def finished(world, agents):
    assert run(world, agents)["status"] == "done"
    agents.use_triple()
    assert run(world, agents, "T2")["status"] == "done"
    set_docs(world)
    return world


def write(world, documenter, **more):
    return fd.write_feature_doc(RUN_ID, plans_dir=world["plans"], runs_dir=world["runs"],
                                repo_path=world["repo"], runner_script=world["script"],
                                invoke_runner=documenter, **more)


def committed(world, doc):
    branch = load_integration(world["runs"], RUN_ID)["branch"]
    return git(world["repo"], "show", f"{branch}:{doc.path}")


def test_the_doc_is_written_and_committed_on_the_run_branch(finished, agents):
    documenter = Documenter()
    doc = write(finished, documenter)
    assert doc.path == "docs/issue-7-add-helpers.md" and doc.skipped is None
    assert doc.cost_usd == 0.2 and doc.overview_missing is None
    text = committed(finished, doc)

    assert text.startswith("# Add helpers\n\nIssue: [#7](u)")
    for heading in ("## What it does", "## How to use it", "## How it works",
                    "## How it was built", "## Tests", "## Run record",
                    "## Notes from the documenter"):
        assert f"\n{heading}\n" in text
    assert "Adds `double` and `triple` to calc." in text
    # the orchestrator's part, from the task records
    assert "### T1 double" in text and "### T2 triple" in text
    assert "- What the builder did (its own words): added double" in text
    assert "- Files the builder changed: `calc.py`" in text
    assert "- Files the tester added: `tests/test_double.py`" in text
    assert "| 1. double(2) is 4 | `tests/test_double.py::test_positive` | met: calc.py |" in text
    assert "- Full test suite: passed (exit 0" in text
    assert "- Reviewer verdict: pass" in text
    assert f"- Run: `{RUN_ID}`" in text and "documenter $0.20" in text

    data = load_integration(finished["runs"], RUN_ID)
    assert data["documentation"]["path"] == doc.path
    assert data["documentation"]["commit"] == doc.commit == data["head"]
    # the documenter read the run's branch, read-only, and saw every task
    (call,) = documenter.calls
    assert call["read_only"] is True and call["role"] == "documenter"
    assert str(call["worktree_path"]) == data["worktree_path"]
    assert "--- BEGIN ISSUE #7 ---" in documenter.prompts[0]
    assert "Builder's summary: added triple" in documenter.prompts[0]
    # nothing in the project's own checkout changed
    assert git(finished["repo"], "rev-parse", "HEAD") == finished["base"]


def test_a_second_time_does_nothing_and_again_writes_a_new_file(finished):
    first = write(finished, Documenter())
    again = write(finished, Documenter())
    assert again.skipped.startswith("already written") and again.path == first.path
    newer = write(finished, Documenter(), again=True)
    assert newer.path == f"docs/issue-7-add-helpers-{RUN_ID}.md"
    assert committed(finished, first)  # the first is still there
    data = load_integration(finished["runs"], RUN_ID)
    assert data["documentation"]["path"] == newer.path
    assert data["earlier_documentation"][0]["path"] == first.path


def test_a_failing_documenter_still_leaves_the_record(finished):
    documenter = Documenter(["timeout", "no json here"])
    doc = write(finished, documenter)
    assert len(documenter.calls) == 2  # one retry
    assert "not usable" in doc.overview_missing
    text = committed(finished, doc)
    assert "_Not written: the documenter's answer was not usable" in text
    assert "## How it was built" in text and "added double" in text


def test_a_documenter_that_changes_files_is_stopped(finished):
    with pytest.raises(fd.DocProblem, match="changed files"):
        write(finished, Documenter(write=True))
    assert "documentation" not in load_integration(finished["runs"], RUN_ID)


def test_nothing_without_a_docs_folder_or_before_every_task_is_done(world, agents):
    documenter = Documenter()
    assert run(world, agents)["status"] == "done"
    assert "no docs folder" in write(world, documenter).skipped
    set_docs(world)
    assert write(world, documenter).skipped == "T2 has not run yet"
    assert documenter.calls == []


def test_the_name_follows_the_plan_and_never_replaces_a_file(tmp_path):
    plan = {"config": {"run_id": "r1"}, "issue": {"number": 4, "title": "Month-end: report!"},
            "tasks": [{"doc_target": None}, {}]}
    assert fd.doc_name(plan, "docs", tmp_path) == "docs/issue-4-month-end-report.md"
    story = {**plan, "source": {"kind": "jira", "key": "KAN-10"},
             "tasks": [{"doc_target": "docs/export.md"}, {"doc_target": "docs/export.md"}]}
    assert fd.doc_name(story, "docs", tmp_path) == "docs/export.md"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "export.md").write_text("theirs")
    assert fd.doc_name(story, "docs", tmp_path) == "docs/export-r1.md"
    split = {**story, "tasks": [{"doc_target": "docs/a.md"}, {"doc_target": "docs/b.md"}]}
    assert fd.doc_name(split, "docs", tmp_path) == "docs/KAN-10-month-end-report.md"


def test_the_write_doc_command(finished, monkeypatch, capsys):
    import orchestrator.cli.commands as cli

    documenter = Documenter()
    real = fd.write_feature_doc
    monkeypatch.setattr("orchestrator.cli.commands.doc.write_feature_doc",
                        lambda run_id, **kw: real(run_id, **{**kw, "invoke_runner": documenter,
                                                            "runner_script": finished["script"]}))
    args = ["write-doc", RUN_ID, "--repo-path", str(finished["repo"]),
            "--plans-dir", str(finished["plans"]), "--runs-dir", str(finished["runs"])]
    assert cli.main(args) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Feature doc: docs/issue-7-add-helpers.md, committed on the run's branch" in out
    assert cli.main(args) == cli.EXIT_OK
    assert "not written: already written" in capsys.readouterr().out
    assert documenter.calls[0]["model"] == fd.DOCUMENTER_MODEL
