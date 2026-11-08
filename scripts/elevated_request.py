#!/usr/bin/env python3
"""The one way an unelevated process asks the elevated worker for something.

`request(verb)` leaves an empty file in the spool, and that is all an unelevated process
can do: the worker reads the file's name and never its content
(`elevated_worker.py` has the threat model). This module also answers what `--check` and
the fix pass ask, with no elevation: `state`, whether this PC's worker is installed, current
and still shut to the user, and `results`, what its last runs came to.

**A PC with no worker is told so only when something wanted it.** With no spool, `request`
leaves a dated note in the checkout's `logs/` instead, and `fix_elevated.py` turns that
into a person's step. Nothing prompts for an install or an upgrade otherwise: an
unreviewed merge plus a prompted admin paste is the dispatcher this design refused. A
worker that is installed keeps working, whatever the checkout has moved on to.

`VERBS` is the list. `elevated_worker.VERBS` is a copy, because the worker runs from a
folder where it can import nothing of ours; a test holds the two equal.

Windows-only: elsewhere `request` says so, and `state` reports nothing to do.

Tested in `tests/test_elevated_request.py`.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

TASK_NAME = "devkit-elevated"
VERBS = ("restart-wsl-service",)

ROOT = r"C:\ProgramData\devkit"
SPOOL = ROOT + r"\requests"
HOME = ROOT + r"\elevated"
WORKER_NAME = "elevated_worker.py"
WORKER = HOME + "\\" + WORKER_NAME
RESULTS = HOME + r"\results.json"
REQUEST_SUFFIX = ".request"
CHECKOUT = Path(__file__).resolve().parents[1]
# What a person runs, from an elevated terminal, in this checkout.
INSTALLER = Path("scripts") / "install-elevated-worker.py"
# The worker's results the pass acts on; the rest clear what it filed.
REFUSED = "refused"
FAILED = "failed"

# A request made where there is no worker, in the checkout: `{verb: when}`.
WAITING = Path("logs") / "elevated-waiting.json"
# How long a request waits. Past this nobody has asked again, so the need is gone, and a
# person's step about it would no longer be true.
WAITING_FOR = 24 * 60 * 60

WINDOWS = os.name == "nt"

# `state`'s answers. `OUTDATED` is reported by `--check` and nothing else: the installed
# copy works, and only a deliberate re-run moves it. `EXPOSED` is the one that matters: the
# user can reach what runs elevated. `UNKNOWN` is a machine that could not say, which files
# nothing and retires nothing.
CURRENT = "current"
MISSING = "missing"
OUTDATED = "outdated"
EXPOSED = "exposed"
UNKNOWN = "unknown"

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
QUERY_TIMEOUT = 60
# `schtasks /Query`'s exit code for a task that is not registered.
NOT_REGISTERED = 1

# Who may hold more than read, and the rights that are read. An allow-list, so an ACE of a
# shape nobody anticipated counts as a hole rather than passing. The hex masks are how the
# OS spells read-and-execute and read in a file DACL.
ADMINS = frozenset({"BA", "SY", "S-1-5-32-544", "S-1-5-18"})
READ_RIGHTS = frozenset({"FR", "GR", "FX", "GX", "FRFX", "GRGX", "0x1200a9", "0x120089"})
# The one more the spool grants the user: add a file (`(WD)` to icacls, `DC` read back).
SPOOL_RIGHTS = READ_RIGHTS | {"DC"}
_OWNER = re.compile(r"^O:(?P<owner>[A-Z]{2}|S-[\d-]+)")
_ACE = re.compile(r"\((?P<type>[^;]*);[^;]*;(?P<rights>[^;]*);[^;]*;[^;]*;(?P<sid>[^;)]*)")
_TASK_DESCRIPTOR = re.compile(r"<SecurityDescriptor>(.*?)</SecurityDescriptor>", re.S)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def install_command(checkout: Path) -> str:
    """The exact command, with this machine's path in it. `-I -S` here and not only in the
    installer's own re-exec: by the time a script runs, `site` has already run."""
    return f'python -I -S "{checkout / INSTALLER}" --yes'


