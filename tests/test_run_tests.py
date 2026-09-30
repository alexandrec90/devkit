"""Tests for `scripts/run-tests.py` — the runner behind the failure artifact.

The contract an agent depends on is not "pytest ran". It is that `logs/test-failures.log`
says exactly what is broken right now: cleared on a pass, so a stale artifact never
sends the next session chasing a failure that is already fixed, and never empty on a
failure, so "the run went red and the artifact says nothing" cannot happen.

`main()` is exercised with `subprocess.run` stubbed rather than by really running
pytest: the point of interest is entirely in what it does with a return code and a
blob of output, and a runner that runs the suite to test the runner recurses.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

run_tests = load_script("scripts/run-tests.py")
fix_plan = load_script("scripts/fix_plan.py")


@pytest.fixture
def artifact(tmp_path, monkeypatch) -> Path:
    """Redirect both the artifact and the root it is reported relative to.

    Both, because `main()` prints `ARTIFACT.relative_to(REPO_ROOT)` — moving only the
    artifact makes that raise a ValueError that has nothing to do with the test.
    """
    path = tmp_path / "logs" / "test-failures.log"
    monkeypatch.setattr(run_tests, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_tests, "ARTIFACT", path)
    # A bare temp root has no git to ask, which is the "run the suite" case; the tests
    # for the targeted default replace this with a changed set of their own.
    monkeypatch.setattr(run_tests, "changed_paths", lambda root, run=None: None)
    for name in run_tests.FULL_SUITE_ENV:
        monkeypatch.delenv(name, raising=False)
    return path


def stub_pytest(monkeypatch, returncode: int, stdout: str = "", stderr: str = ""):
    """Replace the pytest subprocess, recording the argv it would have run."""
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(run_tests.subprocess, "run", fake_run)
    return seen


FAILING_OUTPUT = """\
collected 3 items

tests/test_a.py .                                                        [ 33%]
tests/test_b.py F                                                        [ 66%]

=================================== FAILURES ===================================
_________________________________ test_b_thing _________________________________
/usr/lib/python3.12/site-packages/pluggy/_hooks.py:1 in call
    raise
tests/test_b.py:4: in test_b_thing
    assert 1 == 2
