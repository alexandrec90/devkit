"""A pytest plugin that runs devkit's own suite as if the host were POSIX.

devkit is developed on Windows and gated on `ubuntu-latest`, so a test that asserts a
Windows-only branch without *forcing* that branch passes locally and fails in CI. That
gap is the routine way a green push became a red PR — the push gate closed the "nothing
ran mypy or a test" half, and this closes the "it ran, on the wrong platform" half.

**What it fakes, and why only this much.** Two things decide a Windows branch here:

- `sys.platform`, which is patched on the real `sys` module. Patching the real module
  rather than handing each caller a proxy is what lets a test's own
  `monkeypatch.setattr(sys, "platform", "win32")` still win: monkeypatch runs later and
  restores afterwards, so a test that deliberately exercises the Windows branch keeps
  doing so, and only a test that never said which platform it meant is affected.
- Every module-level `WINDOWS = os.name == "nt"` constant, flipped to `False` across the
  modules already imported. Fifteen modules carry one.

`os.name` itself is deliberately **not** patched, and cannot be: `pathlib.Path` picks its
concrete class off `os.name` at construction, so a faked one makes every `Path` a
`PosixPath`, which raises `UnsupportedOperation` on Windows the moment anything joins to
it. A branch written as a bare `os.name == "nt"` is therefore invisible here; one written
against the module's `WINDOWS` constant is not, which is the reason to prefer the constant.

**What it cannot reach at all.** A hard-coded Windows path literal — `Path(r"C:\\py\\x.exe")`
— is split into components by Windows and left as one filename by POSIX, so the branch
under it never runs off Windows. That is a real class (it caused two of the CI failures
this plugin was written after) and it is *not* gated: the parsing is fixable here, but the
`exists()` underneath it is not, since Windows resolves a backslash path that Linux would
not, and a check that answered "does not exist" for real interpreter paths would be wrong
far more often than right. `tests/support.py`'s `windows_layout` is the convention instead.

A **machine-capability lookup** is the second class, and it is the one that reads as
covered when it is not. `shutil.which` answers about the box the suite is running on,
which no amount of faked platform changes: `scripts/agent_tabs.py`'s `open_agent` returns
early when neither `wt.exe` nor `wt` is on `PATH`, true on `ubuntu-latest` and false on a
Windows dev box, so the branch below it simply does not run there. A test that asserted on
what that branch recorded went green through the entire local push gate — lint, suite,
hook tests and this rehearsal — and failed on PR 329 with an `IndexError` on an empty call
list. The rehearsal does not vouch for a `which()`-gated branch; stub the lookup in the
test, as `test_the_profile_is_read_from_the_machine_rather_than_guessed` now does.

Run it through `scripts/posix-rehearsal.py`, never by hand. That wrapper puts this file on
`PYTHONPATH` before pytest starts, which `-p` needs, and it owns the other half of the
mechanism: the ledger of tests allowed to fail under POSIX. The ledger lives there rather
than here because applying it needs an xfail marker, which the structure gate counts as a
skipped test — which it is not, and arguing that in a suppression is worse than reading
two summary lines out of the run the wrapper already captures.

**Deliberately not a `conftest.py`.** `tests/support.py` explains why this directory
cannot have one — the vendored `scripts/hooks/tests/conftest.py` would race it for the
top-level module name. A `-p`-loaded plugin has a name of its own and cannot collide.

**`_plugin` is in the filename for the same class of reason.** `support.load_script`
registers a hyphen-named script under its stem with the hyphens swapped, so
`scripts/posix-rehearsal.py` occupies `sys.modules["posix_rehearsal"]` — and whichever of
the two loaded second was silently handed the other. The suffix is what keeps the wrapper
and the plugin distinct modules.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from typing import Any

import pytest

# The value `sys.platform` carries on the runner this suite is gated on. Any non-Windows
# value would do; the CI one is the honest choice because it is the answer being rehearsed.
POSIX_PLATFORM = "linux"

# What `windows_exists` asks the kernel, as `preview-ui-host.pid_alive` does.
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102
ERROR_ACCESS_DENIED = 5


def windows_exists(pid: int, kernel32: Any = None) -> bool:
    """Whether `pid` is a running process, asked of the Windows kernel; `PermissionError`
    for one this user may not open, as POSIX's signal 0 raises it."""
    if kernel32 is None:
        # `Any`: `windll` exists only on Windows, which is the only host that asks, and
        # mypy checks this file as Linux too.
        ctypes: Any = importlib.import_module("ctypes")
        kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        if kernel32.GetLastError() == ERROR_ACCESS_DENIED:
            raise PermissionError(pid)
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def signal_zero_probe(
    kill: Callable[[int, int], None], exists: Callable[[int], bool]
) -> Callable[[int, int], None]:
    """`os.kill` for a POSIX branch running on a real Windows host.

    Signal 0 is POSIX's "does it exist"; on Windows it is `CTRL_C_EVENT`, so `os.kill`
    turns it into `GenerateConsoleCtrlEvent` -- Ctrl+C to every process on the console.
    `preview-ui-host.pid_alive`'s POSIX branch did exactly that under the rehearsal, and
    the rehearsal and the shell that started it died a second later with no output. Here
    signal 0 asks `exists` and sends nothing; any other signal is the real one.
    """

    def probe(pid: int, sig: int) -> None:
        if sig != 0:
            kill(pid, sig)
        elif not exists(pid):
            raise ProcessLookupError(pid)

    return probe


