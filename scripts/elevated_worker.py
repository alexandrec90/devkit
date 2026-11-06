#!/usr/bin/env python3
"""The one thing devkit runs as an administrator: a fixed list of verbs that take no input.

Merged code here is unreviewed, so a dispatcher that ran tasks as admin would hand one bad
merge the machine. This is the opposite shape. Task Scheduler runs a *copy* of this file
every five minutes (`install-elevated-worker.py`, task `devkit-elevated`); an unelevated
process may only leave an empty file in the spool, and **the file's name is the entire
message**. What the worker does for a name is the literal argv in `VERBS`.

What a change here has to keep, each held by a test in `tests/test_elevated_worker.py`:

- **Nothing unelevated reaches an elevated process.** A request's content is never opened.
  Only an exact name selects a verb, and anything else in the spool is deleted, logged and
  ignored. `main` takes no arguments, and this file reads neither `sys.argv` nor the
  environment, because the task runs in the logged-on user's session and so with their
  environment, which any unelevated process can set in `HKCU\\Environment`. That is why
  every program below is a literal path rather than `%SystemRoot%`, and every child gets
  `CHILD_ENV` instead of inheriting.
- **The spool is hostile.** The user can write in it, so nothing in it is followed.
  `cycle` refuses outright if the spool is a reparse point, because deleting "anything
  else" through a junction would be an administrator deleting whatever it points at.
  Nothing is deleted recursively, for the same reason. `SENTINEL` is why the junction
  cannot appear in the first place: Windows only lets an empty directory become a mount
  point, and the user may not delete that file.
- **Everything written is under `ROOT`.** No tree of the user's is touched, which is what
  keeps this out of `elevated_writes.py`'s findings.
- **A loop cannot reboot a PC.** `every` is per verb, and the record is in the admin-only
  folder where an unelevated bug cannot reset it. The window is written *before* a verb
  runs, and a verb whose window cannot be written does not run.

No import from the rest of devkit: this runs from a copy, under an interpreter whose
`sys.path` holds the standard library and nothing else.

## The verbs, and why each is here

Measured 2026-11-06 on a Windows 10 workstation with `sc qc` and `sc sdshow`. Both
services grant interactive users `CCLCSWLOCRRC`: query, not `RP` (start) or `WP` (stop).

| Verb | Target | Why it needs this worker |
| --- | --- | --- |
| `restart-wsl-service` | `WSLService` (`wslservice.exe`, LocalSystem, auto) | stopping it is an administrator's. `wsl --shutdown` needs no elevation and is not this: it cannot end a service stuck in STOP_PENDING, which is the case that needed a person (`collector_tasks.WSL_STUCK`). So this ends the process, as `docker-maint.py recover-wsl` does, because `sc stop` is refused while a stop is pending |
| `restart-docker-service` | `com.docker.service` (Docker Desktop Service, LocalSystem, manual) | start and stop are an administrator's. It is not the engine, which is `com.docker.backend`, a process of the user's: Desktop restarts stay in `collectors.py`, unelevated |
| `reboot-when-idle` | the machine | **not elevation.** `SeShutdownPrivilege` is in the unelevated token, so `shutdown.exe` runs without this. It is here for what elevation does buy: the idle gate and a rate limit in a folder no unelevated bug can reset. Nothing else in `scripts/` may spawn `shutdown.exe`, and a test holds that |

Starting Docker Desktop is deliberately not a verb: from here it would run the backend
elevated, and that is the failure `docker-maint.desktop_launch` exists to avoid. After
`restart-wsl-service` it is `collectors.py`'s to bring back. The worker never runs `docker`
either, since that loads CLI plugins from the user's `.docker`, so `busy_images` reads the
process list instead.

Windows-only by nature; on any other platform `main` says so and exits 0.

Tested in `tests/test_elevated_worker.py`.
"""

from __future__ import annotations

import csv
import ctypes
import io
import json
import os
import stat
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

ROOT = r"C:\ProgramData\devkit"
SPOOL = ROOT + r"\requests"
HOME = ROOT + r"\elevated"
# Installed in the spool, undeletable by the user, so the spool is never empty and so can
# never be turned into a mount point (see the module docstring).
SENTINEL = "spool.keep"
REQUEST_SUFFIX = ".request"
LIMITS_NAME = "rate-limit.json"
LOG_NAME = "worker.log"
# A bound, not a retention policy: the spool is the user's, so what it can make this file
# say must not be able to fill the disk.
LOG_BYTES = 256 * 1024

