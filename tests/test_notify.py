"""The task toast: `scripts/notify.py`, and `notify-wrap.py`'s use of it.

There was no test file here at all, which is the whole reason this needed fixing: the
toast was a `from win11toast import toast` behind a bare `except Exception: pass`, that
package was never a dependency of devkit or of any generated project, and so **every
call was a silent no-op** for as long as the wrapper has existed. Nothing was red
anywhere, because a swallowed import and a delivered toast are the same exit code.

So these tests are written against the two properties that failure had: that the toast
needs nothing installed, and that a failure to show one is *said out loud* rather than
swallowed. `test_notify_wrap_propagates_the_wrapped_exit_code` in `test_self_hosting.py`
already covers the wrapper's exit-code contract and is not repeated here.
"""

import ast
import subprocess
import sys
from xml.etree import ElementTree

import pytest
from support import REPO_ROOT, load_script

notify = load_script("scripts/notify.py")


def test_the_toast_needs_no_third_party_import():
    """The constraint that broke this, asserted directly rather than in prose.

    devkit's runtime dependency list is empty by contract, so any import here that is
    not stdlib is unreachable at runtime — which is exactly how the previous version
    failed.
    """
    source = (REPO_ROOT / "scripts" / "notify.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported
    # `ast.walk`, not a scan of top-level lines: the import that broke this was
    # `from win11toast import toast` *inside* the function, four spaces in.
    assert imported, "no imports found - the walk is looking at the wrong thing"


def test_the_payload_is_well_formed_xml():
    parsed = ElementTree.fromstring(  # noqa: S314 - the payload is built one line below
        notify.toast_xml("Lint: Everything", "Passed in 3s")
    )
    assert [element.text for element in parsed.iter("text")] == [
        "Lint: Everything",
        "Passed in 3s",
    ]


@pytest.mark.parametrize(
    "title",
    [
        "Test: Harness Hook Tests — free",
        "Docker: Prune & Compact VHDX",
        "Preview: <not a tag>",
        "Ship: \"quoted\" and 'quoted'",
    ],
)
def test_a_real_task_label_survives_the_payload(title):
    """Live labels carry `&`, `<` and both kinds of quote.

    Unescaped, `LoadXml` rejects the whole document and the toast is lost — the same
    silent nothing this file exists to stop, arriving from the other end.
    """
    parsed = ElementTree.fromstring(notify.toast_xml(title, "Passed in 1s"))  # noqa: S314 - our own payload
    assert next(parsed.iter("text")).text == title


def test_the_interpreter_is_windows_powershell_not_pwsh():
    """PowerShell 7 cannot load a WinRT type, so `powershell` on PATH is not enough.

    On a machine where pwsh shadows it, resolving by name gets "Unable to find type
    [Windows.UI.Notifications.ToastNotificationManager…]" and no toast.
    """
    resolved = notify.powershell_path()
    assert resolved.endswith(r"System32\WindowsPowerShell\v1.0\powershell.exe")
    assert "pwsh" not in resolved


def test_a_missing_interpreter_is_reported_not_swallowed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(notify, "powershell_path", lambda: r"C:\nope\powershell.exe")

    assert notify.notify("Lint: Everything", "Passed in 3s") is False
    assert "notify:" in capsys.readouterr().err


def test_a_failing_toast_is_reported_not_swallowed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(notify.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        notify.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "CreateToastNotifier failed"),
    )

    assert notify.notify("Lint: Everything", "Failed (3s)") is False
    assert "CreateToastNotifier failed" in capsys.readouterr().err


def test_a_crashing_toast_never_reaches_the_wrapped_task(monkeypatch, capsys):
    """The one property the bare `except` had right, kept: a toast cannot fail a task."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(notify.os.path, "exists", lambda _path: True)

    def explode(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("powershell.exe", notify.TIMEOUT_SECONDS)

    monkeypatch.setattr(notify.subprocess, "run", explode)

    assert notify.notify("Lint: Everything", "Passed in 3s") is False
    assert "TimeoutExpired" in capsys.readouterr().err


def test_the_toast_is_spawned_windowless_and_with_a_timeout(monkeypatch):
    """A console child of a console-less parent gets its own visible window.

    Every task completion spawns this, so an unflagged spawn is a window that flashes up
    on each one; an untimed one is a task that never finishes reporting that it finished.
    """
    seen = {}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(notify.os.path, "exists", lambda _path: True)

    def record(cmd, **kwargs):
        seen.update(kwargs, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notify.subprocess, "run", record)
    assert notify.notify("Lint: Everything", "Passed in 3s") is True

    assert seen["creationflags"] == notify.NO_WINDOW
    assert seen["timeout"] == notify.TIMEOUT_SECONDS
    assert seen["cmd"][1:4] == ["-NoProfile", "-NonInteractive", "-Command"]


def test_the_text_travels_in_the_environment_not_the_argv(monkeypatch):
    """A label with a quote in it cannot be pasted into a PowerShell string literal."""
    seen = {}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(notify.os.path, "exists", lambda _path: True)

    def record(cmd, **kwargs):
        seen.update(kwargs, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notify.subprocess, "run", record)
    notify.notify("Docker: Prune & Compact VHDX", "Passed in 3s")

    assert "Prune" not in " ".join(seen["cmd"])
    assert "Prune &amp; Compact" in seen["env"]["DEVKIT_TOAST_XML"]
    assert seen["env"]["DEVKIT_TOAST_APPID"] == notify.APP_ID


def test_it_no_ops_quietly_off_windows(monkeypatch, capsys):
    """CI is Linux, and a toast that cannot exist there is not a failure to report."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert notify.notify("Lint: Everything", "Passed in 3s") is False
    assert capsys.readouterr().err == ""
