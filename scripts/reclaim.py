#!/usr/bin/env python3
"""Machine-scope resource reclaim: what to run when the laptop has gone slow.

Backs the "Machine: Reclaim Resources" task. There are already tools here for pieces of
this -- `docker-maint.py` is DAEMON-scoped and `worktree.py` is BOX-scoped -- and neither
owns the question the user actually asks, which is "why is this machine slow now". On
this hardware that turned out to be three unrelated accumulations at once (2026-08-20),
which is why a third scope exists rather than a fourth mode on one of the other two:

  1. **CPU.** `com.docker.backend.exe` sat at 200-255% of one core. Not a leak and not a
     gradual climb -- it was back at 209% within 229 seconds of a cold start. Three
     carameli containers bind-mount Windows paths (`vs_code\\carameli` -> `/app`), and the
     reloaders inside them stat those trees continuously across the 9p bridge, which the
     backend services on the Windows side. Measured 550 ioctls/s moving 21 KB/s: ~39
     bytes an op, i.e. pure metadata churn. **Restarting Docker cannot fix this** -- the
     spin returns with the stack. Only stopping the stack does.
  2. **Disk.** 13.6 GB of temp and cache trees that nothing in the harness had ever
     cleared, two of them still growing: `%TEMP%\\DiagOutputDir` held 3.13 GB of Remote
     Desktop auto-trace ETLs (100 MB apiece, a new one every few minutes) and
     `%TEMP%\\wsl-crashes` held 1.39 GB of WSLg compositor dumps (142 MB apiece, nine in
     three days). Docker itself returned **0 B** to a prune that day; it was not the
     disk problem, though it had been the week before.
  3. **Memory.** 32.8 GB committed against 15.7 GB of RAM. This one has no remedy here
     and the script says so rather than pretending: it is the user's own editors, browser
     and agent sessions, and the only honest output is a list of who is holding it.

The disk half feeds the memory half, which is why it is worth clearing even when disk is
not the complaint: under commit pressure Windows grows the pagefile, and the pagefile
lives on the same volume. It went 34.5 -> 37.5 GB in one session here, taking ~3 GB of
disk with it while free space was already the binding constraint.

Usage:  python reclaim.py [--yes] [--keep-stacks] [--min-age-days N]

Dry-run by default, like `worktree.py`: this deletes files and stops containers, so the
default has to be the harmless one. The task passes `--yes`.

Writes no artifact of its own -- `devkit_project.py` wraps every dispatched action in
`log-wrap.py`, which is the only layer that knows which checkout's `logs/` a run belongs
to. Anything invoking this unattended must do the same.

Windows-only by nature: it drives Docker Desktop and sweeps `%TEMP%`.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

GB = 1_000_000_000

# Free space below which `worktree.py reconcile` stops being safe to run. It is read off
# worktree.py rather than repeated, because the number that matters is *its* floor and a
# copy would go stale silently -- see `reconcile_is_safe` for what crossing it costs.
DEFAULT_RECONCILE_FLOOR_GB = 20.0

# How old a loose %TEMP% file must be before this sweeps it. Three days clears the churn
# of a week's work while leaving anything a running build might still reopen: %TEMP% is
# where pytest, npm and every subprocess stage their scratch, and a file written this
# morning may still be owned by something live.
DEFAULT_MIN_AGE_DAYS = 3.0


@dataclass(frozen=True)
class SweepTarget:
    """A directory whose whole contents are disposable, and why."""

    label: str
    path: Path
    why: str
    # Named trees are wholly disposable; only the loose sweep of %TEMP% itself needs an
    # age gate, because that directory also holds live scratch.
    min_age_days: float = 0.0


@dataclass(frozen=True)
class Snapshot:
    free_gb: float
    avail_gb: float
    committed_gb: float
    limit_gb: float


def sweep_targets(temp: Path, user: str) -> list[SweepTarget]:
    """The disposable trees under %TEMP%, in the order they are worth clearing.

    Pure so the list can be asserted without a filesystem. Everything here regenerates on
    demand; nothing here is anybody's only copy of anything.
    """
    return [
        SweepTarget(
            "RDP auto-trace ETLs",
            temp / "DiagOutputDir",
            "Remote Desktop writes 100 MB traces continuously and never rotates them out",
        ),
        SweepTarget(
            "WSL crash dumps",
            temp / "wsl-crashes",
            "142 MB per WSLg compositor crash; `guiApplications=false` stops the crashes",
        ),
        SweepTarget(
            "pytest scratch trees",
            temp / f"pytest-of-{user}",
            "one tree per run, kept for post-mortem and never collected",
        ),
        SweepTarget(
            "node compile cache",
            temp / "node-compile-cache",
            "V8 compile cache; rebuilt on next run",
        ),
    ]


def dir_size(path: Path) -> int:
    """Bytes held below `path`, ignoring anything that cannot be read."""
    total = 0
    if not path.is_dir():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def stale_files(root: Path, min_age_days: float, now: float | None = None) -> list[Path]:
    """Files directly under `root` last written more than `min_age_days` ago.

    Deliberately NOT recursive: the recursive case is covered by `sweep_targets`, where a
    whole named tree is known disposable. Walking all of %TEMP% and deleting by age would
    reach into trees whose owner is still running -- a half-emptied cache directory is a
    worse failure than a full one.
    """
    if not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - min_age_days * 86400
    out = []
    for item in root.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                out.append(item)
        except OSError:
            continue
    return out


def reconcile_is_safe(
    free_gb: float, floor_gb: float = DEFAULT_RECONCILE_FLOOR_GB
) -> tuple[bool, str]:
    """Whether `worktree.py reconcile` can be run yet, and the line that explains it.

    This encodes an ordering that is the reverse of the intuitive one, and got it wrong
    once. Reconcile reclaims disk, so running it first to free space reads as obvious --
    but at or below its own free-space floor it enters `RECLAIMING (open PRs reaped too)`
    and destroys boxes whose PR is merely *open*. On 2026-08-20 that took the box count
    from 11 to 2 while their PRs were still unmerged; the pushed commits survived and the
    checkouts did not.

    So disk is freed by the sweep and by Docker FIRST, and reconcile runs only once the
    machine is back above the floor -- where it reaps merged boxes and nothing else.

    `-1.0` for "cannot read the volume" is the same convention `worktree.free_gb` uses,
    and it refuses here too: an unknown disk must not license a destructive pass.
    """
    if free_gb < 0:
        return False, "free space could not be read"
    if free_gb <= floor_gb:
        return False, (
            f"only {free_gb:.1f} GB free -- at or under reconcile's {floor_gb:g} GB floor, "
            "where it reaps boxes whose PR is still open"
        )
    return True, f"{free_gb:.1f} GB free, clear of the {floor_gb:g} GB floor"


def aggregate_tasklist(rows: list[list[str]]) -> list[tuple[str, int, int]]:
    """(image, instances, KB) per image name, largest first.

    `tasklist /FO CSV /NH` columns are image, pid, session, session#, mem-usage -- the
    last as `"1,234 K"`. Split out so the parsing is testable without spawning anything.
    """
    totals: dict[str, list[int]] = {}
    for row in rows:
        if len(row) < 5:
            continue
        raw = row[4].replace(",", "").replace("\xa0", "").replace(" K", "").strip()
        try:
            kb = int(raw)
        except ValueError:
            continue
        entry = totals.setdefault(row[0], [0, 0])
        entry[0] += 1
        entry[1] += kb
    ranked = [(name, n, kb) for name, (n, kb) in totals.items()]
    ranked.sort(key=lambda item: item[2], reverse=True)
    return ranked


def top_memory_holders(limit: int = 6) -> list[tuple[str, int, int]]:
    try:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    rows = list(csv.reader(proc.stdout.splitlines()))
    return aggregate_tasklist(rows)[:limit]


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def snapshot(path: Path | None = None) -> Snapshot:
    """Free disk plus the commit picture, all in GB; -1.0 for anything unreadable."""
    try:
        free = shutil.disk_usage(path or Path.home()).free / GB
    except OSError:
        free = -1.0
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    try:
        # `sys.modules["ctypes"]` rather than `ctypes.windll`, and the indirection is
        # load-bearing rather than clever. mypy resolves that attribute against the
        # platform it is *running* on: a bare `ctypes.windll` fails `[attr-defined]` on
        # the Linux CI runner, and the `# type: ignore` that silences it there is then
        # reported as an unused ignore on this Windows desktop -- the one spelling that
        # cannot be green in both places at once.
        #
        # A getattr on the module with a literal name reads better and does not survive:
        # ruff's B009 is enabled and its autofix rewrites it back to the attribute, so
        # the pre-commit hook silently undid this and reddened the gate. Indexing
        # `sys.modules` types as `ModuleType`, whose attributes are `Any`, and there is
        # no lint rule that wants to unwrap it.
        windll = sys.modules["ctypes"].windll
        ok = windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError, KeyError):
        ok = 0
    if not ok:
        return Snapshot(free, -1.0, -1.0, -1.0)
    limit = status.ullTotalPageFile / GB
    return Snapshot(
        free_gb=free,
        avail_gb=status.ullAvailPhys / GB,
        committed_gb=limit - status.ullAvailPageFile / GB,
        limit_gb=limit,
    )


def memory_verdict(snap: Snapshot) -> list[str]:
    """What the reclaim could not fix, stated plainly rather than left implied.

    Nothing this script does frees committed memory: it stops containers and deletes
    files. When commit is near its limit the useful output is that fact plus the names
    holding it, so the reader can close something -- not a silent success.
    """
    if snap.limit_gb < 0 or snap.committed_gb < 0:
        return ["  memory: could not be read"]
    used = snap.committed_gb / snap.limit_gb if snap.limit_gb else 0.0
    line = f"  commit {snap.committed_gb:.1f} / {snap.limit_gb:.1f} GB ({used:.0%})"
    if used < 0.85:
        return [line + " -- healthy"]
    out = [
        line + " -- near the limit, and nothing here can lower it.",
        "  Stopping containers and deleting files does not free committed memory;",
        "  the holders below are editors, browsers and agent sessions. Close some.",
    ]
    for name, count, kb in top_memory_holders():
        plural = "" if count == 1 else "es"
        out.append(f"    {name:<24} {kb / 1024 / 1024:6.1f} GB  ({count} process{plural})")
    return out


def running_container_names() -> list[str]:
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def stop_stacks(names: list[str], apply: bool) -> None:
    """Stop every running container -- `stop`, never `down`.

    `down` would remove the containers and, with them, anything not on a named volume.
    `stop` is also the verb that *survives*: a container carrying `restart:
    unless-stopped` stays down across a reboot once stopped by hand, which is the whole
    point -- otherwise the backend spin returns with the next boot.
    """
    if not names:
        print("  no containers running -- nothing to stop")
        return
    print(f"  stopping {len(names)} container(s): {', '.join(names)}")
    print("    (`docker stop`, never `down` -- see stop_stacks)")
    if not apply:
        return
    subprocess.run(
        ["docker", "stop", *names],
        capture_output=True,
        timeout=300,
        creationflags=NO_WINDOW,
    )


def sweep(targets: list[SweepTarget], temp: Path, min_age_days: float, apply: bool) -> int:
    """Delete the disposable trees and stale loose files. Returns bytes reclaimed."""
    freed = 0
    for target in targets:
        size = dir_size(target.path)
        if size == 0:
            print(f"  {target.label:<24} nothing to clear")
            continue
        print(f"  {target.label:<24} {size / GB:6.2f} GB  -- {target.why}")
        freed += size
        if apply:
            for item in target.path.rglob("*"):
                try:
                    if item.is_file():
                        item.unlink()
                except OSError:
                    continue

    stale = stale_files(temp, min_age_days)
    stale_bytes = 0
    for item in stale:
        try:
            stale_bytes += item.stat().st_size
        except OSError:
            continue
    if stale:
        print(
            f"  {'loose %TEMP% files':<24} {stale_bytes / GB:6.2f} GB  "
            f"-- {len(stale)} file(s) older than {min_age_days:g} days"
        )
        freed += stale_bytes
        if apply:
            for item in stale:
                try:
                    item.unlink()
                except OSError:
                    continue
    return freed


def banner(text: str) -> str:
    return f"\n{'=' * 60}\n  {text}\n{'=' * 60}\n"


def run_reconcile(devkit: Path, apply: bool) -> None:
    script = devkit / "scripts" / "worktree.py"
    if not script.is_file():
        print(f"  [skip] {script} not found")
        return
    cmd = [sys.executable, str(script), "reconcile"] + (["--yes"] if apply else ["--dry-run"])
    # The child writes straight to the inherited handle, so anything still sitting in this
    # process's buffer would land after it -- which put the whole reconcile listing above
    # this script's own banner the first time it ran.
    sys.stdout.flush()
    subprocess.run(cmd, timeout=1800)
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually do it (default: dry run)")
    parser.add_argument(
        "--keep-stacks",
        action="store_true",
        help="leave running containers alone (skips the largest CPU win)",
    )
    parser.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE_DAYS)
    args = parser.parse_args(argv)
    apply = args.yes

    temp = Path(tempdir())
    before = snapshot()

    print(banner("Machine Reclaim" + ("" if apply else " -- DRY RUN, nothing will change")))
    print(f"  free disk   {before.free_gb:.1f} GB")
    print(f"  commit      {before.committed_gb:.1f} / {before.limit_gb:.1f} GB")

    print("\n-- 1. containers (the CPU half) --")
    if args.keep_stacks:
        print("  --keep-stacks: leaving them up; the bind-mount spin stays with them")
    else:
        stop_stacks(running_container_names(), apply)

    print("\n-- 2. temp and cache trees (the disk half) --")
    freed = sweep(sweep_targets(temp, username()), temp, args.min_age_days, apply)
    print(f"  {'total':<24} {freed / GB:6.2f} GB")

    print("\n-- 3. boxes --")
    after_sweep = snapshot()
    safe, why = reconcile_is_safe(after_sweep.free_gb)
    print(f"  {why}")
    if safe:
        run_reconcile(devkit_dir(), apply)
    else:
        print("  skipping reconcile: it would reap boxes whose PR is still open")

    after = snapshot()
    print(banner("Done" if apply else "Done -- DRY RUN"))
    if apply:
        print(f"  free disk   {before.free_gb:.1f} -> {after.free_gb:.1f} GB")
    else:
        # Never print a before -> after delta here. Nothing was deleted, so any movement
        # is some other process on the machine, and rendering it as an arrow off this
        # script's own banner reads as a result it produced.
        print(f"  free disk   {after.free_gb:.1f} GB, unchanged")
        print(f"  would free  {freed / GB:.2f} GB of temp trees")
    for line in memory_verdict(after):
        print(line)
    if not apply:
        print("\n  Nothing was changed. Re-run with --yes to apply.")
    return 0


def tempdir() -> str:
    import tempfile

    return tempfile.gettempdir()


def username() -> str:
    import getpass

    try:
        return getpass.getuser()
    except OSError:
        return "unknown"


def devkit_dir() -> Path:
    return Path(__file__).resolve().parent.parent


if __name__ == "__main__":
    raise SystemExit(main())
