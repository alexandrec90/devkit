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

import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape, unescape

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# The exit codes every installer's `--check` answers with, and the only thing
# `installers.py` reads off one. `install-git-policy.py` spelled the contract first and
# `policy_runtime.py` carries its own copy of these three names; a job installer gets them
# from here so the two cannot drift apart.
CHECK_CURRENT = 0
CHECK_STALE = 1
CHECK_LEFT_ALONE = 2

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


def console(python: str) -> str:
    """`python.exe` beside `pythonw.exe`: the interpreter a job's *wrapped* command names.

    The inverse of `windowless`, and not an interchangeable preference: the task's own
    `<Command>` must be windowless, and the interpreter inside the wrapped argv must not
    be. `log-wrap.py` spawns it with `CREATE_NO_WINDOW`, which Windows ignores for a
    GUI-subsystem child, so a `pythonw.exe` there is left with no console and every
    process it spawns gets a visible one (`scripts/windowless-jobs.md`). Identity for a
    console interpreter, and for a `pythonw.exe` with no twin beside it.
    """
    if os.path.basename(python).lower() != "pythonw.exe":
        return python
    candidate = os.path.join(os.path.dirname(python), "python.exe")
    return candidate if os.path.isfile(candidate) else python


# The wrapper every scheduled job's command runs under, below the checkout it runs from.
LOG_WRAP = ("scripts", "log-wrap.py")


def logged(label: str, python: str, arguments: str, root: str | Path) -> str:
    """`arguments` -- a script and its own arguments, as the task would run them under
    `python` -- as the `<Arguments>` of a task that runs them under `log-wrap.py --always`.

    The wrapper is what puts a failed run on the harness-events ledger: one
    `scheduled-job-failed` event per failure, naming `logs/<slug>.failed.log`, which
    outlives the next run. Without it a job's failure is the scheduler's `Last Result`
    alone, and the next run overwrites that -- `fix_loop.job_findings` reads it once per
    fix pass, so a job failing at 22:30 and passing at 22:45 was never filed.
    `tests/test_installer_contract.py` holds every installer to it.

    `label` names the wrapper's files (`log_wrap.slug`), so it must not slug to the
    runner's own artifact, which the wrapper would overwrite with the captured console.
    `root` is the checkout: the task's `<WorkingDirectory>` must be the same one, because
    the wrapper resolves `logs/` from the cwd. `python` is the task's own windowless
    interpreter; the wrapped command runs its `console` twin.
    """
    wrapper = os.path.join(str(root), *LOG_WRAP)
    return f'"{wrapper}" --always "{label}" -- "{console(python)}" {arguments}'


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


