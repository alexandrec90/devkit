"""Tests for the scoping and the `.env` pass in devkit's lint runner.

`lint-all.py` is one of the three commands the pre-push gate and the PR gate run, so
what it lints -- and what it silently does not -- decides whether a green push means
anything. The checks below pin the scoping rule that makes `--changed` usable for
non-Python files, and that a missing optional tool is a note while a missing required one
is not a clean run.

The workflow files are deliberately not this runner's: actionlint runs from
`.pre-commit-config.yaml`, where pre-commit builds the binary from a pinned rev, because
here it was a note on every machine that had not run the CI installer.

The runner is exercised as a subprocess against a throwaway repo rather than by
calling `main()`: `REPO_ROOT` is resolved from `__file__` at import time, so the only
honest way to test "what does it lint in a repo shaped like X" is to build an X.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

lint_all = load_script("scripts/lint-all.py")


def build_repo(root: Path, env_example: bool = False) -> Path:
    """A minimal repo with devkit's lint runner in it, committed and lint-CLEAN.

    Clean matters: these tests assert on the exit code, and a fixture where ruff or
    mypy already fail would make every "a finding fails the run" assertion pass for
    the wrong reason. Hence the pared-back `ruff.toml` (the runner's default scope is
    the whole tree, and devkit's own rule set flags its shebang as non-executable
    once the file is a copy) and the `tests/` tree that `MYPY_SCOPE` names.
    """
    (root / "scripts").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "tests").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "lint-all.py", root / "scripts" / "lint-all.py")
    # Copied too, so the fixture exercises the path a real project takes rather than the
    # ImportError fallback. `test_the_runner_still_starts_without_its_interpreter_helper`
    # is what covers the other branch, deliberately and on its own.
    shutil.copy2(
        REPO_ROOT / "scripts" / "project_python.py", root / "scripts" / "project_python.py"
    )
    (root / "ruff.toml").write_text('[lint]\nselect = ["E4", "E7", "E9", "F"]\n', encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
    if env_example:
        (root / ".env.example").write_text("PORT=8000\n", encoding="utf-8")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)
    return root


def run_lint(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/lint-all.py", *args],
        cwd=root,
        capture_output=True,
        text=True,
    )


def test_the_runner_still_starts_without_its_interpreter_helper(tmp_path):
    """`project_python.py` is an optimisation, not a dependency.

    This script is copied into generated projects, and one that arrives without its
    helper must still lint. A hard import turns "the interpreter could not be upgraded"
    into "the linter will not start", which is worse than the behaviour it improves on —
    and it is a `ModuleNotFoundError` before `main()`, so nothing writes an artifact and
    the agent gets a traceback where a lint report should be.
    """
    root = build_repo(tmp_path / "repo")
    (root / "scripts" / "project_python.py").unlink()
    result = run_lint(root)
    # The claim is that it RUNS, not that it passes: mypy legitimately objects to the
    # import it can no longer resolve, and asserting exit 0 here would be asserting
    # something this test does not care about.
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert "ruff: ok" in result.stdout, result.stdout + result.stderr


# --------------------------------------------------------------------------
# Scope selection
# --------------------------------------------------------------------------


def test_python_targets_keeps_lintable_python_and_drops_template_content():
    """`templates/` is content, not source: its `.py` files are linted by the config that
    ships beside them into a generated project, not by devkit's."""
    paths = ["scripts/lint-all.py", "README.md", "templates/core/scripts/notify.py"]
    assert lint_all.python_targets(paths) == ["scripts/lint-all.py"]


def test_explicit_paths_drops_what_no_longer_exists_and_normalises_separators():
    """A deleted path is nothing to lint, and ruff/mypy treat a missing argument as a
    usage error that fails the whole run."""
    assert lint_all.explicit_paths(["scripts\\lint-all.py", "gone/away.py"]) == [
        "scripts/lint-all.py"
    ]


