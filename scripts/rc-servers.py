#!/usr/bin/env python3
"""Keep a `claude remote-control` server up per project, and let the CLI still update.

Remote Control puts a session running on *this* machine in front of the phone at
`claude.ai/code`. One server per project covers every conversation in that project --
it serves up to `--capacity` sessions (32 by default) and spawns them on demand from
the device -- so the unit this manages is the **project**, not the session.

**The reason this is a scheduled job and not a terminal you leave open.** Two facts
collide, and neither is optional:

- A server is a process. If it dies -- and `remote-control` gives up and *exits* after
  roughly ten minutes with no network -- nothing brings it back, and the phone shows an
  offline session with no way to act on it from the phone.
- `agent_clis` refuses to update a CLI while a process of that name is running, because
  Windows cannot replace a running image. A server that is always up therefore means
  `claude` is always running, and the 04:30 `global-tools.py` pass skips the update
  **every night, forever**. `agent_clis`' own docstring says as much: its two callers
  exist because "a night with a session left open skips".

So an always-on server silently disables the update it depends on, and the fix cannot
live in either piece alone. It lives here: this pass owns the restart, and the update
happens in the window it opens between stopping the servers and starting them again.

**One task, not two.** The liveness check wants to run every few minutes and the update
wants to run once a day, which reads like two schedules. It is one, because the daily
half is a predicate (`due_for_update`) rather than a trigger: every tick restarts
whatever is down, and the first tick after `update_at` that finds every server idle
also updates. A second task would double the registrations for a job whose expensive
half is guarded by a date either way.

**Idleness is the transcript's mtime**, borrowed from `resume-sessions.py` along with
its reasoning: the store appends to a session's file for as long as the session lives,
so the file's mtime is its last activity, and reading it costs one syscall instead of a
megabyte of JSON. What that buys here is the difference between a nightly restart and a
nightly interruption -- a turn in flight on the phone at 04:00 keeps its server, and the
update waits for the next tick.

A store that cannot be read at all counts as **busy**, never as idle. That is
`agent_clis`' rule for an unanswerable process enumeration, for the same reason: not
knowing is the one state where acting is a guess about live work.

**Restarting is cheap, and only inside four hours.** `claude remote-control` in a
directory a server was serving brings back every session it served, so a nightly cycle
costs the sessions nothing. That window is about four hours wide; past it the sessions
are gone rather than paused, which is why this restarts immediately after updating
rather than leaving the servers down until someone notices.

**Nothing is served for a project that did not ask for one.** The opt-in is
`devkit.remoteControl` in the workspace file, beside `devkit.onHold` and read the same
way -- a project absent from it gets no server, because a standing server costs real
memory (measured on this desk: 300-420 MB per `claude` process) and a project nobody
opens from a phone is paying it for nothing.

Pure helpers are unit-tested in `tests/test_rc_servers.py`; the `subprocess` and
filesystem shells around them are thin by construction.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_clis
import devkit_jsonc
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Where this job's account of itself lives. It runs windowless, so stdout goes nowhere
# at all -- see `tests/test_scheduled_jobs.py`. Relative, resolved against the checkout
# at write time, which is what `install-rc-schedule.py` advertises to `schedule_health`.
ARTIFACT = Path("logs/rc-servers.log")

# Machine-local and rewritten every pass, so it lives beside the artifact under the
# already-ignored `logs/` rather than becoming a tracked file that differs on every
# desk. It holds the pids this job started: a server it did not start is not its to
# manage, and there is no other way to tell one from a session someone opened by hand.
STATE = Path("logs/rc-servers.state.json")

# The workspace-file setting naming the projects that get a server. A list of checkout
# names, or an object carrying the same list under `projects` plus the knobs below.
# Beside `sweep.ON_HOLD_SETTING`, in the file that is already the registry every tool
# reads the checkouts from.
RC_SETTING = "devkit.remoteControl"

# `same-dir` is `remote-control`'s own default and is kept as this job's default
# deliberately. `worktree` mode would have Claude Code cutting git worktrees on a
# machine where `worktree.py reconcile` already manages a worktree tier under
# `.worktrees/` and reaps what it finds stranded; two unattended worktree managers that
# do not know about each other is how work gets stranded, and nothing here would notice.
# Set it per-project only if you have read both.
DEFAULT_SPAWN = "same-dir"

# Empty means "pass no `--permission-mode`", so a server inherits whatever the project's
# own settings say. Not defaulted to `acceptEdits` even though that is the mode that
# makes a phone usable: it is a standing grant on an unattended machine, and the opt-in
# belongs to the person whose machine it is rather than to this file's default.
DEFAULT_PERMISSION_MODE = ""

# Minutes of transcript silence before a project's server may be restarted. Twenty is
# longer than a slow turn and shorter than a coffee break; the cost of it being too
# small is an interrupted turn, and of too large, an update deferred to tomorrow.
DEFAULT_IDLE_MINUTES = 20

# Earliest wall-clock time the daily update-and-restart may run, in the local zone. A
# missed one is caught up on the next tick rather than skipped -- the machine this runs
# on is a desktop, so "asleep at 04:45" is unusual and "busy at 04:45" is not.
#
# **After `devkit-global-tools` at 04:30, deliberately.** That is the pass that owns the
# agent CLIs; this one is not trying to replace it, only to do the part it structurally
# cannot -- with servers up, its exact-name check finds `claude` running and skips, every
# night. Running first would invert that into this job racing a pass that was about to
# succeed on its own. Ordered second, a desk with no servers configured behaves exactly
# as it does today and this reports "current".
DEFAULT_UPDATE_AT = "04:45"

# Seconds to let a server exit after a polite `taskkill` before insisting. Short: the
# server has no cleanup to do that survives it -- the sessions are resumed from the
# directory, not from anything the process writes on the way out.
STOP_GRACE_SECONDS = 10

QUICK_TIMEOUT = 30

# `CREATE_NO_WINDOW` off Windows is zero, via `sweep`, which already owns that
# `getattr` and the reasoning for it.
NO_WINDOW: int = sweep.NO_WINDOW

# A server outlives the pass that starts it, so it must not share the scheduler's
# console control group: a Ctrl-Break delivered to this process would otherwise reach
# every server it had ever started. Valid alongside `CREATE_NO_WINDOW`, which
# `DETACHED_PROCESS` is not -- the two are mutually exclusive console dispositions, and
# pairing them is a `ValueError` at the spawn rather than a detached child.
NEW_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

WINDOWS = os.name == "nt"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=QUICK_TIMEOUT,
        creationflags=NO_WINDOW,
    )


# --- configuration ----------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """What the workspace file asks this job to serve."""

    projects: tuple[str, ...] = ()
    spawn: str = DEFAULT_SPAWN
    permission_mode: str = DEFAULT_PERMISSION_MODE
    update_at: str = DEFAULT_UPDATE_AT
    idle_minutes: int = DEFAULT_IDLE_MINUTES


def parse_config(text: str) -> Config:
    """Read `RC_SETTING` out of a workspace file.

    Two accepted shapes, because the list is what almost every machine wants and an
    object that exists only to hold one key is a tax on the common case:

        "devkit.remoteControl": ["devkit", "carameli"]
        "devkit.remoteControl": {"projects": ["devkit"], "idleMinutes": 30}

    Malformed input yields an empty `Config`, matching `sweep.parse_workspace` and
    `sweep.on_hold`: this job's failure mode for a broken workspace file is to serve
    nothing, which is visible in the artifact, rather than to crash a scheduled task
    whose stdout goes nowhere.
    """
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return Config()
    if not isinstance(payload, dict):
        return Config()
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return Config()
    raw = settings.get(RC_SETTING)
    if isinstance(raw, list):
        raw = {"projects": raw}
    if not isinstance(raw, dict):
        return Config()

    names = raw.get("projects")
    projects = (
        tuple(name for name in names if isinstance(name, str) and name)
        if isinstance(names, list)
        else ()
    )
    return Config(
        projects=projects,
        spawn=_text(raw.get("spawn"), DEFAULT_SPAWN),
        permission_mode=_text(raw.get("permissionMode"), DEFAULT_PERMISSION_MODE),
        update_at=_text(raw.get("updateAt"), DEFAULT_UPDATE_AT),
        idle_minutes=_positive_int(raw.get("idleMinutes"), DEFAULT_IDLE_MINUTES),
    )


def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _positive_int(value: object, fallback: int) -> int:
    """Ints only, and only useful ones.

    `isinstance(True, int)` is true, so a `bool` slipping through here would set an
    idle window of one minute from a setting someone wrote as `true`.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value


