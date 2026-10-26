"""How plans, tasks and events are printed. Nothing here reads or writes files."""

from __future__ import annotations

from pathlib import Path

from orchestrator.legacy.planning import ExecutionPlan
from orchestrator.planning.profiles import profile_for


def display_issue(issue: dict) -> None:
    """Display the relevant fields from a GitHub Issue."""
    labels = [
        label["name"]
        for label in issue.get("labels", [])
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    ]

    print(f"Issue:       #{issue['number']}")
    print(f"Title:       {issue['title']}")
    print(f"State:       {issue['state']}")
    print(f"URL:         {issue['html_url']}")
    print(f"Labels:      {', '.join(labels) if labels else '(none)'}")
    print("\nDescription:")
    print(issue.get("body") or "(no description)")


def display_plan(plan: ExecutionPlan) -> None:
    task = plan.task
    print(f"Repository:  {task.repository}")
    print(f"Issue:       #{task.issue_number} - {task.title}")
    print(f"Run ID:      {plan.run_id}")
    print(f"Branch:      {plan.branch} (from {plan.base_ref})")
    print(f"Worktree:    {plan.worktree_path}")
    print(f"Timeout:     {plan.timeout_seconds}s, max {plan.max_turns} turns")
    print(f"Model:       {plan.model or '(Claude default)'}")
    print(f"Scope:       {plan.scope}")
    if plan.prohibitions:
        print("\nNOT EXECUTABLE - the Issue prohibits execution:")
        for reason in plan.prohibitions:
            print(f"  - {reason}")


def display_task_plan(record: dict, path: Path) -> None:
    issue, config, planner = record["issue"], record["config"], record["planner"]
    tasks = record["tasks"]
    print(f"AI plan for Issue #{issue['number']} - {issue['title']}")
    print(f"Repository:  {config['repository']}")
    print(f"Run ID:      {config['run_id']}")
    print(f"Branch:      {config['branch']} (from {config['base_ref']})")
    print(f"Model:       {config['model'] or '(Claude default)'}")
    print(f"Timeout:     {config['timeout_seconds']}s")
    if record.get("summary"):
        print(f"\nSummary:     {record['summary']}")

    print(f"\nTasks ({len(tasks)}):")
    for task in tasks:
        depends = ", ".join(task["depends_on"]) or "none"
        print(f"\n  {task['id']}  {task['title']}   (depends on: {depends})")
        print(f"      {task['description']}")
        print(f"      Outcome: {task['expected_outcome']}")
        for criterion in task["acceptance_criteria"]:
            print(f"      Accept:  {criterion}")
        parallel = "parallel-safe" if task["parallel_safe"] else "sequential"
        print(f"      Run as:  {task['complexity']}, {parallel} -> "
              f"{profile_for(task['complexity']).describe()}")
        if task["files"]:
            print(f"      Files:   {', '.join(task['files'])}")
        for step in task["verification"]:
            print(f"      Verify:  {step}")

    metrics = [
        f"{label} {format(planner[key], spec)}"
        for label, key, spec in (
            ("duration", "duration_seconds", ".1f"),
            ("cost USD", "total_cost_usd", ".4f"),
            ("turns", "num_turns", "g"),
        )
        if isinstance(planner.get(key), (int, float))
    ]
    if metrics:
        print(f"\nPlanner:     {', '.join(metrics)}")
    print(f"Plan file:   {path}")


def display_task(record: dict) -> None:
    print(f"Task {record['task_id']} of run {record['run_id']}: {record['status']}")
    print(f"Title:       {record.get('title', '')}")
    print(f"Base commit: {record.get('base_commit', '')[:12]}")
    for earlier in record.get("previous_attempts") or []:
        why = (earlier.get("reasons") or ["no reason recorded"])[0]
        cost = earlier.get("cost_usd")
        spent = f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        print(f"Attempt {earlier.get('attempt')}: {earlier.get('status')}{spent} - {why[:150]}")
    if record.get("attempt", 1) > 1 and record.get("continued_at"):
        print(f"This is attempt {record['attempt']}: the stopped attempt {record['attempt'] - 1} "
              f"continued from {record['continued_at']}, in the same worktree.")
    elif record.get("attempt", 1) > 1:
        print(f"This is attempt {record['attempt']} (a retry, told what attempt {record['attempt'] - 1} got wrong).")
    if record.get("branch"):
        print(f"Branch:      {record['branch']}")
        print(f"Worktree:    {record.get('worktree_path')}")
    if record.get("commit"):
        print(f"Commit:      {record['commit']}")
    merge = record.get("merge") or {}
    if merge.get("branch") and not merge.get("problem"):
        how = {"fast-forward": "fast-forward", "merge": "merged; the full suite passed on the result",
               "already": "already there"}.get(str(merge.get("kind")), str(merge.get("kind")))
        print(f"Merged into: {merge['branch']} ({how})")
    elif merge.get("problem"):
        print(f"Merged into: not merged ({merge['problem']}); see Because")
    reused = record.get("reused_agents") or {}
    stopped = record.get("status") in ("cancelled", "pending", "building", "testing", "reviewing")
    for name, settings in (record.get("roles") or {}).items():
        agent = (record.get("agents") or {}).get(name, {})
        if name in reused and not agent:
            agent = {**reused[name], "outcome": "kept from the stopped attempt"}
        default = "no result recorded (the task was stopped)" if stopped else "not run"
        outcome = agent.get("outcome", default)
        cost = agent.get("total_cost_usd")
        spent = f", spent ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        print(f"  {settings} -> {outcome}{spent}")
    if record.get("cost_usd") is not None:
        print(f"Cost (USD):  {record['cost_usd']}")
    for label, key in (("Full suite", "suite"), ("Named tests", "named_tests")):
        check = (record.get("checks") or {}).get(key)
        if check:
            verdict = "passed" if check["exit_code"] == 0 and not check["timed_out"] else "FAILED"
            print(f"{label + ':':<13}{verdict} (exit {check['exit_code']}, {check['seconds']}s)")
    if record.get("reasons"):
        print("Because:")
        for reason in record["reasons"]:
            print(f"  - {reason}")
    findings = record.get("findings") or {}
    for label, key in (("Tester", "tester_notes"), ("Reviewer", "reviewer_notes")):
        if findings.get(key):
            print(f"{label} notes (not blocking):")
            for note in findings[key]:
                print(f"  - {note}")
    print("History:     " + " -> ".join(step["status"] for step in record.get("history", [])))


def event_lines(item: dict, details: bool) -> list[str]:
    at = item.get("at")
    clock = at[11:19] if isinstance(at, str) and len(at) >= 19 else "--:--:--"
    kind = item.get("tool") if item.get("kind") == "tool" else item.get("kind")
    lines = [f"{clock}  {str(kind):<9} {item.get('title', '')}"]
    result = item.get("result")
    if result:
        lines.append(f"{'':10}└ {result['status']:<7} {result.get('summary', '')}")
    elif item.get("pending"):
        lines.append(f"{'':10}└ waiting...")
    if details:
        for label, text in (("input", item.get("detail")), ("result", (result or {}).get("detail"))):
            if text:
                lines.append(f"{'':12}{label}:")
                lines.extend(f"{'':14}{line}" for line in str(text).splitlines())
    return lines