def not_installed(checkout: Path) -> str:
    return (
        f"the elevated worker is not installed on this PC: from an elevated terminal, run "
        f"`{install_command(checkout)}`. One administrator action per PC, which no job can "
        "take for you."
    )


def static_checkout() -> Path:
    """The permanent checkout, whose `logs/` the fix pass reads, from a box or a session
    tree as much as from itself."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
    import harness_events

    return harness_events.ledger_root() or CHECKOUT


def read_waiting(checkout: Path, now: float) -> list[str]:
    """Each verb asked for on this PC within `WAITING_FOR`, and that the list still has."""
    try:
        held = json.loads((checkout / WAITING).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(held, dict):
        return []
    return [
        verb
        for verb in VERBS
        if isinstance(held.get(verb), int | float) and now - held[verb] < WAITING_FOR
    ]


def note_waiting(checkout: Path, verb: str, now: float) -> None:
    """Say `verb` was wanted and there was no worker. A failure to is not the caller's
    problem: they already have the sentence that says so."""
    path = checkout / WAITING
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({verb: now}), encoding="utf-8")
    except OSError:
        return


def clear_waiting(checkout: Path) -> None:
    (checkout / WAITING).unlink(missing_ok=True)


def _create_empty(path: str) -> bool:
    """Create the empty file; False when a request is already waiting. No content is ever
    written, since none would be read."""
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


def request(
    verb: str, spool: str = SPOOL, worker: str = WORKER, checkout: Path | None = None
) -> str:
    """Ask for `verb`, and say what happened.

    A misspelled verb is a bug in the caller, so it raises. The rest are states of the
    machine and come back as the sentence. This never creates the spool: a non-admin who
    makes a folder under `C:\\ProgramData` owns it, and the installer refuses to take over
    a folder it did not make.
    """
    if verb not in VERBS:
        raise ValueError(f"{verb!r} is not one of {', '.join(VERBS)}")
    if not WINDOWS:
        return "elevated_request: Windows-only; nothing to do here."
    if not (os.path.isdir(spool) and os.path.isfile(worker)):
        root = checkout or static_checkout()
        note_waiting(root, verb, time.time())
        return not_installed(root)
    try:
        created = _create_empty(os.path.join(spool, verb + REQUEST_SUFFIX))
    except OSError as exc:
        return f"{verb} was not requested: the spool refused this user ({exc})"
    if not created:
        return f"{verb} is already waiting for the worker"
    return f"{verb} requested: the worker takes it within five minutes"


# --- is the installed worker the one this checkout ships --------------------------


def ace_problems(
    sddl: str, admins: frozenset[str] = ADMINS, rights: frozenset[str] = READ_RIGHTS
) -> list[str]:
    """Each ACE of `sddl` granting anyone but an administrator more than `rights`."""
    return [
        f"{ace['sid']} holds {ace['rights']}"
        for ace in _ACE.finditer(sddl)
        if ace["type"] == "A" and ace["sid"] not in admins and ace["rights"] not in rights
    ]


def acl_problems(
    sddl: str, admins: frozenset[str] = ADMINS, rights: frozenset[str] = READ_RIGHTS
) -> list[str]:
    """What is wrong with a folder's descriptor for holding code an administrator runs.

    The owner counts as much as the ACEs: an owner may rewrite the DACL whatever it says,
    and `C:\\ProgramData` lets any user create a folder and so own it. Empty only for a
    folder only administrators can change.
    """
    owner = _OWNER.match(sddl)
    if owner is None:
        return [f"its descriptor could not be read: {sddl[:60]!r}"]
    problems = [] if owner["owner"] in admins else [f"owned by {owner['owner']}"]
    return problems + ace_problems(sddl, admins, rights)


def task_problems(document: str) -> list[str]:
    """What is wrong with the registered task's descriptor, off `schtasks /Query /XML`.

    `devkit_schtasks.secured` hands a registering user full access so unelevated jobs may
    replace their tasks. On this one that would let the user rewrite what runs elevated,
    and `/Create /F` keeps whatever access the task already had. No descriptor at all is
    no better: the access is then whoever's registered it."""
    held = _TASK_DESCRIPTOR.search(document)
    if held is None or not held.group(1).strip():
        return ["it carries no security descriptor"]
    return ace_problems(held.group(1))


