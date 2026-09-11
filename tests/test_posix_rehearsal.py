"""Tests for the POSIX rehearsal: the plugin's pure halves and the wrapper around it.

The plugin's *effect* -- a suite that fails under a faked platform -- is exercised by an
end-to-end case at the bottom that runs pytest on a two-test throwaway package, because
that is the only way to prove the flip reaches a module the way it will in the real run.
Everything above it is the pure logic, testable without a subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import posix_rehearsal_plugin as plugin
import pytest
from support import REPO_ROOT, load_script, windows_layout

rehearsal = load_script("scripts/posix-rehearsal.py")


# --- the ledger format ---------------------------------------------------------------


def test_a_ledger_entry_is_an_id_and_a_reason():
    parsed = rehearsal.parse_ledger("tests/test_x.py::test_y  # drives schtasks, no POSIX twin\n")
    assert parsed == {"tests/test_x.py::test_y": "drives schtasks, no POSIX twin"}


def test_comments_and_blank_lines_are_not_entries():
    assert rehearsal.parse_ledger("# a heading\n\n   \n# another\n") == {}


def test_a_reason_may_contain_the_comment_marker():
    """`partition` splits on the first `#` only; a reason mentioning one keeps it."""
    parsed = rehearsal.parse_ledger("a::b  # see issue #12 about the console flag\n")
    assert parsed["a::b"] == "see issue #12 about the console flag"


def test_an_entry_without_a_reason_is_reported():
    entries = rehearsal.parse_ledger("a::b\nc::d  # a genuinely written down reason here\n")
    assert rehearsal.unreasoned(entries) == ["a::b"]


def test_a_gesture_at_a_reason_is_not_one():
    """Four words is a bar against `# windows`, not a quality judgement."""
    assert rehearsal.unreasoned(rehearsal.parse_ledger("a::b  # windows only\n")) == ["a::b"]


def test_the_shipped_ledger_parses_and_every_entry_carries_a_reason():
    """The ledger is the mechanism's only escape hatch, so it is held to its own rule."""
    entries = rehearsal.read_ledger()
    assert rehearsal.unreasoned(entries) == [], (
        "every line in posix-rehearsal-ledger.txt needs a reason after the `#`"
    )


def test_a_missing_ledger_is_empty_rather_than_an_error(tmp_path):
    """The steady state is no entries at all; a deleted file must not fail every run."""
    assert rehearsal.read_ledger(tmp_path / "nope.txt") == {}


def test_the_shipped_ledger_exists_so_the_convention_is_discoverable():
    """An absent file reads as "there is no such mechanism" to the next agent."""
    assert rehearsal.LEDGER.is_file()


# --- finding the constants to flip ---------------------------------------------------


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def test_a_module_level_windows_constant_is_found():
    table = {"a": _module("a", WINDOWS=True), "b": _module("b")}
    assert plugin.windows_constants(table) == ["a"]


def test_a_constant_already_false_is_left_alone():
    """On a POSIX host every constant is already `False`; there is nothing to flip and
    nothing to restore, and reporting them would make the count meaningless."""
    assert plugin.windows_constants({"a": _module("a", WINDOWS=False)}) == []


def test_a_non_boolean_windows_attribute_is_not_a_platform_constant():
    """`WINDOWS` as a set of hostnames, a string, a class -- flipping one to `False` would
    corrupt the module rather than rehearse it."""
    table = {"a": _module("a", WINDOWS="C:\\"), "b": _module("b", WINDOWS=1)}
    assert plugin.windows_constants(table) == []


def test_the_convention_the_discovery_depends_on_is_still_the_repos():
    """Guards the guard. The flip finds nothing if the installers stop spelling their
    platform check as a module-level `WINDOWS`, and a rehearsal that rehearses nothing
    passes -- the failure mode of every check built on a scan.

    Asserted on the name rather than the value, because the value is `False` on a POSIX
    host and under the rehearsal itself, and a test that only holds on Windows is the
    exact thing this file exists to prevent.
    """
    carriers = (
        load_script("scripts/harness-switch.py"),
        load_script("scripts/install-upgrade-schedule.py"),
    )
    for module in carriers:
        assert isinstance(getattr(module, "WINDOWS", None), bool), (
            f"{module.__name__} no longer carries a module-level WINDOWS constant; the "
            f"rehearsal cannot reach a bare `os.name == 'nt'` branch"
        )