E   assert 1 == 2
=========================== short test summary info ============================
FAILED tests/test_b.py::test_b_thing - assert 1 == 2
"""


# --- filter_output ------------------------------------------------------------


def test_filter_output_drops_everything_before_the_failures_section():
    kept = run_tests.filter_output(FAILING_OUTPUT)
    assert "collected 3 items" not in kept
    assert "[ 33%]" not in kept
    assert "assert 1 == 2" in kept


def test_filter_output_drops_library_frames():
    """An agent cannot fix a frame inside site-packages, and a long third-party
    traceback hides the one first-party frame that can be fixed."""
    kept = run_tests.filter_output(FAILING_OUTPUT)
    assert "site-packages" not in kept
    assert "tests/test_b.py:4" in kept


def test_filter_output_keeps_the_short_summary_even_with_no_failures_banner():
    """`-q` runs that error during collection print a summary and no FAILURES header."""
    raw = "=========================== short test summary info ===\nERROR tests/test_x.py\n"
    assert "ERROR tests/test_x.py" in run_tests.filter_output(raw)


def test_filter_output_returns_nothing_for_a_clean_run():
    assert run_tests.filter_output("3 passed in 0.4s\n") == ""


# --- cap_failure_blocks -------------------------------------------------------


def test_cap_failure_blocks_leaves_a_short_block_alone():
    text = "_____ test_a _____\nline\nline"
    assert run_tests.cap_failure_blocks(text, limit=10) == text


def test_cap_failure_blocks_truncates_and_says_it_did():
    text = "_____ test_a _____\n" + "\n".join(f"line{i}" for i in range(20))
    capped = run_tests.cap_failure_blocks(text, limit=5)
    assert capped.splitlines()[:5] == ["_____ test_a _____", "line0", "line1", "line2", "line3"]
    assert "21 lines total, truncated" in capped


def test_cap_failure_blocks_caps_each_block_independently():
    """One deep failure must not eat the budget of the other twenty."""
    text = "_____ test_a _____\n" + "a\n" * 20 + "_____ test_b _____\nb"
    capped = run_tests.cap_failure_blocks(text, limit=3)
    assert "_____ test_b _____" in capped
    assert capped.count("truncated") == 1


def test_cap_failure_blocks_is_pure():
    text = "_____ test_a _____\nline"
    run_tests.cap_failure_blocks(text, limit=1)
    assert text == "_____ test_a _____\nline"


def template_runner(monkeypatch) -> types.ModuleType:
    """The template's runner: it has no `{{ }}` (template_refresh lists it), so it loads as-is.

    With no bytecode written: a `__pycache__` beside it would be copied into every
    generated project with the rest of `templates/core/scripts/`.
    """
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    path = REPO_ROOT / "templates" / "core" / "scripts" / "run-tests.py.tmpl"
    loader = importlib.machinery.SourceFileLoader("template_run_tests", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# 57d473e9's shape: two failures, the first with a long message pytest repeats,
# indented, under its summary entry. The summary rode on the second failure's block, so
# that block's cap cut the second FAILED line, and the fix prompt named one test of two.
TWO_RED = "\n".join(
    [
        "=================================== FAILURES ===================================",
        "_____ test_one _____",
        "E   AssertionError: sets differ",
        "_____ test_two _____",
        "E   AssertionError: offenders",
        "=========================== short test summary info ============================",
        "FAILED tests/test_a.py::test_one - AssertionError: sets differ",
        *[f"  diff line {i}" for i in range(30)],
        "FAILED tests/test_b.py::test_two - AssertionError: offenders",
        "========================= 2 failed, 9 passed in 3.2s ==========================",
    ]
)


@pytest.mark.parametrize("runner", ["devkit", "template"])
def test_the_short_summary_keeps_every_failed_line_however_long_a_message_is(runner, monkeypatch):
    module = run_tests if runner == "devkit" else template_runner(monkeypatch)
    capped = module.cap_failure_blocks(TWO_RED, limit=5)
    ids = [line.split()[1] for line in capped.splitlines() if line.startswith("FAILED ")]
    assert ids == ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    kept = [line for line in capped.splitlines() if line.startswith("  diff line")]
    assert len(kept) == module.SUMMARY_LINES_PER_ENTRY
    assert f"  ... ({30 - len(kept)} more lines, truncated)" in capped
    assert capped.splitlines()[-1].startswith("=====")
    assert fix_plan.signature_from_logs([capped]) == tuple(ids)


# --- main ---------------------------------------------------------------------


def test_a_passing_run_clears_the_artifact(artifact, monkeypatch):
    artifact.parent.mkdir(parents=True)
    artifact.write_text("stale failure from an earlier run", encoding="utf-8")
    stub_pytest(monkeypatch, 0, "3 passed\n")

    assert run_tests.main([]) == 0
    assert artifact.read_text(encoding="utf-8") == ""


def test_collecting_nothing_is_not_a_failure(artifact, monkeypatch):
    """`stop.py` calls this with the changed files under tests/. Editing a conftest or
    a support module collects nothing, and reporting that as red blocks the stop with
    "no tests ran" — which no source edit can resolve."""
    stub_pytest(monkeypatch, run_tests.PYTEST_NO_TESTS_COLLECTED, "no tests ran\n")

    assert run_tests.main(["tests/support.py"]) == 0
    assert artifact.read_text(encoding="utf-8") == ""


def test_a_failing_run_writes_the_artifact_with_the_fix_command(artifact, monkeypatch):
    stub_pytest(monkeypatch, 1, FAILING_OUTPUT)

    assert run_tests.main([]) == 1
    body = artifact.read_text(encoding="utf-8")
    assert "# source: scripts/run-tests.py" in body
    assert "--tb=long" in body
    assert "test_b.py::test_b_thing" in body


def test_an_unrecognised_failure_shape_falls_back_to_raw_output(artifact, monkeypatch):
    """Never leave the agent with an empty artifact: if filtering strips everything —
    an internal error, a crashed interpreter — the raw text is better than nothing."""
    stub_pytest(monkeypatch, 2, "", "INTERNALERROR> RecursionError\n")

    assert run_tests.main([]) == 1
    assert "INTERNALERROR" in artifact.read_text(encoding="utf-8")


def test_changed_asks_pytest_for_the_last_failed_subset(artifact, monkeypatch):
    seen = stub_pytest(monkeypatch, 0)
    run_tests.main(["--changed"])
    assert "--last-failed" in seen[0]
    assert ["--last-failed-no-failures", "all"] == seen[0][-2:]


def test_unknown_arguments_are_passed_through_as_pytest_targets(artifact, monkeypatch):
    """The Stop hook passes the changed test files positionally."""
    seen = stub_pytest(monkeypatch, 0)
    run_tests.main(["tests/test_sweep.py", "-k", "reap"])
    assert seen[0][-3:] == ["tests/test_sweep.py", "-k", "reap"]


def test_it_runs_pytest_with_this_interpreter(artifact, monkeypatch):
    """A bare `python` takes the machine default, which in a box is not the box's venv."""
    seen = stub_pytest(monkeypatch, 0)
    run_tests.main([])
    assert seen[0][:3] == [run_tests.sys.executable, "-m", "pytest"]


