#!/usr/bin/env python3
"""Install the elevated worker on this PC: the one job devkit runs as an administrator.

**This is the single exception to two rules**, and the only one there can be: scheduled
jobs run unelevated, and no job needs a trigger or a principal only an administrator may
register (`scripts/CLAUDE.md`). Some recoveries cannot be had any other way: a service's
DACL refuses the user (`elevated_worker.py` has the measurements). So a person registers
this once per PC from an elevated terminal, and **no job can do it for them**, which is
why `installers.py` holds this file out of `discover`, and why `--yes` refuses to run
without elevation rather than failing halfway.

**Nothing elevated comes from a folder the user can write.** The auto-updating checkout is
such a folder, so a task pointed at it would let any merged change rewrite what runs as
admin. So is the interpreter on a typical PC, under the user's `AppData`: running that,
with the worker safely copied, still hands admin to anything that can replace
`python314.dll`. The install therefore copies both into `C:\\ProgramData\\devkit\\elevated\\`:

- **the worker**, so upgrading it is this deliberate re-run and nothing else, and
- **a private interpreter** of 36 MB or so, stdlib only, with `site-packages` left behind.

Its `<Command>` is a literal path, never `%windir%`, and the task runs in the user's
session and so with their environment, so the interpreter takes two guards. They are not
redundant: `-I -S` ignores `PYTHONPATH`, `PYTHONHOME` and `PYTHONINSPECT` (probed), while a
`._pth` file alone still honours `PYTHONINSPECT` (probed: it sat at a prompt). The `._pth`
pins `sys.path`, and is documented to shut out the registry's `PythonPath` too, which
sits under `HKCU` and so is also the user's to write.

**Three things that look like the house pattern and must not be used here:**

- `devkit_schtasks.secured` grants the registering user full access to the task, so that
  unelevated jobs may replace it. On this task that is privilege escalation: a user who may
  rewrite the action of a task that runs elevated is an administrator. `task_document`
  carries its own read-only descriptor, which `secured` then leaves alone.
- `devkit_schtasks.logged` would run `log-wrap.py` from the checkout, as admin. So this job
  is off the ledger: the worker's exit code reaches `schedule_health` through the scheduler,
  and its account is `ARTIFACT`, outside `logs/` because it may write nowhere else.
- `C:\\ProgramData` lets any user create a folder and thereby own it, and an owner can
  rewrite any DACL. So inheritance is cut, and `claim_root` refuses a folder it did not
  make rather than adopting whatever is in it. `elevated_request.request` never creates one.

`--check` needs no elevation and is what a person reads; `fix_elevated.py` asks the same
question of `elevated_request.state` each pass, and files the morning step while it is owed.

Elevated, this run writes no bytecode: `__pycache__` in the checkout is exactly the
root-owned file `elevated_writes.py` exists to catch.

Windows-only by nature. On any other platform it says so and exits 0.

Tested in `tests/test_install_elevated_worker.py`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# Before the first import of ours, which is what would write it: see the module docstring.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: E402 - the line above has to run before these, and only `sys.path` is exempt
import devkit_schtasks
import docker_vhdx
import elevated_request
import harness_state
import installer_cli
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

TASK_NAME = elevated_request.TASK_NAME
GROUP = "maintenance"

ROOT = elevated_request.ROOT
SPOOL = elevated_request.SPOOL
HOME = elevated_request.HOME
WORKER_NAME = elevated_request.WORKER_NAME
SENTINEL = "spool.keep"

ARTIFACT = "C:/ProgramData/devkit/elevated/worker.log"

INTERVAL_MINUTES = 5
# Above a verb's two steps and their settle, below the next few fires: `IgnoreNew` means
# one wedged run suppresses every fire until this expires.
TIME_LIMIT = "PT15M"

INTERPRETER = HOME + r"\python"
PYTHONW = INTERPRETER + r"\pythonw.exe"
PYTHON = INTERPRETER + r"\python.exe"
# Isolated, no `site`, no bytecode. The module docstring has why each of the first two.
INTERPRETER_FLAGS = ("-I", "-S", "-B")
# What the worker imports, tried once on the copy before any task points at it.
SMOKE = "import csv, ctypes, json, subprocess, dataclasses, stat, traceback"
SMOKE_TIMEOUT = 60

# The private interpreter is the DLLs, `DLLs\` and `Lib\`, minus everything below.
INTERPRETER_FILES = ("python.exe", "pythonw.exe", "python3*.dll", "vcruntime*.dll")
INTERPRETER_FOLDERS = ("Lib", "DLLs")
LEFT_BEHIND = (
    "site-packages",
    "__pycache__",
    "test",
    "idlelib",
    "tkinter",
    "turtledemo",
    "ensurepip",
)

SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
# The principal: whoever is logged on, at the highest level their token holds. The shape
# of Windows' own `SilentCleanup`. A group rather than a user, so nothing depends on which
# account the elevated terminal belonged to. In their session, because `GetLastInputInfo`
# is session-local and would read always-idle from session 0.
PRINCIPALS = (
    "  <Principals>\n"
    '    <Principal id="Author">\n'
    f"      <GroupId>{USERS}</GroupId>\n"
    "      <RunLevel>HighestAvailable</RunLevel>\n"
    "    </Principal>\n"
    "  </Principals>\n"
)
# Administrators and SYSTEM may change it; everyone else may read it, so the unelevated
# `--check` and `schedule_health` can. See the module docstring for why not `secured`.
SECURITY = "D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;FR;;;AU)"

WINDOWS = os.name == "nt"

# The interpreter whose folder is copied; see `devkit_schtasks.windowless`.
windowless = devkit_schtasks.windowless


def worker_arguments() -> str:
    """The task's `<Arguments>`: the interpreter's guards, then the installed copy."""
    return f'{" ".join(INTERPRETER_FLAGS)} "{HOME}\\{WORKER_NAME}"'


def elevate(document: str) -> str:
    """A `devkit_schtasks.task_xml` document as the one job that runs elevated. Here and
    not in `devkit_schtasks`, so the shared builder has no way to spell this and a test
    can hold `HighestAvailable` to this one file."""
    return (
        document.replace(
            "</RegistrationInfo>",
            f"  <SecurityDescriptor>{SECURITY}</SecurityDescriptor>\n  </RegistrationInfo>",
            1,
        )
        .replace("  <Settings>\n", PRINCIPALS + "  <Settings>\n", 1)
        .replace("<Actions>", '<Actions Context="Author">', 1)
    )


def task_document() -> str:
    """The task, with no value in it from this checkout or this user: every path literal,
    so `--check` compares what `--yes` would register from any tree."""
    return elevate(
        devkit_schtasks.task_xml(
            PYTHONW,
            worker_arguments(),
            devkit_schtasks.repeating_trigger(INTERVAL_MINUTES),
            time_limit=TIME_LIMIT,
            working_dir=HOME,
            enabled=TASK_NAME not in harness_state.stood_down(),
        )
    )


def _run_argv(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`devkit_schtasks.Runner` shape: a spawn failure is a returncode, not a traceback."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def own_argv(path: str) -> list[str]:
    return ["icacls", path, "/setowner", f"*{ADMINISTRATORS}"]


def lock_argv(path: str) -> list[str]:
    """Admin-only below `path`, by SID so a localised group name cannot break it. Cutting
    inheritance is the point: `C:\\ProgramData` gives every user create rights."""
    return [
        "icacls",
        path,
        "/inheritance:r",
        "/grant:r",
        f"*{SYSTEM}:(OI)(CI)F",
        f"*{ADMINISTRATORS}:(OI)(CI)F",
        f"*{USERS}:(OI)(CI)RX",
    ]


def spool_argv(path: str, sid: str) -> list[str]:
    """The user may add a file to the spool and nothing else: no subfolders, so no
    junctions, and no delete, so not the sentinel. No `(OI)`, so the grant stays on the
    folder and reaches no file in it."""
    return ["icacls", path, "/grant", f"*{sid}:(WD)"]


def refusal(elevated: bool, static_checkout: bool) -> str:
    """Why this run may not install; "" when it may."""
    if not elevated:
        return (
            "install-elevated-worker: this needs an elevated terminal. Open one as "
            f"administrator and run: {elevated_request.install_command(REPO_ROOT)}"
        )
    if not static_checkout:
        return (
            f"install-elevated-worker: {REPO_ROOT} is a temporary checkout, whose work has not "
            "landed. What runs as admin comes from the static devkit checkout: run it there."
        )
    return ""


def root_problems() -> str:
    """Why `ROOT` is not admin-only, read back off the folder; "" when it is."""
    return "; ".join(elevated_request.acl_problems(elevated_request.descriptor(ROOT)))


def claim_root(run: installer_cli.Runner = _run_argv) -> str:
    """Make `ROOT` admin-only, or say why not. Refuses a folder that was not made admin-only
    by an earlier install, because whoever owns it can undo any ACL set here. The result is
    read back rather than taken from `icacls`' exit code: a folder keeps an ACE `/grant:r`
    did not name, and exit 0 says nothing about that."""
    if os.path.lexists(ROOT):
        if os.path.islink(ROOT) or os.path.isjunction(ROOT):
            return f"{ROOT} is a link. Delete it, then re-run."
        problems = root_problems()
        if problems:
            return (
                f"{ROOT} exists and an install did not make it ({problems}). Check what is in "
                "it, delete it from this terminal, then re-run."
            )
        return ""
    try:
        os.mkdir(ROOT)
    except FileExistsError:
        return f"{ROOT} appeared while this ran, so this did not make it. Re-run to check it."
    failed = _each(run, (own_argv(ROOT), lock_argv(ROOT)))
    if failed:
        return failed
    problems = root_problems()
    return (
        f"{ROOT} did not end up admin-only ({problems}). Nothing was installed." if problems else ""
    )


def _each(run: installer_cli.Runner, commands: Sequence[Sequence[str]]) -> str:
    """Run each command until one fails; what it said, or "" when all passed."""
    for argv in commands:
        result = run(argv)
        if result.returncode:
            return (result.stderr or result.stdout or f"{argv[0]} failed").strip()
    return ""


def copy_interpreter(source: Path, target: Path) -> None:
    """The private interpreter, from the install `source`. `symlinks=False`, the default,
    so a link in the source becomes a file here rather than a way back out of the folder."""
    target.mkdir()
    for pattern in INTERPRETER_FILES:
        for file in source.glob(pattern):
            shutil.copyfile(file, target / file.name)
    for folder in INTERPRETER_FOLDERS:
        shutil.copytree(
            source / folder, target / folder, ignore=shutil.ignore_patterns(*LEFT_BEHIND)
        )
    pth = f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    (target / pth).write_text("\n".join(INTERPRETER_FOLDERS) + "\n", encoding="ascii")


def install_files(source: Path, sid: str, run: installer_cli.Runner) -> str:
    """Everything under `ROOT`, ending with the interpreter tried once. "" when it worked."""
    home = Path(HOME)
    spool = Path(SPOOL)
    home.mkdir(exist_ok=True)
    spool.mkdir(exist_ok=True)
    (spool / SENTINEL).touch()
    if os.path.isdir(INTERPRETER):
        shutil.rmtree(INTERPRETER)
    copy_interpreter(source, Path(INTERPRETER))
    shutil.copyfile(REPO_ROOT / "scripts" / WORKER_NAME, home / WORKER_NAME)
    return _each(
        run,
        (spool_argv(SPOOL, sid), [PYTHON, *INTERPRETER_FLAGS, "-c", SMOKE]),
    )


def install(run: installer_cli.Runner = _run_argv) -> int:
    """`--yes`. Files first, so nothing is scheduled against a folder that is not ready."""
    claimed = claim_root(run)
    if claimed:
        print(claimed, file=sys.stderr)
        return 1
    sid = devkit_schtasks.user_sid(run)
    if not sid:
        print("could not read this user's SID, so the spool cannot name them", file=sys.stderr)
        return 1
    try:
        failed = install_files(Path(windowless(sys.executable)).parent, sid, run)
    except OSError as exc:
        # The likeliest is the worker mid-run, holding its interpreter open.
        print(f"{exc}. If the worker was running, re-run in a minute.", file=sys.stderr)
        return 1
    if failed:
        print(failed, file=sys.stderr)
        return 1
    ok, message = devkit_schtasks.register(TASK_NAME, task_document(), run)
    print(message if ok else f"the files are in place, the task is not: {message}")
    return 0 if ok else 1


def check(run: installer_cli.Runner = _run_argv) -> int:
    """`--check`: the shared check on the task, then this PC's copy and folders."""
    code, message = devkit_schtasks.run_check(TASK_NAME, task_document(), run)
    if code:
        print(message, file=sys.stderr)
        return code
    state, why = elevated_request.state(REPO_ROOT, run)
    if state != elevated_request.CURRENT:
        print(f"{TASK_NAME} is {state}: {why}. Re-run with --yes, elevated.", file=sys.stderr)
        return devkit_schtasks.CHECK_STALE
    print(message)
    return devkit_schtasks.CHECK_CURRENT


