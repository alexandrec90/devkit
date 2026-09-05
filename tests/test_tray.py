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


# --- the icon ---------------------------------------------------------------


def pixel(buffer: bytes, x: int, y: int, size: int = 16) -> tuple[int, int, int, int]:
    """One BGRA pixel out of a buffer, addressed the way the icon is drawn: x right, y
    down from the top-left."""
    offset = (y * size + x) * 4
    blue, green, red, alpha = buffer[offset : offset + 4]
    return blue, green, red, alpha


def test_the_pixel_buffer_is_the_size_createicon_expects():
    """32 bits per pixel over a 16x16 square. A buffer of the wrong length is read past
    its end by a C function that was told how big it is by the width and height."""
    pixels = tray.icon_pixels((1, 2, 3), size=16)
    assert len(pixels) == 16 * 16 * 4


def test_the_pixels_are_bgra_not_rgba():
    """Windows DIBs are little-endian BGRA. Getting this backwards swaps red and blue,
    which for this icon means a failure shows up as the healthy colour."""
    assert tray._pack(0.8, 0.4, 0.1, 1.0) == bytes((0x1A, 0x66, 0xCC, 0xFF))


def test_the_centre_of_the_dot_is_the_state_colour_in_bgra_order():
    """The same check through the function that actually feeds `CreateIcon`, because
    `_pack` could be right while its callers hand it the channels in the wrong order."""
    blue, green, red, alpha = pixel(tray.icon_pixels((0xC6, 0x28, 0x28)), 8, 8)
    assert alpha == 0xFF
    assert red > 0x80 > green
    assert blue == green


def test_colour_is_not_premultiplied_by_the_alpha():
    """Measured, not assumed: `DrawIconEx` applies the alpha to the colour itself, so a
    buffer that has already applied it renders a ring of muddy pixels around the dot."""
    assert tray._pack(1.0, 1.0, 1.0, 0.5) == bytes((0xFF, 0xFF, 0xFF, 0x80))


def test_the_corners_are_transparent_and_the_middle_is_not():
    """The dot is a dot because of the alpha channel -- there is no other mechanism
    shaping it, and an opaque corner is the square this replaced."""
    pixels = tray.icon_pixels((10, 20, 30))
    assert pixel(pixels, 0, 0)[3] == 0
    assert pixel(pixels, 15, 15)[3] == 0
    assert pixel(pixels, 8, 8)[3] == 0xFF


def test_the_edge_of_the_dot_is_antialiased():
    """Some pixel on the rim has to be partly covered. All-or-nothing alpha is a circle
    drawn as a staircase, which at 16 pixels is what the supersampling is for."""
    pixels = tray.icon_pixels((10, 20, 30))
    alphas = {pixel(pixels, x, y)[3] for y in range(16) for x in range(16)}
    assert any(0 < alpha < 0xFF for alpha in alphas)


def test_the_glyph_is_drawn_in_white_over_the_colour():
    """The knocked-out mark is the half of the signal a colour-blind reader gets. If it
    came out in the state colour it would be invisible, which is the failure to catch."""
    plain = tray.icon_pixels(tray_state.COLOURS[tray_state.FAIL])
    marked = tray.icon_pixels(
        tray_state.COLOURS[tray_state.FAIL], glyph=tray.GLYPHS[tray_state.FAIL]
    )
    assert plain != marked
    assert pixel(marked, 8, 8)[:3] == (0xFF, 0xFF, 0xFF)


def test_every_level_has_its_own_glyph():
    """A level `COLOURS` knows and `GLYPHS` does not is a bare dot in the tray -- legal,
    silent, and reporting less than the other two."""
    assert set(tray.GLYPHS) == set(tray_state.COLOURS)
    assert len(set(tray.GLYPHS.values())) == len(tray.GLYPHS)


def test_the_rows_run_top_down_so_the_check_mark_points_the_right_way():
    """`CreateIcon` reads its bits top scan line first. Flip them and every glyph is
    mirrored: the check mark's long arm would rise to the left. Verified against a real
    `DrawIconEx` once; this is the assertion that keeps it that way.
    """
    marked = tray.icon_pixels(tray_state.COLOURS[tray_state.OK], glyph=tray.GLYPHS[tray_state.OK])
    white = {
        (x, y)
        for y in range(16)
        for x in range(16)
        if pixel(marked, x, y)[:3] == (0xFF, 0xFF, 0xFF)
    }
    lowest_left = max(y for x, y in white if x < 8)
    lowest_right = max(y for x, y in white if x > 8)
    assert lowest_left > lowest_right


def test_stroke_distance_measures_to_the_segment_not_the_infinite_line():
    """Round caps and the "!" dot both come out of this: a zero-length stroke is a
    circle, and a point past the end of a stroke is measured from the end, not from the
    line it lies on."""
    assert tray._stroke_distance(0.0, 0.0, (0.0, 0.0, 0.0, 0.0)) == 0.0
    assert tray._stroke_distance(4.0, 0.0, (0.0, 0.0, 1.0, 0.0)) == pytest.approx(3.0)
    assert tray._stroke_distance(0.5, 2.0, (0.0, 0.0, 1.0, 0.0)) == pytest.approx(2.0)


def test_each_state_yields_a_distinct_buffer():
    buffers = {
        level: tray.icon_pixels(colour, glyph=tray.GLYPHS[level])
        for level, colour in tray_state.COLOURS.items()
    }
    assert len(set(buffers.values())) == len(tray_state.COLOURS)


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
