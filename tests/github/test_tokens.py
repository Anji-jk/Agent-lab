"""M5.1: the GitHub tokens may come from the project's .env; nothing else does."""

import orchestrator.cli.commands as cli
from orchestrator.github.tokens import ENV_FILE_VARIABLE, load_tokens, read_env_file

FINE = "github_pat_" + "F" * 40
CLASSIC = "ghp_" + "C" * 36


def env_file(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_both_tokens_are_read_and_nothing_else(tmp_path):
    path = env_file(tmp_path, "\n".join([
        "# my tokens",
        f"GITHUB_TOKEN={FINE}",
        f'export AGENT_LAB_PROJECT_TOKEN="{CLASSIC}"',
        "ANTHROPIC_API_KEY=sk-ant-should-not-be-taken",
        "OTHER=1",
    ]))
    assert read_env_file(path) == {"GITHUB_TOKEN": FINE, "AGENT_LAB_PROJECT_TOKEN": CLASSIC}


def test_quotes_spaces_and_comments(tmp_path):
    path = env_file(tmp_path, f"  GITHUB_TOKEN = '{FINE}'  \nAGENT_LAB_PROJECT_TOKEN={CLASSIC} # board\n")
    assert read_env_file(path) == {"GITHUB_TOKEN": FINE, "AGENT_LAB_PROJECT_TOKEN": CLASSIC}


def test_empty_values_and_broken_lines_are_skipped(tmp_path):
    path = env_file(tmp_path, "GITHUB_TOKEN=\nnot a line\n=x\nAGENT_LAB_PROJECT_TOKEN=\"\"\n")
    assert read_env_file(path) == {}


def test_a_missing_file_gives_nothing(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}


def test_a_set_variable_wins_over_the_file(tmp_path):
    env_file(tmp_path, f"GITHUB_TOKEN={FINE}\nAGENT_LAB_PROJECT_TOKEN={CLASSIC}\n")
    environ = {"GITHUB_TOKEN": "from-the-window"}
    assert load_tokens(tmp_path, environ) == ["AGENT_LAB_PROJECT_TOKEN"]
    assert environ == {"GITHUB_TOKEN": "from-the-window", "AGENT_LAB_PROJECT_TOKEN": CLASSIC}


def test_an_empty_variable_counts_as_not_set(tmp_path):
    env_file(tmp_path, f"GITHUB_TOKEN={FINE}\n")
    environ = {"GITHUB_TOKEN": "  "}
    assert load_tokens(tmp_path, environ) == ["GITHUB_TOKEN"] and environ["GITHUB_TOKEN"] == FINE


def test_another_file_can_be_named(tmp_path):
    other = tmp_path / "elsewhere.env"
    other.write_text(f"GITHUB_TOKEN={FINE}\n", encoding="utf-8")
    environ = {ENV_FILE_VARIABLE: str(other)}
    assert load_tokens(tmp_path / "project", environ) == ["GITHUB_TOKEN"]


def test_loaded_tokens_do_not_reach_an_agent(tmp_path):
    from agent_env import build_agent_env  # pyright: ignore[reportMissingImports]  (scripts/ is on the test path)
    env_file(tmp_path, f"GITHUB_TOKEN={FINE}\nAGENT_LAB_PROJECT_TOKEN={CLASSIC}\n")
    environ = {"PATH": "/bin"}
    load_tokens(tmp_path, environ)
    assert build_agent_env(environ) == {"PATH": "/bin"}


def test_every_command_loads_the_tokens_first(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "load_tokens", lambda root: seen.append(root))
    monkeypatch.setattr(cli, "run_report", lambda args: 0)
    assert cli.main(["report", "r"]) == 0
    assert seen == [cli.PROJECT_ROOT]


def test_the_tests_never_read_the_real_env_file(no_env_file):
    import os
    assert os.environ[ENV_FILE_VARIABLE] == str(no_env_file) and not no_env_file.exists()