def descriptor(path: str) -> str:
    """`path`'s owner and DACL as SDDL; "" when it cannot be read.

    Through the API rather than `Get-Acl` in a child: a Windows PowerShell started from
    PowerShell 7 inherits a `PSModulePath` it cannot load its own security module from."""
    if sys.platform != "win32":  # also what tells mypy `windll` exists below
        return ""
    file_object, owner_and_dacl, sddl_revision = 1, 0x1 | 0x4, 1
    held = ctypes.c_void_p()
    text = ctypes.c_wchar_p()
    try:
        advapi32 = ctypes.windll.advapi32
        code = advapi32.GetNamedSecurityInfoW(
            path, file_object, owner_and_dacl, None, None, None, None, ctypes.byref(held)
        )
        if code != 0:
            return ""
        converted = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            held, sddl_revision, owner_and_dacl, ctypes.byref(text), None
        )
        return (text.value or "") if converted else ""
    except (AttributeError, OSError):
        return ""
    finally:
        for allocated in (held, text):
            if allocated:
                ctypes.windll.kernel32.LocalFree(allocated)


def run_query(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Windowless, since the scheduled pass reaches this. Failing to spawn is its own
    exit code, so it cannot read as `NOT_REGISTERED`."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=QUERY_TIMEOUT,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 3, "", str(exc))


def copy_differs(repo_worker: Path, installed: Path) -> bool:
    return repo_worker.read_bytes() != installed.read_bytes()


def state(
    checkout: Path,
    run: Runner = run_query,
    worker: str = WORKER,
    folders: Sequence[str] = (ROOT, HOME),
    read_descriptor: Callable[[str], str] = descriptor,
) -> tuple[str, str]:
    """`(CURRENT | MISSING | OUTDATED | EXPOSED | UNKNOWN, why)` for this PC. Needs no
    elevation, and the pass asks every run.

    Exposure is asked before age: a copy behind the checkout is nothing to act on, and a
    task the user can rewrite is. The why is stable across runs, because a finding is
    filed from it."""
    if not WINDOWS:
        return CURRENT, "Windows-only"
    query = run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
    if query.returncode not in (0, NOT_REGISTERED):
        return UNKNOWN, "the scheduler did not answer"
    if query.returncode == NOT_REGISTERED or not os.path.isfile(worker):
        return MISSING, "not installed"
    if task_problems(query.stdout or ""):
        return EXPOSED, f"the {TASK_NAME} task is not read-only to the user"
    for folder in folders:
        held = read_descriptor(folder)
        if not held:
            return UNKNOWN, f"{folder}'s descriptor could not be read"
        if acl_problems(held):
            return EXPOSED, f"{folder} is not admin-only"
    try:
        if copy_differs(checkout / "scripts" / WORKER_NAME, Path(worker)):
            return OUTDATED, f"its copy differs from scripts/{WORKER_NAME}"
    except OSError:
        return UNKNOWN, "a copy could not be read"
    return CURRENT, "installed and current"


def results(path: str = RESULTS) -> dict[str, tuple[str, str]]:
    """`{subject: (result, detail)}`, the worker's latest word on each: its own record, in
    a folder the user can read and not write. Nothing there is an empty answer."""
    try:
        with open(path, encoding="utf-8") as handle:
            held = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(held, dict):
        return {}
    return {
        str(subject): (str(entry.get("result", "")), str(entry.get("detail", "")))
        for subject, entry in held.items()
        if isinstance(entry, dict)
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=VERBS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    print(request(args.verb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
