"""What each agent in a task's pipeline may do.

A task is built by one agent, tested by a second and reviewed by a third.
Each gets a different set of tools, expressed as Claude Code command-line
flags so that no setting on your machine can widen them.

These flags are one layer. The orchestrator also checks the result: after
every agent it looks at what actually changed (see ``task_checks``), because
a rule such as "write only under tests/" cannot be fully expressed once an
agent is allowed to run commands.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.roles import BUILDER, REVIEWER, ROLE_NAMES, TESTER  # noqa: F401


TEST_DIR = "tests"

# Claude Code's shell tool is "Bash" where it finds Git Bash and "PowerShell"
# on Windows without it (checked live: that machine has no Bash tool at all).
# A tool that does not exist is dropped silently, so every role names both
# and every rule below is written for both, with no platform detection.
SHELLS = ("Bash", "PowerShell")
WORKING_TOOLS = "Read,Grep,Glob,Edit,Write," + ",".join(SHELLS)

# Git commands that change history, branches or the index. Only the
# orchestrator does those, so no agent gets them.
GIT_WRITE_RULES = tuple(
    f"{shell}(git {verb}:*)"
    for shell in SHELLS
    for verb in (
        "add", "commit", "push", "pull", "fetch", "checkout", "switch",
        "reset", "restore", "rebase", "merge", "cherry-pick", "revert",
        "branch", "tag", "stash", "worktree", "config", "clean", "remote",
    )
)

# Commands an agent may run to look around and to run the tests.
READ_AND_TEST_RULES = tuple(
    f"{shell}({command}:*)"
    for shell in SHELLS
    for command in ("python -m pytest", "git status", "git diff", "git log")
)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    name: str
    read_only: bool
    tools: str | None
    allowed: tuple[str, ...]
    denied: tuple[str, ...]
    may_write: tuple[str, ...]  # path prefixes the role may change; () = none
    purpose: str

    def runner_options(self) -> dict:
        """Keyword arguments for ``runner_adapter.invoke_runner``."""
        if self.read_only:
            return {"read_only": True}
        return {
            "isolated": True,
            "tools": self.tools,
            "allowed_tools": list(self.allowed),
            "disallowed_tools": list(self.denied),
        }


ROLES: dict[str, RoleSpec] = {
    BUILDER: RoleSpec(
        name=BUILDER,
        read_only=False,
        tools=WORKING_TOOLS,
        allowed=("Read", "Grep", "Glob", "Edit", "Write", *READ_AND_TEST_RULES),
        denied=("mcp__*", *GIT_WRITE_RULES),
        may_write=("",),  # anywhere in the worktree; the plan's files are checked
        purpose="Makes the change. Cannot commit or move branches.",
    ),
    TESTER: RoleSpec(
        name=TESTER,
        read_only=False,
        tools=WORKING_TOOLS,
        allowed=(
            "Read", "Grep", "Glob",
            f"Edit({TEST_DIR}/**)", f"Write({TEST_DIR}/**)",
            *READ_AND_TEST_RULES,
        ),
        denied=("mcp__*", *GIT_WRITE_RULES),
        may_write=(f"{TEST_DIR}/",),
        purpose="Writes and runs tests for each acceptance criterion. Cannot change the code under test.",
    ),
    REVIEWER: RoleSpec(
        name=REVIEWER,
        read_only=True,
        tools=None,
        allowed=(),
        denied=(),
        may_write=(),
        purpose="Reads the change and judges each criterion. Cannot change anything.",
    ),
}


def role(name: str) -> RoleSpec:
    try:
        return ROLES[name]
    except KeyError:
        raise ValueError(f"Unknown role: {name!r}") from None
