#!/usr/bin/env python3
"""The scheduled-command collectors: one Windows Scheduled Task each, named after it.

A container collector keeps its own clock, so `collectors.py` only has to keep the
container up. A **scheduled** one (`collectors_config.Collector.command`) has no clock of
its own -- social-scraper drives the host's Chrome and the profiles in its checkout -- so the
clock is a task, and this module is everything about that task:

- **Registered by `collectors.py maintain`, not by an installer.** Whether this machine
  runs a collector is its assignment (`run-here`), which changes at runtime; an
  installer is driven by a fixed `TASK_NAME`. So the 15-minute pass that already acts on
  the assignment registers the task on a `run` machine and deletes it on any other, and
  re-registers it the moment the declared cadence drifts -- `devkit_schtasks.run_check`
  against the same document, as every installer does.
- **Named after the collector, never `devkit-`.** It is the project's job, not devkit's,
  so `schedule_health` has to be told about it: the tray and the fix pass both pass
  `collectors.scheduled_tasks` as `also=`. Each failed fire is filed on the ledger by the
  `log-wrap.py --always` it runs under (`task_document`); a task gone missing, which no
  run can report, is filed by the pass (`fix_loop.unscheduled`).
- **The task runs `collectors.py fire <name>`, not the command itself.** The command is
  read from the workspace file at fire time, so editing it needs no re-registration; the
  wrapper is what keeps the run window-less (`NO_WINDOW` on a console child -- see
  `scripts/windowless-jobs.md`), starts the compose services it `needs`, and writes
  `logs/collector-<name>.log`, which `pythonw.exe` would otherwise send nowhere. It exits
  with the command's own code, so the scheduler's `Last Result` is the project's verdict
  and the tray reads it through `schedule_health` like any other job.

Tested in `tests/test_collector_tasks.py`.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import shutil
import subprocess
import sys
import time as _time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collectors_config as config
import devkit_schtasks
import sweep

# Every spawn here is reachable from a scheduled task under `pythonw.exe`; see
# `tests/test_scheduled_jobs.py`. Zero off Windows, where the flag does not exist.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The task's limit, and the command's a little inside it, so a wedged run is ended by
# the wrapper -- which records why -- rather than killed by the scheduler, which does not.
TIME_LIMIT = "PT1H"
FIRE_TIMEOUT = 55 * 60
NEEDS_TIMEOUT = 600
SCHTASKS_TIMEOUT = 60
# The re-ask after a `compose up` failed (`needs_failed`): an engine that answers at
# all answers `docker info` in seconds.
PROBE = 30
# How long a fire whose `compose up` failed fast on a silent engine waits for it, and how
# often it asks, before trying once more (`start_needs`). The collectors pass restarts a
# wedged Docker Desktop (`collectors.revive_engine`), which took four to five minutes on
# 2026-10-08; a fire landing inside one failed at once, "docker compose up failed for db",
# the cause that recurred through fourteen resolutions of other things (74c2ccd2).
ENGINE_WAIT = 360
ENGINE_POLL = 15
# Docker Desktop installing an update of itself: its updater (`Docker Desktop
# Updater-<from> (<to>).exe`) runs `Docker Desktop Installer.exe` and waits for it, and
# the engine is down until the installer starts the new app. 2026-10-09: the backend the
# collectors pass's 19:45 UTC restart relaunched found 4.94.0 and installed it from 19:55
# to 20:28; the scrape fired into it and failed on a silent engine (38b0ffb4), and the
# pass read the half hour as a restart that had not helped (3012d246). Lowercased
# prefixes, matched against `UPDATE_LISTING`'s image names.
UPDATER_IMAGES = ("docker desktop installer", "docker desktop updater")
UPDATE_LISTING = ("tasklist", "/FI", "IMAGENAME eq Docker Desktop*", "/FO", "CSV", "/NH")
# How long the output of a child already killed at its timeout is waited for.
REAP_TIMEOUT = 30
# `spawn`'s code for a child it ended at its timeout, as `timeout(1)` reports one.
TIMED_OUT = 124

# Lines of the command's output the log keeps: the tail, where the error is.
LOG_LINES = 300

# What every collector command runs with on top of this process's environment. The
# command runs in the project's *static* checkout, and a bare `uv run` relocks whenever
# the lock disagrees with what it resolves -- a sibling path dependency that moved is
# enough -- which writes `uv.lock` on the default branch. That is how ibkr_trader's `main`
# held an uncommitted lock from 2026-10-03 on (an interactive `uv run`, not this job, but
# this job runs `uv run` in a checkout every half hour). Frozen, uv installs from the
# lock as committed and never rewrites it.
COMMAND_ENV = {"UV_FROZEN": "1"}

# When the scheduler fired, as an ISO UTC timestamp in the command's environment. The
# command's own clock starts minutes later -- the `needs` coming up, `uv run` starting --
# and the next fire is due one interval after *this* moment, not after that one. 45f7adeb:
# social-scraper's 17:30 fire reached its scrape at 17:35:44 (compose 3m24s, uv 2m20s
# under load), so a cycle deadline counted from the scrape's own start ran past 18:00.
FIRED_AT = "DEVKIT_FIRED_AT"


def command_env(
    base: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment a collector's command runs in: `base` (this process's) plus
    `COMMAND_ENV`, so nothing it runs rewrites the checkout's tracked files, plus `extra`
    (`fire`'s `FIRED_AT`)."""
    return {**(os.environ if base is None else base), **COMMAND_ENV, **(extra or {})}