def test_changed_paths_is_the_diff_plus_untracked_files_that_still_exist(monkeypatch, tmp_path):
    for name in ("a.py", "b.md", "new.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    listed = {"diff": ["b.md", "a.py", "deleted.py"], "ls-files": ["new.py", "a.py"]}
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_all, "_git", lambda *args: listed[args[0]])
    assert lint_all.changed_paths() == ["a.py", "b.md", "new.py"]


def refused_git_env(tmp_path: Path) -> dict[str, str]:
    """An environment where git refuses every repository as another owner's.

    `GIT_TEST_ASSUME_DIFFERENT_OWNER` is git's own switch for the refusal a tree made by
    an elevated process gets; an empty global config keeps a machine-wide
    `safe.directory` from waving it through.
    """
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def test_a_git_that_refuses_the_tree_fails_a_changed_run_instead_of_linting_nothing(tmp_path):
    """9feac8aa: `_git` returned `[]` on any non-zero exit, so a dubious-ownership refusal
    read as a clean tree and `--changed` printed "nothing to do" over a modified file."""
    root = build_repo(tmp_path / "repo")
    (root / "ok.py").write_text("x = 2\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "scripts/lint-all.py", "--changed"],
        cwd=root,
        capture_output=True,
        text=True,
        env=refused_git_env(tmp_path),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "nothing to do" not in result.stdout
    assert "dubious ownership" in result.stdout
    artifact = (root / "logs" / "lint-errors.log").read_text(encoding="utf-8")
    assert artifact.startswith("# source: scripts/lint-all.py\n# git\n")
    assert "dubious ownership" in artifact


def test_git_raises_when_it_cannot_answer_and_lists_lines_when_it_can(monkeypatch):
    def fake(returncode: int, stdout: str = "", stderr: str = ""):
        done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
        return lambda *a, **k: done

    monkeypatch.setattr(lint_all.subprocess, "run", fake(0, "a.py\nb.py\n"))
    assert lint_all._git("diff") == ["a.py", "b.py"]
    monkeypatch.setattr(lint_all.subprocess, "run", fake(128, stderr="fatal:\n  refused\n"))
    with pytest.raises(lint_all.GitFailed, match="`git diff` failed: fatal: refused"):
        lint_all._git("diff")
    monkeypatch.setattr(lint_all.subprocess, "run", fake(1))
    with pytest.raises(lint_all.GitFailed, match="failed: exit 1"):
        lint_all._git("diff")

    def absent(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(lint_all.subprocess, "run", absent)
    with pytest.raises(lint_all.GitFailed, match="could not start"):
        lint_all._git("diff")


def test_scope_unknown_fails_and_puts_gits_answer_in_the_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(lint_all, "ARTIFACT", tmp_path / "logs" / "lint-errors.log")
    assert lint_all.scope_unknown(lint_all.GitFailed("`git diff` failed: fatal: no")) == 1
    text = (tmp_path / "logs" / "lint-errors.log").read_text(encoding="utf-8")
    assert "# git\n" in text and "`git diff` failed: fatal: no" in text


def test_python_sections_lints_the_targets_or_else_the_whole_repo(monkeypatch):
    fixed: list[list[str]] = []

    def run(cmd, **kw):
        fixed.append(cmd[3:])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(lint_all.subprocess, "run", run)
    monkeypatch.setattr(lint_all, "run_tool", lambda name, cmd, hint: f"{name} {cmd[3:]};")
    assert lint_all.python_sections(["a.py"]) == (
        "ruff ['check', 'a.py', '--output-format=full'];mypy ['a.py', '--show-error-codes'];"
    )
    assert fixed == [
        ["check", "a.py", "--fix", "--unsafe-fixes", "--show-fixes"],
        ["format", "a.py"],
    ]
    assert lint_all.python_sections([]) == (
        "ruff ['check', '.', '--output-format=full'];"
        f"mypy {[*lint_all.MYPY_SCOPE, '--show-error-codes']};"
    )


# ruff's own spelling of "times" in its `--show-fixes` block.
TIMES = "\N{MULTIPLICATION SIGN}"


def test_applied_fixes_reads_ruffs_show_fixes_block():
    output = (
        "F821 Undefined name `g`\n --> a.py:3:5\n\n"
        f"Fixed 3 errors:\n- a.py:\n    1 {TIMES} F841 (unused-variable)\n"
        f"    1 {TIMES} F401 (unused-import)\n"
        f"- tests/b.py:\n    1 {TIMES} F541 (f-string-missing-placeholders)\n\n"
        "Found 4 errors (3 fixed, 1 remaining).\n"
    )
    assert lint_all.applied_fixes(output) == [
        f"a.py -- 1 {TIMES} F841 (unused-variable), 1 {TIMES} F401 (unused-import)",
        f"tests/b.py -- 1 {TIMES} F541 (f-string-missing-placeholders)",
    ]
    assert lint_all.applied_fixes("All checks passed!\n") == []


def test_a_fix_that_rewrote_a_file_is_said_not_passed_over_as_ok(tmp_path):
    """802b5602: an unsafe F841 fix deleted an assignment from a test mid-edit, and the
    run printed only "ruff: ok". The rewrite is named on the terminal, by file and rule."""
    root = build_repo(tmp_path / "repo")
    (root / "mid_edit.py").write_text(
        "def f() -> int:\n    open_now = len([])\n    return 1\n", encoding="utf-8"
    )
    result = run_lint(root, "--changed", "--no-secrets")
    said = f"ruff --fix changed mid_edit.py -- 1 {TIMES} F841 (unused-variable)"
    assert said in result.stdout, result.stdout + result.stderr


def test_an_explicit_path_list_needs_no_git(tmp_path):
    """`--paths` is the way round a git that refuses the tree, so it must not ask git."""
    root = build_repo(tmp_path / "repo")
    result = subprocess.run(
        [sys.executable, "scripts/lint-all.py", "--paths", "ok.py"],
        cwd=root,
        capture_output=True,
        text=True,
        env=refused_git_env(tmp_path),
    )
    assert "ruff: ok" in result.stdout, result.stdout + result.stderr


def test_changed_python_files_is_the_python_subset_of_the_working_tree_diff(monkeypatch):
    monkeypatch.setattr(lint_all, "changed_paths", lambda: ["ok.py", "README.md"])
    assert lint_all.changed_python_files() == ["ok.py"]


def test_env_files_is_empty_in_devkit_and_live_in_a_project_shaped_repo(tmp_path):
    """devkit has no `.env*` at all; a generated project always ships `.env.example`.

    Reading the filesystem rather than hardcoding a list is what lets one vendored
    runner be inert here and live there without an `if project ==` branch.

    Asserting the pass *passes*, not merely that it ran: the first version of this
    test only checked that "dotenv-linter" appeared in the output, which it did — as
    "dotenv-linter: FAILED", because v4 needs a `check` subcommand and a bare file
    list is a usage error. Only the generated-project arrival test caught it.
    """
    assert lint_all.env_files() == []
    if shutil.which("dotenv-linter") is None:
        pytest.skip("dotenv-linter not installed; the gate that installs it is CI's job")
    root = build_repo(tmp_path / "repo", env_example=True)
    result = run_lint(root)
    assert "dotenv-linter: ok" in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout


def test_a_malformed_env_file_fails_the_run_and_lands_in_the_artifact(tmp_path):
    """The whole point of a pass: a finding must fail the run AND be fixable from the
    file. `.claude/rules/engineering.md` is explicit that an agent fixes lint from
    `logs/lint-errors.log`, never the terminal."""
    if shutil.which("dotenv-linter") is None:
        pytest.skip("dotenv-linter not installed; the gate that installs it is CI's job")
    root = build_repo(tmp_path / "repo", env_example=True)
    # A leading space in the key is invalid, and nothing else in the toolchain reads
    # this file — ruff and mypy never see it, so without this pass it ships broken.
    (root / ".env.example").write_text(" PORT=8000\n", encoding="utf-8")
    result = run_lint(root)
    assert result.returncode == 1, result.stdout
    artifact = (root / "logs" / "lint-errors.log").read_text(encoding="utf-8")
    assert "# dotenv-linter" in artifact, artifact
    assert ".env.example" in artifact, artifact


# --------------------------------------------------------------------------
# `--changed` must reach non-Python files without widening the Python passes
# --------------------------------------------------------------------------


def test_changed_run_lints_an_env_edit_with_no_python_in_the_diff(tmp_path):
    """Before the non-Python passes existed, editing only a non-Python file printed
    "no changed Python files; nothing to do" and exited 0, so an agent could break the
    file and have its own gate wave it through. The pass has to at least be reached:
    "ok" with the tool installed, a skip note without it, never "nothing to do"."""
    root = build_repo(tmp_path / "repo", env_example=True)
    (root / ".env.example").write_text("PORT=8001\n", encoding="utf-8")
    result = run_lint(root, "--changed")
    assert "nothing to do" not in result.stdout, result.stdout
    assert "dotenv-linter" in result.stdout, result.stdout


def test_changed_run_with_no_python_does_not_widen_to_the_whole_repo(tmp_path):
    """`scope` falls back to `["."]` when `targets` is empty.

    Left ungated, a one-file `.env` edit would have quietly turned a per-turn check
    into a whole-repo ruff and mypy pass — slow, and reporting findings the turn did
    not cause.
    """
    root = build_repo(tmp_path / "repo", env_example=True)
    (root / ".env.example").write_text("PORT=8001\n", encoding="utf-8")
    result = run_lint(root, "--changed")
    assert "ruff:" not in result.stdout, f"ruff ran on a diff with no Python:\n{result.stdout}"
    assert "mypy:" not in result.stdout, f"mypy ran on a diff with no Python:\n{result.stdout}"


def test_changed_run_still_lints_python_when_python_changed(tmp_path):
    root = build_repo(tmp_path / "repo")
    (root / "ok.py").write_text("y = 2\n", encoding="utf-8")
    result = run_lint(root, "--changed")
    assert "ruff:" in result.stdout, result.stdout


def test_a_clean_changed_run_still_reports_nothing_to_do(tmp_path):
    """The early return must survive: an empty diff is the common case per turn."""
    root = build_repo(tmp_path / "repo")
    result = run_lint(root, "--changed")
    assert result.returncode == 0
    assert "nothing to do" in result.stdout, result.stdout


# --------------------------------------------------------------------------
# A missing tool: a note for an optional one, not a clean run for a required one
# --------------------------------------------------------------------------


def test_a_missing_linter_is_a_note_and_never_an_artifact_entry():
    """A missing tool must not become a finding: nothing in the source tree fixes it.

    This is `run_tool`'s existing contract, asserted here for a bare executable — the
    `_missing_module` probe covers only `-m` invocations, so this reaches the
    FileNotFoundError branch instead.
    """
    absent = ["definitely-not-a-real-linter-9d2f", "--check"]
    assert lint_all.run_tool("dotenv-linter", absent, "hint") == ""


COLOURED_FAILURE = [
    sys.executable,
    "-c",
    "import sys; print('\\x1b[1m\\x1b[91mB023\\x1b[0m a.py:1:1'); sys.exit(1)",
]


def test_a_finding_reaches_the_artifact_without_colour_codes():
    """Claude Code runs every tool under `FORCE_COLOR=3`, which ruff and mypy obey even
    into a pipe, so `logs/lint-errors.log` -- a file agents grep -- carried escape codes
    around every rule and path."""
    section = lint_all.run_tool("ruff", COLOURED_FAILURE, "hint")
    assert "\x1b" not in section
    assert "B023 a.py:1:1" in section


def test_a_hung_linter_is_killed_at_the_bound_and_reported():
    """A project's runner once ran pip-audit for 18 minutes with no bound. A tool that
    never answers is a failure with a section, not a skip, and never a wait."""
    hung = [sys.executable, "-c", "import time; time.sleep(120)"]
    began = time.monotonic()
    section = lint_all.run_tool("ruff", hung, "hint", timeout=2)
    assert time.monotonic() - began < 60
    assert section.startswith("# ruff\n")
    assert "killed after 2s" in section
    assert "ruff" not in lint_all._SKIPPED


def test_no_skipped_required_tool_means_no_complaint():
    lint_all._SKIPPED.clear()
    assert lint_all.not_clean_reason() == ""


def test_a_skipped_optional_tool_is_still_clean():
    """dotenv-linter is genuinely optional — nothing in pyproject.toml declares it — so
    a machine without it has not failed to check anything it promised."""
    lint_all._SKIPPED.clear()
    lint_all._SKIPPED.append("dotenv-linter")
    assert lint_all.not_clean_reason() == ""


def test_a_skipped_required_tool_names_itself_and_the_way_out():
    """The bug this whole seam exists for: `run_tool` returns "" both for "passed" and
    for "skipped", so a run where every linter was absent produced no sections and
    printed the word `clean` — a false negative on every rule at once."""
    lint_all._SKIPPED.clear()
    lint_all._SKIPPED.extend(["ruff", "mypy"])
    reason = lint_all.not_clean_reason()
    assert "NOT CLEAN" in reason
    assert "ruff, mypy" in reason
    assert "pyproject.toml" in reason
    lint_all._SKIPPED.clear()


def test_the_runner_lints_no_workflow_and_names_who_does():
    """The workflow pass moved to pre-commit's actionlint hook. A second copy here would
    be the same inert note it was; the runner's own source says where it went, so an
    agent reading `lint-all.py` for the workflow linter is sent to the right file."""
    source = (REPO_ROOT / "scripts" / "lint-all.py").read_text(encoding="utf-8")
    assert "workflow_files" not in source
    assert ".pre-commit-config.yaml" in source


@pytest.mark.parametrize("config", ["pyproject.toml", "templates/core/pyproject.toml.tmpl"])
def test_mypys_unchecked_body_note_is_off_so_the_artifact_opens_on_the_error(config):
    """fc2ecb0e: a mypy-only red's `logs/lint-errors.log` opened with 162
    `annotation-unchecked` notes, 28 KB, before the one error a fixer had to read."""
    text = (REPO_ROOT / config).read_text(encoding="utf-8")
    mypy = text.split("[tool.mypy]", 1)[1].split("\n[", 1)[0]
    assert 'disable_error_code = ["annotation-unchecked"]' in mypy


# --------------------------------------------------------------------------
# A narrowed run scans for secrets the way the commit will
# --------------------------------------------------------------------------

# A stand-in for the real hook under its real id: `pygrep` needs no network and no hook
# environment, so the fixture exercises pre-commit's own `run --files` path offline.
STAND_IN_SECRETS_CONFIG = (
    "repos:\n  - repo: local\n    hooks:\n      - id: detect-secrets\n"
    "        name: detect secrets\n        language: pygrep\n        entry: 'FAKE_KEY_[0-9]+'\n"
)


def secrets_repo(root: Path) -> Path:
    """`build_repo` plus the stand-in hook, committed past the machine's own hooks.

    `core.hooksPath` is global where `install-git-policy.py` ran, and its policy refuses a
    commit carrying a `.pre-commit-config.yaml` from a tree with no pre-commit of its own.
    """
    build_repo(root)
    (root / ".pre-commit-config.yaml").write_text(STAND_IN_SECRETS_CONFIG, encoding="utf-8")
    for cmd in (
        ["git", "config", "core.hooksPath", str(root / ".nohooks")],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "hook"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)
    return root


def run_secrets_lint(root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PRE_COMMIT_HOME": str(tmp_path / "pre-commit-home")}
    env.pop("SKIP", None)
    return subprocess.run(
        [sys.executable, "scripts/lint-all.py", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
    )


def test_secrets_targets_are_the_selection_only_where_the_hook_is_declared(monkeypatch, tmp_path):
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    assert lint_all.secrets_targets(["a.md"], skip=False) == [], "no pre-commit config"
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text("repos:\n  - repo: local\n    hooks:\n      - id: ruff\n", encoding="utf-8")
    assert lint_all.secrets_targets(["a.md"], skip=False) == [], "config without the hook"
    config.write_text(STAND_IN_SECRETS_CONFIG, encoding="utf-8")
    assert lint_all.secrets_targets(["a.md", "b.py"], skip=False) == ["a.md", "b.py"]
    assert lint_all.secrets_targets(["a.md"], skip=True) == [], "--no-secrets"
    assert lint_all.secrets_targets([], skip=False) == []


def test_devkits_own_config_declares_the_hook_the_pass_mirrors():
    """The pass is inert without the hook id, so dropping it from the config would turn
    the pass off with nothing red anywhere."""
    assert lint_all.secrets_targets(["README.md"], skip=False) == ["README.md"]


def test_secrets_section_runs_the_commit_hook_over_exactly_the_paths(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(lint_all, "run_tool", lambda name, cmd, hint: calls.append(cmd) or "")
    assert lint_all.secrets_section([]) == ""
    assert calls == []
    lint_all.secrets_section(["a.md", "b.py"])
    assert calls == [
        [sys.executable, "-m", "pre_commit", "run", "detect-secrets", "--files", "a.md", "b.py"]
    ]


def test_a_secret_in_a_non_python_file_fails_a_paths_run(tmp_path):
    """08654413: a fixer shipped a hard-coded key with lint, tests and both ratchets clean,
    and the commit's detect-secrets hook refused it a whole dispatch later. A non-Python
    file matters most: before this pass, `--paths notes.md` was "nothing to do"."""
    root = secrets_repo(tmp_path / "repo")
    (root / "notes.md").write_text("key = FAKE_KEY_123\n", encoding="utf-8")
    result = run_secrets_lint(root, tmp_path, "--paths", "notes.md")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "detect-secrets: FAILED" in result.stdout, result.stdout
    artifact = (root / "logs" / "lint-errors.log").read_text(encoding="utf-8")
    assert "# detect-secrets\n" in artifact and "notes.md" in artifact, artifact


def test_report_fails_on_a_section_or_a_skipped_required_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(lint_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_all, "ARTIFACT", tmp_path / "logs" / "lint-errors.log")
    lint_all._SKIPPED.clear()
    assert lint_all.report("") == 0
    assert lint_all.ARTIFACT.read_text(encoding="utf-8") == ""
    assert lint_all.report("# ruff\nbad\n") == 1
    assert "# ruff\nbad" in lint_all.ARTIFACT.read_text(encoding="utf-8")
    lint_all._SKIPPED.append("detect-secrets")
    assert lint_all.report("") == 1, "a required pass that could not run is not clean"
    lint_all._SKIPPED.clear()


def test_a_clean_file_passes_the_secrets_pass(tmp_path):
    root = secrets_repo(tmp_path / "repo")
    (root / "notes.md").write_text("nothing to see\n", encoding="utf-8")
    result = run_secrets_lint(root, tmp_path, "--paths", "notes.md")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "detect-secrets: ok" in result.stdout, result.stdout


def test_no_secrets_keeps_the_stop_hooks_turn_free_of_the_pass(tmp_path):
    root = secrets_repo(tmp_path / "repo")
    (root / "notes.md").write_text("key = FAKE_KEY_123\n", encoding="utf-8")
    result = run_secrets_lint(root, tmp_path, "--paths", "notes.md", "--no-secrets")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to do" in result.stdout, result.stdout