def logon_trigger(delay: str = "PT30S") -> str:
    """A trigger that fires when the user logs on.

    For the jobs whose subject is a *desktop* rather than the machine -- anything that
    puts a window or a tray icon in front of someone -- and for closing the gap after a
    restart: the next repetition of a repeating job can be a full interval away, and every
    job here runs with the interactive token, so logon is the first moment it could run.

    Combine with another trigger by concatenating: `<Triggers>` holds an unordered
    choice, so `repeating_trigger(15) + logon_trigger()` is one valid document.

    No `<UserId>` here, and none is optional: `secured` adds the registering user's SID.
    A logon trigger with no user fires on *anyone's* logon, and only an administrator may
    register one -- a non-elevated `/Create` answers `ERROR: Access is denied.` (probed;
    so does a `BootTrigger`, which is why there is no builder for one). The three jobs
    that carried such a trigger could only ever be registered elevated, and every
    scheduled repair of them failed (9385b3d7).
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


# --- the check every installer answers -------------------------------------------
#
# Six installers each carried a `registered_command` that parsed `schtasks /Query /FO
# LIST /V` for a `Task To Run:` label (in two languages) and a `drifted` that asked one
# question of it: does the registered line still contain this checkout's script path.
# Four more had no check at all, only `--status`, which printed the scheduler's table and
# exited with whatever `schtasks` did. Nothing could drive all ten the same way, so the
# pass that keeps them current (`installers.py`) had nothing to drive.
#
# One implementation, and it compares documents rather than lines: `schtasks /Query /XML`
# returns the task as it was registered, and `task_xml` is the task as the installer would
# register it now, so the check is the fields of that document a re-register would
# change. Deliberately including the interpreter, which the old copies excused as noise:
# `scripts/CLAUDE.md` says the interpreter is part of "which checkout", and the venv
# trampoline `schedule_health.virtualenv_interpreter` reports is exactly a task whose
# interpreter drifted. A re-register from the installer is the fix for that, so the check
# has to see it.


@dataclass(frozen=True)
class Registration:
    """The fields of a task document a re-register can change."""

    command: str
    arguments: str
    enabled: bool
    security: str = ""


# --- who may replace a task --------------------------------------------------------
#
# A task document with no `<SecurityDescriptor>` takes its access from the process that
# registers it, and an **elevated** process makes the task Administrators': the
# registering user is left read access and nothing more. Every scheduled job here runs
# with the user's ordinary token, so from then on each `--yes` any of them makes on that
# task -- the fix pass's `maintain`, the daily installers job -- answers `ERROR: Access is
# denied.`, whichever task is running (5282d37c, 086329c7). Fixer sessions run elevated on
# this machine, so one `installers.py maintain` from a fixer was enough to lock three
# jobs. Reproduced with probe tasks: registered elevated without a descriptor, a
# non-elevated `/Create /F` is refused; with the one below it succeeds, and a running
# task re-registers itself. The access is set when the task is *created*: `/F` over an
# existing task keeps it, which is why `register` recreates one that has no descriptor.
#
# So every registration names the registering user in its descriptor, by SID -- the one
# spelling that survives a renamed account and a localised builtin -- and `run_check`
# reads a registration without it as stale, which is how an existing Administrators'
# task gets named rather than refusing every repair in silence.
WHOAMI_ARGV = ("whoami", "/user", "/fo", "csv", "/nh")
_SID = re.compile(r'^"[^"]*","(S-1-\d+(?:-\d+)+)"$')
_REGISTRATION_END = "</RegistrationInfo>"


def user_sid(run: Runner) -> str:
    """The SID of the user this process runs as, or "" when it cannot be read.

    "" registers the document as it is -- the behaviour before the descriptor existed --
    rather than failing an install over a lookup."""
    try:
        result = run(list(WHOAMI_ARGV))
    except OSError:
        return ""
    match = _SID.match((result.stdout or "").strip()) if result.returncode == 0 else None
    return match.group(1) if match else ""


def security_descriptor(sid: str) -> str:
    """Full access for SYSTEM, Administrators and `sid`: the user whose jobs maintain it."""
    return f"D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;FA;;;{sid})"


def secured(document: str, sid: str) -> str:
    """`document` as `sid` may register it: its descriptor in `<RegistrationInfo>`, and
    every logon trigger scoped to its logon (`logon_trigger` says why). Unchanged without
    a SID; each part is added once."""
    if not sid:
        return document
    document = _LOGON.sub(lambda match: _for_user(match.group(0), sid), document)
    if "<SecurityDescriptor>" in document or _REGISTRATION_END not in document:
        return document
    line = f"  <SecurityDescriptor>{security_descriptor(sid)}</SecurityDescriptor>\n  "
    return document.replace(_REGISTRATION_END, line + _REGISTRATION_END, 1)


_LOGON = re.compile(r"<LogonTrigger>.*?</LogonTrigger>", re.S)


def _for_user(trigger: str, sid: str) -> str:
    """One `<LogonTrigger>` with `<UserId>` before its `<Enabled>`, the order probed to
    register; unchanged when it names a user already."""
    if "<UserId>" in trigger:
        return trigger
    anchor = "<Enabled>" if "<Enabled>" in trigger else "</LogonTrigger>"
    return trigger.replace(anchor, f"<UserId>{sid}</UserId>\n      {anchor}", 1)


# A few fields out of a document that has exactly one shape -- `task_xml`'s, which is
# also what `schtasks /Query /XML` hands back for a task registered from it. A regex over
# that shape rather than an XML parser, for two reasons that point the same way: the
# parser refuses the `str` the pipe delivers (a UTF-16 declaration over 8-bit bytes, with
# CRCRLF line endings), and `xml.etree` on text a subprocess handed back is the thing
# ruff's S314 exists to flag. The `<Enabled>` that matters is the one under `<Settings>`;
# every trigger carries one too.
_EXEC = re.compile(r"<Exec>(.*?)</Exec>", re.S)
_SETTINGS = re.compile(r"<Settings>(.*?)</Settings>", re.S)
_ENTITIES = {"&quot;": '"', "&apos;": "'"}


def _field(block: str, tag: str) -> str | None:
    """The text of `<tag>` inside `block`, entities decoded; None when it is absent."""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    return None if match is None else unescape(match.group(1), _ENTITIES)


def parse_task(text: str) -> Registration | None:
    """`text` -- a task document, ours or the scheduler's -- as a `Registration`.

    None for anything that is not a task document: the `ERROR: The system cannot find
    the file specified.` a query for an unregistered task prints, an empty pipe, a
    localised message. Every one of those means "nothing is registered as this", which
    is what the caller reports for None.
    """
    action = _EXEC.search(text)
    if action is None:
        return None
    command = _field(action.group(1), "Command")
    if not command or not command.strip():
        return None
    arguments = _field(action.group(1), "Arguments") or ""
    settings = _SETTINGS.search(text)
    enabled = (_field(settings.group(1), "Enabled") if settings else None) or "true"
    security = (_field(text, "SecurityDescriptor") or "").strip()
    return Registration(
        command.strip(), arguments.strip(), enabled.strip().lower() == "true", security
    )


def query_xml_argv(name: str) -> list[str]:
    """The query `run_check` makes. `/XML` rather than `/FO LIST /V` because the
    document is locale-neutral and separates the command from its arguments, which the
    `Task To Run:` line joins back together with a space and truncates."""
    return ["schtasks", "/Query", "/TN", name, "/XML"]


def _same_path(left: str, right: str) -> bool:
    """Windows hands back whatever case a path was registered with."""
    return left.strip().strip('"').lower() == right.strip().strip('"').lower()


def drift(registered: Registration | None, expected: Registration) -> list[str]:
    """Why the registered task is not the one the installer would register now; [] when
    it is.

    Every reason is a thing `--yes` changes, so the list is exactly the set of repairs
    a re-register makes -- which is what lets `installers.py` run one without reading
    this. Enablement is compared because it is in the document: an installer registers a
    stood-down job disabled and an ordinary one enabled, so a job that is disabled with
    nobody having stood it down (the 471-missed-runs incident) is drift, and so is one
    somebody re-enabled by hand while the ledger still says off.
    """
    if registered is None:
        return ["nothing is scheduled"]
    reasons: list[str] = []
    if not _same_path(registered.command, expected.command):
        reasons.append(f"runs `{registered.command}`, not `{expected.command}`")
    if " ".join(registered.arguments.split()) != " ".join(expected.arguments.split()):
        reasons.append(f"runs with `{registered.arguments}`, not `{expected.arguments}`")
    if registered.enabled != expected.enabled:
        reasons.append(
            "is disabled, and nobody stood it down"
            if expected.enabled
            else "is enabled, and it was stood down"
        )
    if expected.security and registered.security != expected.security:
        reasons.append(
            f"is secured `{registered.security or 'by whoever registered it'}`, not "
            f"`{expected.security}` -- if an elevated shell registered it, only an elevated "
            "--yes can replace it"
        )
    return reasons


def run_check(name: str, document: str, run: Runner) -> tuple[int, str]:
    """`(exit code, message)` for an installer's `--check`, against the document its
    `--yes` would register.

    Taking the document rather than its pieces is the point: the check and the install
    read the same string, so an installer cannot pass its own check with one command line
    and register another. `CHECK_LEFT_ALONE` only for a document this module cannot read,
    which is a bug in the caller rather than a state of the machine.

    The document is compared `secured`, as `register` would register it.
    """
    expected = parse_task(secured(document, user_sid(run)))
    if expected is None:
        return CHECK_LEFT_ALONE, f"schedule: {name}'s own task document could not be parsed"
    result = run(query_xml_argv(name))
    registered = parse_task(result.stdout or "") if result.returncode == 0 else None
    reasons = drift(registered, expected)
    if reasons:
        return (
            CHECK_STALE,
            f"schedule: {name} {'; '.join(reasons)}. Re-run with --yes to (re)register it.",
        )
    return CHECK_CURRENT, f"schedule: {name} is registered as this checkout would register it."


def register(name: str, xml: str, run: Runner) -> tuple[bool, str]:
    """Write the document, register it, and clean up. `(ok, message)`.

    The temporary file is removed on every path including the failing one: it holds a
    full command line, and leaving copies of that in the temp directory is untidy in a
    way that eventually reads as a leak.

    Registered `secured`, so a task an elevated shell registers stays replaceable by the
    user's own jobs. A task's access is fixed when it is *created* -- `/F` over an
    existing one keeps the old access whatever the new document says -- so one registered
    without a descriptor is deleted first (`_clear_unsecured`); that is what lets an
    elevated `--yes` heal a task an elevated shell locked.
    """
    sid = user_sid(run)
    locked = bool(sid) and _clear_unsecured(name, run)
    path = write_task_file(secured(xml, sid))
    try:
        result = run(register_argv(name, path))
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "schtasks failed").strip()
        if locked:
            message += (
                f" -- {name} predates its security descriptor and this shell may not "
                "delete it, so an elevated shell registered it: run this --yes once from "
                "an elevated one"
            )
        return False, message
    return True, (result.stdout or f"registered {name}").strip()


def delete_argv(name: str) -> list[str]:
    return ["schtasks", "/Delete", "/TN", name, "/F"]


def _clear_unsecured(name: str, run: Runner) -> bool:
    """Delete `name` if it is registered without a security descriptor; True when that
    delete was refused.

    A running task survives its own deletion and re-creation (probed), which is what
    lets the fix pass do this to `devkit-fix-pass` from inside it."""
    result = run(query_xml_argv(name))
    held = parse_task(result.stdout or "") if result.returncode == 0 else None
    if held is None or held.security:
        return False
    return run(delete_argv(name)).returncode != 0