def fired_env(fired: _dt.datetime) -> dict[str, str]:
    """`FIRED_AT` for a fire at `fired`; a naive time is this machine's local one."""
    return {FIRED_AT: fired.astimezone(_dt.UTC).isoformat(timespec="seconds")}


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class Reporter(Protocol):
    """`collectors.Report`'s shape, without importing the module that imports this one."""

    def say(self, line: str) -> None: ...

    def fail(self, line: str) -> None: ...


def task_name(collector: config.Collector) -> str:
    return collector.project


def log_path(name: str) -> Path:
    """The fire's own record, relative to the devkit checkout."""
    return Path(f"logs/collector-{name}.log")


def interpreter() -> str:
    """The task's `<Command>`: the window-less twin of a console interpreter.

    Through `windowless`, which resolves a venv's stub to its base install -- so a
    `run-here` typed in a box registers an interpreter that outlives the box.
    """
    return devkit_schtasks.windowless(sweep.console_python())


def task_arguments(root: Path, name: str) -> str:
    """`fire` is named because the default mode is `status`, which runs nothing."""
    script = PureWindowsPath(root) / "scripts" / "collectors.py"
    return f'"{script}" fire {name}'


def wrapper_label(name: str) -> str:
    """The `log-wrap.py` label a fire runs under; it slugs to `scheduled-collector-<name>`,
    clear of the fire's own `collector-<name>.log` (`log_path`)."""
    return f"Scheduled collector: {name}"


def task_document(collector: config.Collector, root: Path, python: str) -> str:
    """The task XML. The working directory is the devkit checkout, where `logs/` is, and
    the fire runs under `log-wrap.py --always` (`devkit_schtasks.logged`), which files
    each failed run on the harness-events ledger, as every devkit job's does."""
    name = task_name(collector)
    return devkit_schtasks.task_xml(
        python,
        devkit_schtasks.logged(wrapper_label(name), python, task_arguments(root, name), root),
        devkit_schtasks.repeating_trigger(collector.minutes),
        time_limit=TIME_LIMIT,
        working_dir=str(PureWindowsPath(root)),
    )


