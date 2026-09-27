"""Tests for `scripts/hook-tests.py` — the runner behind "Test: Harness Hook Tests".

The behaviour worth pinning is not "pytest runs". It is the two things that make this
script safe to point at an arbitrary checkout: it resolves the target through the CWD
rather than through `Path(__file__)` (because `devkit_project.py` invokes every
DEVKIT-owned action with the cwd set to the chosen project), and it REFUSES a checkout
that does not vendor the harness instead of exiting 0 over an empty run.

That second one is the whole reason this file exists. devkit's CLAUDE.md names the
silent-skip — a gate reporting green having run nothing — as the quietest failure in
the harness, and records that devkit shipped exactly that in `stop.py` for several
releases. A hook-test runner that treats a missing `scripts/hooks/tests/` as "nothing
to do" is the same bug with a different filename.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from support import load_script

hook_tests = load_script("scripts/hook-tests.py")


@pytest.fixture
def vendored(tmp_path):
    """A checkout carrying the vendored tier, with one passing test in it."""
    tests = tmp_path / "scripts" / "hooks" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return tmp_path


# --- the refusal ------------------------------------------------------------


def test_a_checkout_without_the_vendored_tier_is_refused(tmp_path, capsys):
    """Exit 2, not 0. ibkr_trader is a real example: it satisfies the test/lint/
    sync-codex contract but ships no `scripts/hooks/` at all, so this is the answer
    the task gives there and it has to be visibly different from "passed"."""
    assert hook_tests.main(["--root", str(tmp_path)]) == 2
    assert "does not vendor the devkit harness" in capsys.readouterr().err


def test_the_refusal_writes_no_artifact(tmp_path):
    """A cleared artifact is this harness's signal for "ran and passed".

    Writing one on the refusal path would make the next agent read an empty
    `hook-test-failures.log` as a clean run of a suite that never executed.
    """
    hook_tests.main(["--root", str(tmp_path)])
    assert not (tmp_path / hook_tests.ARTIFACT).exists()


def test_vendors_harness_keys_off_the_manifest_path(tmp_path):
    assert not hook_tests.vendors_harness(tmp_path)
    (tmp_path / "scripts" / "hooks" / "tests").mkdir(parents=True)
    assert hook_tests.vendors_harness(tmp_path)


# --- the real runs ----------------------------------------------------------


def test_a_passing_run_clears_the_artifact(vendored):
    stale = vendored / hook_tests.ARTIFACT
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# a failure from three runs ago\n", encoding="utf-8")

    assert hook_tests.main(["--root", str(vendored)]) == 0
    # Cleared, not deleted: an empty artifact is the positive signal, and a stale one
    # would send the next agent chasing a failure that is already fixed.
    assert stale.read_text(encoding="utf-8") == ""


def test_a_failing_run_writes_the_artifact_and_names_the_fix(vendored):
    (vendored / "scripts" / "hooks" / "tests" / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8"
    )
    assert hook_tests.main(["--root", str(vendored)]) == 1

    artifact = (vendored / hook_tests.ARTIFACT).read_text(encoding="utf-8")
    assert "# source: devkit scripts/hook-tests.py" in artifact
    assert "--tb=long" in artifact, "the artifact must say how to get a fuller trace"
    assert "test_bad" in artifact, "the failing test id has to reach the artifact"


def test_the_target_comes_from_the_cwd_not_from_devkits_own_tree(vendored, monkeypatch):
    """`devkit_project.py` runs DEVKIT-owned actions with cwd set to the CHECKOUT.

    So a default run must test wherever it was invoked, not devkit. Resolving through
    `Path(__file__)` here would make one task silently report devkit's own hook suite
    for every project in the picker — green everywhere, and meaningless in all but one.
    """
    monkeypatch.chdir(vendored)
    assert hook_tests.main([]) == 0
    assert (vendored / hook_tests.ARTIFACT).exists()


# --- the artifact cap -------------------------------------------------------


def test_cap_leaves_short_output_alone():
    assert hook_tests.cap("one\ntwo", limit=10) == "one\ntwo"


def test_cap_truncates_and_says_so():
    capped = hook_tests.cap("\n".join(str(n) for n in range(50)), limit=5)
    assert capped.splitlines()[:5] == ["0", "1", "2", "3", "4"]
    assert "50 lines total, truncated" in capped


def test_cap_is_pure():
    """It is separated from the subprocess call so the truncation is testable without
    provoking a 200-line pytest failure to produce the input."""
    original = "\n".join(str(n) for n in range(20))
    hook_tests.cap(original, limit=3)
    assert original == "\n".join(str(n) for n in range(20))


def test_the_runner_uses_this_interpreter(vendored, monkeypatch):
    """`sys.executable`, never a bare `python`.

    The task is launched by VS Code, whose PATH is not the venv's. A bare `python`
    would run whichever interpreter the desktop resolves and report ImportErrors that
    have nothing to do with the code under test.
    """
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(hook_tests.subprocess, "run", fake_run)
    hook_tests.main(["--root", str(vendored)])
    assert seen["cmd"][0] == sys.executable
    assert seen["cmd"][1:3] == ["-m", "pytest"]
    assert seen["cwd"] == vendored.resolve()


def test_the_run_gets_a_temp_root_of_its_own_and_leaves_none(vendored, monkeypatch):
    """97d20f01: under the machine-wide `pytest-of-<user>`, another session's run holding
    its `pytest-current` link made pytest's exit-time cleanup raise access-denied, so a
    green suite exited 1 with no failed test to name."""
    seen: list[Path] = []

    def fake_run(cmd, **_kwargs):
        (flag,) = [a for a in cmd if a.startswith("--basetemp=")]
        seen.append(Path(flag.split("=", 1)[1]))
        assert seen[-1].is_dir()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(hook_tests.subprocess, "run", fake_run)
    assert hook_tests.main(["--root", str(vendored)]) == 0
    assert len(seen) == 1 and not seen[0].exists()