def uninstall(run: installer_cli.Runner = _run_argv) -> int:
    """`--uninstall --yes`: the task, then `ROOT`, which only an administrator may delete."""
    handled = installer_cli.answer(
        TASK_NAME, status=False, uninstall=True, apply=True, run=run, windows=WINDOWS
    )
    if handled:
        return handled
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    return 0


def plan() -> str:
    return (
        f"Would, as an administrator:\n"
        f"  make       {ROOT}, admin-only, inheritance cut\n"
        f"  copy       this interpreter's stdlib to {INTERPRETER}\n"
        f"  copy       scripts/{WORKER_NAME} to {HOME}\n"
        f"  make       {SPOOL}, where this user may add a file and nothing else\n"
        f"  register   {TASK_NAME}, every {INTERVAL_MINUTES} minutes, at the highest run level,\n"
        f"             as whoever is logged on, with no window\n"
        f"  records    {ARTIFACT}\n\n"
        f"The installed copy does not follow this checkout: upgrading it is this same\n"
        f"command, run again. One administrator action per PC, which no job can take.\n\n"
        f"Dry run. From an elevated terminal: {elevated_request.install_command(REPO_ROOT)}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--uninstall", action="store_true", help="take it off this PC")
    mode.add_argument("--status", action="store_true")
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether this PC's task, copy and folders are current; needs no elevation",
    )
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually do it")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not WINDOWS:
        print("install-elevated-worker: Windows-only; nothing to do here.")
        return 0
    if args.check:
        return check()
    if args.status or (args.uninstall and not args.apply):
        handled = installer_cli.answer(
            TASK_NAME,
            status=args.status,
            uninstall=args.uninstall,
            apply=False,
            run=_run_argv,
            windows=WINDOWS,
        )
        return handled or 0
    if not args.apply:
        print(plan())
        return 0
    static = sweep.source_checkout(REPO_ROOT) == REPO_ROOT
    why = refusal(docker_vhdx.is_elevated(), static or args.uninstall)
    if why:
        print(why, file=sys.stderr)
        return 1
    return uninstall() if args.uninstall else install()


if __name__ == "__main__":
    sys.exit(main())
