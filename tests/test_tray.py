"""`tray.py`: the parts of a Windows message loop that can be tested without one.

Most of this file is `ctypes`, and a mocked `ctypes` call asserts only that the mock was
written. So the tests here cover the two things that are real: the pixel buffer handed
to `CreateIcon`, and the signature declarations -- which is where this actually broke
twice, both times as *nothing happening* rather than as an error.

The message loop itself was verified by running it: `python scripts/tray.py
--poll-seconds 3` under a timeout, which draws a real icon and exits only when killed.
That is not something a suite can assert, so `--once` exists to give the whole read path
a runnable, assertable form.
"""

from __future__ import annotations

import ast

import pytest
from support import REPO_ROOT, load_script

tray = load_script("scripts/tray.py")
tray_state = load_script("scripts/tray_state.py")


# --- the signatures ---------------------------------------------------------


def test_declaring_is_idempotent_and_safe_on_any_platform():
    """It runs on every `Tray.run`, and off Windows it must return rather than explode --
    devkit runs on POSIX and importing this module there is supported."""
    tray._declare()
    tray._declare()


def win32_calls() -> set[str]:
    """Every `user32`/`shell32`/`kernel32` function this module calls, from its source.

    Read out of the AST rather than off the loaded DLLs, so this whole section runs on
    POSIX. The Windows-only versions of these assertions were `skipif`-ed, which meant
    the checks that mattered most did not run in CI at all -- and the bugs they are
    about are invisible at runtime, so nothing else would have caught them either.
    """
    tree = ast.parse((REPO_ROOT / "scripts" / "tray.py").read_text(encoding="utf-8"))
    return {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"user32", "shell32", "kernel32"}
    }


def test_every_win32_call_is_declared_or_deliberately_not():
    """The check that would have caught all three signature bugs in this file at once.

    Each was invisible: a truncated `restype` makes a call *appear* to succeed against a
    handle to nothing, and an undeclared handle argument raises inside a callback where
    the error is printed and ignored. Neither reddens anything.
    """
    declared = set(tray._RESTYPES) | set(tray._ARGTYPES)
    undeclared = sorted(
        call
        for call in win32_calls()
        if call not in declared and call.partition(".")[2] not in tray.NEEDS_NO_SIGNATURE
    )
    assert not undeclared, (
        f"{undeclared} are called with no entry in _RESTYPES or _ARGTYPES. If a call "
        f"really touches no handle, add it to NEEDS_NO_SIGNATURE with the reason."
    )


def test_the_message_fallback_is_declared():
    """`DefWindowProcW` is only reached for messages the tray does not handle, so it
    fails on ordinary background traffic rather than on anything the tray does."""
    assert tray._ARGTYPES["user32.DefWindowProcW"][-1] is tray.wintypes.LPARAM


def test_no_signature_is_declared_for_a_call_that_was_removed():
    """The other direction: a stale declaration is a claim about code that is gone. One
    (`LoadCursorW`) was already sitting in the table when this test was written."""
    calls = win32_calls()
    for table in (tray._RESTYPES, tray._ARGTYPES):
        for name in table:
            assert name in calls, f"{name} is declared but nothing calls it"


def test_nothing_is_exempted_that_is_never_called():
    assert {f"user32.{name}" for name in tray.NEEDS_NO_SIGNATURE} <= win32_calls()


def test_every_declared_name_is_qualified_by_its_dll():
    """The tables are keyed `<dll>.<function>` and `_declare` splits on the dot. A bare
    name would be a `KeyError` on the module, raised the first time the tray starts."""
    for table in (tray._RESTYPES, tray._ARGTYPES):
        for name in table:
            module, dot, function = name.partition(".")
            assert dot and module in {"user32", "shell32", "kernel32"} and function