def selected(
    config: Config, known: Sequence[str], held: frozenset[str]
) -> tuple[list[str], list[str]]:
    """`(serve, notes)` -- the projects to serve, and a line for each one refused.

    A name that is not in the workspace is reported rather than passed over: the whole
    setting is hand-edited, and a typo that silently serves nothing looks exactly like a
    machine where the job is working.

    A project on hold is refused for the reason `upgrade-project.py` refuses it -- a
    paused project is one nothing should be in flight for, and a phone-reachable server
    is an invitation to start something.
    """
    serve: list[str] = []
    notes: list[str] = []
    for name in config.projects:
        if name not in known:
            notes.append(f"{name}: not a checkout in the workspace file -- skipped")
        elif name in held:
            notes.append(f"{name}: on hold (workspace `{sweep.ON_HOLD_SETTING}`) -- skipped")
        else:
            serve.append(name)
    return serve, notes


# --- is anyone working in there? --------------------------------------------


def slug(cwd: Path) -> str:
    """A directory's name in Claude Code's transcript store.

    The store keys a directory by its path with every character that is not a letter,
    a digit or a hyphen replaced by one, so `C:\\Users\\alexa\\vs-code\\devkit` is filed
    under `C--Users-alexa-vs-code-devkit`.
    """
    return "".join(char if char.isalnum() or char == "-" else "-" for char in str(cwd))