def test_with_basetemp_goes_right_after_the_module_and_keeps_the_rest():
    cmd = ["py", "-m", "pytest", "-q", "tests/test_a.py"]
    assert run_tests.with_basetemp(cmd, "/t/x") == [
        "py",
        "-m",
        "pytest",
        "--basetemp=/t/x",
        "-q",
        "tests/test_a.py",
    ]
    assert cmd == ["py", "-m", "pytest", "-q", "tests/test_a.py"]  # not mutated


def test_every_run_gets_a_temp_root_of_its_own_and_leaves_none(artifact, monkeypatch):
    """97d20f01: under the machine-wide `pytest-of-<user>`, another session's run holding
    its `pytest-current` link made pytest's exit-time cleanup raise access-denied, so a
    green suite exited 1 with no failed test to name."""
    seen: list[Path] = []

    def fake_run(cmd, **_kwargs):
        (flag,) = [a for a in cmd if a.startswith("--basetemp=")]
        seen.append(Path(flag.split("=", 1)[1]))
        assert seen[-1].is_dir()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(run_tests.subprocess, "run", fake_run)
    run_tests.main([])
    run_tests.main([])
    assert len(set(seen)) == 2
    assert not any(path.exists() for path in seen)


def test_the_suite_is_handed_to_workers_when_xdist_is_installed(artifact, monkeypatch):
    """438s serially on this workstation, 109s across eight workers, and the gate runs
    this on every push.

    Reversion check: drop `_parallel_args()` from `main()` and this fails.
    """
    seen = stub_pytest(monkeypatch, 0)
    monkeypatch.setattr(run_tests, "_parallel_args", lambda: ["-n", "auto"])
    run_tests.main([])
    assert seen[0][-2:] == ["-n", "auto"]


def test_the_command_is_unchanged_where_xdist_is_absent(artifact, monkeypatch):
    seen = stub_pytest(monkeypatch, 0)
    monkeypatch.setattr(run_tests, "_parallel_args", list)
    run_tests.main([])
    assert "-n" not in seen[0]


def test_the_changed_subset_still_parallelises(artifact, monkeypatch):
    """`--last-failed` and xdist compose; the flag must not be dropped on that path."""
    seen = stub_pytest(monkeypatch, 0)
    monkeypatch.setattr(run_tests, "_parallel_args", lambda: ["-n", "auto"])
    run_tests.main(["--changed"])
    assert "-n" in seen[0] and "--last-failed" in seen[0]


# --- the default is what changed ----------------------------------------------------


def changed(monkeypatch, tmp_path, *paths: str, tests: tuple[str, ...] = ()):
    """A changed set for the runner to target, and the test files that exist for it."""
    for name in tests:
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "tests" / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(run_tests, "changed_paths", lambda root, run=None: list(paths))


def test_tests_for_names_a_test_file_by_its_module_and_a_test_by_itself(tmp_path):
    for name in ("test_fix_pass.py", "test_sweep.py"):
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "tests" / name).write_text("", encoding="utf-8")
    paths = [
        "scripts/fix-pass.py",
        "scripts\\sweep.py",
        "tests/test_sweep.py",
        "scripts/nothing_tested.py",
        "README.md",
        "tests/support.py",
    ]
    assert run_tests.tests_for(paths, tmp_path) == (
        ["tests/test_fix_pass.py", "tests/test_sweep.py"],
        ["scripts/nothing_tested.py", "README.md", "tests/support.py"],
    )


def test_by_default_only_the_tests_named_by_the_changed_files_run(artifact, monkeypatch, tmp_path):
    """Every agent ran the whole suite by reflex and hit the same harness red, one
    session after another; the gate is CI's, and the default here is what the change
    could have broken."""
    changed(monkeypatch, tmp_path, "scripts/fix_plan.py", "README.md", tests=("test_fix_plan.py",))
    seen = stub_pytest(monkeypatch, 0)
    assert run_tests.main([]) == 0
    assert seen[0][-1] == "tests/test_fix_plan.py"


def test_the_contract_tests_run_with_every_change(artifact, monkeypatch, tmp_path, capsys):
    """54bb72df: #467 added an import to fix-pass.py, ran the tests its files named, and
    went red on test_scheduled_jobs, which reads every module and is named by none."""
    changed(monkeypatch, tmp_path, "README.md", "scripts/untested.py")
    for rel in run_tests.CONTRACT_TESTS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    seen = stub_pytest(monkeypatch, 0)
    assert run_tests.main([]) == 0
    assert seen[0][-len(run_tests.CONTRACT_TESTS) :] == list(run_tests.CONTRACT_TESTS)
    out = capsys.readouterr().out
    assert "no test named for README.md" in out and "no test named for scripts/untested.py" in out
    assert "the contract tests" in out


