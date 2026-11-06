"""The feature doc: written once every task of a run is done, committed on the run's branch.

One Markdown file in the project's docs folder (``docs_dir`` in .agent-lab.toml), in two parts:

* **What it does, How to use it, How it works**: written by the documenter, a read-only agent
  that reads the code on the run's branch and answers with the three sections as JSON. It
  cannot change a file; the orchestrator writes the doc.
* **How it was built, Tests, Run record**: written by the orchestrator from its own records
  (each task's record, the run's branch record and the plan), so no agent can get them wrong:
  what each builder did, the files it and the tester changed, the commit, the attempts, which
  test checks which acceptance criterion, the test suite, and the reviewer's evidence.

If the documenter fails twice, the doc is still written with the orchestrator's part and a
line saying the overview is missing. An existing file is never overwritten: a second doc for
the same name gets the run ID in its name. Nothing happens without a docs folder setting, or
before every task is done; the reason is returned instead.
"""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.core.project_config import load_project_config
from orchestrator.execution import agents as ta
from orchestrator.execution import checks as tc
from orchestrator.execution import integration as ig
from orchestrator.execution import runner_adapter
from orchestrator.execution.diagnose import failure_reason
from orchestrator.planning.plan_file import load_task_plan
from orchestrator.prompts.loader import render
from orchestrator.records.run_registry import RunRegistryError, utc_now
from orchestrator.records.task_registry import load_task

DOCUMENTER_TIMEOUT = 900
DOCUMENTER_MAX_TURNS = 20
DOCUMENTER_MODEL = "sonnet"
ATTEMPTS = 2  # one retry: a bad answer is often fine the second time
SECTIONS = (("what_it_does", "What it does"), ("how_to_use", "How to use it"),
            ("how_it_works", "How it works"))
MAX_SECTION_CHARS = 12_000


class DocProblem(Exception):
    """The doc cannot be written now; the message says why and what to do."""


@dataclass
class FeatureDoc:
    path: str | None = None       # repository-relative
    commit: str | None = None
    skipped: str | None = None    # why nothing was written
    notes: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    overview_missing: str | None = None


# --- the orchestrator's part -------------------------------------------------------------

def _cell(text: Any) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _code(paths: list[str]) -> str:
    return ", ".join(f"`{path}`" for path in paths) or "(none recorded)"


def _money(value: Any) -> str:
    return f"${value:.2f}" if isinstance(value, (int, float)) else "-"


def _total_cost(record: dict[str, Any]) -> float:
    earlier = sum(p.get("cost_usd") or 0 for p in record.get("previous_attempts") or []
                  if isinstance(p, dict) and isinstance(p.get("cost_usd"), (int, float)))
    now = record.get("cost_usd")
    return earlier + (now if isinstance(now, (int, float)) else 0)


def _name(task: dict[str, Any]) -> str:
    return task["id"] + (f" ({task['jira_key']})" if task.get("jira_key") else "")