def transcript_dir(cwd: Path, store: Path) -> Path:
    return store / slug(cwd)


def sessions_store(config_dir: str | None = None) -> Path:
    """Where Claude Code keeps transcripts, honouring `CLAUDE_CONFIG_DIR`.

    Same resolution as `resume-sessions.sessions_root`. A machine that moved the store
    and did not get this would read an empty directory as "nobody is working", which is
    the one wrong answer that costs an interrupted turn.
    """
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def last_activity(cwd: Path, store: Path) -> float | None:
    """Newest transcript mtime for `cwd`; `None` when the store cannot be read.

    `0.0` -- infinitely long ago -- is the honest answer for a directory the store has
    never heard of, and it is a different answer from `None`. A project with no sessions
    yet is idle; a store that is missing entirely means the question was not answered,
    and `is_idle` refuses to guess.
    """
    if not store.is_dir():
        return None
    directory = transcript_dir(cwd, store)
    if not directory.is_dir():
        return 0.0
    mtimes = []
    for path in directory.glob("*.jsonl"):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            # A transcript deleted between the glob and the stat is not an error; a
            # store that is unreadable as a whole was already caught above.
            continue
    return max(mtimes) if mtimes else 0.0


def is_idle(cwd: Path, store: Path, idle_minutes: int, now: float | None = None) -> bool:
    """Whether `cwd` has been quiet long enough to restart its server.

    Unknown counts as busy -- see the module docstring.
    """
    latest = last_activity(cwd, store)
    if latest is None:
        return False
    return ((time.time() if now is None else now) - latest) >= idle_minutes * 60


# --- processes ---------------------------------------------------------------


def pid_query_argv(pid: int) -> list[str]:
    return ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"]


def parse_pid_image(stdout: str) -> str:
    """The image name `tasklist` reports for a pid, normalised, or "" for no match.

    With no match `tasklist` prints an `INFO:` line rather than nothing and exits 0, so
    a row is only a process when it has the columns a process row has. Parsed through
    `csv` for `agent_clis.parse_process_names`' reason: the fields are quoted and an
    image name may legitimately contain a comma.
    """
    for row in csv.reader(io.StringIO(stdout or "")):
        if len(row) >= 2 and row[0].strip():
            return agent_clis.normalise_process(row[0])
    return ""