SYSTEM32 = r"C:\Windows\System32"
SC = SYSTEM32 + r"\sc.exe"
SHUTDOWN = SYSTEM32 + r"\shutdown.exe"
TASKKILL = SYSTEM32 + r"\taskkill.exe"
TASKLIST = SYSTEM32 + r"\tasklist.exe"
# What every child runs with. Not an inherited environment, for the module docstring's
# reason; `SystemRoot` is the one variable these programs cannot start without.
CHILD_ENV = {"SystemRoot": r"C:\Windows", "SystemDrive": "C:"}

WSL_SERVICE = "WSLService"
DOCKER_SERVICE = "com.docker.service"

# `sc`'s own exit codes, which are not failures for a restart of a service in that state.
ALREADY_RUNNING = 1056
NOT_STARTED = 1062

# Rate limits, per verb. A restart ends every WSL distro or Docker's helper, so half an
# hour is a recovery and not a loop; a day is the most reboots a bug may cost.
RESTART_EVERY = 30 * 60
REBOOT_EVERY = 24 * 60 * 60

# `reboot-when-idle`'s gate.
IDLE_SECONDS = 30 * 60
# Exact image names, lower-cased. Exact on purpose: `com.docker.build.exe` and
# `codex-windows-sandbox-service.exe` are resident, so anything looser never reads idle.
# `docker build` runs as the `docker-buildx.exe` plugin and `docker compose build` as
# `docker-compose.exe`, so those two are what a build looks like from outside the CLI.
AGENT_IMAGES = frozenset({"claude.exe", "codex.exe"})
BUILD_IMAGES = frozenset({"docker-buildx.exe", "docker-compose.exe"})
REBOOT_DELAY = 120
REBOOT_NOTICE = "devkit: rebooting this idle machine. shutdown /a cancels."

# How long a stop gets before its start, and how long any one program may take. The task's
# own limit (`install-elevated-worker.TIME_LIMIT`) sits above several of these.
SETTLE_SECONDS = 10
STEP_TIMEOUT = 120
LISTING_TIMEOUT = 30

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Step:
    """One fixed command and the exit codes that mean it did its job."""

    argv: tuple[str, ...]
    accepted: frozenset[int] = frozenset({0})


@dataclass(frozen=True)
class Verb:
    steps: tuple[Step, ...]
    every: int
    idle_only: bool = False


VERBS: dict[str, Verb] = {
    "restart-wsl-service": Verb(
        (
            Step((TASKKILL, "/F", "/FI", f"SERVICES eq {WSL_SERVICE}")),
            Step((SC, "start", WSL_SERVICE), frozenset({0, ALREADY_RUNNING})),
        ),
        RESTART_EVERY,
    ),
    "restart-docker-service": Verb(
        (
            Step((SC, "stop", DOCKER_SERVICE), frozenset({0, NOT_STARTED})),
            Step((SC, "start", DOCKER_SERVICE), frozenset({0, ALREADY_RUNNING})),
        ),
        RESTART_EVERY,
    ),
    "reboot-when-idle": Verb(
        (Step((SHUTDOWN, "/r", "/t", str(REBOOT_DELAY), "/c", REBOOT_NOTICE)),),
        REBOOT_EVERY,
        idle_only=True,
    ),
}

REQUEST_NAMES: dict[str, str] = {f"{verb}{REQUEST_SUFFIX}": verb for verb in VERBS}


def verb_of(name: str) -> str | None:
    """The verb a spool entry's exact name selects, or None for every other name.

    A plain lookup and nothing cleverer: no case folding, no stripping, no parsing. A
    `..\\`, a trailing dot, a `:stream` and a homoglyph all miss it the same way.
    """
    return REQUEST_NAMES.get(name)


@dataclass(frozen=True)
class Layout:
    """Where the worker reads and writes. `main` builds the only real one, from constants."""

    root: str = ROOT
    spool: str = SPOOL
    home: str = HOME

    @property
    def limits(self) -> str:
        return os.path.join(self.home, LIMITS_NAME)

    @property
    def log(self) -> str:
        return os.path.join(self.home, LOG_NAME)


