#!/usr/bin/env python3
"""Register a devkit background job as a Windows Scheduled Task, from XML.

Both of this workspace's unattended jobs -- `install-reconcile-task.py` and
`install-upgrade-schedule.py` -- registered themselves with `schtasks /Create /SC ...`,
and that is the whole reason this module exists: **the three settings that decide
whether a scheduled job on a laptop actually runs have no command-line flags at all.**
`schtasks.exe` cannot express any of them, so every task it creates silently inherits
the server-shaped defaults:

| Default | What it does on a laptop |
| --- | --- |
| `DisallowStartIfOnBatteries=true` | every fire while unplugged is **skipped** |
| `StopIfGoingOnBatteries=true` | a run in progress is **killed** when you unplug |
| `StartWhenAvailable=false` | a fire missed while asleep or off is **never caught up** |

Measured, not theorised: this workspace's reconcile task was found stopped for five
days with every box it manages leaking its port slot and volume set, and its daily
sibling loses a whole day's upgrade run for any night the lid is closed at 03:00. A
scheduled job that quietly does not run is the most expensive kind of broken, because
nothing anywhere goes red.

`/XML` is the only registration path that reaches those settings, which makes the
generated document -- not an argv -- the artifact worth testing. Every builder here is
pure and returns a string; `register` is the thin shell that writes it and calls
`schtasks`.

Two mechanical details that are easy to get wrong and fail confusingly:

- **The file must be UTF-16.** `schtasks /XML` honours the encoding the document
  declares, and a UTF-8 file declaring `encoding="UTF-16"` is rejected with a parse
  error that names neither.
- **`<Settings>` children are a schema sequence, not a set.** Order is not free, and a
  misordered document fails validation rather than being reordered. The order below was
  verified by registering it and reading the settings back, which is also what
  `tests/test_devkit_schtasks.py` pins.

Deliberately *not* set: `WakeToRun`. Waking a sleeping laptop at 03:00 to open
dependency PRs is worse than catching up on the next wake, which `StartWhenAvailable`
already handles.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from xml.sax.saxutils import escape

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# An hour is generous for either job and finite, which is the point. The default is
# three days, and `MultipleInstancesPolicy=IgnoreNew` means a single wedged run
# suppresses **every** later fire until the limit expires -- a fifteen-minute job that
# hangs once would then be silently dead for three days, which is indistinguishable
# from the failure this module exists to prevent.
DEFAULT_TIME_LIMIT = "PT1H"

# Midnight on a date already past. A `TimeTrigger` needs a start boundary, and a
# repeating job wants one that has definitely elapsed so the first repetition is due
# immediately rather than at some arbitrary future minute.
EPOCH_START = "2020-01-01T00:00:00"


def repeating_trigger(interval_minutes: int, start: str = EPOCH_START) -> str:
    """A trigger that fires every `interval_minutes`, forever.

    `<Repetition>` carries no `<Duration>` on purpose: the element is optional and its
    absence means "indefinitely", while any value present is a stopping point. A
    duration of one day looks harmless and turns the job off after a day.
    """
    return (
        "    <TimeTrigger>\n"
        f"      <StartBoundary>{escape(start)}</StartBoundary>\n"
        "      <Repetition>\n"
        f"        <Interval>PT{int(interval_minutes)}M</Interval>\n"
        "      </Repetition>\n"
        "      <Enabled>true</Enabled>\n"
        "    </TimeTrigger>\n"
    )


def daily_trigger(at: str, start_date: str = "2020-01-01") -> str:
    """A trigger that fires once a day at `at` (HH:MM, 24-hour)."""
    return (
        "    <CalendarTrigger>\n"
        f"      <StartBoundary>{escape(start_date)}T{escape(at)}:00</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay>\n"
        "        <DaysInterval>1</DaysInterval>\n"
        "      </ScheduleByDay>\n"
        "    </CalendarTrigger>\n"
    )


def task_xml(
    command: str,
    arguments: str,
    trigger: str,
    *,
    time_limit: str = DEFAULT_TIME_LIMIT,
    author: str = "devkit",
) -> str:
    """The full task document: one action, one trigger, and the settings that matter.

    No `<Principals>` block, which is a decision rather than an omission. Naming a
    principal means naming a user id, and every spelling of that is a way to fail on
    someone else's machine -- a renamed account, a domain that does not resolve, a
    localised builtin. Omitting it registers the task as whoever ran the installer,
    with the interactive logon type, which is what both jobs had anyway.

    `command` and `arguments` are separate elements, not one command line, because that
    is the shape `<Exec>` takes. Both are XML-escaped: a checkout path containing `&`
    is unusual and produces a document that fails to parse rather than a task that runs
    the wrong thing, but a generator that can emit invalid XML is one you cannot trust
    with a path you did not choose.
    """
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{TASK_NS}">\n'
        "  <RegistrationInfo>\n"
        f"    <Author>{escape(author)}</Author>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        f"{trigger}"
        "  </Triggers>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        f"    <ExecutionTimeLimit>{escape(time_limit)}</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        "  <Actions>\n"
        "    <Exec>\n"
        f"      <Command>{escape(command)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def register_argv(name: str, xml_path: Path) -> list[str]:
    """`schtasks` argv registering (or replacing) `name` from a document.

    `/F` so re-running an installer is an update rather than an error -- the natural
    thing to do after the checkout moves or a knob changes, and the only thing that
    makes these installers idempotent.
    """
    return ["schtasks", "/Create", "/TN", name, "/XML", str(xml_path), "/F"]


def write_task_file(xml: str, directory: Path | None = None) -> Path:
    """Write `xml` where `schtasks` can read it, in the encoding it declares.

    UTF-16 specifically -- see this module's docstring. `NamedTemporaryFile` is not used
    because it holds the handle open, and on Windows `schtasks` then cannot read the
    file it was pointed at.
    """
    target = Path(directory or tempfile.gettempdir()) / "devkit-task.xml"
    target.write_text(xml, encoding="utf-16")
    return target


def register(name: str, xml: str, run: Runner) -> tuple[bool, str]:
    """Write the document, register it, and clean up. `(ok, message)`.

    The temporary file is removed on every path including the failing one: it holds a
    full command line, and leaving copies of that in the temp directory is untidy in a
    way that eventually reads as a leak.
    """
    path = write_task_file(xml)
    try:
        result = run(register_argv(name, path))
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "schtasks failed").strip()
    return True, (result.stdout or f"registered {name}").strip()