def pid_is_server(pid: int, run: Runner = run_command, windows: bool = WINDOWS) -> bool:
    """Whether `pid` is a live `claude`.

    The name is checked, not just the pid: pids are reused, and a recycled one naming
    some unrelated process would have this job report a server that is not there and
    then `taskkill` a stranger on the next cycle.

    Off Windows, and whenever the machine cannot be asked, the answer is "yes". Both are
    the `agent_clis` rule again: an unanswerable question about a running process is not
    a licence to start a second one or to kill the first.
    """
    if not windows:
        return True
    try:
        result = run(pid_query_argv(pid))
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return parse_pid_image(result.stdout or "") == "claude"


def stop_argv(pid: int, force: bool = False) -> list[str]:
    """`taskkill` for one pid and its children.

    `/T` because a server spawns MCP servers, and leaving those behind leaks the memory
    the restart was partly meant to reclaim. Polite first: a hard kill is not known to
    lose anything -- sessions are resumed from the *directory*, and the documented
    recovery path is the same one a network-outage exit takes -- but "not known to" is a
    weaker claim than asking nicely and waiting `STOP_GRACE_SECONDS` first.
    """
    argv = ["taskkill", "/PID", str(int(pid)), "/T"]
    return [*argv, "/F"] if force else argv


def launch_argv(claude: str, name: str, config: Config) -> list[str]:
    """The server command for one project.

    `--name` is not cosmetic. Without it the auto-generated title is the hostname plus a
    random pair of words, so every project on this machine arrives at the phone as
    `<host>-<adjective>-<noun>` and the session list is unusable for the one thing it is
    for -- picking the right project on a small screen.
    """
    argv = [claude, "remote-control", "--name", name, "--spawn", config.spawn]
    if config.permission_mode:
        argv += ["--permission-mode", config.permission_mode]
    return argv


def claude_executable(which: Callable[[str], str | None] = shutil.which) -> str:
    """`claude`, from PATH or from the native install location.

    A scheduled task runs with the system PATH rather than a login shell's, and the
    native installer puts the binary under `~/.local/bin`, which is on neither by
    default. Resolving only through `which` is how this job would report "claude is not
    installed" on the machine it was written for.
    """
    found = which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
    ):
        if candidate.is_file():
            return str(candidate)
    return ""


# --- state -------------------------------------------------------------------