def _suite_line(label: str, result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    passed = result.get("exit_code") == 0 and not result.get("timed_out")
    tail = [line.strip() for line in str(result.get("output_tail") or "").splitlines()
            if line.strip()]
    last = f": `{_cell(tail[-1])}`" if tail else ""
    seconds = result.get("seconds")
    took = f", {seconds:.1f} s" if isinstance(seconds, (int, float)) else ""
    return (f"- {label}: {'passed' if passed else 'FAILED'} (exit {result.get('exit_code')}"
            f"{took}){last}")


def built_section(plan: dict[str, Any], records: dict[str, dict[str, Any]],
                  merges: dict[str, dict[str, Any]]) -> str:
    lines = ["## How it was built", "",
             "Copied from the orchestrator's records of each task.", ""]
    for task in plan["tasks"]:
        record = records[task["id"]]
        checks = record.get("checks") or {}
        merge = merges.get(task["id"]) or record.get("merge") or {}
        lines += [f"### {_name(task)} {task['title']}", ""]
        lines.append(f"- What the builder did (its own words): "
                     f"{_cell(checks.get('builder_summary')) or '(no summary recorded)'}")
        lines.append(f"- Files the builder changed: {_code(checks.get('builder_changed') or [])}")
        lines.append(f"- Files the tester added: {_code(checks.get('tester_changed') or [])}")
        commit = record.get("commit")
        kind = merge.get("kind")
        lines.append(f"- Commit: `{str(commit)[:12]}`" + (f" ({kind} into the run's branch)"
                                                          if kind else "") if commit
                     else "- Commit: (none recorded)")
        earlier = [p for p in record.get("previous_attempts") or [] if isinstance(p, dict)]
        attempts = f"- Attempts: {len(earlier) + 1}"
        for item in earlier:
            reason = _cell((item.get("reasons") or ["no reason recorded"])[0])
            attempts += f"; attempt {item.get('attempt')} {item.get('status')}: {reason}"
        lines += [attempts, f"- Cost: {_money(_total_cost(record))}", ""]
    return "\n".join(lines)


def tests_section(plan: dict[str, Any], records: dict[str, dict[str, Any]]) -> str:
    lines = ["## Tests", "",
             "Each acceptance criterion, the tests the tester wrote for it, and where the "
             "reviewer found it met. The suites were run by the orchestrator.", ""]
    for task in plan["tasks"]:
        record = records[task["id"]]
        checks, findings = record.get("checks") or {}, record.get("findings") or {}
        coverage = checks.get("coverage") or {}
        evidence = checks.get("review_evidence") or {}
        uncovered = findings.get("uncovered") or {}
        lines += [f"### {_name(task)} {task['title']}", "",
                  "| Acceptance criterion | Tests | Reviewer |", "|---|---|---|"]
        for number, criterion in enumerate(task["acceptance_criteria"], 1):
            key = str(number)
            tests = coverage.get(key) or []
            tests_text = ("<br>".join(f"`{_cell(t)}`" for t in tests) if tests else
                          f"not automated: {_cell(uncovered.get(key) or uncovered.get(number))}"
                          if (key in uncovered or number in uncovered) else "(none recorded)")
            review = f"met: {_cell(evidence[key])}" if evidence.get(key) else "met"
            lines.append(f"| {number}. {_cell(criterion)} | {tests_text} | {review} |")
        lines.append("")
        for line in (_suite_line("Full test suite", checks.get("suite")),
                     _suite_line("Tests named by the tester", checks.get("named_tests"))):
            if line:
                lines.append(line)
        verdict = findings.get("reviewer_verdict") or "pass"
        lines.append(f"- Reviewer verdict: {verdict}")
        for note in (findings.get("reviewer_notes") or []) + (findings.get("tester_notes") or []):
            lines.append(f"- Note: {_cell(note)}")
        lines.append("")
    return "\n".join(lines)


def run_section(plan: dict[str, Any], records: dict[str, dict[str, Any]],
                branch: str, documenter_cost: float) -> str:
    config, planner = plan.get("config") or {}, plan.get("planner") or {}
    tasks_cost = sum(_total_cost(record) for record in records.values())
    planner_cost = planner.get("total_cost_usd") if isinstance(
        planner.get("total_cost_usd"), (int, float)) else 0.0
    ends = sorted(str(r.get("updated_at")) for r in records.values() if r.get("updated_at"))
    total = tasks_cost + planner_cost + documenter_cost
    return "\n".join([
        "## Run record", "",
        f"- Run: `{config.get('run_id')}`",
        f"- Branch: `{branch}`, from `{config.get('base_ref')}` at "
        f"`{str(config.get('base_commit'))[:12]}`",
        f"- Planned: {plan.get('created_at') or '-'}; last task finished: "
        f"{ends[-1] if ends else '-'}",
        f"- Cost: {_money(total)} (planner {_money(planner_cost)}, tasks "
        f"{_money(tasks_cost)}, documenter {_money(documenter_cost)})", ""])


# --- the documenter ----------------------------------------------------------------------

def _feature_block(plan: dict[str, Any]) -> str:
    story, issue = ta.story_of(plan), plan.get("issue") or {}
    if story:
        return (f"--- BEGIN STORY {story['key']} ---\nTitle: {story['title']}\nEpic: "
                f"{story['epic']}\n\n{ta._cut(story['description'], ta.MAX_ISSUE_CHARS)}\n"
                "--- END STORY ---")
    return (f"--- BEGIN ISSUE #{issue.get('number')} ---\nTitle: {issue.get('title', '')}\n\n"
            f"{ta._cut(issue.get('description') or '', ta.MAX_ISSUE_CHARS)}\n--- END ISSUE ---")


def _tasks_block(plan: dict[str, Any], records: dict[str, dict[str, Any]]) -> str:
    items = []
    for task in plan["tasks"]:
        checks = records[task["id"]].get("checks") or {}
        files = sorted({*(checks.get("builder_changed") or []),
                        *(checks.get("tester_changed") or [])})
        criteria = "".join(f"\n    - {item}" for item in task["acceptance_criteria"])
        items.append(f"- {_name(task)} {task['title']}\n  Acceptance criteria:{criteria}\n"
                     f"  Builder's summary: {checks.get('builder_summary') or '(none)'}\n"
                     f"  Files: {', '.join(files) or '(none recorded)'}")
    return "--- BEGIN TASKS ---\n" + "\n".join(items) + "\n--- END TASKS ---"


def documenter_prompt(plan: dict[str, Any], records: dict[str, dict[str, Any]]) -> str:
    item_source = "the Issue" if ta.story_of(plan) is None else "Jira (the story and its sub-tasks)"
    return render("documenter", feature_block=_feature_block(plan),
                  tasks_block=_tasks_block(plan, records), item_source=item_source)


def parse_documenter(text: object) -> tuple[dict[str, str], list[str]]:
    data = ta.extract_json(text)
    sections = {}
    for key, title in SECTIONS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ta.ReportError(f"{key} must be non-empty text.")
        sections[key] = value.strip()[:MAX_SECTION_CHARS]
    notes = data.get("notes") or []
    if not isinstance(notes, list):
        raise ta.ReportError("notes must be a list.")
    return sections, [str(note) for note in notes]


def _run_documenter(plan: dict[str, Any], records: dict[str, dict[str, Any]], worktree: Path,
                    run_id: str, *, invoke_runner, runner_script: Path, model: str | None,
                    timeout: int, max_turns: int) -> tuple[dict[str, str] | None, list[str],
                                                           float, str | None]:
    """The three sections, the documenter's notes, its cost, and why it failed (or None)."""
    prompt, cost, problem = documenter_prompt(plan, records), 0.0, None
    for attempt in range(1, ATTEMPTS + 1):
        agent_id = f"{run_id}-doc" + (f"-r{attempt}" if attempt > 1 else "")
        with tempfile.TemporaryDirectory(prefix="agent-lab-doc-") as scratch:
            prompt_file = Path(scratch) / "documenter-prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            outcome = invoke_runner(
                run_id=agent_id, worktree_path=worktree, prompt_file=prompt_file,
                timeout_seconds=timeout, model=model, max_turns=max_turns,
                keep_result_text=True, read_only=True, role="documenter",
                runner_script=runner_script)
        spent = outcome.result.get("total_cost_usd")
        cost += spent if isinstance(spent, (int, float)) else 0.0
        if tc.changed_paths(worktree):
            raise DocProblem(
                f"The documenter changed files in the run's branch worktree {worktree}, which it "
                "must not do. Nothing was committed; look at git status there and discard them.")
        if outcome.outcome != "success":
            problem = (f"the documenter {outcome.outcome.replace('_', ' ')}: "
                       f"{failure_reason(outcome.outcome, outcome.error, outcome.result, outcome.process_exit_code)}")
            continue
        try:
            sections, notes = parse_documenter(outcome.result.get("result_text"))
        except ta.ReportError as exc:
            problem = f"the documenter's answer was not usable: {exc}"
            continue
        return sections, notes, cost, None
    return None, [], cost, problem


