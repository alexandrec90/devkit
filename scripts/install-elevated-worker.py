#!/usr/bin/env python3
"""Install the elevated worker on this PC: the one job devkit runs as an administrator.

**This is the single exception to two rules**, and the only one there can be: scheduled
jobs run unelevated, and no job needs a trigger or a principal only an administrator may
register (`scripts/CLAUDE.md`). One recovery cannot be had any other way: `WSLService`'s
DACL refuses the user (`elevated_worker.py` has the measurement). So a person registers
this once per PC from an elevated terminal, and **no job can do it for them**, which is
why `installers.py` holds this file out of `discover`, and why `--yes` refuses to run
without elevation rather than failing halfway.

**The residual risk, which cannot be closed.** This installer and the interpreter it copies
are read from folders the user can write: the checkout, and on a typical PC a Python
under `AppData`. Anything already running as that user can have changed either before
this runs, and an elevated run then carries that over. So **the install assumes a clean
machine at that moment.** `-I -S` keeps `PYTHON*` and user `site-packages` out of a run
started with them, and the re-exec below catches one started without, but by then the first
interpreter has already run `site`. That is why nothing in the harness prompts for this:
`fix_elevated.py` files a person's step only for a real request waiting on a PC with no
worker, and never for an upgrade. An unreviewed merge plus a prompted admin paste is the
dispatcher this design refused. A worker that is installed keeps working.

What the install does own is everything after that moment. The task runs a copy of the
worker and a private interpreter (36 MB or so, stdlib only) in
`C:\\ProgramData\\devkit\\elevated\\`, so a merged change cannot rewrite what runs as admin,
and neither can anything that writes to the user's Python.

Its `<Command>` is a literal path, never `%windir%`, and the task runs in the user's
session and so with their environment, so the interpreter takes two guards. They are not
redundant: `-I -S` ignores `PYTHONPATH`, `PYTHONHOME` and `PYTHONINSPECT` (probed), while a
`._pth` file alone still honours `PYTHONINSPECT` (probed: it sat at a prompt). The `._pth`
pins `sys.path`, and is documented to shut out the registry's `PythonPath` too, which
sits under `HKCU` and so is also the user's to write.

**What looks like the house pattern and must not be used here:**

- `devkit_schtasks.register` writes the document to a fixed path in the user's temp folder
  and an elevated `schtasks` then reads it: a watcher swapped it 20 rounds of 20.
  `register_task` writes it into the locked folder instead, with no user-writable file.
- `devkit_schtasks.secured` grants the registering user full access to the task, so that
  unelevated jobs may replace it. On this task that is privilege escalation: a user who may
  rewrite the action of a task that runs elevated is an administrator. `task_document`
  carries its own read-only descriptor, which `secured` then leaves alone. `/Create /F`
  keeps the access a task already had, so `register_task` deletes it first and reads the
  descriptor back.
- `devkit_schtasks.logged` would run `log-wrap.py` from the checkout, as admin. So this job
  is off the ledger: `elevated_request.results` is how its outcomes reach the pass, and its
  account is `ARTIFACT`, outside `logs/` because it may write nowhere else.
- `os.mkdir` then `icacls` leaves a gap in which the user can plant `HOME` or `requests`
  and keep owning them, because `C:\\ProgramData` lets any user create a folder and an
  owner can rewrite any DACL. `make_protected` creates `ROOT` with its descriptor in one
  call, `claim_root` refuses one that does not verify, and `tree_problems` reads back the
  owner and ACL of everything beneath before anything there runs, on a reinstall too.

Every program this runs is an absolute path with a scrubbed environment (`absolute`,
`CLEAN_ENV`). Elevated, this run writes no bytecode: `__pycache__` in the checkout is
exactly the root-owned file `elevated_writes.py` exists to catch.

Windows-only by nature. On any other platform it says so and exits 0.

Tested in `tests/test_install_elevated_worker.py`.
"""

from __future__ import annotations

import argparse
import ctypes
import ntpath
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from ctypes import wintypes
from pathlib import Path

WINDOWS = os.name == "nt"
SYSTEM32 = r"C:\Windows\System32"
# All an elevated child of this file gets. The user's would carry whatever anything
# unelevated put in `HKCU\Environment`.
CLEAN_ENV = {"SystemRoot": r"C:\Windows", "SystemDrive": "C:"}


