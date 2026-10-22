import os
import subprocess
import sys

import pytest

from orchestrator.execution import checks as tc


def git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("x = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_a():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_clean_repo_has_no_changes(repo):
    assert tc.changed_paths(repo) == []
    assert tc.snapshot(repo) == {}


def test_changes_new_and_deleted_files_are_seen(repo):
    (repo / "app.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 1\n")
    (repo / "tests" / "test_app.py").unlink()
    assert tc.changed_paths(repo) == ["app.py", "new.py", "tests/test_app.py"]
    snap = tc.snapshot(repo)
    assert snap["tests/test_app.py"] == tc.DELETED
    assert snap["app.py"] != snap["new.py"]


def test_noise_is_ignored(repo):
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_bytes(b"x")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "v").write_text("x")
    assert tc.changed_paths(repo) == []


def test_filenames_with_spaces_are_kept_whole(repo):
    (repo / "my file.py").write_text("z = 1\n")
    assert tc.changed_paths(repo) == ["my file.py"]


def test_changed_between_detects_edit_of_already_changed_file(repo):
    (repo / "app.py").write_text("x = 2\n")
    before = tc.snapshot(repo)
    (repo / "app.py").write_text("x = 3\n")
    after = tc.snapshot(repo)
    assert tc.changed_between(before, after) == ["app.py"]


def test_changed_between_detects_revert_to_head(repo):
    (repo / "app.py").write_text("x = 2\n")
    before = tc.snapshot(repo)
    (repo / "app.py").write_text("x = 1\n")
    assert tc.changed_between(before, tc.snapshot(repo)) == ["app.py"]


def test_changed_between_ignores_untouched_files(repo):
    (repo / "app.py").write_text("x = 2\n")
    snap = tc.snapshot(repo)
    assert tc.changed_between(snap, dict(snap)) == []


def test_under_matches_folders_not_name_prefixes():
    assert tc.under("tests/a.py", ("tests/",))
    assert tc.under("tests/a.py", ("tests",))
    assert not tc.under("tests_extra/a.py", ("tests/",))
    assert tc.under("anything", ("",))
    assert tc.under("src\\a.py", ("src/",))
    assert not tc.under("a.py", ())


def test_scope_violations(repo):
    paths = ["src/a.py", "src/b.py", "tests/test_a.py", "README.md"]
    assert tc.scope_violations(paths, ["src/a.py"], ("tests/",)) == ["src/b.py", "README.md"]
    assert tc.scope_violations(paths, [], ("tests/",)) == []


def test_removed_test_lines_flags_edit_and_delete_not_addition(repo):
    (repo / "tests" / "test_app.py").write_text("def test_a():\n    assert False\n")
    (repo / "tests" / "test_new.py").write_text("def test_n():\n    assert True\n")
    paths = tc.changed_paths(repo)
    assert tc.removed_test_lines(repo, paths, ("tests/",)) == ["tests/test_app.py"]


def test_removed_test_lines_pure_addition_is_fine(repo):
    with open(repo / "tests" / "test_app.py", "a") as handle:
        handle.write("def test_b():\n    assert True\n")
    assert tc.removed_test_lines(repo, tc.changed_paths(repo), ("tests/",)) == []