def run_argv(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`devkit_schtasks.Runner`: captured, window-less, a spawn failure is a returncode."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=SCHTASKS_TIMEOUT,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def is_registered(name: str, run: Runner) -> bool:
    return run(devkit_schtasks.query_xml_argv(name)).returncode == 0


def interval_tag(collector: config.Collector) -> str:
    return f"<Interval>PT{int(collector.minutes)}M</Interval>"


def check(collector: config.Collector, root: Path, python: str, run: Runner) -> tuple[int, str]:
    """`devkit_schtasks.run_check`, plus the one field it does not compare: the cadence.

    Every installer's interval is a constant, so `drift` never needed the trigger; here
    it is `minutes` in a hand-edited file, and an edit the check could not see would
    never reach the registered task.
    """
    name = task_name(collector)
    code, message = devkit_schtasks.run_check(name, task_document(collector, root, python), run)
    if code != devkit_schtasks.CHECK_CURRENT:
        return code, message
    registered = run(devkit_schtasks.query_xml_argv(name)).stdout or ""
    if interval_tag(collector) not in registered:
        return (
            devkit_schtasks.CHECK_STALE,
            f"schedule: {name} fires on another interval than every {collector.minutes} minutes",
        )
    return code, message


def keep_registered(
    collector: config.Collector, root: Path, python: str, run: Runner, report: Reporter
) -> None:
    """Register the task unless the one registered is already this document."""
    name = task_name(collector)
    document = task_document(collector, root, python)
    code, message = check(collector, root, python, run)
    if code == devkit_schtasks.CHECK_CURRENT:
        report.say(f"{name}: scheduled every {collector.minutes} minutes")
        return
    ok, out = devkit_schtasks.register(name, document, run)
    if ok:
        report.say(f"{name}: registered, every {collector.minutes} minutes ({message})")
    else:
        report.fail(f"{name}: could not register its task -- {out}")


def keep_removed(name: str, run: Runner, report: Reporter) -> None:
    """Delete the task, if there is one: this machine is not the one that runs it."""
    if not is_registered(name, run):
        report.say(f"{name}: not scheduled on this machine")
        return
    result = run(devkit_schtasks.delete_argv(name))
    if result.returncode == 0:
        report.say(f"{name}: removed its scheduled task -- this machine does not run it")
    else:
        detail = (result.stderr or result.stdout or "schtasks failed").strip()
        report.fail(f"{name}: could not remove its scheduled task -- {detail}")


def describe(collector: config.Collector, root: Path, python: str, run: Runner) -> str:
    """Read-only, for `collectors.py status`: is the registered task the current one."""
    code, message = check(collector, root, python, run)
    if code == devkit_schtasks.CHECK_CURRENT:
        return f"scheduled every {collector.minutes} minutes"
    return message.removeprefix("schedule: ")


# --- one fire ----------------------------------------------------------------------


def kill_tree(pid: int) -> str:
    """End `pid` and everything under it; "" when that was done, else why it was not.

    `Popen.kill` ends only the direct child -- `uv` -- and leaves the interpreter and the
    Chrome it drives running, still holding the browser profile's lock, so every later
    fire fails on that lock. `taskkill /T` takes the subtree; elsewhere, the child alone.

    Best effort, never a raise. a4796ff6: on a machine loaded by a wedged
    Docker engine `taskkill` itself ran past its minute, and the `TimeoutExpired` it
    raised replaced the log of the fire that had timed out with a traceback about the
    cleanup. The caller still kills the direct child either way.
    """
    if os.name != "nt":
        return ""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=SCHTASKS_TIMEOUT,
            check=False,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return f"taskkill did not finish within {SCHTASKS_TIMEOUT}s"
    except OSError as exc:
        return f"taskkill could not run: {exc}"
    return ""


def spawn(
    argv: Sequence[str], cwd: Path, timeout: int, env: Mapping[str, str] | None = None
) -> tuple[int, str]:
    """`(exit code, stdout+stderr)`; a spawn that could not happen is a code, not a raise.

    stderr is merged into stdout, so the log keeps the two in the order they were written.
    `env` is added to `command_env`'s.
    """
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
            env=command_env(extra=env),
        )
    except FileNotFoundError:
        return 127, f"{argv[0]} is not on PATH"
    except OSError as exc:
        return 126, str(exc)
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stuck = kill_tree(process.pid)
        process.kill()
        try:
            out, _ = process.communicate(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            # A grandchild taskkill could not end still holds the pipe open, and an
            # unbounded wait here would hold the fire until the scheduler killed it.
            out = ""
        ended = f"ended it, but not its children: {stuck}" if stuck else "ended it and its children"
        return TIMED_OUT, f"{(out or '').strip()}\ntimed out after {timeout}s; {ended}"
    return process.returncode, (out or "").strip()


class Spawner(Protocol):
    """`spawn`'s shape: `(argv, cwd, timeout)`, and `env` for the command itself."""

    def __call__(
        self, argv: Sequence[str], cwd: Path, timeout: int, env: Mapping[str, str] | None = ...
    ) -> tuple[int, str]: ...


def resolve(argv: Sequence[str], which: Callable[[str], str | None] | None = None) -> list[str]:
    """`argv` with its program resolved on PATH, so `uv` finds `uv.exe`; as-is when not
    found, and the spawn then reports exit 127 naming it."""
    found = (which or shutil.which)(argv[0])
    return [found or argv[0], *argv[1:]]


def probe(checkout: Path, run: Spawner) -> tuple[int, str]:
    """`docker info`'s `(exit code, server version)`: whether the engine answers at all."""
    return run(["docker", "info", "--format", "{{.ServerVersion}}"], checkout, PROBE)


def updater_listed(listing: str) -> bool:
    """Whether a `tasklist /FO CSV` listing names Docker Desktop's updater or installer."""
    rows = csv.reader(io.StringIO(listing))
    return any(row and row[0].strip().lower().startswith(UPDATER_IMAGES) for row in rows)


def desktop_updating(checkout: Path, run: Spawner) -> bool:
    """Whether Docker Desktop is installing an update of itself; never off Windows."""
    if os.name != "nt":
        return False
    code, out = run(list(UPDATE_LISTING), checkout, PROBE)
    return code == 0 and updater_listed(out)


def await_engine(checkout: Path, run: Spawner) -> tuple[int, str]:
    """`probe`, asked every `ENGINE_POLL` seconds until it answers or `ENGINE_WAIT` has
    passed; its last answer."""
    deadline = _time.monotonic() + ENGINE_WAIT
    answer = (1, "")
    while (left := deadline - _time.monotonic()) > 0:
        _time.sleep(min(ENGINE_POLL, left))
        answer = probe(checkout, run)
        if answer[0] == 0:
            break
    return answer


def start_needs(
    needs: Sequence[str], checkout: Path, run: Spawner
) -> tuple[int, str, tuple[int, str] | None]:
    """`compose up --wait` of `needs`: `(exit code, output, the engine's last answer)`.

    A fast failure is asked whether the engine answers. A silent one is waited for
    (`await_engine`) and the start tried once more once it answers: a fire landing in a
    Docker Desktop restart is a fire minutes late, not one lost. The engine's answer is
    None where nobody asked -- a start that went through or timed out.
    """
    up = ["docker", "compose", "up", "-d", "--wait", *needs]
    code, out = run(up, checkout, NEEDS_TIMEOUT)
    if code in (0, TIMED_OUT):
        return code, out, None
    engine = probe(checkout, run)
    if engine[0] == 0:
        return code, out, engine
    engine = await_engine(checkout, run)
    if engine[0] != 0:
        return code, out, engine
    code, out = run(up, checkout, NEEDS_TIMEOUT)
    return code, out, None


def needs_failed(
    needs: Sequence[str],
    checkout: Path,
    code: int,
    out: str,
    run: Spawner,
    engine: tuple[int, str] | None = None,
) -> list[str]:
    """The log lines for a `compose up --wait` of `needs` that failed, ending on an
    `error:` line naming only the kind, which `log-wrap.py` files as the cause.

    3e8e7f26: `up --wait db` printed nothing for its whole `NEEDS_TIMEOUT` and the log
    asked "is Docker Desktop running?" -- it was, with the db up and healthy for hours,
    and the cause read `timed out after Ns`, the same as the scrape itself running past
    `FIRE_TIMEOUT`. So the engine is asked again (`engine`, when `start_needs` already
    asked it), and on a timeout the services too, so the log says which stalled while the
    evidence still exists. A silent engine is its own cause, whatever compose said.
    """
    names = ", ".join(needs)
    lines = [f"could not start {names} (exit {code}), so the command was not run", ""]
    lines += out.splitlines()[-40:]
    status, answer = engine or probe(checkout, run)
    if status != 0:
        return [
            *lines,
            f"`docker info` did not answer either (exit {status}): {first(answer)}",
            "error: the Docker engine did not answer",
        ]
    if code != TIMED_OUT:
        return [*lines, f"error: docker compose up failed for {names}"]
    _code, state = run(["docker", "compose", "ps", "--all", *needs], checkout, PROBE)
    return [
        *lines,
        f"the Docker engine answers now (server {first(answer)}); {names} as compose sees it:",
        *state.splitlines()[-10:],
        f"error: {names} did not come up healthy within the compose timeout",
    ]


def first(text: str) -> str:
    """`text`'s first line, or a word saying there was none."""
    return (text.strip().splitlines() or ["(nothing)"])[0]


def fire(
    collector: config.Collector,
    checkout: Path,
    run: Spawner = spawn,
    fired: _dt.datetime | None = None,
) -> tuple[int, list[str]]:
    """Run the collector once. `(exit code, log lines)`; the code is the command's own.

    The command is told when the scheduler fired (`FIRED_AT`) -- `fired`, now when None
    -- which is taken before the `needs` come up, since they are part of the cycle.

    A `needs` that cannot start because Docker Desktop is installing an update
    (`desktop_updating`) skips this run, exit 0: the update is Docker's maintenance, the
    next fire runs on the new engine, and `collectors.revive_engine` fails an update that
    outlasts its `HOLD_LIMIT`.
    """
    stamp = fired_env(fired or _dt.datetime.now(_dt.UTC))
    lines = [f"command: {' '.join(collector.command)}", f"cwd: {checkout}"]
    if not (checkout / ".git").exists():
        return 2, [*lines, f"no checkout at {checkout} -- nothing to run"]
    if collector.needs:
        code, out, engine = start_needs(collector.needs, checkout, run)
        if code != 0:
            engine = engine or probe(checkout, run)
            if engine[0] != 0 and desktop_updating(checkout, run):
                names = ", ".join(collector.needs)
                return 0, [
                    *lines,
                    f"Docker Desktop is installing an update, so {names} cannot start: "
                    "this run is skipped, and the next fire runs on the updated engine",
                ]
            failed = needs_failed(collector.needs, checkout, code, out, run, engine)
            return code, [*lines, *failed]
        lines.append(f"started: {', '.join(collector.needs)}")
    code, out = run(resolve(collector.command), checkout, FIRE_TIMEOUT, stamp)
    output = out.splitlines()
    if len(output) > LOG_LINES:
        output = [f"... {len(output) - LOG_LINES} earlier lines dropped", *output[-LOG_LINES:]]
    return code, [*lines, "", *output]


def running(name: str, run: Runner) -> bool:
    """Whether the scheduler has `name`'s task running right now.

    The status column of `/FO CSV`, one row per trigger. A run started by hand beside a
    scheduled one is two processes on one browser profile, and the second dies on its
    lock; nothing else here would stop that, because `IgnoreNew` only governs fires.
    """
    result = run(["schtasks", "/Query", "/TN", name, "/FO", "CSV", "/NH"])
    if result.returncode != 0:
        return False
    rows = csv.reader(io.StringIO(result.stdout or ""))
    return any(row and row[-1].strip().lower() == "running" for row in rows)


def inherited_streams() -> dict:
    """The streams a window-less child must be handed to write where a reader is.

    `docker-maint.inherited_streams` owns the account: `NO_WINDOW` gives the child a
    console of its own, and a child not told otherwise writes into that hidden console.
    `sys.stdout` is None under `pythonw.exe` and has no `fileno` under pytest; both mean
    there is nothing to hand down.
    """
    streams = {}
    for key, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        streams[key] = stream
    return streams


def stream(argv: Sequence[str], cwd: Path) -> int:
    """Run `argv` with its output going straight to this terminal; its exit code."""
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            check=False,
            creationflags=NO_WINDOW,
            env=command_env(),
            **inherited_streams(),
        ).returncode
    except FileNotFoundError:
        print(f"{argv[0]} is not on PATH", flush=True)
        return 127
    except OSError as exc:
        print(str(exc), flush=True)
        return 126


Streamer = Callable[[Sequence[str], Path], int]


def run_once(collector: config.Collector, checkout: Path, run: Streamer = stream) -> int:
    """One run by hand, in the foreground: what `fire` does, minus the capture and the
    timeout, for a person watching the terminal -- a first run, or a check after a change.
    """
    if not (checkout / ".git").exists():
        print(f"no checkout at {checkout} -- nothing to run", flush=True)
        return 2
    if collector.needs:
        print(f"starting {', '.join(collector.needs)} in {checkout}", flush=True)
        code = run(["docker", "compose", "up", "-d", "--wait", *collector.needs], checkout)
        if code != 0:
            print(
                f"could not start {', '.join(collector.needs)} (exit {code}), so the "
                f"command was not run -- is Docker Desktop running?",
                flush=True,
            )
            return code
    print(f"running `{' '.join(collector.command)}` in {checkout}", flush=True)
    return run(resolve(collector.command), checkout)


def render(name: str, code: int, lines: Sequence[str], when: _dt.datetime) -> str:
    head = f"# collector {name} {when.isoformat(timespec='seconds')} -- exit {code}"
    return "\n".join([head, *lines, ""])
