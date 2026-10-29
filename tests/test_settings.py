"""R6: every setting is read in one place, from the shell and .env, and checked."""

import re
from pathlib import Path

import pytest

import orchestrator.cli.commands as cli
from orchestrator import settings as st
from orchestrator.errors import ExecutionRefused

FINE = "github_pat_" + "F" * 40
CLASSIC = "ghp_" + "C" * 36
ROOT = Path(st.__file__).resolve().parent


def env_file(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def load(environ=None, text=None, tmp_path=None):
    path = env_file(tmp_path, text) if text is not None else None
    return st.load_settings(environ if environ is not None else {}, env_file=path)


def test_the_defaults(tmp_path):
    s = load()
    assert s == st.Settings() and s.max_parallel == 3 and s.test_timeout is None


def test_the_names_in_the_file_are_read_and_no_others(tmp_path):
    path = env_file(tmp_path, "\n".join([
        "# my settings", f"GITHUB_TOKEN={FINE}", f'export AGENT_LAB_PROJECT_TOKEN="{CLASSIC}"',
        "AGENT_LAB_TEST_TIMEOUT=900", "AGENT_LAB_WORKTREE_ROOT=/x", "AGENT_LAB_REPOSITORY=a/b",
        "ANTHROPIC_API_KEY=sk-ant-should-not-be-taken", "OTHER=1"]))
    assert st.read_env_file(path) == {
        "GITHUB_TOKEN": FINE, "AGENT_LAB_PROJECT_TOKEN": CLASSIC, "AGENT_LAB_TEST_TIMEOUT": "900",
        "AGENT_LAB_WORKTREE_ROOT": "/x", "AGENT_LAB_REPOSITORY": "a/b"}


def test_every_setting_can_come_from_the_file(tmp_path):
    s = load(text="\n".join([
        f"GITHUB_TOKEN={FINE}", f"AGENT_LAB_PROJECT_TOKEN={CLASSIC}", "AGENT_LAB_REPOSITORY=o/r",
        "AGENT_LAB_PYTHON=/py", "AGENT_LAB_WORKTREE_ROOT=/w", "AGENT_LAB_AGENT_RUNS_DIR=/r",
        "AGENT_LAB_MAX_PARALLEL=2", "AGENT_LAB_TEST_TIMEOUT=900"]), tmp_path=tmp_path)
    assert (s.github_token, s.project_token, s.repository, s.python) == (FINE, CLASSIC, "o/r", "/py")
    assert (s.worktree_root, s.agent_runs_dir) == (Path("/w"), Path("/r"))
    assert (s.max_parallel, s.test_timeout) == (2, 900)
    assert {n for n in st.SETTING_NAMES} == {
        "GITHUB_TOKEN", "AGENT_LAB_PROJECT_TOKEN", "AGENT_LAB_REPOSITORY", "AGENT_LAB_PYTHON",
        "AGENT_LAB_WORKTREE_ROOT", "AGENT_LAB_AGENT_RUNS_DIR", "AGENT_LAB_MAX_PARALLEL",
        "AGENT_LAB_TEST_TIMEOUT"}


def test_quotes_spaces_and_comments(tmp_path):
    path = env_file(tmp_path, f"  GITHUB_TOKEN = '{FINE}'  \nAGENT_LAB_PROJECT_TOKEN={CLASSIC} # board\n")
    assert st.read_env_file(path) == {"GITHUB_TOKEN": FINE, "AGENT_LAB_PROJECT_TOKEN": CLASSIC}


def test_empty_values_and_broken_lines_are_skipped(tmp_path):
    path = env_file(tmp_path, "GITHUB_TOKEN=\nnot a line\n=x\nAGENT_LAB_PROJECT_TOKEN=\"\"\n")
    assert st.read_env_file(path) == {} and st.read_env_file(tmp_path / "nope.env") == {}


def test_the_shell_wins_over_the_file_and_an_empty_variable_does_not(tmp_path):
    text = f"GITHUB_TOKEN={FINE}\nAGENT_LAB_PROJECT_TOKEN={CLASSIC}\nAGENT_LAB_TEST_TIMEOUT=900\n"
    s = load({"GITHUB_TOKEN": "from-the-window", "AGENT_LAB_TEST_TIMEOUT": "  "}, text, tmp_path)
    assert (s.github_token, s.project_token, s.test_timeout) == ("from-the-window", CLASSIC, 900)


def test_a_given_environment_ignores_the_real_file_unless_one_is_named():
    assert st.load_settings({}) == st.Settings()  # the project's real .env is not read


def test_the_real_environment_reads_the_file_named_by_the_variable(tmp_path, monkeypatch):
    other = env_file(tmp_path, "AGENT_LAB_MAX_PARALLEL=2\n")
    monkeypatch.setenv(st.ENV_FILE_VARIABLE, str(other))
    monkeypatch.delenv(st.MAX_PARALLEL, raising=False)
    assert st.load_settings().max_parallel == 2
    monkeypatch.setenv(st.MAX_PARALLEL, "5")
    assert st.load_settings().max_parallel == 5


def test_the_file_goes_into_the_settings_not_into_the_environment(tmp_path, monkeypatch):
    import os
    other = env_file(tmp_path, f"GITHUB_TOKEN={FINE}\n")
    monkeypatch.setenv(st.ENV_FILE_VARIABLE, str(other))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert st.load_settings().github_token == FINE
    assert "GITHUB_TOKEN" not in os.environ


def test_the_board_token_falls_back_to_the_issue_token():
    assert st.Settings(github_token="g").board_token() == ("g", "GITHUB_TOKEN")
    assert st.Settings(github_token="g", project_token="p").board_token() == ("p", "AGENT_LAB_PROJECT_TOKEN")
    assert st.Settings().board_token() == (None, "AGENT_LAB_PROJECT_TOKEN")


@pytest.mark.parametrize("name, value, words", [
    ("AGENT_LAB_TEST_TIMEOUT", "soon", "whole number of seconds"),
    ("AGENT_LAB_TEST_TIMEOUT", "10", "between 60 and 86400"),
    ("AGENT_LAB_TEST_TIMEOUT", "99999", "between 60 and 86400"),
    ("AGENT_LAB_MAX_PARALLEL", "0", "from 1 to 8"),
    ("AGENT_LAB_MAX_PARALLEL", "many", "from 1 to 8"),
    ("AGENT_LAB_MAX_PARALLEL", "9", "from 1 to 8"),
    ("AGENT_LAB_REPOSITORY", "noslash", "OWNER/REPO"),
])
def test_a_bad_value_is_refused_naming_the_setting(name, value, words):
    with pytest.raises(ExecutionRefused, match=f"{name}.*{words}"):
        st.load_settings({name: value})


def test_a_bad_setting_stops_the_command_with_its_name(monkeypatch, capsys, git_repo):
    monkeypatch.setenv("AGENT_LAB_TEST_TIMEOUT", "soon")
    assert cli.main(["budget", "r", "--repo-path", str(git_repo)]) == cli.EXIT_REFUSED
    assert "AGENT_LAB_TEST_TIMEOUT" in capsys.readouterr().err


def test_no_repository_is_a_clear_error_not_a_hidden_default(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_LAB_REPOSITORY")
    assert cli.main(["issue", "1"]) == cli.EXIT_ERROR
    assert "AGENT_LAB_REPOSITORY" in capsys.readouterr().err
    assert cli.main(["issue", "1", "--repo", "noslash"]) == cli.EXIT_ERROR


def test_the_repository_comes_from_the_setting_when_not_given(monkeypatch, git_repo):
    seen = []
    monkeypatch.setenv("AGENT_LAB_REPOSITORY", "me/mine")
    monkeypatch.setattr(cli.plan, "run_ai_plan", lambda args: seen.append(args.repo) or 0)
    here = ["--repo-path", str(git_repo)]
    assert cli.main(["ai-plan", "3", *here]) == 0 and seen == ["me/mine"]
    assert cli.main(["ai-plan", "3", *here, "--repo", "x/y"]) == 0 and seen[-1] == "x/y"


def test_no_hard_coded_repository_remains_in_the_code():
    for path in ROOT.rglob("*.py"):
        assert "Anji-jk/Agent-lab" not in path.read_text(encoding="utf-8"), path


def test_only_settings_py_reads_the_environment():
    """The runner script (a separate process) and the agents' filter read it themselves."""
    reads = re.compile(r"os\.environ|os\.getenv|os\.putenv|environ\.get\(")
    found = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
             if p.name != "settings.py" and reads.search(p.read_text(encoding="utf-8"))]
    assert found == []


def test_no_github_token_is_on_the_agents_list():
    from agent_env import build_agent_env  # pyright: ignore[reportMissingImports]
    env = build_agent_env({"GITHUB_TOKEN": FINE, "AGENT_LAB_PROJECT_TOKEN": CLASSIC, "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


# --- the project's own settings come before the orchestrator's ---


@pytest.fixture
def project(git_repo, monkeypatch):
    from orchestrator.core.project import ensure_data_dir, find_project
    found = find_project(git_repo)
    ensure_data_dir(found)
    st.use_project(found)
    yield found
    st.use_project(None)


def test_the_projects_env_file_wins_over_the_orchestrators(project, tmp_path, monkeypatch):
    for name in ("AGENT_LAB_REPOSITORY", "GITHUB_TOKEN", "AGENT_LAB_MAX_PARALLEL"):
        monkeypatch.delenv(name, raising=False)
    orchestrators = env_file(tmp_path, f"GITHUB_TOKEN={CLASSIC}\nAGENT_LAB_MAX_PARALLEL=2\n")
    monkeypatch.setenv("AGENT_LAB_ENV_FILE", str(orchestrators))
    project.env_file.write_text(f"GITHUB_TOKEN={FINE}\n", encoding="utf-8")
    s = st.load_settings()
    assert s.github_token == FINE  # the project's own
    assert s.max_parallel == 2  # not set by the project: the orchestrator's


def test_the_shell_still_wins_over_the_projects_env_file(project, monkeypatch):
    project.env_file.write_text(f"GITHUB_TOKEN={FINE}\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", CLASSIC)
    assert st.load_settings().github_token == CLASSIC


def test_the_projects_repository_comes_from_its_toml_before_the_variable(project, monkeypatch):
    monkeypatch.setenv("AGENT_LAB_REPOSITORY", "someone/other-project")
    (project.root / ".agent-lab.toml").write_text('repository = "me/ledger"\n', encoding="utf-8")
    assert st.load_settings().repository == "me/ledger"


def test_without_a_project_only_the_orchestrators_files_are_read(git_repo, monkeypatch):
    (git_repo / ".agent-lab").mkdir()
    (git_repo / ".agent-lab" / ".env").write_text(f"GITHUB_TOKEN={FINE}\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert st.active_project() is None and st.load_settings().github_token is None


def test_a_command_does_not_leave_its_project_behind(git_repo, monkeypatch):
    monkeypatch.setattr(cli.plan, "run_show_plan", lambda args: 0)
    assert cli.main(["show-plan", "r", "--repo-path", str(git_repo)]) == 0
    assert st.active_project() is None