def test_editing_an_import_or_helper_in_a_test_file_is_not_weakening(repo):
    (repo / "tests" / "test_app.py").write_text(
        "import os\n\n\ndef test_a():\n    assert True\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "imports")
    # The import line changes and a test is added; no test or assert is removed.
    (repo / "tests" / "test_app.py").write_text(
        "import sys\n\n\ndef test_a():\n    assert True\n\n\ndef test_b():\n    assert 1\n"
    )
    assert tc.removed_test_lines(repo, tc.changed_paths(repo), ("tests/",)) == []


def test_deleting_or_renaming_a_test_is_weakening(repo):
    (repo / "tests" / "test_app.py").write_text("def test_renamed():\n    assert True\n")
    assert tc.removed_test_lines(repo, ["tests/test_app.py"], ("tests/",)) == ["tests/test_app.py"]
    (repo / "tests" / "test_app.py").unlink()
    assert tc.removed_test_lines(repo, ["tests/test_app.py"], ("tests/",)) == ["tests/test_app.py"]


def test_new_untracked_test_files_are_never_weakening(repo):
    (repo / "tests" / "test_new.py").write_text("def test_n():\n    assert True\n")
    assert tc.removed_test_lines(repo, ["tests/test_new.py"], ("tests/",)) == []


def test_diff_and_commit(repo):
    (repo / "app.py").write_text("x = 2\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_bytes(b"x")
    patch = tc.diff_text(repo)
    assert "+x = 2" in patch and "pyc" not in patch
    before = tc.head_commit(repo)
    sha = tc.commit_all(repo, "task T1")
    assert sha != before and tc.head_commit(repo) == sha
    assert tc.changed_paths(repo) == []
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an|%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert log == "Agent Lab Orchestrator|task T1"


def test_staging_works_with_ignored_cache_folders_present(repo):
    # The live M2.8 run failed here: an exclude pattern named a folder that
    # .gitignore ignores, and git add refused.
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.py[cod]\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore caches")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "v").write_text("x")
    (repo / "tests" / "__pycache__").mkdir()
    (repo / "tests" / "__pycache__" / "t.pyc").write_bytes(b"x")
    (repo / "app.py").write_text("x = 9\n")
    assert "+x = 9" in tc.diff_text(repo)
    tc.commit_all(repo, "with caches around")
    assert tc.changed_paths(repo) == []


def test_staging_takes_odd_names_literally_and_records_deletions(repo):
    (repo / "a[1].py").write_text("odd = 1\n")
    (repo / "tests" / "test_app.py").unlink()
    tc.commit_all(repo, "odd name and a deletion")
    assert tc.changed_paths(repo) == []
    listed = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True,
                            text=True, check=True).stdout.split()
    assert "a[1].py" in listed and "tests/test_app.py" not in listed


def test_git_errors_report_the_message_not_the_hint(tmp_path):
    with pytest.raises(tc.CheckError) as error:
        tc.head_commit(tmp_path)
    assert "hint:" not in str(error.value) and "rev-parse" in str(error.value)


def test_commit_with_nothing_staged_fails(repo):
    with pytest.raises(tc.CheckError):
        tc.commit_all(repo, "empty")


def test_diff_is_truncated(repo):
    (repo / "big.py").write_text("\n".join(f"v{i} = {i}" for i in range(500)))
    patch = tc.diff_text(repo, limit=300)
    assert patch.endswith("... (diff truncated)\n") and len(patch) < 400


def test_commit_hook_does_not_run(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repo / "app.py").write_text("x = 5\n")
    assert tc.commit_all(repo, "hooked")


def test_base_commit_selection(repo):
    first = tc.head_commit(repo)
    (repo / "a.txt").write_text("a")
    second = tc.commit_all(repo, "second")
    git(repo, "checkout", "-q", "-b", "other", first)
    (repo / "b.txt").write_text("b")
    other = tc.commit_all(repo, "other")
    assert tc.pick_base_commit(repo, [first, second]) == second
    assert tc.pick_base_commit(repo, [second]) == second
    assert tc.pick_base_commit(repo, [second, other]) is None
    assert tc.pick_base_commit(repo, []) is None


def test_commit_exists(repo):
    assert tc.commit_exists(repo, tc.head_commit(repo))
    assert not tc.commit_exists(repo, "0" * 40)


def test_current_branch(repo):
    assert tc.current_branch(repo) == "main"
    git(repo, "checkout", "-q", "--detach")
    assert tc.current_branch(repo) is None


def test_not_a_repo_raises(tmp_path):
    with pytest.raises(tc.CheckError):
        tc.head_commit(tmp_path)


def test_valid_test_id():
    dirs = ("tests/",)
    assert tc.valid_test_id("tests/test_a.py", dirs)
    assert tc.valid_test_id("tests/test_a.py::test_x", dirs)
    assert tc.valid_test_id("tests/sub/test_a.py::test_x[1-2]", dirs)
    for bad in ("-k foo", "--co", "/etc/x.py", "../x.py", "tests/../x.py",
                "src/test_a.py", "tests/test_a.txt", "tests/a.py; rm -rf", "", None, 5):
        assert not tc.valid_test_id(bad, dirs), bad


def test_run_command_pass_fail_timeout_and_missing(tmp_path):
    ok = tc.run_command([sys.executable, "-c", "print('hi')"], tmp_path, timeout=30)
    assert ok.passed and "hi" in ok.output_tail
    bad = tc.run_command([sys.executable, "-c", "import sys; sys.exit(3)"], tmp_path, timeout=30)
    assert not bad.passed and bad.exit_code == 3
    slow = tc.run_command([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=1)
    assert slow.timed_out and not slow.passed
    missing = tc.run_command(["no-such-program-xyz"], tmp_path, timeout=5)
    assert not missing.passed and missing.exit_code is None


def test_output_is_cut_to_the_tail(tmp_path):
    result = tc.run_command(
        [sys.executable, "-c", "print('a' * 10000 + 'END')"], tmp_path, timeout=30
    )
    assert len(result.output_tail) == tc.OUTPUT_TAIL_CHARS
    assert result.output_tail.rstrip().endswith("END")


def test_run_suite_and_named_tests(repo):
    suite = tc.run_suite(repo, timeout=120)
    assert suite.passed
    (repo / "tests" / "test_bad.py").write_text("def test_b():\n    assert False\n")
    assert not tc.run_suite(repo, timeout=120).passed
    assert tc.run_named_tests(repo, ["tests/test_app.py"], timeout=120).passed
    assert not tc.run_named_tests(repo, ["tests/test_bad.py::test_b"], timeout=120).passed


# --- which Python runs the tests (M3.8: a run started from system Python had no pytest) ---


def test_project_python_prefers_the_projects_venv(tmp_path):
    venv = tmp_path / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    venv.mkdir(parents=True)
    python = venv / ("python.exe" if sys.platform == "win32" else "python")
    python.write_text("")
    assert tc.project_python(tmp_path, environ={}) == str(python)


def test_project_python_falls_back_to_this_python_without_a_venv(tmp_path):
    assert tc.project_python(tmp_path, environ={}) == sys.executable


def test_the_python_variable_wins(tmp_path):
    chosen = str(tmp_path / "elsewhere" / "python.exe")
    assert tc.project_python(tmp_path, environ={tc.PYTHON_VARIABLE: chosen}) == chosen


def test_with_python_on_path_puts_its_folder_first_and_keeps_the_rest(tmp_path):
    python = str(tmp_path / "venv" / "python.exe")
    env = tc.with_python_on_path({"Path": "a" + os.pathsep + "b", "X": "1"}, python)
    assert env["Path"].split(os.pathsep) == [str(tmp_path / "venv"), "a", "b"]
    assert env["X"] == "1" and "PATH" not in env  # the existing key is reused, not duplicated
    assert tc.with_python_on_path({}, python)["PATH"] == str(tmp_path / "venv")


def test_pytest_problem_is_none_for_a_python_with_pytest():
    assert tc.pytest_problem(sys.executable) is None


def test_pytest_problem_explains_a_python_without_it(monkeypatch):
    def no_pytest(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="",
                                           stderr="C:\\Python314\\python.exe: No module named pytest\n")

    monkeypatch.setattr(tc.subprocess, "run", no_pytest)
    assert tc.pytest_problem("C:\\Python314\\python.exe") == "C:\\Python314\\python.exe: No module named pytest"


def test_pytest_problem_explains_a_python_that_does_not_exist(tmp_path):
    problem = tc.pytest_problem(str(tmp_path / "missing-python.exe"))
    assert problem and "could not be started" in problem