def test_the_notify_structure_matches_the_size_windows_expects():
    """`cbSize` selects the layout. A structure that does not match the size it declares
    is rejected by the shell with no error the caller can see."""
    assert tray.ctypes.sizeof(tray.NOTIFYICONDATA) > 0
    fields = dict(tray.NOTIFYICONDATA._fields_)
    assert fields["szTip"] == tray.wintypes.WCHAR * 128


# --- the read path ----------------------------------------------------------


def test_once_prints_what_the_tray_would_show(capsys, monkeypatch):
    """`--once` is the whole read path in a form a suite can assert, and the form a
    human uses to check the tray is telling the truth."""
    monkeypatch.setattr(
        tray_state,
        "refresh",
        lambda now=None: [
            tray_state.JobState("devkit-a", tray_state.OK),
            tray_state.JobState("devkit-b", tray_state.FAIL, "it broke"),
        ],
    )
    assert tray.main(["--once"]) == 0
    out = capsys.readouterr().out
    assert "devkit-a" in out and "devkit-b" in out and "it broke" in out


def test_once_works_on_a_machine_with_nothing_registered(capsys, monkeypatch):
    monkeypatch.setattr(tray_state, "refresh", lambda now=None: [])
    assert tray.main(["--once"]) == 0
    assert "no scheduled jobs" in capsys.readouterr().out


def test_a_posix_machine_is_told_so_and_stays_green(capsys, monkeypatch):
    """Not a failure: a missing tray on POSIX is not a fault, and a non-zero exit would
    redden a scheduled caller for the platform it is running on."""
    monkeypatch.setattr(tray.os, "name", "posix")
    assert tray.main([]) == 0
    assert "Windows only" in capsys.readouterr().out


def test_the_artifact_says_why_a_missing_tray_is_worth_a_file(tmp_path):
    tray.write_artifact("# hello\n", tmp_path)
    assert (tmp_path / tray.ARTIFACT).read_text(encoding="utf-8") == "# hello\n"


@pytest.mark.parametrize(
    "error", [OSError("schtasks is gone"), ValueError("unparseable"), KeyError("no colour")]
)
def test_a_poll_that_hits_a_known_failure_leaves_the_icon_up(monkeypatch, error):
    """A tray that died on one bad `schtasks` answer would vanish from the notification
    area, which reads as "nothing is wrong" -- the opposite of what happened."""

    def explode(now=None):
        raise error

    monkeypatch.setattr(tray_state, "refresh", explode)
    instance = tray.Tray()
    instance.poll()
    assert instance.states == []


def test_a_poll_that_hits_something_unexpected_is_not_swallowed(monkeypatch):
    """The other half of catching narrowly. A bare `except` here would turn any defect
    in `tray_state` into a tray that sits there showing a machine it stopped reading --
    which is indistinguishable from a healthy one."""

    def explode(now=None):
        raise RuntimeError("a defect, not a bad answer")

    monkeypatch.setattr(tray_state, "refresh", explode)
    with pytest.raises(RuntimeError):
        tray.Tray().poll()


def test_the_window_procedure_is_kept_alive_by_the_object():
    """The only Python reference to a callback Windows holds a raw pointer to. Letting
    it be collected is a crash in a message loop, with no traceback."""
    assert tray.Tray().proc is not None


def test_make_icon_is_a_no_op_without_a_windows_dll(monkeypatch):
    """`make_icon` returning None rather than raising is what lets `notify` fall back to
    a tooltip-only icon instead of taking the tray down."""
    monkeypatch.setattr(tray, "user32", None)
    assert tray.make_icon((1, 2, 3)) is None


def test_the_window_class_struct_declares_its_own_size():
    """`WNDCLASSEX` is registered by `cbSize`, and a struct whose fields do not match the
    size it declares is refused by `RegisterClassExW` with no error the caller sees."""
    fields = dict(tray.WNDCLASSEX._fields_)
    assert fields["cbSize"] is tray.wintypes.UINT
    assert fields["lpfnWndProc"] is tray.WNDPROC
    assert fields["lpszClassName"] is tray.wintypes.LPCWSTR