# --- putting it together -----------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50].strip("-") or "feature"


def doc_name(plan: dict[str, Any], docs_dir: str, worktree: Path) -> str:
    """The planner's doc_target when every task that names one agrees, else
    <docs>/<KEY or issue-N>-<title>.md; with the run ID added if that file exists already."""
    targets = Counter(task.get("doc_target") for task in plan["tasks"] if task.get("doc_target"))
    story = ta.story_of(plan)
    label = story["key"] if story else f"issue-{(plan.get('issue') or {}).get('number')}"
    title = (plan.get("issue") or {}).get("title") or "feature"
    name = (next(iter(targets)) if len(targets) == 1
            else f"{docs_dir}/{label}-{_slug(title)}.md")
    if (worktree / name).exists():
        stem, suffix = name.rsplit(".", 1) if "." in name.rsplit("/", 1)[-1] else (name, "md")
        name = f"{stem}-{plan['config']['run_id']}.{suffix}"
    return name


def _heading(plan: dict[str, Any], run_id: str, branch: str) -> str:
    story, issue = ta.story_of(plan), plan.get("issue") or {}
    source = plan.get("source") or {}
    if story:
        title = f"# {story['key']} {story['title']}"
        where = f"Story: [{story['key']}]({source.get('url')}) · Epic: {story['epic']}"
    else:
        title = f"# {issue.get('title') or 'Feature'}"
        where = f"Issue: [#{issue.get('number')}]({issue.get('url')})"
    return "\n".join([title, "", where, "",
                      f"> Written by Agent Lab after run `{run_id}`, on branch `{branch}`. The "
                      "first three sections are the documenter agent's; the rest is copied from "
                      "the orchestrator's records.", ""])


