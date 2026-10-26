"""Shared fakes for the board tests (M5.2, M5.3): a stored plan, a connected board,
and stand-ins for the Issue and board clients that record every call.

A file of its own, with a unique name: the test folders have no __init__.py.
"""

from orchestrator.github.board import save_board
from orchestrator.github.client import GitHubAPIError
from orchestrator.planning.plan_file import save_task_plan

RUN, REPO, PARENT = "run-pub", "Anji-jk/Agent-lab", 7
COLUMNS = {"todo": {"name": "Todo", "option_id": "o1"},
           "in_progress": {"name": "In progress", "option_id": "o2"},
           "attention": {"name": "Needs Review", "option_id": "o3"},
           "done": {"name": "Done", "option_id": "o4"}}
STATUS_FIELD = 11


def task(task_id, title, criteria, depends=()):
    return {"id": task_id, "title": title, "description": f"Do {title}.",
            "depends_on": list(depends), "expected_outcome": "it works", "files": ["calc.py"],
            "verification": [], "acceptance_criteria": list(criteria),
            "complexity": "small", "parallel_safe": False}


def make_lab(tmp_path):
    plans = runs = tmp_path / "orchestration"
    save_task_plan(plans, RUN, {
        "issue": {"repository": REPO, "number": PARENT, "title": "Add helpers", "url": "u",
                  "description": "Add double and triple."},
        "config": {"repository": REPO, "base_ref": "main", "base_commit": "a" * 40,
                   "model": None, "timeout_seconds": 60, "run_id": RUN, "branch": "b",
                   "worktree_path": "w"},
        "summary": "s",
        "tasks": [task("T1", "double", ["double(2) is 4", "double(-1) is -2"]),
                  task("T2", "triple", ["triple(2) is 6"], depends=["T1"])],
        "planner": {"run_id": f"{RUN}-plan"},
    })
    save_board(runs, {"owner": "Anji-jk", "number": 2, "repository": REPO,
                      "url": "https://github.com/users/Anji-jk/projects/2",
                      "status_field_id": STATUS_FIELD, "columns": COLUMNS})
    return {"plans": plans, "runs": runs}


class FakeIssues:
    """Stands in for GitHubClient: numbers from 100, ids from 9000, every call recorded."""

    def __init__(self, repository=REPO, fail=None, existing=()):
        self.repository, self.fail = repository, dict(fail or {})
        self.calls, self.sub = [], list(existing)
        self.next = 100

    def _maybe_fail(self, what):
        if self.fail.get(what):
            self.fail[what] -= 1
            raise GitHubAPIError(f"GitHub API returned HTTP 403 for {what}.")

    def create_issue(self, title, body):
        self._maybe_fail("create")
        self.next += 1
        self.calls.append(("create", title, body))
        return {"id": 9000 + self.next, "number": self.next, "title": title, "body": body}

    def add_sub_issue(self, parent, sub_issue_id):
        self._maybe_fail("link")
        self.calls.append(("link", parent, sub_issue_id))

    def list_sub_issues(self, parent):
        self.calls.append(("list", parent))
        return list(self.sub)

    def close_issue(self, number):
        self._maybe_fail("close")
        self.calls.append(("close", number))

    def add_issue_comment(self, number, comment):
        self._maybe_fail("comment")
        self.calls.append(("comment", number, comment))
        return {"id": 1}

    def comments(self):
        return [(c[1], c[2]) for c in self.calls if c[0] == "comment"]

    def get_issue(self, number):
        self.calls.append(("get", number))
        return {"id": 8000 + number, "number": number}


class FakeProject:
    token_variable = "AGENT_LAB_PROJECT_TOKEN"

    def __init__(self, fail=0, fail_moves=None):
        self.calls, self.fail = [], fail
        self.fail_moves = dict(fail_moves or {})  # item id -> how many moves of it fail

    def add_issue(self, issue_id):
        if self.fail:
            self.fail -= 1
            raise GitHubAPIError("GitHub returned HTTP 403 for project 2 of Anji-jk.")
        self.calls.append(("add", issue_id))
        return 500 + issue_id % 100

    def set_field(self, item_id, field_id, option_id):
        if self.fail_moves.get(item_id):
            self.fail_moves[item_id] -= 1
            raise GitHubAPIError("GitHub returned HTTP 502 for project 2 of Anji-jk.")
        self.calls.append(("move", item_id, field_id, option_id))

    def moves(self):
        return [(c[1], c[3]) for c in self.calls if c[0] == "move"]


def writes(issues, project):
    return [c[0] for c in issues.calls if c[0] not in ("list", "get")] + [c[0] for c in project.calls]