@dataclass(frozen=True)
class Machine:
    """What `cycle` does to the machine, passed in so a test can do none of it."""

    run: Callable[[Sequence[str]], int]
    reboot_refusal: Callable[[], str]
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Entry:
    """A spool entry as `lstat` saw it, with nothing followed."""

    name: str
    path: str
    regular: bool
    directory: bool


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failed: bool = False

    def say(self, line: str, failed: bool = False) -> None:
        self.lines.append(line)
        self.failed = self.failed or failed


def is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def plain_directory(path: str) -> bool:
    """Whether `path` is a real directory: no symlink, no junction, and not missing."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not is_reparse(info)


def verbatim(path: str) -> str:
    """`path` with Win32's name rewriting off, so an entry named `x.request.` is that
    entry and not the `x.request` Win32 would strip it to."""
    prefix = "\\\\?\\"
    return prefix + path if os.name == "nt" and not path.startswith(prefix) else path


def scan(spool: str) -> list[Entry]:
    """The spool's entries, each `lstat`ed, in name order. Streams are not entries."""
    found = []
    with os.scandir(verbatim(spool)) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            linked = is_reparse(info)
            regular = stat.S_ISREG(info.st_mode) and not linked
            directory = stat.S_ISDIR(info.st_mode) and not linked
            found.append(Entry(entry.name, entry.path, regular, directory))
    return sorted(found, key=lambda entry: entry.name)


def remove(entry: Entry) -> bool:
    """Delete the entry itself and never what it names. `rmdir` is not recursive, so a
    directory with anything in it stays and is reported; a walk would be path-based, and
    the user could swap a junction in under it. The installer's ACL refuses them
    subdirectories anyway."""
    try:
        if entry.directory:
            os.rmdir(entry.path)
        else:
            os.unlink(entry.path)  # a symlink or a junction goes; its target does not
    except OSError:
        return False
    return True


def shown(name: str) -> str:
    """A name the user chose, safe to put on a log line."""
    return ascii(name)[:120]


def limited(last: float | None, every: int, now: float) -> bool:
    """Whether a verb that last ran at `last` is still inside its window. A `last` in the
    future reads as inside it: a wrong clock must not open the limit."""
    return last is not None and now - last < every


def read_limits(path: str, now: float) -> dict[str, float]:
    """The last run of each verb. A record that exists and cannot be read is every verb
    having just run, rewritten so that costs one window rather than all of them. One that
    cannot be rewritten either raises: nothing runs, and `guarded` reports it."""
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        loaded = None
    if isinstance(loaded, dict) and all(isinstance(v, int | float) for v in loaded.values()):
        return {str(verb): float(value) for verb, value in loaded.items()}
    limits = {verb: now for verb in VERBS}
    write_limits(path, limits)
    return limits


def write_limits(path: str, limits: dict[str, float]) -> None:
    temporary = path + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(limits, handle)
    os.replace(temporary, path)


def run_steps(verb: str, steps: Sequence[Step], machine: Machine, report: Report) -> None:
    """Every step of a verb. One that fails does not stop the next: a stop refused must
    still reach its start."""
    for index, step in enumerate(steps):
        if index:
            machine.sleep(SETTLE_SECONDS)
        code = machine.run(step.argv)
        report.say(
            f"{verb}: {os.path.basename(step.argv[0])} {step.argv[1]} exited {code}",
            failed=code not in step.accepted,
        )


def serve(
    entry: Entry, verb: str, limits: dict[str, float], layout: Layout, machine: Machine
) -> Report:
    """Act on one request. The order is the safety: the window is checked, the request is
    gone and the window is saved, all before anything runs, so a crash at any point leaves
    a verb that did not run rather than one that runs again."""
    report = Report()
    spec = VERBS[verb]
    now = machine.now()
    if limited(limits.get(verb), spec.every, now):
        # Dropped, not held: a restart run hours late, on a stale request, ends whatever
        # is running by then. A caller that still needs it asks again.
        report.say(f"{verb}: inside its rate limit; request dropped", failed=not remove(entry))
        return report
    refusal = machine.reboot_refusal() if spec.idle_only else ""
    if refusal:
        report.say(f"{verb}: deferred, {refusal}")
        return report
    if not remove(entry):
        report.say(f"{verb}: the request could not be deleted; not run", failed=True)
        return report
    limits[verb] = now
    try:
        write_limits(layout.limits, limits)
    except OSError as exc:
        report.say(f"{verb}: the rate limit could not be saved ({exc}); not run", failed=True)
        return report
    run_steps(verb, spec.steps, machine, report)
    return report