def base_python() -> str:
    """The real interpreter: a virtualenv's `python.exe` is a stub for it."""
    candidate = os.path.join(sys.base_prefix, "python.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def isolated_argv(script: str, arguments: Sequence[str]) -> list[str]:
    """This run again under `-I -S`, or [] when it already is one."""
    if sys.flags.isolated and sys.flags.no_site:
        return []
    return [base_python(), "-I", "-S", "-B", script, *arguments]


def reexec(script: str, arguments: Sequence[str], run: Callable = subprocess.run) -> int | None:
    """The exit code of this run re-executed isolated; None when it needs no re-exec."""
    argv = isolated_argv(script, arguments)
    return run(argv, env=dict(CLEAN_ENV), cwd=SYSTEM32).returncode if argv else None


# First, before anything of ours is imported: the module docstring says how far this goes.
if __name__ == "__main__" and WINDOWS:
    _code = reexec(__file__, sys.argv[1:])
    if _code is not None:
        sys.exit(_code)

# Before the first import of ours, which is what would write it: see the module docstring.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: E402 - the lines above have to run before these, and only `sys.path` is exempt
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
SCRATCH = HOME + r"\tmp"
WORKER_NAME = elevated_request.WORKER_NAME
SENTINEL = "spool.keep"

ARTIFACT = "C:/ProgramData/devkit/elevated/worker.log"

INTERVAL_MINUTES = 5
# Above the worker's worst run, whose `wsl --update` alone may take ten minutes, and not by
# much: `IgnoreNew` means one wedged run suppresses every fire until this expires.
TIME_LIMIT = "PT25M"

# Programs named bare by the modules this drives, and what `absolute` makes of them.
TOOLS = frozenset({"icacls", "schtasks", "whoami"})

INTERPRETER = HOME + r"\python"
PYTHONW = INTERPRETER + r"\pythonw.exe"
PYTHON = INTERPRETER + r"\python.exe"
# Isolated, no `site`, no bytecode. The module docstring has why each of the first two.
INTERPRETER_FLAGS = ("-I", "-S", "-B")
# What the worker imports, tried once on the copy before any task points at it.
SMOKE = "import ctypes, json, re, subprocess, dataclasses, stat, traceback"

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

ADMINISTRATORS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
# `ROOT` from its first instant: owned by Administrators, protected, so nothing of
# `C:\ProgramData`'s `Users:(CI)(WD,AD,WEA,WA)` or `CREATOR OWNER` is inherited.
ROOT_DESCRIPTOR = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
# `CreateDirectoryW`'s errors, and the one `make_protected` answers off Windows.
ERROR_ALREADY_EXISTS = 183
ERROR_NOT_SUPPORTED = 50
# The principal: whoever is logged on, at the highest level their token holds. The shape
# of Windows' own `SilentCleanup`. A group rather than a user, so nothing depends on which
# account the elevated terminal belonged to.
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


def absolute(argv: Sequence[str]) -> list[str]:
    """`argv` naming its program in full: a bare name would be found through this
    process's folder, its cwd and the user's PATH. Only `TOOLS`, System32 and the private
    interpreter are ever run; anything else is a bug here and raises."""
    program = argv[0]
    if program in TOOLS:
        return [ntpath.join(SYSTEM32, f"{program}.exe"), *argv[1:]]
    if ntpath.isabs(program) and program.lower().startswith((SYSTEM32.lower(), ROOT.lower())):
        return list(argv)
    raise ValueError(f"{program!r} is not a program this installer runs")


def _run_argv(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`devkit_schtasks.Runner` shape: a spawn failure is a returncode, not a traceback.
    Absolute, scrubbed, and from a cwd the user cannot write."""
    command = absolute(argv)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=dict(CLEAN_ENV),
            cwd=SYSTEM32,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def own_argv(path: str) -> list[str]:
    """Administrators own everything below `path`. `/L` so a link is not followed, and the
    read-back in `tree_problems`, not this exit code, is what is believed."""
    return ["icacls", path, "/setowner", f"*{ADMINISTRATORS}", "/T", "/C", "/L"]


def spool_argv(path: str, sid: str) -> list[str]:
    """The user may add a file to the spool and nothing else: no subfolders, so no
    junctions, and no delete, so not the sentinel. No `(OI)`, so the grant stays on the
    folder and reaches no file in it."""
    return ["icacls", path, "/grant", f"*{sid}:(WD)"]


def make_protected(path: str, sddl: str) -> int:
    """Create `path` with `sddl` as its descriptor, in one call; 0, or the Win32 error.

    There is no moment at which the folder exists with any other access, which is the
    whole of the point, and an existing folder is an error rather than something reused.
    Elsewhere there is no such call, which is ERROR_NOT_SUPPORTED."""
    if sys.platform != "win32":  # also what tells mypy `WinDLL` exists below
        return ERROR_NOT_SUPPORTED

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    ]
    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    held = wintypes.LPVOID()
    if not convert(sddl, 1, ctypes.byref(held), None):
        return ctypes.get_last_error() or 1
    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), held, False)
    made = kernel32.CreateDirectoryW(path, ctypes.byref(attributes))
    error = 0 if made else ctypes.get_last_error() or 1
    kernel32.LocalFree(held)
    return error


def is_link(path: str) -> bool:
    """Whether `path` is any reparse point, junctions and mount points included."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    linked = attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return bool(linked) or stat.S_ISLNK(info.st_mode)


def judged(path: str, rights: frozenset[str] = elevated_request.READ_RIGHTS) -> list[str]:
    """What is wrong with `path` as a thing an administrator trusts: read back, not assumed."""
    if is_link(path):
        return [f"{path} is a link"]
    held = elevated_request.acl_problems(elevated_request.descriptor(path), rights=rights)
    return [f"{path}: {problem}" for problem in held]


def walk(top: str) -> Iterator[str]:
    """Every path below `top`, not following a link."""
    pending = [top]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                yield entry.path
                if entry.is_dir(follow_symlinks=False) and not is_link(entry.path):
                    pending.append(entry.path)


def spool_problems() -> list[str]:
    """The spool and what is in it. Plain files there are the user's by design, and not
    run or read by anything, so only what should not be there at all counts."""
    problems = judged(SPOOL, elevated_request.SPOOL_RIGHTS)
    for path in walk(SPOOL):
        if os.path.basename(path) == SENTINEL:
            problems += judged(path)
        elif is_link(path) or not os.path.isfile(path):
            problems.append(f"{path} does not belong in the spool")
    return problems


def tree_problems(complete: bool = False) -> list[str]:
    """Everything wrong with what is under `ROOT`: owner, ACL and links, entry by entry,
    `HOME` and `INTERPRETER` and all beneath them. `complete` for after an install, when
    what a first install has not made yet must exist."""
    problems = judged(ROOT)
    for folder in (HOME, SPOOL, *((INTERPRETER,) if complete else ())):
        if not os.path.lexists(folder):
            problems += [f"{folder} is missing"] if complete else []
        elif folder == SPOOL:
            problems += spool_problems()
        else:
            problems += judged(folder)
    if os.path.isdir(HOME) and not is_link(HOME):
        problems += [problem for path in walk(HOME) for problem in judged(path)]
    return problems


def claim_root() -> str:
    """Make `ROOT` admin-only from its first instant, or say why not. One that exists is
    kept only if its owner and ACL verify, since whoever owns it can undo any ACL set here;
    nothing is adopted, and nothing is repaired."""
    if os.path.lexists(ROOT):
        problems = judged(ROOT)
        if problems:
            return (
                f"{ROOT} exists and an install did not make it ({'; '.join(problems)}). Check "
                "what is in it, delete it from this terminal, then re-run."
            )
        return ""
    error = make_protected(ROOT, ROOT_DESCRIPTOR)
    if error == ERROR_ALREADY_EXISTS:
        return f"{ROOT} appeared while this ran, so this did not make it. Re-run to check it."
    if error:
        return f"{ROOT} could not be created (error {error}). Nothing was installed."
    problems = judged(ROOT)
    return f"{ROOT} did not end up admin-only ({'; '.join(problems)})." if problems else ""


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


def _clear_and_retry(remover: Callable, path: str, _error: BaseException) -> None:
    """`shutil.rmtree`'s `onexc`: a read-only file refuses a delete, even to an admin."""
    os.chmod(path, stat.S_IWRITE)
    remover(path)


def install_files(source: Path, sid: str, run: installer_cli.Runner) -> str:
    """Everything under `ROOT`, owners set. "" when it worked. Runs nothing from there."""
    home = Path(HOME)
    spool = Path(SPOOL)
    home.mkdir(exist_ok=True)
    Path(SCRATCH).mkdir(exist_ok=True)
    spool.mkdir(exist_ok=True)
    (spool / SENTINEL).touch()
    if os.path.isdir(INTERPRETER):
        shutil.rmtree(INTERPRETER, onexc=_clear_and_retry)
    copy_interpreter(source, Path(INTERPRETER))
    shutil.copyfile(REPO_ROOT / "scripts" / WORKER_NAME, home / WORKER_NAME)
    return _each(run, (own_argv(ROOT), spool_argv(SPOOL, sid)))


def register_task(run: installer_cli.Runner) -> str:
    """Register the task from a document in the locked folder; "" when it is registered
    and its descriptor reads back read-only.

    Not `devkit_schtasks.register`: see the module docstring. `/Create /F` keeps the
    access a task already has, so any existing one goes first, and a descriptor that still
    reads wrong takes the task with it, since a task the user can rewrite is worse than
    none."""
    if run(devkit_schtasks.query_xml_argv(TASK_NAME)).returncode == 0:
        failed = _each(run, (devkit_schtasks.delete_argv(TASK_NAME),))
        if failed:
            return f"the existing {TASK_NAME} could not be deleted: {failed}"
    path = devkit_schtasks.write_task_file(task_document(), Path(HOME))
    try:
        failed = _each(run, (devkit_schtasks.register_argv(TASK_NAME, path),))
    finally:
        path.unlink(missing_ok=True)
    if failed:
        return failed
    held = run(devkit_schtasks.query_xml_argv(TASK_NAME))
    problems = elevated_request.task_problems(held.stdout or "")
    if not problems:
        return ""
    run(devkit_schtasks.delete_argv(TASK_NAME))
    return f"{TASK_NAME} read back wrong ({'; '.join(problems)}), so it was deleted again."


def refused(problems: Sequence[str]) -> int:
    """Name a few of `problems` and stop. A tree that does not verify is not repaired."""
    print("\n".join(problems[:5]), file=sys.stderr)
    print(f"Nothing was run from {ROOT}. Check it, delete it, then re-run.", file=sys.stderr)
    return 1


def install(run: installer_cli.Runner = _run_argv) -> int:
    """`--yes`. Nothing is run from `ROOT`, and nothing is scheduled, until all of it has
    been read back, on a reinstall as on a first."""
    claimed = claim_root()
    if claimed:
        print(claimed, file=sys.stderr)
        return 1
    found = tree_problems()
    if found:
        return refused(found)
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
    found = tree_problems(complete=True)
    if found:
        return refused(found)
    failed = _each(run, ([PYTHON, *INTERPRETER_FLAGS, "-c", SMOKE],)) or register_task(run)
    if failed:
        print(failed, file=sys.stderr)
        return 1
    print(f"installed {TASK_NAME}")
    return 0


def check(run: installer_cli.Runner = _run_argv) -> int:
    """`--check`: the shared check on the task, then this PC's copy and folders. The one
    place a copy behind the checkout is reported: nothing prompts for it."""
    code, message = devkit_schtasks.run_check(TASK_NAME, task_document(), run)
    if code:
        print(message, file=sys.stderr)
        return code
    state, why = elevated_request.state(REPO_ROOT, run)
    if state != elevated_request.CURRENT:
        print(f"{TASK_NAME} is {state}: {why}.", file=sys.stderr)
        return devkit_schtasks.CHECK_STALE
    print(message)
    return devkit_schtasks.CHECK_CURRENT


def uninstall(run: installer_cli.Runner = _run_argv) -> int:
    """`--uninstall --yes`: the task, then `ROOT`, which only an administrator may delete."""
    handled = installer_cli.answer(
        TASK_NAME, status=False, uninstall=True, apply=True, run=run, windows=WINDOWS
    )
    # `answer` returns 0 both for a task it deleted and, off Windows, for "nothing to do
    # here", so the platform decides whether `ROOT` goes too: never on a machine it just
    # said it would leave alone.
    if handled or not WINDOWS:
        return handled or 0
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT, onexc=_clear_and_retry)
    return 0


def plan() -> str:
    return (
        f"Would, as an administrator:\n"
        f"  make       {ROOT}, admin-only from its first instant\n"
        f"  copy       this interpreter's stdlib to {INTERPRETER}\n"
        f"  copy       scripts/{WORKER_NAME} to {HOME}\n"
        f"  make       {SPOOL}, where this user may add a file and nothing else\n"
        f"  verify     the owner and ACL of everything there, before running any of it\n"
        f"  register   {TASK_NAME}, every {INTERVAL_MINUTES} minutes, at the highest run level,\n"
        f"             as whoever is logged on, with no window\n"
        f"  records    {ARTIFACT}\n\n"
        f"This reads the checkout and this Python, both of which this user can write, so it\n"
        f"assumes a clean machine right now. Nothing needs you to run it unless a request is\n"
        f"waiting on a PC with no worker. The installed copy does not follow this checkout:\n"
        f"it keeps working, and upgrading it is this same command, run again by choice.\n"
        f"One administrator action per PC, which no job can take.\n\n"
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