def windows_constants(modules: dict[str, object]) -> list[str]:
    """Names of modules carrying a module-level `WINDOWS` that is currently `True`.

    Taken off the live module table rather than a written list, so a sixteenth module
    gaining the constant is covered without anyone remembering this file.
    """
    found = []
    for name, module in modules.items():
        if getattr(module, "WINDOWS", None) is True:
            found.append(name)
    return sorted(found)


# --- the pytest hooks ----------------------------------------------------------------


class PosixRehearsal:
    """Holds the flip so it can be re-asserted and undone.

    The two halves have deliberately different lifetimes.

    `WINDOWS` is flipped once after collection and re-asserted before each test. It is a
    plain attribute write, so a test that *reassigns* the constant rather than
    monkeypatching it would leak a `True` into every test after it — and a rehearsal
    that passes or fails on collection order is worse than none.

    `sys.platform` is faked around the **test body only**, and that is not a nicety:
    pytest's own `tmp_path` factory branches on it, and under a faked one it takes the
    POSIX path and dies on `os.getuid`, which Windows does not have. Every test taking a
    `tmp_path` then errors in setup — 2155 of them here — which looks like a catastrophic
    rehearsal rather than a broken one. Setup and teardown therefore run on the honest
    platform, and only the code under test sees the faked one.
    """

    def __init__(self) -> None:
        # Read once, at configure time, and never again: a test that monkeypatches
        # `sys.platform` records the *faked* value and its teardown puts that back after
        # `restore`, so re-reading it per test took "linux" for the host.
        self.real_platform = sys.platform
        self.flipped: list[str] = []
        self.real_kill: Callable[[int, int], None] | None = None

    def apply(self) -> None:
        self.flipped = windows_constants(dict(sys.modules))
        self.flip_constants()

    def flip_constants(self) -> None:
        for name in self.flipped:
            # `Any`, because the attribute is one mypy cannot know a `ModuleType` has --
            # and a `type: ignore` here would be the wrong shape of claim: the attribute
            # genuinely may not exist, which is what the guard below is for.
            module: Any = sys.modules.get(name)
            if module is not None and getattr(module, "WINDOWS", None) is True:
                module.WINDOWS = False

    def honest_platform(self) -> None:
        """The host's platform back, whatever a previous test's teardown left."""
        sys.platform = self.real_platform

    def fake_platform(self) -> None:
        if self.real_platform == "win32" and self.real_kill is None:
            self.real_kill = os.kill
            os.kill = signal_zero_probe(os.kill, windows_exists)
        sys.platform = POSIX_PLATFORM

    def restore(self) -> None:
        sys.platform = self.real_platform
        if self.real_kill is not None:
            os.kill = self.real_kill
            self.real_kill = None


def pytest_configure(config):
    config._posix_rehearsal = PosixRehearsal()


def pytest_collection_finish(session):
    """Flip after collection, because collection is what imports the modules to flip.

    Every test module reaches its `load_script` calls at import time, so by the end of
    collection the module table holds everything the suite is going to exercise. A module
    imported later, inside a test body, is not flipped — the constant is read off
    `sys.modules` here, not hooked.
    """
    session.config._posix_rehearsal.apply()


def pytest_runtest_setup(item):
    rehearsal = getattr(item.config, "_posix_rehearsal", None)
    if rehearsal is not None:
        rehearsal.honest_platform()
        rehearsal.flip_constants()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Fake the platform for the duration of the test body, and only that.

    A wrapper rather than a setup hook because the fixtures have to be built on the real
    platform — see `PosixRehearsal`. `finally` rather than a teardown hook because a test
    that raises must still hand the honest platform back to pytest's own reporting.
    """
    rehearsal = getattr(item.config, "_posix_rehearsal", None)
    if rehearsal is None:
        yield
        return
    rehearsal.fake_platform()
    try:
        yield
    finally:
        rehearsal.restore()


def pytest_sessionfinish(session, exitstatus):
    rehearsal = getattr(session.config, "_posix_rehearsal", None)
    if rehearsal is not None:
        rehearsal.restore()
