#!/usr/bin/env python3
"""The machine half of the Remote Control job: processes, transcripts, and state.

`rc_config.py` reads what to serve; this reaches the machine to find out what is true
and to change it; `rc-servers.py` is the pass that drives both and is the only one of
the three the scheduler names.

**Unknown is busy, on every path here.** A store that cannot be read (`last_activity`
returning `None`) and a machine that cannot be asked about a pid (`pid_is_server`
returning `True` on a failed enumeration) both fall on the side of "someone is working".
That is `agent_clis`' rule for the same question, and its reason: not knowing is the one
state where acting is a guess about live work. The cost of getting this backwards is an
interrupted turn on someone's phone.
"""

from __future__ import annotations

import csv
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
import rc_config
import sweep

# Seconds to let a server exit after a polite `taskkill` before insisting. Short: the
# server has no cleanup to do that survives it -- the sessions are resumed from the
# directory, not from anything the process writes on the way out.
STOP_GRACE_SECONDS = 10

QUICK_TIMEOUT = 30

# `CREATE_NO_WINDOW` off Windows is zero, via `sweep`, which already owns that `getattr`
# and the reasoning for it.
NO_WINDOW: int = sweep.NO_WINDOW

# A server outlives the pass that starts it, so it must not share the scheduler's
# console control group: a Ctrl-Break delivered to that process would otherwise reach
# every server it had ever started. Valid alongside `CREATE_NO_WINDOW`, which
# `DETACHED_PROCESS` is not -- the two are mutually exclusive console dispositions, and
# pairing them is a `ValueError` at the spawn rather than a detached child.
NEW_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

WINDOWS = os.name == "nt"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """The short, captured probes this module makes: `tasklist` and `taskkill`.

    Not `agent_clis.run_command`, whose `Runner` takes a timeout as a second positional
    argument. The two signatures are both callables and would type-check against each
    other while failing at the first call, which is exactly what happened once here.
    """
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=QUICK_TIMEOUT,
        creationflags=NO_WINDOW,
    )


# --- is anyone working in there? --------------------------------------------


def slug(cwd: Path | str) -> str:
    """A directory's name in Claude Code's transcript store.

    The store keys a directory by its path with every character that is not a letter, a
    digit or a hyphen replaced by one, so `C:\\Users\\alexa\\vs-code\\devkit` is filed
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

    The mtime rather than a timestamp parsed out of the file, for `resume-sessions.py`'s
    reason: the store appends to a session's transcript for as long as the session
    lives, so the file's mtime is its last activity and reading it costs one syscall
    instead of a megabyte of JSON.

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
    """Whether `cwd` has been quiet long enough to restart its server."""
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

    Off Windows, and whenever the machine cannot be asked, the answer is "yes" -- see
    the module docstring.
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


def launch_argv(claude: str, name: str, config: rc_config.Config) -> list[str]:
    """The server command for one project.

    `--name` is not cosmetic. Without it the auto-generated title is the hostname plus a
    random pair of words, so every project on this machine arrives at the phone as
    `<host>-<adjective>-<noun>` and the session list is unusable for the one thing it is
    for -- picking the right project on a small screen.
    """
    argv = [
        claude,
        "remote-control",
        "--name",
        name,
        "--spawn",
        config.spawn,
        # Bounded on purpose. A server holds every session the phone spawns for as long
        # as it lives, so the default of 32 is an unbounded-in-practice memory budget for
        # a process that is meant to run all week.
        "--capacity",
        str(config.capacity),
    ]
    if config.permission_mode:
        argv += ["--permission-mode", config.permission_mode]
    return argv


def user_idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input, or `None` when unanswerable.

    `GetLastInputInfo` through `ctypes`, which is stdlib -- the harness contract forbids
    an installed dependency here, and this is the whole of what a `psutil`-shaped one
    would have been used for.

    The value is session-wide rather than per-window, which is exactly the question
    being asked: is a person at this desk. `None` off Windows, and on any failure.
    """
    if not WINDOWS:
        return None
    import ctypes

    class LastInput(ctypes.Structure):
        _fields_ = (("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong))

    info = LastInput()
    info.cbSize = ctypes.sizeof(LastInput)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ticks = ctypes.windll.kernel32.GetTickCount64()
    except (AttributeError, OSError):
        return None
    # Both are milliseconds since boot. `GetTickCount64` rather than `GetTickCount` so
    # the arithmetic does not wrap after 49 days of uptime -- on a desktop left on all
    # the time, which is the machine this feature is for, that wrap is not hypothetical.
    return max(0.0, (ticks - info.dwTime) / 1000.0)


def at_the_desk(away_minutes: int) -> bool:
    """Whether someone is using this machine right now.

    `False` when the question cannot be answered, which is the safe direction here and
    the opposite of the rule elsewhere in this module: the *action* gated on this is
    stopping servers, and a wrong "yes" destroys sessions. Everywhere else the action is
    a restart and the danger is interrupting one, so unknown means busy. The rule is not
    "unknown is always X" -- it is "unknown never destroys".
    """
    idle = user_idle_seconds()
    if idle is None:
        return False
    return idle < away_minutes * 60


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
    local = Path.home() / ".local" / "bin"
    for candidate in (local / "claude.exe", local / "claude"):
        if candidate.is_file():
            return str(candidate)
    return ""


def start_server(
    project: Path, name: str, claude: str, config: rc_config.Config, logs: Path
) -> tuple[int, str]:
    """Launch one server. `(pid, error)` -- pid is 0 when it could not be started.

    The child's streams go to a per-project file rather than to `DEVNULL`. A server that
    refuses to start says why on stderr and then exits, and `DEVNULL` is the difference
    between this job reporting "started, then gone" every fifteen minutes forever and
    reporting the reason once.
    """
    try:
        logs.mkdir(parents=True, exist_ok=True)
        handle = (logs / f"rc-{name}.out").open("w", encoding="utf-8")
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
        return 0, str(error)
    finally:
        # The child holds its own duplicate; this one is the parent's, and keeping it
        # open pins the file for as long as the scheduled task lives.
        handle.close()
    return process.pid, ""


# --- state -------------------------------------------------------------------


@dataclass
class State:
    """The pids this job started, and the date it last ran the update.

    Only the pids *this job* started. A server someone launched by hand is not its to
    restart or to kill, and there is no other way to tell the two apart.
    """

    servers: dict[str, int] = field(default_factory=dict)
    last_update: str = ""

    @classmethod
    def load(cls, path: Path) -> "State":
        """Read it, treating anything unreadable as empty.

        A corrupt state file must not wedge the job: the worst an empty one causes is a
        pass that starts servers it thinks are missing, and `pid_is_server` plus
        `remote-control`'s own same-directory resume make that recoverable. A crash here
        would leave every server down until someone read a log nobody watches.
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
        payload = {"servers": self.servers, "last_update": self.last_update}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