def cycle(layout: Layout, machine: Machine) -> Report:
    """One pass over the spool. A deferred or dropped request is not a failure; a spool
    that cannot be trusted, a step that failed and an entry that would not go are."""
    report = Report()
    if not all(plain_directory(path) for path in (layout.root, layout.spool, layout.home)):
        report.say(f"refused: {layout.spool} or a parent is missing or a reparse point", True)
        return report
    limits = read_limits(layout.limits, machine.now())
    for entry in scan(layout.spool):
        if entry.regular and entry.name == SENTINEL:
            continue
        verb = verb_of(entry.name) if entry.regular else None
        if verb is None:
            report.say(f"ignored {shown(entry.name)}: deleted", failed=not remove(entry))
            continue
        served = serve(entry, verb, limits, layout, machine)
        report.lines += served.lines
        report.failed = report.failed or served.failed
    return report


# --- the gate on a reboot ---------------------------------------------------------


def elapsed_seconds(tick: int, last_input: int) -> float:
    """Seconds between two `GetTickCount` readings, which wrap every 49.7 days."""
    return ((tick - last_input) & 0xFFFFFFFF) / 1000


def busy_images(listing: str) -> list[str]:
    """Each image in `tasklist /FO CSV /NH` output that holds a reboot back."""
    names = {row[0].strip().lower() for row in csv.reader(io.StringIO(listing)) if row}
    return sorted(names & (AGENT_IMAGES | BUILD_IMAGES))


def refusal_of(idle: float | None, listing: str | None) -> str:
    """Why this machine may not reboot now; "" when it may. Whatever could not be read
    reads as busy: the cost of being wrong in that direction is one more cycle."""
    if idle is None:
        return "the last input could not be read"
    if idle < IDLE_SECONDS:
        return f"input {int(idle)}s ago"
    if listing is None:
        return "the process list could not be read"
    busy = busy_images(listing)
    return f"running: {', '.join(busy)}" if busy else ""


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


def idle_seconds() -> float | None:
    """Seconds since this session's last input. Session-local, which is why the task runs
    in the user's session and not as SYSTEM, where this would always read idle."""
    info = _LastInput(cbSize=ctypes.sizeof(_LastInput))
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return elapsed_seconds(ctypes.windll.kernel32.GetTickCount(), info.dwTime)
    except (AttributeError, OSError):
        return None


def spawn(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv: no shell, a fixed cwd, a fixed environment, no window."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        cwd=SYSTEM32,
        env=dict(CHILD_ENV),
        creationflags=NO_WINDOW,
    )


def run_step(argv: Sequence[str]) -> int:
    try:
        return spawn(argv, STEP_TIMEOUT).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def process_listing() -> str | None:
    try:
        done = spawn((TASKLIST, "/FO", "CSV", "/NH"), LISTING_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def reboot_refusal() -> str:
    idle = idle_seconds()
    listing = process_listing() if idle is not None and idle >= IDLE_SECONDS else None
    return refusal_of(idle, listing)


# --- the run ----------------------------------------------------------------------


def append_log(path: str, lines: Sequence[str], when: float, keep: int = LOG_BYTES) -> None:
    """Add `lines`, then cut the file back to its newest `keep` bytes."""
    if not lines:
        return
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(f"{stamp}Z {line}\n" for line in lines)
    if os.path.getsize(path) > keep:
        with open(path, "rb") as handle:
            handle.seek(-keep, os.SEEK_END)
            tail = handle.read()
        with open(path, "wb") as handle:
            handle.write(tail[tail.find(b"\n") + 1 :])


def guarded(layout: Layout, machine: Machine) -> int:
    """`cycle` and its log. Non-zero is what reaches anyone: this job cannot write the
    ledger, so its exit code is what `schedule_health` reads off the scheduler. A crash is
    logged, since `pythonw` has nowhere to print one, and re-raised, which is that code."""
    try:
        report = cycle(layout, machine)
    except Exception:
        append_log(layout.log, traceback.format_exc().splitlines(), machine.now())
        raise
    try:
        append_log(layout.log, report.lines, machine.now())
    except OSError:
        return 1
    return 1 if report.failed else 0


def main() -> int:
    """No parameters and no `sys.argv`: there is nothing an unelevated caller could pass."""
    if os.name != "nt":
        print("elevated_worker: Windows-only; nothing to do here.")
        return 0
    return guarded(Layout(), Machine(run_step, reboot_refusal))


if __name__ == "__main__":
    sys.exit(main())
