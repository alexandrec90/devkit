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
     spin returns with the stack. Only stopping the stack does. The stop is a *window*
     rather than a verdict, though: see "What it puts back" below.
  2. **Disk.** 13.6 GB of temp and cache trees that nothing in the harness had ever
     cleared, two of them still growing: `%TEMP%\\DiagOutputDir` held 3.13 GB of Remote
     Desktop auto-trace ETLs (100 MB apiece, a new one every few minutes) and
     `%TEMP%\\wsl-crashes` held 1.39 GB of WSLg compositor dumps (142 MB apiece, nine in
     three days). Docker itself returned **0 B** to a prune that day; it was not the
     disk problem, though it had been the week before.
  3. **Memory.** 32.8 GB committed against 15.7 GB of RAM. This one has no remedy here
     and the script says so rather than pretending: it is the user's own editors, browser
     and agent sessions, and the only honest output is a list of who is holding it.

A fourth was added on 2026-08-27, when this script would have found nothing:

  4. **Disk, the durable half.** The machine was at 20.9 GB free with the pagefile at its
     boot size -- so no reboot could return a byte, and the commit story above did not
     apply. Every tree in (2) was already clear, because none of the space was in %TEMP%
     at all. It was in **superseded versions of the tools this workspace installs**: six
     codex releases beside the one `current` names, two claude CLIs behind the running
     one, twenty VS Code build caches for one live build, three interrupted extension
     installs dating to March, and three generations of playwright chromium. 4.5 GB, in
     no tool's own cleanup path -- each installer writes the new version and leaves the
     old one, forever. `superseded_trees` is that sweep, and it is the only section here
     whose targets grow with *elapsed weeks* rather than with a session's work, which is
     why nothing had ever noticed them.

The disk half feeds the memory half, which is why it is worth clearing even when disk is
not the complaint: under commit pressure Windows grows the pagefile, and the pagefile
lives on the same volume. It went 34.5 -> 37.5 GB in one session here, taking ~3 GB of
disk with it while free space was already the binding constraint.

What it will not touch is Windows' own feature-update staging -- `C:\\$GetCurrent` held
5.67 GB on that machine, staged the previous September and still the largest single item
on the volume. A delete under a `$`-prefixed system path is refused by Windows and by
this workspace's shell guard, and a script that cannot do the thing should say so with
the size and the remedy rather than stay quiet: `protected_staging`.

What it puts back
-----------------

**Every container it stopped goes back up before the run ends, including when the run
dies.** The first version left them down on the grounds that the stop *is* the CPU fix
(1), which is true and was still the wrong default: this script is the one-click answer
to "the machine is slow", not a decision to end the working day, and a run that failed
part-way through left a machine whose stacks were down, whose engine might be down with
them, and whose only record said the task had passed. Restoring is therefore in a
`finally` -- an exception, a `TimeoutExpired` from a wedged engine, or a Ctrl-C all still
put the stacks back. `--leave-stopped` keeps the old behaviour for a caller that really
does want the CPU back for the rest of the day, and `--keep-stacks` still means never
touch them at all.

Nothing here reports success on somebody else's failure, either. `run_reconcile` used to
discard the child's exit code, so a reconcile that failed printed its error to the
terminal while this script exited 0 -- and `log-wrap.py` empties the artifact on a pass,
so the one durable record of the run was a zero-byte file saying it had gone fine. Every
step that can fail now names itself in `failures`, and a non-empty list is the exit code.

Usage:  python reclaim.py [--yes] [--keep-stacks] [--leave-stopped] [--min-age-days N]

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
import re
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

# Windows stages a feature update into one of these and never removes it -- `$GetCurrent`
# alone was 5.67 GB here, eleven months old. Reported, never deleted: `Remove-Item` on a
# `$`-prefixed system path is refused, and `Windows.old` needs `takeown` before it can be
# touched at all. Neither belongs in an unattended pass.
PROTECTED_STAGING = ("$GetCurrent", "$WINDOWS.~BT", "$WINDOWS.~WS", "Windows.old")

