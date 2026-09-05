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
# Every task this module registers is devkit's own -- `schedule_health` finds them by
# their name prefix, not by this. Metadata, and deliberately not a parameter.
AUTHOR = "devkit"

DEFAULT_TIME_LIMIT = "PT1H"

# Midnight on a date already past. A `TimeTrigger` needs a start boundary, and a
# repeating job wants one that has definitely elapsed so the first repetition is due
# immediately rather than at some arbitrary future minute.
EPOCH_START = "2020-01-01T00:00:00"


def venv_home(python: str | Path) -> Path | None:
    """The base install a virtualenv interpreter defers to, or None for a real one.

    `pyvenv.cfg` sits one level above `Scripts/` (or `bin/`) and names the base install
    in `home`. Reading it is the only spelling that covers every builder, which is the
    point: the builders disagree about what the files in `Scripts/` even *are*, and
    `windowless` needs the answer rather than a guess about one builder's layout.
    """
    executable = Path(python)
    try:
        text = (executable.parent.parent / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "home" and value.strip():
            return Path(value.strip())
    return None


def windowless(python: str) -> str:
    """The interpreter a task's `<Command>` must name, given a console one.

    `pythonw.exe` is the same interpreter built as a GUI-subsystem app, so the scheduler
    allocates no console for it and nothing flashes on the desktop. Every installer
    wanted that and every installer carried its own copy of these two lines, each one
    deliberately not imported from the others on the grounds that the shared thing worth
    extracting was the *policy* rather than the code.

    That was wrong, and it took a uv-built venv to show why. The copies all resolved
    `pythonw.exe` *beside* the interpreter they were handed -- and inside a virtualenv
    that file is not an interpreter at all. It is a stub deferring to the base install
    named in `pyvenv.cfg`: CPython's is a copy that loads the base in-process, while
    **uv's is a trampoline that spawns it as a child**, and a child of a console-less
    parent is precisely what Windows hands a brand new visible console to. So the task
    was GUI-subsystem, the file really was named `pythonw.exe`, and a window opened
    anyway -- every fire of `devkit-global-tools`, the one job whose interpreter came
    from a `.venv`. The name was checked; the property it stands for was not.

    Resolving through `home` also settles a hazard `install-global-tools.interpreter`
    already warned about from the other end -- a box's `.venv` is deleted the moment its
    PR merges, taking the task's `<Command>` with it. The base install outlives every
    venv, and these jobs import nothing but the standard library, so it runs them.

    Identity off Windows, where there is no `pythonw.exe` to find.
    """
    candidates = []
    home = venv_home(python)
    if home is not None:
        candidates.append(home / "pythonw.exe")
    candidates.append(Path(python).parent / "pythonw.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return python


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


def boot_trigger(delay: str = "PT1M") -> str:
    """A trigger that fires once, shortly after the machine starts.

    A repeating `TimeTrigger` already survives a reboot -- Task Scheduler restores the
    repetition, and `StartWhenAvailable` catches up a fire the machine slept through --
    so this is not what makes a job come back. What it fixes is the *gap*: after a
    restart the next repetition can be a full interval away, and for a job whose subject
    is "a process that should be running", fifteen minutes of not running is the whole
    failure it exists to prevent.

    The delay is not decoration. At the instant a boot trigger would otherwise fire,
    the network stack is often not up, mapped drives are not mounted, and a job that
    probes either gets a wrong answer rather than a late one.

    Combine with another trigger by concatenating: `<Triggers>` holds an unordered
    choice, so `repeating_trigger(15) + boot_trigger()` is one valid document.
    """
    return (
        "    <BootTrigger>\n"
        f"      <Delay>{escape(delay)}</Delay>\n"
        "      <Enabled>true</Enabled>\n"
        "    </BootTrigger>\n"
    )


def logon_trigger(delay: str = "PT30S") -> str:
    """A trigger that fires when the user logs on.

    For the jobs whose subject is a *desktop* rather than the machine -- anything that
    puts a window or a tray icon in front of someone. A `BootTrigger` runs before there
    is a session to draw into; this one runs when there is.

    No `<UserId>`, for `task_xml`'s reason: naming a principal means naming a user id,
    and every spelling of that is a way to fail on someone else's machine. Without one
    the trigger belongs to whoever registered the task.
    """
    return (
        "    <LogonTrigger>\n"
        f"      <Delay>{escape(delay)}</Delay>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
    )


def task_xml(
    command: str,
    arguments: str,
    trigger: str,
    *,
    time_limit: str = DEFAULT_TIME_LIMIT,
    working_dir: str = "",
    enabled: bool = True,
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

    **`working_dir` is what lets a scheduled job write an artifact.** A task with no
    `<WorkingDirectory>` starts in `system32`, so anything resolving `logs/` from the
    cwd -- `log-wrap.py`, and every runner that follows the failure-artifact rule --
    writes its report into a Windows system directory, where it is both unfindable and
    likely unwritable. The two jobs that predate this compensated by passing every path
    as an absolute argument, which works and does not generalise: it makes each new job
    responsible for remembering, and the one that forgot is what this parameter was
    added for. `<Exec>` children are an ordered sequence like `<Settings>`, so it goes
    last, after `<Arguments>`.

    `<Author>` is the constant `AUTHOR`: a knob no caller turned still costs an argument
    slot. **`enabled=False` registers a task that exists and does not fire**, for an
    installer run while the jobs group is stood down -- a document flag, not a later
    `/Change /DISABLE`, whose gap a 15-minute `reconcile` can fire in.
    """
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{TASK_NS}">\n'
        "  <RegistrationInfo>\n"
        f"    <Author>{AUTHOR}</Author>\n"
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
        f"    <Enabled>{'true' if enabled else 'false'}</Enabled>\n"
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
        + (
            f"      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>\n"
            if working_dir
            else ""
        )
        + "    </Exec>\n"
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
