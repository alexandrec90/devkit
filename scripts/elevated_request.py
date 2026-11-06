#!/usr/bin/env python3
"""The one way an unelevated process asks the elevated worker for something.

`request(verb)` leaves an empty file in the spool, and that is all an unelevated process
can do: the worker reads the file's name and never its content
(`elevated_worker.py` has the threat model). This module also answers the question the
operator's morning list needs, `state`: is this PC's worker installed, and is it the one
this checkout ships. Installing is one administrator action per PC, which no job can take,
so a missing or outdated worker is a person's step (`fix_elevated.py`).

`VERBS` is the list. `elevated_worker.VERBS` is a copy, because the worker runs from a
folder where it can import nothing of ours; a test holds the two equal.

Windows-only: elsewhere `request` says so, and `state` reports nothing to do.

Tested in `tests/test_elevated_request.py`.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

TASK_NAME = "devkit-elevated"
VERBS = ("restart-wsl-service", "restart-docker-service", "reboot-when-idle")

ROOT = r"C:\ProgramData\devkit"
SPOOL = ROOT + r"\requests"
HOME = ROOT + r"\elevated"
WORKER_NAME = "elevated_worker.py"
WORKER = HOME + "\\" + WORKER_NAME
REQUEST_SUFFIX = ".request"
# What a person runs, from an elevated terminal, in this checkout.
CHECKOUT = Path(__file__).resolve().parents[1]
INSTALLER = Path("scripts") / "install-elevated-worker.py"

WINDOWS = os.name == "nt"

# `state`'s answers. `UNKNOWN` is a machine that could not say, which files nothing and
# retires nothing.
CURRENT = "current"
MISSING = "missing"
OUTDATED = "outdated"
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
_OWNER = re.compile(r"^O:(?P<owner>[A-Z]{2}|S-[\d-]+)")
_ACE = re.compile(r"\((?P<type>[^;]*);[^;]*;(?P<rights>[^;]*);[^;]*;[^;]*;(?P<sid>[^;)]*)")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def install_command(checkout: Path) -> str:
    """The exact command, with this machine's path in it."""
    return f'python "{checkout / INSTALLER}" --yes'


def not_installed(checkout: Path) -> str:
    return (
        f"the elevated worker is not installed on this PC: from an elevated terminal, run "
        f"`{install_command(checkout)}`. One administrator action per PC, which no job can "
        "take for you."
    )


def _create_empty(path: str) -> bool:
    """Create the empty file; False when a request is already waiting. No content is ever
    written, since none would be read."""
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


def request(verb: str, spool: str = SPOOL, worker: str = WORKER, checkout: Path = CHECKOUT) -> str:
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
        return not_installed(checkout)
    try:
        created = _create_empty(os.path.join(spool, verb + REQUEST_SUFFIX))
    except OSError as exc:
        return f"{verb} was not requested: the spool refused this user ({exc})"
    if not created:
        return f"{verb} is already waiting for the worker"
    return f"{verb} requested: the worker takes it within five minutes"


# --- is the installed worker the one this checkout ships --------------------------


def acl_problems(sddl: str, admins: frozenset[str] = ADMINS) -> list[str]:
    """What is wrong with a folder's descriptor for holding code an administrator runs.

    The owner counts as much as the ACEs: an owner may rewrite the DACL whatever it says,
    and `C:\\ProgramData` lets any user create a folder and so own it. Empty only for a
    folder only administrators can change.
    """
    owner = _OWNER.match(sddl)
    if owner is None:
        return [f"its descriptor could not be read: {sddl[:60]!r}"]
    problems = []
    if owner["owner"] not in admins:
        problems.append(f"owned by {owner['owner']}")
    for ace in _ACE.finditer(sddl):
        allows = ace["type"] == "A"
        if allows and ace["sid"] not in admins and ace["rights"] not in READ_RIGHTS:
            problems.append(f"{ace['sid']} holds {ace['rights']}")
    return problems


def descriptor(path: str) -> str:
    """`path`'s owner and DACL as SDDL; "" when it cannot be read.

    Through the API rather than `Get-Acl` in a child: a Windows PowerShell started from
    PowerShell 7 inherits a `PSModulePath` it cannot load its own security module from."""
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
    """`(CURRENT | MISSING | OUTDATED | UNKNOWN, why)` for this PC. Needs no elevation.

    The why is stable across runs, because a finding is filed from it."""
    if not WINDOWS:
        return CURRENT, "Windows-only"
    registered = run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode
    if registered not in (0, NOT_REGISTERED):
        return UNKNOWN, "the scheduler did not answer"
    if registered == NOT_REGISTERED or not os.path.isfile(worker):
        return MISSING, "not installed"
    try:
        if copy_differs(checkout / "scripts" / WORKER_NAME, Path(worker)):
            return OUTDATED, f"its copy differs from scripts/{WORKER_NAME}"
    except OSError:
        return UNKNOWN, "a copy could not be read"
    for folder in folders:
        held = read_descriptor(folder)
        if not held:
            return UNKNOWN, f"{folder}'s descriptor could not be read"
        if acl_problems(held):
            return OUTDATED, f"{folder} is not admin-only"
    return CURRENT, "installed and current"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=VERBS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    print(request(args.verb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