# `<product>-<revision>` -- how playwright, and only playwright, names a browser build.
_REVISIONED = re.compile(r"^(?P<product>.+?)-(?P<revision>\d+)$")


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
class Disposable:
    """Version directories a keep-rule has already decided are dead, and why they exist.

    `paths` is the *result* of a rule rather than a rule to apply later, so every rule can
    be its own small function with its own test -- and so nothing here has to grow a
    boolean per tool, which is the shape this file's own vendoring rules warn about.
    """

    label: str
    paths: tuple[Path, ...]
    why: str


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


def cache_targets(home: Path) -> list[SweepTarget]:
    """Wholly disposable trees outside %TEMP%, under the user's profile.

    Separate from `sweep_targets` because that list's guarantee is that everything in it
    sits under the directory passed in, which a test asserts -- these are rooted in the
    profile instead. A tree only belongs here if it is disposable *entire*; anything where
    the newest copy has to survive is a keep-rule, and goes through `superseded_trees`.
    """
    code = home / "AppData" / "Roaming" / "Code"
    return [
        SweepTarget(
            "VS Code .vsix installers",
            code / "CachedExtensionVSIXs",
            "the installer for every extension update, kept after it has been applied",
        ),
    ]


def _subdirs(root: Path) -> list[Path]:
    try:
        return [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return []


def _old_enough(paths: list[Path], min_age_days: float, now: float | None = None) -> list[Path]:
    """Drop anything written within `min_age_days`, so a fresh install is never a victim.

    The age gate is the second half of every keep-rule below and not a nicety: a keep-rule
    reads the *current* state of a directory, and an install that is still in flight looks
    exactly like a superseded version until its pointer is written.

    Zero means no gate rather than "older than this instant": the strict comparison is a
    race against Windows's coarser file timestamps, and `sweep` lost it on a file written
    milliseconds before the cutoff was computed.
    """
    if min_age_days <= 0:
        return list(paths)
    cutoff = (now if now is not None else time.time()) - min_age_days * 86400
    kept = []
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                kept.append(path)
        except OSError:
            continue
    return kept


def all_but_live(root: Path, pointer: Path) -> list[Path]:
    """Version directories under `root` that `pointer` does not resolve to.

    Fails closed on purpose: a keep-rule that cannot name its keeper returns nothing
    rather than everything. `pointer` is a junction on Windows, and an unresolvable one
    means the install is mid-update or broken -- either way not the moment to delete its
    siblings.
    """
    try:
        keeper = pointer.resolve(strict=True)
    except OSError:
        return []
    if keeper.parent.resolve() != root.resolve():
        return []
    return [path for path in _subdirs(root) if path.resolve() != keeper]


def all_but_newest(
    root: Path, keep: int = 1, min_age_days: float = 0.0, now: float | None = None
) -> list[Path]:
    """Version directories under `root` except the `keep` most recently written.

    For the tools that install a new version beside the old one and leave no pointer
    behind: the claude CLI launches the newest it finds, and VS Code names its build cache
    after a commit hash, so mtime is the only ordering either of them offers.
    """
    ranked = sorted(_subdirs(root), key=_mtime, reverse=True)
    return _old_enough(ranked[keep:], min_age_days, now)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def superseded_revisions(
    root: Path, min_age_days: float = 0.0, now: float | None = None
) -> list[Path]:
    """`<product>-<revision>` directories beaten by a higher revision of the same product.

    Playwright's layout, where three chromium generations had accumulated at ~400 MB each.
    Grouping matters: the newest *directory* here is a firefox build, and keeping only that
    would delete the chromium every checkout actually runs. A name that does not parse --
    `.links`, anything hand-made -- is left alone rather than guessed about.
    """
    newest: dict[str, int] = {}
    parsed: list[tuple[str, int, Path]] = []
    for path in _subdirs(root):
        match = _REVISIONED.match(path.name)
        if not match:
            continue
        product, revision = match["product"], int(match["revision"])
        parsed.append((product, revision, path))
        newest[product] = max(newest.get(product, -1), revision)
    dead = [path for product, revision, path in parsed if revision < newest[product]]
    return _old_enough(dead, min_age_days, now)


def orphaned_installs(
    root: Path, min_age_days: float = 0.0, now: float | None = None
) -> list[Path]:
    """`.<uuid>` staging directories VS Code left behind when an extension install died.

    It renames the staging directory into place on success, so a dot-prefixed one that has
    survived the age gate is by construction a failure nobody is coming back for. Scoped
    to the leading dot: everything else under `extensions/` is an installed extension.
    """
    return _old_enough([p for p in _subdirs(root) if p.name.startswith(".")], min_age_days, now)


def superseded_trees(
    home: Path, min_age_days: float = DEFAULT_MIN_AGE_DAYS, now: float | None = None
) -> list[Disposable]:
    """Every superseded-version group, in the order they are worth clearing.

    Pure, so the whole set can be asserted without a filesystem. What each entry has in
    common is that *some other copy is live and stays*, which is what separates this list
    from `sweep_targets`: there the whole tree goes.
    """
    codex = home / ".codex" / "packages" / "standalone"
    code = home / "AppData" / "Roaming" / "Code"
    return [
        Disposable(
            "superseded codex releases",
            tuple(all_but_live(codex / "releases", codex / "current")),
            "~380 MB per release; `current` names the live one and nothing prunes the rest",
        ),
        Disposable(
            "superseded claude versions",
            tuple(
                all_but_newest(
                    home / ".local" / "share" / "claude" / "versions", 1, min_age_days, now
                )
            ),
            "the launcher runs the newest; the rest are a rollback nobody performs",
        ),
        Disposable(
            "VS Code build caches",
            tuple(all_but_newest(code / "CachedData", 1, min_age_days, now)),
            "one V8 cache per build ever run -- twenty of them here, for one live build",
        ),
        Disposable(
            "interrupted extension installs",
            tuple(orphaned_installs(home / ".vscode" / "extensions", min_age_days, now)),
            "VS Code stages an install in `.<uuid>` and abandons it there when it fails",
        ),
        Disposable(
            "superseded playwright browsers",
            tuple(
                superseded_revisions(
                    home / "AppData" / "Local" / "ms-playwright", min_age_days, now
                )
            ),
            "`npx playwright install` re-fetches one if a checkout still pins that revision",
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


def staging_verdict(drive: Path = Path("C:/")) -> list[str]:
    """The GB this script found and may not touch, with the one remedy that works.

    Same contract as `memory_verdict`: name what the reclaim cannot do rather than finish
    on a total that reads as "the volume is as clear as it gets". On 2026-08-27 this was
    5.67 GB -- larger than everything the rest of the run could free put together, and it
    had been sitting there since the previous September with nothing reporting it.
    """
    found = protected_staging(drive)
    if not found:
        return []
    out = ["  windows update staging -- real GB, and not this script's to delete:"]
    for path, size in found:
        out.append(f"    {path!s:<20} {size / GB:6.2f} GB")
    out.append("  Disk Cleanup as admin (`cleanmgr /d C:`) -> Windows Update Cleanup;")
    out.append("  a delete from a script or an agent shell is refused, by design.")
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


def docker(args: list[str], timeout: int) -> tuple[bool, str]:
    """Run one docker verb. Returns (ok, the line to print when it is not).

    Never raises, and that is the fix rather than the tidiness: `docker stop` ran under
    `capture_output=True` with its exit code discarded, so a refusal was invisible, while
    a wedged engine raised `TimeoutExpired` out of `main` -- killing the run after the
    containers were down and before anything could put them back. Both spellings of "the
    engine did not co-operate" are a printed line now, and the run continues to its
    restore.
    """
    try:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except FileNotFoundError:
        return False, "docker is not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"`docker {args[0]}` did not return within {timeout}s"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    said = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, said[0] if said else f"`docker {args[0]}` exited {proc.returncode}"


def stop_stacks(names: list[str], apply: bool) -> tuple[list[str], str | None]:
    """Stop every running container -- `stop`, never `down`.

    `down` would remove the containers and, with them, anything not on a named volume.
    `stop` is also the verb `restart_stacks` can undo: the containers still exist, so
    putting them back is `docker start` and costs seconds rather than a rebuild.

    Returns the names to restore plus a failure line, and returns the names **even when
    the stop failed**: `docker stop a b c` is not atomic, so a call that reports an error
    has usually stopped some prefix of its arguments, and the list of what to put back is
    the list of what was up. `docker start` on a container that never went down is a
    no-op, which is what makes the pessimistic list the safe one.
    """
    if not names:
        print("  no containers running -- nothing to stop")
        return [], None
    print(f"  stopping {len(names)} container(s): {', '.join(names)}")
    print("    (`docker stop`, never `down` -- see stop_stacks)")
    if not apply:
        return list(names), None
    ok, detail = docker(["stop", *names], timeout=300)
    if not ok:
        print(f"    [warn] {detail}")
        return list(names), f"docker stop: {detail}"
    return list(names), None


def restart_stacks(names: list[str], apply: bool) -> str | None:
    """Put back exactly what `stop_stacks` took down. Returns a failure line, or None.

    Idempotent by construction -- `docker start` on a running container exits 0 -- which
    is what lets this be called from a `finally` without first working out how far the
    run got.

    An engine that has gone away since the stop is the one failure worth spelling out:
    `docker start` cannot bring the stacks back through a dead daemon, and the remedy is
    a different script, so the line names it rather than leaving the reader with a raw
    `npipe` error.
    """
    if not names:
        return None
    print(f"  restarting {len(names)} container(s): {', '.join(names)}")
    if not apply:
        return None
    ok, detail = docker(["start", *names], timeout=300)
    if ok:
        return None
    print(f"    [warn] {detail}")
    print("    the engine may be down: `python scripts/docker-maint.py restart-engine`")
    return f"docker start: {detail}"


def sweep(targets: list[SweepTarget], temp: Path, min_age_days: float, apply: bool) -> int:
    """Delete the disposable trees and stale loose files. Returns bytes reclaimed.

    Under `--yes` the figure is what *went*, measured by re-reading the tree, rather than
    what was there to go. The two differ whenever a file is held open or owned by another
    account, and this script now sweeps trees where that is ordinary -- reporting the
    optimistic number would put GB in the total that are still on the volume, under the
    same banner as a `free disk` delta that disagrees with it.
    """
    freed = 0
    for target in targets:
        size = dir_size(target.path)
        if size == 0:
            print(f"  {target.label:<24} nothing to clear")
            continue
        print(f"  {target.label:<24} {size / GB:6.2f} GB  -- {target.why}")
        if not apply:
            freed += size
            continue
        # `min_age_days` on the target had never been read, so a caller that set it got a
        # full delete and no warning. Honouring it is the fix -- and 0.0 has to mean *no
        # gate* rather than "older than this instant", which is a race a file written
        # milliseconds earlier loses on Windows's coarser timestamps. It cost a green
        # existing test to find, which is the only reason it was found.
        cutoff = time.time() - target.min_age_days * 86400 if target.min_age_days else None
        for item in target.path.rglob("*"):
            try:
                if item.is_file() and (cutoff is None or item.stat().st_mtime < cutoff):
                    item.unlink()
            except OSError:
                continue
        freed += size - dir_size(target.path)

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
        if not apply:
            freed += stale_bytes
        else:
            for item in stale:
                try:
                    size = item.stat().st_size
                    item.unlink()
                    freed += size
                except OSError:
                    continue
    return freed


def purge_trees(paths: tuple[Path, ...], apply: bool) -> int:
    """Remove whole version directories. Returns bytes that actually went away.

    `ignore_errors` plus a re-measure rather than a raise: one locked file inside a
    superseded playwright build must not abort the four groups after it, and it must not
    be counted as freed either.
    """
    freed = 0
    for path in paths:
        size = dir_size(path)
        if not apply:
            freed += size
            continue
        shutil.rmtree(path, ignore_errors=True)
        freed += size - dir_size(path)
    return freed


def sweep_versions(groups: list[Disposable], apply: bool) -> int:
    """Report and clear every superseded-version group. Returns bytes reclaimed."""
    freed = 0
    for group in groups:
        if not group.paths:
            print(f"  {group.label:<30} nothing superseded")
            continue
        size = sum(dir_size(path) for path in group.paths)
        print(f"  {group.label:<30} {size / GB:6.2f} GB  {len(group.paths)} dir(s) -- {group.why}")
        freed += purge_trees(group.paths, apply)
    return freed


def protected_staging(drive: Path = Path("C:/")) -> list[tuple[Path, int]]:
    """Windows update staging directories present on `drive`, largest first.

    Report-only, and the point of reporting is that this is routinely the biggest single
    item on the volume while being the one thing here nothing automated may delete. An
    empty list is the good case and prints nothing.
    """
    found = []
    for name in PROTECTED_STAGING:
        path = drive / name
        size = dir_size(path)
        if size:
            found.append((path, size))
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def banner(text: str) -> str:
    return f"\n{'=' * 60}\n  {text}\n{'=' * 60}\n"


def run_reconcile(devkit: Path, apply: bool) -> str | None:
    """Hand the box tier the disk the sweep just freed. Returns a failure line, or None.

    The exit code used to be discarded. Reconcile is the only child here whose streams
    are inherited, so it is also the only one that can print an error the terminal
    shows -- and with the code dropped, this script exited 0 over it and `log-wrap.py`
    emptied the artifact on the way out. The one durable record of a run that had visibly
    failed was a zero-byte file meaning "passed".
    """
    script = devkit / "scripts" / "worktree.py"
    if not script.is_file():
        print(f"  [skip] {script} not found")
        return None
    cmd = [sys.executable, str(script), "reconcile"] + (["--yes"] if apply else ["--dry-run"])
    # The child writes straight to the inherited handle, so anything still sitting in this
    # process's buffer would land after it -- which put the whole reconcile listing above
    # this script's own banner the first time it ran.
    sys.stdout.flush()
    try:
        proc = subprocess.run(cmd, timeout=1800)
    except subprocess.TimeoutExpired:
        sys.stdout.flush()
        print("  [warn] reconcile did not finish within 30 minutes")
        return "reconcile timed out"
    except OSError as exc:
        sys.stdout.flush()
        print(f"  [warn] reconcile could not be started: {exc}")
        return f"reconcile could not be started: {exc}"
    sys.stdout.flush()
    if proc.returncode != 0:
        print(f"  [warn] reconcile exited {proc.returncode} -- its output is above")
        return f"reconcile exited {proc.returncode}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually do it (default: dry run)")
    parser.add_argument(
        "--keep-stacks",
        action="store_true",
        help="leave running containers alone (skips the largest CPU win)",
    )
    parser.add_argument(
        "--leave-stopped",
        action="store_true",
        help="do not restart the containers this stopped (keeps the CPU back all day)",
    )
    parser.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE_DAYS)
    args = parser.parse_args(argv)
    apply = args.yes

    temp = Path(tempdir())
    before = snapshot()

    print(banner("Machine Reclaim" + ("" if apply else " -- DRY RUN, nothing will change")))
    print(f"  free disk   {before.free_gb:.1f} GB")
    print(f"  commit      {before.committed_gb:.1f} / {before.limit_gb:.1f} GB")

    # Every step that can fail appends one line here, and a non-empty list is the exit
    # code. Nothing below aborts the run: a docker verb that failed must not cost the
    # disk sweep, and neither may cost the restore.
    failures: list[str] = []
    stopped: list[str] = []
    restored = False
    try:
        print("\n-- 1. containers (the CPU half) --")
        if args.keep_stacks:
            print("  --keep-stacks: leaving them up; the bind-mount spin stays with them")
        else:
            stopped, failed = stop_stacks(running_container_names(), apply)
            if failed:
                failures.append(failed)

        print("\n-- 2. temp and cache trees (the disk half) --")
        profile = home()
        freed = sweep(
            [*sweep_targets(temp, username()), *cache_targets(profile)],
            temp,
            args.min_age_days,
            apply,
        )
        print(f"  {'total':<24} {freed / GB:6.2f} GB")

        print("\n-- 3. superseded tool versions (the half no reboot returns) --")
        versions = sweep_versions(superseded_trees(profile, args.min_age_days), apply)
        print(f"  {'total':<30} {versions / GB:6.2f} GB")
        freed += versions

        print("\n-- 4. boxes --")
        after_sweep = snapshot()
        safe, why = reconcile_is_safe(after_sweep.free_gb)
        print(f"  {why}")
        if safe:
            failed = run_reconcile(devkit_dir(), apply)
            if failed:
                failures.append(failed)
        else:
            print("  skipping reconcile: it would reap boxes whose PR is still open")

        print("\n-- 5. putting the containers back --")
        failed = restore(stopped, apply, args.leave_stopped)
        restored = True
        if failed:
            failures.append(failed)

        after = snapshot()
        print(banner("Done" if apply else "Done -- DRY RUN"))
        if apply:
            print(f"  free disk   {before.free_gb:.1f} -> {after.free_gb:.1f} GB")
        else:
            # Never print a before -> after delta here. Nothing was deleted, so any
            # movement is some other process on the machine, and rendering it as an arrow
            # off this script's own banner reads as a result it produced.
            print(f"  free disk   {after.free_gb:.1f} GB, unchanged")
            print(f"  would free  {freed / GB:.2f} GB of caches and superseded versions")
        for line in memory_verdict(after):
            print(line)
        for line in staging_verdict():
            print(line)
        if not apply:
            print("\n  Nothing was changed. Re-run with --yes to apply.")
    finally:
        # The whole point of the `finally`: a Ctrl-C, a crash, or anything raised out of
        # the four steps above must still leave the machine the way this found it. The
        # normal path has already restored, and `restart_stacks` is idempotent anyway, so
        # the flag is about not printing a second, confusing section.
        if not restored and stopped and not args.leave_stopped:
            print("\n-- interrupted: putting the containers back --")
            restore(stopped, apply, args.leave_stopped)

    for line in failures:
        print(f"  [failed] {line}")
    return 1 if failures else 0


def restore(stopped: list[str], apply: bool, leave_stopped: bool) -> str | None:
    """Put the stacks back unless the caller asked for the CPU instead.

    Split out so the `finally` and the normal path share one decision: `--leave-stopped`
    has to suppress the restore in both, and a second copy of that test is how one of
    them would come to disagree.
    """
    if leave_stopped:
        if stopped:
            print("  --leave-stopped: they stay down, and the CPU stays back")
        return None
    if not stopped:
        print("  nothing was stopped -- nothing to put back")
        return None
    return restart_stacks(stopped, apply)


def tempdir() -> str:
    import tempfile

    return tempfile.gettempdir()


def home() -> Path:
    """Seam, for the same reason `tempdir` is one: the tests must not reach the profile."""
    return Path.home()


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