def compose(plan: dict[str, Any], records: dict[str, dict[str, Any]], merges: dict[str, Any],
            branch: str, sections: dict[str, str] | None, problem: str | None,
            documenter_cost: float, notes: list[str]) -> str:
    parts = [_heading(plan, plan["config"]["run_id"], branch)]
    for key, title in SECTIONS:
        body = sections[key] if sections else (
            f"_Not written: {problem}. Run write-doc again to try once more._")
        parts.append(f"## {title}\n\n{body}\n")
    parts += [built_section(plan, records, merges), tests_section(plan, records),
              run_section(plan, records, branch, documenter_cost)]
    if notes:
        parts.append("## Notes from the documenter\n\n" + "\n".join(f"- {_cell(n)}" for n in notes)
                     + "\n")
    return "\n".join(parts).rstrip() + "\n"


def write_feature_doc(
    run_id: str,
    *,
    plans_dir: Path,
    runs_dir: Path,
    repo_path: Path,
    runner_script: Path = runner_adapter.DEFAULT_RUNNER_SCRIPT,
    invoke_runner=runner_adapter.invoke_runner,
    model: str | None = DOCUMENTER_MODEL,
    timeout: int = DOCUMENTER_TIMEOUT,
    max_turns: int = DOCUMENTER_MAX_TURNS,
    again: bool = False,
) -> FeatureDoc:
    """Write and commit the run's feature doc, or return why it was not written.

    ``again`` writes it even if this run already has one (as a new file; nothing is replaced).
    Raises DocProblem for something a person must fix first.
    """
    docs_dir = load_project_config(repo_path).docs_dir
    if not docs_dir:
        return FeatureDoc(skipped="no docs folder: set docs_dir in .agent-lab.toml")
    plan = load_task_plan(plans_dir, run_id)
    records = {}
    for task in plan["tasks"]:
        try:
            records[task["id"]] = load_task(runs_dir, run_id, task["id"])
        except (RunRegistryError, OSError, ValueError):
            return FeatureDoc(skipped=f"{task['id']} has not run yet")
        if records[task["id"]].get("status") != "done":
            return FeatureDoc(skipped=f"{task['id']} is {records[task['id']].get('status')}, "
                                      "not done")
    data = ig.load_integration(runs_dir, run_id)
    if data is None:
        raise DocProblem(f"Run {run_id} has no branch record ({ig.integration_path(runs_dir, run_id)}).")
    if data.get("documentation") and not again:
        done = data["documentation"]
        return FeatureDoc(path=done.get("path"), commit=done.get("commit"),
                          skipped="already written (write-doc --again writes a new one)")
    worktree = Path(data["worktree_path"])
    if not worktree.is_dir():
        raise DocProblem(f"The run's branch worktree {worktree} is missing; restore it with "
                         f"git worktree add {worktree} {data['branch']}.")
    if tc.changed_paths(worktree):
        raise DocProblem(f"The run's branch worktree {worktree} has uncommitted changes; commit "
                         "or discard them first.")

    sections, notes, cost, problem = _run_documenter(
        plan, records, worktree, run_id, invoke_runner=invoke_runner,
        runner_script=Path(runner_script), model=model, timeout=timeout, max_turns=max_turns)
    merges = {m.get("task"): m for m in data.get("merges") or [] if isinstance(m, dict)}
    name = doc_name(plan, docs_dir, worktree)
    text = compose(plan, records, merges, data["branch"], sections, problem, cost, notes)
    label = (ta.story_of(plan) or {}).get("key") or f"issue #{(plan.get('issue') or {}).get('number')}"
    commit = ig.commit_file(runs_dir, run_id, name, text, f"Feature doc for {label}: {name}")
    doc = FeatureDoc(path=name, commit=commit, notes=notes, cost_usd=cost,
                     overview_missing=problem)
    ig.record_documentation(runs_dir, run_id, {
        "path": name, "commit": commit, "at": utc_now(), "documenter_cost_usd": cost,
        "overview_missing": problem, "notes": notes})
    return doc


def describe(doc: FeatureDoc) -> str:
    if doc.skipped:
        where = f" ({doc.path}, commit {str(doc.commit)[:12]})" if doc.path else ""
        return f"Feature doc: not written: {doc.skipped}{where}."
    text = (f"Feature doc: {doc.path}, committed on the run's branch ({str(doc.commit)[:12]}), "
            f"documenter {_money(doc.cost_usd)}.")
    if doc.overview_missing:
        text += f" The overview is missing: {doc.overview_missing}."
    return text

