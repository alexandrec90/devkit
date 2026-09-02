#!/usr/bin/env python3
"""A taskbar tray indicator for every devkit scheduled job.

Unattended jobs are invisible by construction: they run windowless, their stdout goes
nowhere, and the only sign one has stopped is a line at the start of the next session --
which requires someone to start a session. This is the always-on half of that. One icon,
coloured by the worst thing the scheduler is reporting, with the whole set behind a
right-click.

`schedule_health` decides what counts as a problem and `tray_state` decides how loud
each one is; this file is the `ctypes` that draws the result and nothing else.

**Stdlib only, like everything under `scripts/`.** There is no tray library in the
standard library, but there is `ctypes`, and Shell_NotifyIcon is four calls. A
dependency for this would be the first runtime dependency in the repo, in a process
started by Task Scheduler before any virtualenv is guaranteed to exist.

Windows only. `main` says so and exits 0 elsewhere rather than failing: a POSIX machine
running devkit is a supported thing, and a missing tray is not a fault there.

Run it directly to see it; `install-tray.py` registers it to start at logon.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tray_state

REPO_ROOT = Path(__file__).resolve().parents[1]

# Its own record, for the contract every devkit job satisfies. Written only when the
# tray cannot start, which is the one failure nobody would otherwise see -- a tray that
# is not there looks exactly like a tray reporting nothing wrong.
ARTIFACT = Path("logs/tray.log")

WINDOW_CLASS = "DevkitTrayWindow"
ICON_UID = 1

# The private message the icon sends this window. Anything from WM_APP up is ours.
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
TPM_RIGHTBUTTON = 0x0002
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
HWND_MESSAGE = -3

# `ShellExecuteW`'s nCmdShow: open the log in a normal window rather than minimised.
SW_SHOWNORMAL = 1

# Menu command ids. Jobs occupy `FIRST_JOB` upward, one per row, so the id a click
# reports is an index into the list the menu was built from.
CMD_REFRESH = 100
CMD_EXIT = 101
FIRST_JOB = 200

# How often to ask the scheduler, in milliseconds. Two minutes: the fastest devkit job
# runs every fifteen, so anything tighter is asking a question whose answer cannot have
# changed, and each ask spawns a `schtasks`.
POLL_MS = 120_000

# 16x16 is what the notification area asks for at 100% scale, and Windows downsamples a
# larger icon cleanly while it cannot invent detail for a smaller one.
ICON_SIZE = 16

# Annotated `Any` rather than left to inference. `ctypes.windll.<x>` is untyped, so the
# inferred type is `WinDLL | None` and every one of the twenty-odd calls below becomes a
# union-attr error -- twenty suppressions for one fact the `os.name` guard already states
# and `main` already enforces by refusing to run off Windows.
user32: Any = ctypes.windll.user32 if os.name == "nt" else None
shell32: Any = ctypes.windll.shell32 if os.name == "nt" else None
kernel32: Any = ctypes.windll.kernel32 if os.name == "nt" else None

# Every function here that returns a handle, with the type it really returns.
#
# **This is not tidiness.** A `ctypes` function with no declared `restype` returns
# `c_int` -- 32 bits -- and on 64-bit Windows every one of these returns a 64-bit
# pointer. The high half is silently discarded, so the truncated value is a handle to
# nothing: `CreateWindowExW` appears to succeed and the icon then attaches to a window
# that does not exist. It fails as *nothing happening*, with no error anywhere, which is
# the worst way for a tray to fail because that is also what a healthy quiet tray does.
_RESTYPES = {
    "CreateIcon": wintypes.HICON,
    "CreateWindowExW": wintypes.HWND,
    "CreatePopupMenu": wintypes.HMENU,
    "DefWindowProcW": ctypes.c_longlong,
}


def _declare() -> None:
    """Pin the signatures above. Idempotent, and a no-op off Windows.

    `argtypes` matters for the same reason `restype` does and fails more loudly: with
    none declared, `ctypes` guesses each argument's width from the Python value, so a
    module handle large enough to need 64 bits raises `OverflowError: int too long to
    convert` at the call. That is the better failure of the two -- the undeclared
    *return* is the silent one -- but both come from the same omission.
    """
    if user32 is None:
        return
    for name, restype in _RESTYPES.items():
        getattr(user32, name).restype = restype
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateIcon.argtypes = [
        wintypes.HINSTANCE,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BYTE,
        wintypes.BYTE,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_size_t,
        wintypes.LPCWSTR,
    ]
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.LPVOID,
    ]
    # The one that is easy to forget, because it is only reached for messages this
    # window does not handle -- so it fails on the ordinary background traffic rather
    # than on anything the tray does, and the error arrives as "exception ignored in
    # callback" from inside the message loop where it cannot be raised.
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    # `SetTimer` takes and returns a UINT_PTR; the default `c_int` truncates the id on
    # the way back, which only matters if the timer is ever cancelled by handle.
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    # These two take `self.hwnd`, which is a real 64-bit handle now that
    # `CreateWindowExW` returns one. Undeclared, they are the same
    # `OverflowError: int too long to convert` this module has already produced twice.
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetMessageW.argtypes = [wintypes.LPVOID, wintypes.HWND, wintypes.UINT, wintypes.UINT]
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]


class NOTIFYICONDATA(ctypes.Structure):
    """The `NOTIFYICONDATAW` the shell expects.

    Declared to the size Windows 2000 and later use. `szTip` is 128 wide here (the
    version that came with balloon support); the older 64-wide layout is selected by
    `cbSize`, so getting that field wrong is how this fails silently rather than loudly.
    """

    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    )


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    )


def icon_pixels(colour: tuple[int, int, int], size: int = ICON_SIZE) -> bytes:
    """A solid square of `colour`, as the BGRA rows `CreateIcon` takes.

    A square rather than a glyph, and that is a decision rather than laziness: at 16
    pixels a shape carries no information a colour does not, and the notification area
    is the one place where a reader is identifying something from across a desk. The
    colours differ in brightness as well as hue -- see `tray_state.COLOURS`.

    Bottom-up row order is DIB convention, and irrelevant to a solid fill; it is stated
    because it stops being irrelevant the moment someone draws a shape here.
    """
    red, green, blue = colour
    return bytes((blue, green, red, 0xFF)) * (size * size)


def make_icon(colour: tuple[int, int, int], size: int = ICON_SIZE):
    """An `HICON` of one colour, or None when it could not be made.

    The AND mask is all zeros, meaning "every pixel opaque". With a 32-bit XOR bitmap
    Windows uses the alpha channel, and the mask only has to not subtract from it.
    """
    if user32 is None:
        return None
    mask = b"\x00" * (size * size // 8)
    pixels = icon_pixels(colour, size)
    handle = user32.CreateIcon(None, size, size, 1, 32, mask, pixels)
    return handle or None


class Tray:
    """The icon, its window, and the poll timer.

    One object rather than module globals because the window procedure has to reach the
    current state from inside a C callback, and a bound method is the least surprising
    way to give it one. The `WNDPROC` instance is kept on `self` deliberately: it is the
    only Python reference to the callback Windows holds a raw pointer to, and letting it
    be collected is a crash in a message loop with no traceback.
    """

    def __init__(self, poll_ms: int = POLL_MS):
        self.poll_ms = poll_ms
        self.hwnd = None
        self.icons: dict[str, int] = {}
        self.states: list[tray_state.JobState] = []
        self.proc = WNDPROC(self._on_message)

    # --- drawing -------------------------------------------------------------

    def icon_for(self, level: str):
        """One `HICON` per colour, made once. Icons are a limited resource, and a tray
        that made a new one every poll would leak three an hour, forever."""
        if level not in self.icons:
            handle = make_icon(tray_state.COLOURS[level])
            if handle is None:
                return None
            self.icons[level] = handle
        return self.icons[level]

    def notify(self, action: int) -> bool:
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = self.hwnd
        data.uID = ICON_UID
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self.icon_for(tray_state.overall(self.states)) or 0
        data.szTip = tray_state.tooltip(self.states)
        return bool(shell32.Shell_NotifyIconW(action, ctypes.byref(data)))

    def poll(self) -> None:
        """Ask the scheduler and redraw. Never raises into the message loop.

        A tray that died on one bad `schtasks` answer would vanish from the notification
        area, which reads as "nothing is wrong" -- the opposite of what had happened.
        """
        try:
            self.states = tray_state.refresh()
        except (OSError, ValueError, KeyError):
            # Narrow rather than bare, and these three are the reachable ones: the
            # scheduler spawn (OSError), a `schtasks` answer that does not parse
            # (ValueError), and a state with no colour (KeyError). Anything else is a
            # defect that should crash loudly here rather than leave a tray quietly
            # reporting a machine it has stopped reading.
            self.states = []
        if self.hwnd:
            self.notify(NIM_MODIFY)

    # --- the menu ------------------------------------------------------------

    def show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        for index, item in enumerate(self.states):
            user32.AppendMenuW(menu, MF_STRING, FIRST_JOB + index, tray_state.menu_label(item))
        if not self.states:
            user32.AppendMenuW(menu, MF_STRING, 0, "no scheduled jobs registered")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_REFRESH, "Refresh now")
        user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "Exit")

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Required before TrackPopupMenu, and the reason a tray menu otherwise refuses to
        # close when you click away from it.
        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)

    def open_artifact(self, index: int) -> None:
        """Open one job's own record, if it has one and it exists."""
        if not 0 <= index < len(self.states):
            return
        artifact = self.states[index].artifact
        if not artifact:
            return
        path = REPO_ROOT / artifact
        if path.is_file():
            # `ShellExecuteW` rather than `os.startfile`: the same effect, through a
            # handle this module already holds, and without a lint suppression that
            # every future reader would have to re-justify.
            shell32.ShellExecuteW(None, "open", str(path), None, None, SW_SHOWNORMAL)

    # --- the loop ------------------------------------------------------------

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == WM_TIMER:
            self.poll()
            return 0
        if message == WM_TRAY and lparam in (WM_RBUTTONUP, WM_LBUTTONDBLCLK):
            self.show_menu()
            return 0
        if message == WM_COMMAND:
            command = wparam & 0xFFFF
            if command == CMD_EXIT:
                user32.DestroyWindow(hwnd)
            elif command == CMD_REFRESH:
                self.poll()
            elif command >= FIRST_JOB:
                self.open_artifact(command - FIRST_JOB)
            return 0
        if message == WM_DESTROY:
            self.notify(NIM_DELETE)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self) -> int:
        _declare()
        instance = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = self.proc
        cls.hInstance = instance
        cls.lpszClassName = WINDOW_CLASS
        if not user32.RegisterClassExW(ctypes.byref(cls)):
            return 2
        # A message-only window: it has no presence on screen, cannot be alt-tabbed to,
        # and exists solely to receive the icon's callbacks. The alternative is a hidden
        # top-level window, which shows up in enumerations and confuses window managers.
        self.hwnd = user32.CreateWindowExW(
            0,
            WINDOW_CLASS,
            WINDOW_CLASS,
            0,
            0,
            0,
            0,
            0,
            wintypes.HWND(HWND_MESSAGE),
            None,
            instance,
            None,
        )
        if not self.hwnd:
            return 2

        self.poll()
        if not self.notify(NIM_ADD):
            return 2
        user32.SetTimer(self.hwnd, 1, self.poll_ms, None)

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        return 0


def write_artifact(text: str, root: Path = REPO_ROOT) -> None:
    path = root / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--once",
        action="store_true",
        help="print what the tray would show and exit, without drawing anything",
    )
    parser.add_argument(
        "--poll-seconds", type=int, default=POLL_MS // 1000, help="seconds between checks"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.once:
        found = tray_state.refresh()
        print(tray_state.tooltip(found))
        for item in found:
            print("  " + tray_state.menu_label(item))
        return 0

    if os.name != "nt":
        # Not a failure. devkit runs on POSIX and the tray is the one part of it that
        # cannot; saying so and exiting 0 keeps a scheduled caller green.
        print("tray: Windows only -- nothing to show here")
        return 0

    code = Tray(poll_ms=max(args.poll_seconds, 1) * 1000).run()
    if code:
        write_artifact(
            "# source: scripts/tray.py\n"
            "The tray could not create its window or register its icon. A tray that is "
            "not running looks exactly like a tray reporting nothing wrong, which is why "
            "this file exists.\n"
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
