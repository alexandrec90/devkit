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

import subprocess
from pathlib import Path

import pytest
from support import load_script

run_tests = load_script("scripts/run-tests.py")


@pytest.fixture
def artifact(tmp_path, monkeypatch) -> Path:
    """Redirect both the artifact and the root it is reported relative to.

    Both, because `main()` prints `ARTIFACT.relative_to(REPO_ROOT)` — moving only the
    artifact makes that raise a ValueError that has nothing to do with the test.
    """
    path = tmp_path / "logs" / "test-failures.log"
    monkeypatch.setattr(run_tests, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_tests, "ARTIFACT", path)
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
