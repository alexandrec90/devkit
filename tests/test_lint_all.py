"""Tests for the workflow and `.env` passes in devkit's lint runner.

`.claude/hooks/session-start.sh` has always installed actionlint and dotenv-linter
into every session, and until these passes existed nothing ever ran them — a tool
downloaded on every startup, in every project, and never invoked. The checks below
exist to keep that from silently becoming true again, and to pin down the scoping
rule that makes `--changed` usable for non-Python files.

The runner is exercised as a subprocess against a throwaway repo rather than by
calling `main()`: `REPO_ROOT` is resolved from `__file__` at import time, so the only
honest way to test "what does it lint in a repo shaped like X" is to build an X.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

lint_all = load_script("scripts/lint-all.py")

MINIMAL_WORKFLOW = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hello
"""

# `runs-on` is required; omitting it is a schema error actionlint reports and a YAML
# parser happily accepts — so this is broken in exactly the way only actionlint sees.
BROKEN_WORKFLOW = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  build:
    steps:
      - run: echo hello
"""


def build_repo(root: Path, workflow: str = MINIMAL_WORKFLOW, env_example: bool = False) -> Path:
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
    (root / ".github" / "workflows").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "lint-all.py", root / "scripts" / "lint-all.py")
    # Copied too, so the fixture exercises the path a real project takes rather than the
    # ImportError fallback. `test_the_runner_still_starts_without_its_interpreter_helper`
    # is what covers the other branch, deliberately and on its own.
    shutil.copy2(
        REPO_ROOT / "scripts" / "project_python.py", root / "scripts" / "project_python.py"
    )
    (root / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
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


def test_workflow_files_finds_devkits_own_workflows():
    found = lint_all.workflow_files()
    assert "\n".join(found), "devkit has workflows but the selector returned none"
    assert all(f.startswith(".github/workflows/") for f in found), found


def test_workflow_files_narrows_to_the_changed_list():
    every = lint_all.workflow_files()
    assert lint_all.workflow_files([every[0]]) == [every[0]]
    # A changed path that is not a workflow must not drag one in.
    assert lint_all.workflow_files(["README.md"]) == []


def test_markdown_files_include_authored_instructions_but_not_generated_skills():
    found = lint_all.markdown_files()
    assert ".claude/rules/authoring.md" in found
    assert "README.md" in found
    assert not any(path.startswith(".agents/") for path in found)


def test_markdown_files_narrows_to_the_changed_list():
    assert lint_all.markdown_files(["README.md", "ok.py"]) == ["README.md"]


def test_markdown_linters_reject_real_findings(tmp_path):
    markdownlint = lint_all.node_tool("markdownlint-cli2")
    remark = lint_all.node_tool("remark")
    if markdownlint is None or remark is None:
        pytest.skip("npm install has not provisioned the Markdown linters")

    broken_structure = tmp_path / "structure.md"
    broken_structure.write_text("# First\n\n# Second\n", encoding="utf-8")
    section = lint_all.run_tool("markdownlint", [markdownlint, str(broken_structure)], "fix")
    assert "# markdownlint" in section and "MD025" in section

    broken_reference = tmp_path / "reference.md"
    broken_reference.write_text("# Reference\n\nSee [missing][target].\n", encoding="utf-8")
    section = lint_all.run_tool(
        "remark",
        [remark, "--frail", "--use", "remark-preset-lint-recommended", str(broken_reference)],
        "fix",
    )
    assert "# remark" in section and "no-undefined-references" in section


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
    """The dotenv pass must have the same teeth as the actionlint one."""
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


def test_changed_run_lints_a_workflow_edit_with_no_python_in_the_diff(tmp_path):
    """The Stop hook runs `--changed` every turn. Before this, editing only a
    workflow printed "no changed Python files; nothing to do" and exited 0, so an
    agent could break CI and have its own pre-stop gate wave it through."""
    root = build_repo(tmp_path / "repo")
    (root / ".github" / "workflows" / "ci.yml").write_text(
        MINIMAL_WORKFLOW.replace("echo hello", "echo goodbye"), encoding="utf-8"
    )
    result = run_lint(root, "--changed")
    assert "nothing to do" not in result.stdout, result.stdout
    assert "actionlint" in result.stdout, result.stdout


def test_changed_run_with_no_python_does_not_widen_to_the_whole_repo(tmp_path):
    """`scope` falls back to `["."]` when `targets` is empty.

    Left ungated, a one-file workflow edit would have quietly turned a per-turn check
    into a whole-repo ruff and mypy pass — slow, and reporting findings the turn did
    not cause.
    """
    root = build_repo(tmp_path / "repo")
    (root / ".github" / "workflows" / "ci.yml").write_text(
        MINIMAL_WORKFLOW.replace("echo hello", "echo goodbye"), encoding="utf-8"
    )
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
# The pass has teeth
# --------------------------------------------------------------------------


def test_a_broken_workflow_fails_the_run_and_lands_in_the_artifact(tmp_path):
    """The whole point: a finding must fail the run AND be fixable from the file.

    `.claude/rules/engineering.md` is explicit that an agent fixes lint from
    `logs/lint-errors.log`, never the terminal, so a pass that failed the run without
    writing its section would be worse than no pass at all.
    """
    if shutil.which("actionlint") is None:
        pytest.skip("actionlint not installed; the gate that installs it is CI's job")
    clean = run_lint(build_repo(tmp_path / "clean"))
    assert clean.returncode == 0, (
        f"the fixture is not lint-clean, so nothing below proves anything:\n{clean.stdout}"
    )

    root = build_repo(tmp_path / "repo", workflow=BROKEN_WORKFLOW)
    result = run_lint(root)
    assert result.returncode == 1, result.stdout
    artifact = (root / "logs" / "lint-errors.log").read_text(encoding="utf-8")
    assert "# actionlint" in artifact, artifact
    assert "ci.yml" in artifact, artifact


def test_a_missing_linter_is_a_note_and_never_an_artifact_entry(tmp_path):
    """A missing tool must not become a finding: nothing in the source tree fixes it.

    This is `run_tool`'s existing contract, asserted here for the two executables —
    the `_missing_module` probe covers only `-m` invocations, so these reach the
    FileNotFoundError branch instead and that path had no test.
    """
    absent = ["definitely-not-a-real-linter-9d2f", "--check"]
    assert lint_all.run_tool("actionlint", absent, "hint") == ""


# --------------------------------------------------------------------------
# A required linter that could not run is NOT a clean run
# --------------------------------------------------------------------------


def test_no_skipped_required_tool_means_no_complaint():
    lint_all._SKIPPED.clear()
    assert lint_all.not_clean_reason() == ""


def test_a_skipped_optional_tool_is_still_clean():
    """The node linters are genuinely optional — nothing in pyproject.toml declares
    them — so a machine without them has not failed to check anything it promised."""
    lint_all._SKIPPED.clear()
    lint_all._SKIPPED.append("markdownlint")
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


def test_markdown_sections_is_empty_when_there_is_no_markdown():
    """Extracted from `main` so it does not carry four branches for two optional node
    tools. The empty case is the one `main` used to guard with an `if`."""
    assert lint_all.markdown_sections([]) == ""