# --- the flip itself -----------------------------------------------------------------


def test_apply_flips_the_constants_and_leaves_the_platform_alone(monkeypatch):
    """The platform is faked around the test *body* only, never at collection.

    pytest builds `tmp_path` on the platform it sees at setup, and its factory calls
    `os.getuid` when that says POSIX -- which Windows has not got. Faking it any earlier
    errored every test taking a `tmp_path`, which is most of this suite.
    """
    target = _module("fake_installer", WINDOWS=True)
    monkeypatch.setitem(sys.modules, "fake_installer", target)
    monkeypatch.setattr(sys, "platform", "win32")
    state = plugin.PosixRehearsal()

    state.apply()
    assert target.WINDOWS is False
    assert sys.platform == "win32"


def test_the_platform_is_faked_and_handed_back(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    state = plugin.PosixRehearsal()

    state.fake_platform()
    assert sys.platform == plugin.POSIX_PLATFORM
    state.restore()
    assert sys.platform == "win32"


def test_tmp_path_still_works_under_the_rehearsal(tmp_path):
    """The regression the split exists for, asserted from inside a rehearsed run: this
    test takes a `tmp_path`, so its mere collection proves setup saw the real platform.

    Held here rather than in a comment because the failure it guards against does not
    look like a platform bug -- it looks like the rehearsal itself being unusable.
    """
    assert tmp_path.is_dir()


def test_the_flip_is_re_asserted_so_one_test_cannot_leak_into_the_next(monkeypatch):
    """A test that *reassigns* the constant rather than monkeypatching it would otherwise
    hand a `True` to every test after it, and an order-dependent rehearsal is worse than
    no rehearsal: it would pass or fail on collection order."""
    target = _module("fake_installer", WINDOWS=True)
    monkeypatch.setitem(sys.modules, "fake_installer", target)
    state = plugin.PosixRehearsal()
    state.apply()

    target.WINDOWS = True  # what a careless test leaves behind
    state.flip_constants()
    assert target.WINDOWS is False


def test_restore_puts_the_hosts_platform_back(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    state = plugin.PosixRehearsal()
    state.apply()
    state.restore()
    assert sys.platform == "win32"


# --- the convention for the class the rehearsal cannot gate --------------------------


def test_windows_layout_makes_both_spellings_real(tmp_path):
    """What the helper is for: a real directory holding both interpreter names, so the
    branch that picks between them runs on every platform instead of only this one."""
    console, windowless = windows_layout(tmp_path, "python.exe", "pythonw.exe")
    assert console.is_file() and windowless.is_file()
    assert console.parent == windowless.parent == tmp_path


def test_windows_layout_keeps_the_order_it_was_given(tmp_path):
    """Callers unpack it positionally, so a sorted or set-backed return would silently
    swap the two names in a test whose whole point is telling them apart."""
    first, second = windows_layout(tmp_path, "pythonw.exe", "python.exe")
    assert first.name == "pythonw.exe"
    assert second.name == "python.exe"


def test_windows_layout_paths_split_the_same_way_on_every_platform(tmp_path):
    """The property the literal `r"C:\\py\\python.exe"` does not have: `name` is the file
    and `parent` is the directory, here and on CI's runner alike."""
    (console,) = windows_layout(tmp_path, "python.exe")
    assert console.name == "python.exe"
    assert console.parent != console


# --- reading the run against the ledger ----------------------------------------------


SUMMARY = """\
= short test summary info =
FAILED tests/test_a.py::test_one - AssertionError
ERROR tests/test_b.py::test_two
"""


def test_both_failures_and_errors_are_read():
    """A setup that takes a Windows-only path off Windows is the same defect at a
    different phase; reading only `FAILED` would let every one of them through."""
    assert rehearsal.failed_tests(SUMMARY) == {
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    }


def test_a_clean_run_reads_as_nothing_failed():
    assert rehearsal.failed_tests("42 passed in 30.11s\n") == set()


def test_an_unledgered_failure_is_the_finding():
    unexpected, fixed = rehearsal.verdict({"a::b"}, {})
    assert unexpected == ["a::b"]
    assert fixed == []


def test_a_ledgered_failure_is_expected_and_passes_the_gate():
    assert rehearsal.verdict({"a::b"}, {"a::b": "drives schtasks, no POSIX twin"}) == ([], [])


def test_a_ledger_entry_that_now_passes_is_reported_so_the_line_gets_deleted():
    """The ratchet. Left alone the line goes on excusing whatever test later takes that
    id -- the same reason `structure_check.py --tighten` exists."""
    unexpected, fixed = rehearsal.verdict(set(), {"a::b": "was a schtasks thing once"})
    assert unexpected == []
    assert fixed == ["a::b"]


def test_the_two_are_reported_together_rather_than_one_hiding_the_other():
    unexpected, fixed = rehearsal.verdict({"new::x"}, {"stale::y": "a written down reason"})
    assert (unexpected, fixed) == (["new::x"], ["stale::y"])


# --- the wrapper ---------------------------------------------------------------------


def test_the_command_loads_the_plugin():
    argv = rehearsal.command("py")
    assert argv[:3] == ["py", "-m", "pytest"]
    assert argv[argv.index("-p") + 1] == plugin.__name__


def test_the_plugin_directory_is_on_pythonpath():
    """`-p` resolves before collection puts `tests/` on the path itself, so the wrapper
    has to arrange it -- this is the whole reason the wrapper exists."""
    assert str(REPO_ROOT / "tests") in rehearsal.environment({})["PYTHONPATH"]


def test_an_existing_pythonpath_is_prepended_to_not_replaced():
    """uv runs with one of its own; dropping it breaks the interpreter this then spawns."""
    result = rehearsal.environment({"PYTHONPATH": "/already/here"})["PYTHONPATH"]
    assert result.endswith("/already/here")
    assert str(REPO_ROOT / "tests") in result


def test_a_pass_clears_the_artifact(tmp_path, monkeypatch, capsys):
    """A stale artifact sends the next agent chasing a failure that is already fixed --
    `run-tests.py`'s rule, and the reason this writes the same shape."""
    artifact = tmp_path / "posix-rehearsal.log"
    artifact.write_text("old failure\n", encoding="utf-8")
    monkeypatch.setattr(rehearsal, "ARTIFACT", artifact)

    code = rehearsal.main([], runner=lambda *_a, **_k: _completed(0))
    assert code == 0
    assert artifact.read_text(encoding="utf-8") == ""
    assert "passed" in capsys.readouterr().out


def test_no_tests_collected_is_not_a_failure_of_this_runner(tmp_path, monkeypatch):
    """Same carve-out as `run-tests.py`: a collection scope that holds nothing is not a
    reason to refuse a push."""
    monkeypatch.setattr(rehearsal, "ARTIFACT", tmp_path / "a.log")
    assert rehearsal.main([], runner=lambda *_a, **_k: _completed(5)) == 0


def test_a_failure_writes_an_artifact_that_says_what_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rehearsal, "ARTIFACT", tmp_path / "posix-rehearsal.log")
    raw = "= FAILURES =\n____ test_x ____\nE   AssertionError\n= short test summary info =\n"

    code = rehearsal.main([], runner=lambda *_a, **_k: _completed(1, raw))
    assert code == 1
    written = (tmp_path / "posix-rehearsal.log").read_text(encoding="utf-8")
    assert "test_x" in written
    # The two ways out, named in the artifact rather than left to be remembered.
    assert "monkeypatch" in written
    assert "posix-rehearsal-ledger.txt" in written
    assert "FAILED" in capsys.readouterr().out


def test_an_unparseable_run_still_leaves_the_agent_something(tmp_path, monkeypatch):
    """If filtering strips everything -- a collection error, a pytest output shape the
    filter does not know -- the raw text beats an empty file."""
    monkeypatch.setattr(rehearsal, "ARTIFACT", tmp_path / "a.log")
    rehearsal.main([], runner=lambda *_a, **_k: _completed(1, "ImportError: no module named x"))
    assert "ImportError" in (tmp_path / "a.log").read_text(encoding="utf-8")


def test_a_ledgered_failure_does_not_refuse_the_push(tmp_path, monkeypatch):
    """The escape hatch, end to end: pytest exits 1, and the gate still passes because
    the one failure is a line someone wrote down."""
    monkeypatch.setattr(rehearsal, "ARTIFACT", tmp_path / "a.log")
    ledger = tmp_path / "ledger.txt"
    ledger.write_text("t.py::t  # drives schtasks, which POSIX has no twin for\n", encoding="utf-8")
    monkeypatch.setattr(rehearsal, "LEDGER", ledger)

    raw = "= short test summary info =\nFAILED t.py::t - AssertionError\n"
    assert rehearsal.main([], runner=lambda *_a, **_k: _completed(1, raw)) == 0


def test_a_ledger_entry_that_stopped_being_true_refuses_the_push(tmp_path, monkeypatch, capsys):
    """A green suite is not enough: the stale line has to be deleted, and the artifact
    says which one rather than leaving it to be found."""
    monkeypatch.setattr(rehearsal, "ARTIFACT", tmp_path / "a.log")
    ledger = tmp_path / "ledger.txt"
    ledger.write_text("t.py::t  # this reason stopped being true\n", encoding="utf-8")
    monkeypatch.setattr(rehearsal, "LEDGER", ledger)

    assert rehearsal.main([], runner=lambda *_a, **_k: _completed(0)) == 1
    written = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert "t.py::t" in written
    assert "Delete their lines" in written
    assert "FAILED" in capsys.readouterr().out


def test_a_run_that_never_started_is_not_read_as_nothing_failed(tmp_path, monkeypatch):
    """The failure mode that would make the whole gate silently inert: a collection error
    exits non-zero with no summary to parse, and "no FAILED lines" must not read as clean.
    """
    monkeypatch.setattr(rehearsal, "ARTIFACT", tmp_path / "a.log")
    crash = "ERROR: file or directory not found: tests/\n"
    assert rehearsal.main([], runner=lambda *_a, **_k: _completed(4, crash)) == 1


def test_the_shared_filter_is_run_tests_own(tmp_path):
    """Loaded by path, so a rename of `run-tests.py` has to fail loudly here rather than
    silently leave this writing an artifact in a shape nothing else uses."""
    module = rehearsal._run_tests_module()
    assert callable(module.filter_output)
    assert callable(module.cap_failure_blocks)


def test_a_path_python_cannot_load_is_an_importerror_not_a_none(tmp_path):
    """`spec_from_file_location` answers None for a path with no loader, and the
    `module_from_spec` after it would raise somewhere far less legible."""
    unloadable = tmp_path / "run-tests.txt"
    unloadable.write_text("", encoding="utf-8")
    with pytest.raises(ImportError):
        rehearsal._run_tests_module(unloadable)


def _completed(code: int, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["pytest"], code, out, "")


# --- end to end ----------------------------------------------------------------------


REHEARSAL_SUBJECT = '''
"""A stand-in for a devkit installer, with the platform branch they all carry."""
import os

WINDOWS = os.name == "nt"


def registered_argv():
    if not WINDOWS:
        return []
    return ["schtasks", "/Query"]
'''

FORGETFUL_TEST = """
import subject


def test_it_queries_the_task():
    assert subject.registered_argv() == ["schtasks", "/Query"]
"""

CAREFUL_TEST = """
import subject


def test_it_queries_the_task(monkeypatch):
    monkeypatch.setattr(subject, "WINDOWS", True)
    assert subject.registered_argv() == ["schtasks", "/Query"]
"""


def _rehearse(tmp_path: Path, test_body: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "subject.py").write_text(REHEARSAL_SUBJECT, encoding="utf-8")
    (tmp_path / "test_subject.py").write_text(test_body, encoding="utf-8")
    env = rehearsal.environment()
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{env['PYTHONPATH']}"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", plugin.__name__, "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )


def test_a_test_that_never_said_which_platform_it_meant_fails_the_rehearsal(tmp_path):
    """The regression the whole mechanism exists for, and the exact shape of the
    `install-upgrade-schedule` failure that passed here and reddened the PR gate: the
    assertion is about the Windows branch, and nothing in the test says so."""
    result = _rehearse(tmp_path, FORGETFUL_TEST)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "test_it_queries_the_task" in result.stdout


def test_a_test_that_forces_the_branch_still_passes(tmp_path):
    """The other half, and the one that decides whether the mechanism is usable: forcing
    the branch has to keep working, or the only way to green is to delete coverage."""
    result = _rehearse(tmp_path, CAREFUL_TEST)
    assert result.returncode == 0, result.stdout + result.stderr