def test_every_contract_test_listed_exists():
    """A renamed contract test would drop out of every default run without a word."""
    missing = [t for t in run_tests.CONTRACT_TESTS if not (REPO_ROOT / t).is_file()]
    assert missing == []


def test_with_contracts_keeps_the_named_tests_first_and_adds_each_once(tmp_path):
    for rel in ("tests/test_a.py", "tests/test_test_contract.py", "tests/test_doc_claims.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    got = run_tests.with_contracts(["tests/test_a.py", "tests/test_doc_claims.py"], tmp_path)
    assert got == ["tests/test_a.py", "tests/test_doc_claims.py", "tests/test_test_contract.py"]


def test_nothing_changed_runs_nothing_and_says_so(artifact, monkeypatch, tmp_path, capsys):
    changed(monkeypatch, tmp_path)
    seen = stub_pytest(monkeypatch, 0)
    artifact.parent.mkdir(parents=True)
    artifact.write_text("stale", encoding="utf-8")
    assert run_tests.main([]) == 0
    assert seen == [] and artifact.read_text(encoding="utf-8") == ""
    assert "--all runs the suite" in capsys.readouterr().out


def test_all_ci_pre_commit_and_explicit_targets_run_the_whole_suite(
    artifact, monkeypatch, tmp_path
):
    """The gate and the push gate mirror it want everything; `--all` is the person's
    spelling of the same, and an explicit target is already a choice."""
    changed(monkeypatch, tmp_path, "scripts/fix_plan.py", tests=("test_fix_plan.py",))
    seen = stub_pytest(monkeypatch, 0)
    monkeypatch.setattr(run_tests, "_parallel_args", list)
    run_tests.main(["--all"])
    monkeypatch.setenv("CI", "true")
    run_tests.main([])
    monkeypatch.delenv("CI")
    monkeypatch.setenv("PRE_COMMIT", "1")
    run_tests.main([])
    monkeypatch.delenv("PRE_COMMIT")
    run_tests.main(["tests/test_sweep.py"])
    assert [cmd[-1] for cmd in seen] == ["-q", "-q", "-q", "tests/test_sweep.py"]


def test_when_git_cannot_say_what_changed_the_suite_runs(artifact, monkeypatch, capsys):
    seen = stub_pytest(monkeypatch, 0)
    monkeypatch.setattr(run_tests, "_parallel_args", list)
    assert run_tests.main([]) == 0
    assert seen[0][-1] == "-q"
    assert "git cannot say what changed" in capsys.readouterr().out


def git_answers(answers: dict[tuple[str, ...], tuple[int, str]]):
    seen: list[tuple[str, ...]] = []

    def run(cmd, **_kwargs):
        args = tuple(cmd[1:])
        seen.append(args)
        code, out = answers.get(args, (1, ""))
        return subprocess.CompletedProcess(cmd, code, out, "")

    return run, seen


def test_changed_paths_is_everything_since_the_branch_left_origins_default(tmp_path):
    run, seen = git_answers(
        {
            ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (
                0,
                "refs/remotes/origin/main\n",
            ),
            ("merge-base", "HEAD", "origin/main"): (0, "abc123\n"),
            ("diff", "--name-only", "abc123"): (0, "scripts/b.py\nscripts/a.py\n"),
            ("ls-files", "--others", "--exclude-standard"): (0, "tests/test_new.py\n"),
        }
    )
    assert run_tests.changed_paths(tmp_path, run) == [
        "scripts/a.py",
        "scripts/b.py",
        "tests/test_new.py",
    ]
    assert seen[0] == ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")


def test_changed_paths_is_none_without_an_origin_to_compare_against(tmp_path):
    run, _seen = git_answers({})
    assert run_tests.changed_paths(tmp_path, run) is None


def test_the_default_branch_is_origins_head_then_main_or_master(tmp_path):
    run, _seen = git_answers(
        {
            ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (
                0,
                "refs/remotes/origin/trunk\n",
            )
        }
    )
    assert run_tests.default_branch(tmp_path, run) == "trunk"
    run, _seen = git_answers(
        {("rev-parse", "--verify", "--quiet", "refs/remotes/origin/master"): (0, "")}
    )
    assert run_tests.default_branch(tmp_path, run) == "master"
    run, _seen = git_answers({})
    assert run_tests.default_branch(tmp_path, run) == "main"