@dataclass
class State:
    """The pids this job started, and the date it last ran the update."""

    servers: dict[str, int] = field(default_factory=dict)
    last_update: str = ""

    @classmethod
    def load(cls, path: Path) -> "State":
        """Read it, treating anything unreadable as empty.

        A corrupt state file must not wedge the job: the worst an empty one causes is a
        pass that starts servers it thinks are missing, and `pid_is_server` plus
        `remote-control`'s own same-directory resume make that recoverable. A crash
        here would leave every server down until someone read a log nobody watches.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(payload, dict):
            return cls()
        raw = payload.get("servers")
        servers = {}
        if isinstance(raw, dict):
            for name, pid in raw.items():
                if isinstance(name, str) and isinstance(pid, int) and not isinstance(pid, bool):
                    servers[name] = pid
        last = payload.get("last_update")
        return cls(servers=servers, last_update=last if isinstance(last, str) else "")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"servers": self.servers, "last_update": self.last_update}, indent=2) + "\n",
            encoding="utf-8",
        )


def valid_time(at: str) -> bool:
    """`HH:MM`, 24-hour. Same check and same reason as `install-upgrade-schedule`."""
    hours, _, minutes = at.partition(":")
    if not (hours.isdigit() and minutes.isdigit()) or len(hours) != 2 or len(minutes) != 2:
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def due_for_update(last_update: str, now: _dt.datetime, update_at: str) -> bool:
    """Whether today's update-and-restart is still owed.

    Compared against the *date*, not against an interval, so a machine that was off at
    `update_at` runs it on the first tick after it comes back rather than waiting for
    tomorrow. That is `StartWhenAvailable` expressed in the one place the scheduler's
    version of it cannot reach -- the scheduler catches up a missed *fire*, and every
    fire happens here, so only this predicate knows a day was missed.
    """
    if not valid_time(update_at):
        return False
    today = now.date().isoformat()
    if last_update == today:
        return False
    return now.strftime("%H:%M") >= update_at


# --- the pass ----------------------------------------------------------------


@dataclass
class Pass:
    """What one run did, as the artifact and the exit code consume it."""

    lines: list[str] = field(default_factory=list)
    failures: int = 0

    def say(self, line: str) -> None:
        self.lines.append(line)

    def fail(self, line: str) -> None:
        self.lines.append(line)
        self.failures += 1


def start_server(
    project: Path, name: str, claude: str, config: Config, logs: Path
) -> tuple[int, str]:
    """Launch one server. `(pid, error)` -- pid is 0 when it could not be started.

    The child's streams go to a per-project file rather than to `DEVNULL`. A server that
    refuses to start says why on stderr and then exits, and `DEVNULL` is the difference
    between this job reporting "started, then gone" every fifteen minutes forever and
    reporting the reason once.
    """
    logs.mkdir(parents=True, exist_ok=True)
    output = logs / f"rc-{name}.out"
    try:
        handle = output.open("w", encoding="utf-8")
    except OSError as error:
        return 0, str(error)
    try:
        process = subprocess.Popen(
            launch_argv(claude, name, config),
            cwd=str(project),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW | NEW_GROUP,
        )
    except OSError as error:
        handle.close()
        return 0, str(error)
    finally:
        # The child holds its own duplicate of the handle; this one is the parent's and
        # keeping it open pins the file for as long as the scheduled task lives.
        if not handle.closed:
            handle.close()
    return process.pid, ""


def stop_server(
    pid: int, run: Runner = run_command, sleep: Callable[[float], None] = time.sleep
) -> str:
    """Ask `pid` to exit, insist if it does not. "" on success, else the reason."""
    try:
        run(stop_argv(pid))
        sleep(STOP_GRACE_SECONDS)
        if not pid_is_server(pid, run):
            return ""
        result = run(stop_argv(pid, force=True))
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    if result.returncode != 0:
        return (result.stderr or result.stdout or "taskkill failed").strip()
    return ""


def ensure_up(
    names: Sequence[str],
    root: Path,
    state: State,
    claude: str,
    config: Config,
    logs: Path,
    run: Runner = run_command,
    report: Pass | None = None,
) -> Pass:
    """Start a server for every named project that has not got a live one."""
    result = report or Pass()
    for name in names:
        pid = state.servers.get(name, 0)
        if pid and pid_is_server(pid, run):
            result.say(f"{name}: up (pid {pid})")
            continue
        started, error = start_server(root / name, name, claude, config, logs)
        if started:
            state.servers[name] = started
            result.say(f"{name}: started (pid {started})")
        else:
            state.servers.pop(name, None)
            result.fail(f"{name}: could not start -- {error}")
    return result


def cycle(
    names: Sequence[str],
    root: Path,
    state: State,
    claude: str,
    config: Config,
    logs: Path,
    store: Path,
    run: Runner = run_command,
    report: Pass | None = None,
    update: Callable[..., object] | None = None,
) -> tuple[Pass, bool]:
    """Stop every server, update the CLI, start them again. `(report, ran)`.

    `ran` is false when a project was busy, and nothing is stopped in that case --
    stopping *some* servers to update a binary that a still-running one is executing
    would fail the update and cost the stopped sessions for nothing.

    The order is the whole point of this function and is the reason the update is called
    from here rather than left to `global-tools.py`: Windows cannot replace a running
    image, so the update only has anything to do in the gap between the two loops.
    """
    result = report or Pass()
    busy = [name for name in names if not is_idle(root / name, store, config.idle_minutes)]
    if busy:
        result.say(f"update deferred -- active in {', '.join(sorted(busy))}")
        return result, False

    for name in names:
        pid = state.servers.get(name, 0)
        if not pid:
            continue
        error = stop_server(pid, run)
        if error:
            result.fail(f"{name}: could not stop pid {pid} -- {error}")
        else:
            result.say(f"{name}: stopped (pid {pid})")
        state.servers.pop(name, None)

    # Only Claude: `codex` has nothing to do with a Remote Control server, and updating
    # it here would make this job's success depend on a CLI it never stopped -- one
    # Codex session open anywhere would redden a pass that did its own work correctly.
    #
    # No `run=` either. `agent_clis` has its own `Runner`, which takes a timeout this
    # module's does not; handing it the local one type-checks only because both are
    # callables, and fails at the first call.
    updater = agent_clis.run_pass if update is None else update
    outcome = updater(agents=agent_clis.select_agents(["claude"]), yes=True)
    for line in getattr(outcome, "lines", ()):
        result.say(f"  {line}")
    if getattr(outcome, "failures", 0):
        result.fail(f"claude update: {getattr(outcome, 'summary', 'failed')}")
    else:
        result.say(f"claude update: {getattr(outcome, 'summary', 'done')}")

    ensure_up(names, root, state, claude, config, logs, run, result)
    return result, True


def render(lines: Sequence[str], failures: int, when: _dt.datetime) -> str:
    """The artifact. Written on every exit path, including the ones with nothing to say.

    A file whose mtime moves every fifteen minutes is how a reader tells "the job is
    healthy and quiet" from "the job has not run since Tuesday", which is exactly the
    failure `tests/test_scheduled_jobs.py` exists because of.
    """
    head = f"# rc-servers {when.isoformat(timespec='seconds')} -- {failures} failure(s)"
    return "\n".join([head, *lines, ""])


def write_artifact(text: str, root: Path = REPO_ROOT) -> None:
    path = root / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=("status", "up", "cycle", "maintain", "down"),
        help=(
            "status: report only (default). up: start what is down. cycle: stop, update "
            "the CLI, start again. maintain: what the scheduler runs -- up, plus cycle "
            "once a day. down: stop everything."
        ),
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument(
        "--devkit",
        type=Path,
        default=REPO_ROOT,
        help="the devkit checkout to write the artifact under (default: this one)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = args.devkit.expanduser().resolve()
    workspace = args.workspace or sweep.default_workspace(root)
    now = _dt.datetime.now()
    report = Pass()

    def finish(code: int) -> int:
        """Every exit path leaves the artifact, including this one's failures."""
        text = render(report.lines, report.failures, now)
        write_artifact(text, root)
        print(text, end="")
        return code

    if workspace is None or not Path(workspace).is_file():
        report.fail(f"no workspace file at {workspace}")
        return finish(2)

    text = Path(workspace).read_text(encoding="utf-8")
    config = parse_config(text)
    if not config.projects:
        report.say(f"nothing to serve -- no `{RC_SETTING}` in {Path(workspace).name}")
        return finish(0)

    names, notes = selected(config, sweep.parse_workspace(text), sweep.on_hold(text))
    for note in notes:
        report.fail(note)
    if not names:
        return finish(2 if report.failures else 0)

    projects_root = Path(workspace).parent
    logs = root / ARTIFACT.parent
    state = State.load(root / STATE)
    store = sessions_store()

    if args.mode == "status":
        for name in names:
            pid = state.servers.get(name, 0)
            live = "up" if pid and pid_is_server(pid) else "down"
            quiet = (
                "idle" if is_idle(projects_root / name, store, config.idle_minutes) else "active"
            )
            report.say(f"{name}: {live} (pid {pid or '-'}), {quiet}")
        report.say(f"last update: {state.last_update or 'never'}")
        return finish(0)

    claude = claude_executable()
    if not claude and args.mode != "down":
        report.fail("claude is not on PATH and not in ~/.local/bin -- nothing can be started")
        return finish(2)

    if args.mode == "down":
        for name in names:
            pid = state.servers.get(name, 0)
            if not pid:
                continue
            error = stop_server(pid)
            report.fail(f"{name}: {error}") if error else report.say(f"{name}: stopped")
            state.servers.pop(name, None)
        state.save(root / STATE)
        return finish(2 if report.failures else 0)

    if args.mode == "cycle" or (
        args.mode == "maintain" and due_for_update(state.last_update, now, config.update_at)
    ):
        _, ran = cycle(names, projects_root, state, claude, config, logs, store, report=report)
        if ran:
            state.last_update = now.date().isoformat()
        state.save(root / STATE)
        return finish(2 if report.failures else 0)

    ensure_up(names, projects_root, state, claude, config, logs, report=report)
    state.save(root / STATE)
    return finish(2 if report.failures else 0)


if __name__ == "__main__":
    sys.exit(main())
