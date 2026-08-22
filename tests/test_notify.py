"""Tests for `scripts/notify.py` — the toast every VS Code task ends with.

Its whole contract is that it must never be the reason a task fails. It is called from
`notify-wrap.py` after the wrapped command has already run and its exit code is already
decided, so an exception raised here would turn a passing task red *after* the work
succeeded — the least diagnosable failure shape in the harness. Hence the bare
`except Exception`, and hence the tests below, which exist to keep the swallow honest:
it must swallow a broken toast, and it must not swallow the call itself.

`win11toast` is deliberately not installed (see the mypy override in `pyproject.toml`),
so the import path is exercised with a stub in `sys.modules` rather than the real
dependency.
"""

from __future__ import annotations

import sys
import types

import pytest
from support import load_script

notify_script = load_script("scripts/notify.py")


@pytest.fixture
def fake_win11toast(monkeypatch):
    """A stub `win11toast` recording what it was asked to show."""
    calls: list[tuple[str, str]] = []
    module = types.ModuleType("win11toast")
    module.toast = lambda title, message: calls.append((title, message))
    monkeypatch.setitem(sys.modules, "win11toast", module)
    return calls


def test_it_no_ops_off_windows(monkeypatch, fake_win11toast):
    """The scripts calling this run in CI too, on Linux runners with no toast surface."""
    monkeypatch.setattr(notify_script.sys, "platform", "linux")
    notify_script.notify("title", "message")
    assert fake_win11toast == []


def test_it_toasts_on_windows(monkeypatch, fake_win11toast):
    monkeypatch.setattr(notify_script.sys, "platform", "win32")
    notify_script.notify("Lint", "passed")
    assert fake_win11toast == [("Lint", "passed")]


def test_a_broken_toast_never_reaches_the_caller(monkeypatch):
    """The regression this guards: a task that had already passed reported a failure
    because the notification backend raised."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("WinRT is unavailable in this session")

    module = types.ModuleType("win11toast")
    module.toast = explode
    monkeypatch.setitem(sys.modules, "win11toast", module)
    monkeypatch.setattr(notify_script.sys, "platform", "win32")

    notify_script.notify("Lint", "passed")  # must not raise


def test_a_missing_dependency_never_reaches_the_caller(monkeypatch):
    """The dependency is optional by design, so its absence is the normal case."""
    monkeypatch.setitem(sys.modules, "win11toast", None)
    monkeypatch.setattr(notify_script.sys, "platform", "win32")

    notify_script.notify("Lint", "passed")  # must not raise
