#!/usr/bin/env python3
"""The host half of a box's teardown: delete the directory, evict what holds it.

`worktree.py` owns the other two halves — the container tier (`compose_down`,
`remove_images`) and git (`git worktree remove`) — and for a year it owned no host
tier at all. Nothing on this machine was ever asked to let go of the box before its
directory was deleted, which is how a husk gets made:

1. an agent runs `npm run dev` in a box, and vite maps a native `.node` binding;
2. Windows refuses to delete a **mapped image** with `Access is denied`;
3. `git worktree remove` deletes what it can, dies on that one file, and has by then
   already removed `.git`;
4. what is left is a directory no `git worktree remove` can ever succeed on again,
   and the direct-delete fallback meets the same lock.

Two of those accrued on 2026-08-30 and made every scheduled `reconcile` — and
`reclaim.py`, which runs it and reports its exit code — permanently red. This module
is the step that was missing between 2 and 3.

It is a module of its own rather than four more functions in `worktree.py` because
`worktree.py` is already over every structural limit the repo keeps a baseline for,
and because the seam is real: nothing here knows what a `ReapPlan` is, and the
subject is the machine rather than the box registry.
"""

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bg_sessions
import sweep

# Every path under the box that a live process has mapped as an image, asked of the two
# sources that can answer it without a third-party module: the process's own executable,
# and its loaded modules. Deliberately NOT the command line -- an agent's shell sitting in
# a box names the box path too, and killing the session that asked for the reap is a worse
# failure than the leaked husk this exists to prevent. A mapped image is the thing Windows
# actually refuses the delete over, so it is also the only thing worth killing for.
_HOLDERS_PS = """\
$needle = '__ROOT__'
foreach ($p in Get-Process) {
  $hit = $false
  try { if ($p.Path -and $p.Path.ToLower().StartsWith($needle)) { $hit = $true } } catch {}
  if (-not $hit) {
    try {
      foreach ($m in $p.Modules) {
        if ($m.FileName -and $m.FileName.ToLower().StartsWith($needle)) { $hit = $true; break }
      }
    } catch {}
  }
  if ($hit) { "$($p.Id)|$($p.ProcessName)" }
}
"""


def holders_script(path: Path) -> str:
    """The PowerShell that names every process holding an image under `path`.

    Pure, so the interpolation is assertable: the path lands inside a single-quoted
    PowerShell literal, lowercased because the comparison is, and with a trailing
    separator so a sibling box whose name merely starts the same way cannot match.
    """
    root = str(path).replace("/", "\\").rstrip("\\").lower() + "\\"
    return _HOLDERS_PS.replace("__ROOT__", root.replace("'", "''"))


def parse_holders(text: str) -> list[tuple[int, str]]:
    """`pid|name` rows into pairs, skipping anything that is not one and this process.

    Split out so the eviction can be tested against captured output rather than against
    the machine's live process table.
    """
    out: list[tuple[int, str]] = []
    for line in text.splitlines():
        pid, _, name = line.strip().partition("|")
        if not pid.isdigit() or not name:
            continue
        number = int(pid)
        if number != os.getpid():
            out.append((number, name))
    return out


def box_holders(path: Path, run=sweep.run_windowless) -> list[tuple[int, str]]:
    """Processes running an executable or module out of `path`. Empty when unaskable.

    No `os.name` guard, deliberately: a missing `powershell` raises `FileNotFoundError`
    and lands on the same empty list, so the one branch that answers on Linux is the
    branch a Linux CI runner can execute. A guard would make every test of the caller
    Windows-only, which is how this file's Windows-specific halves went untested before.
    """
    try:
        completed = run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                holders_script(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            creationflags=sweep.NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return parse_holders(completed.stdout or "")


# How long to let Windows finish tearing a killed process down before retrying the
# delete. `taskkill` returns once the kill is *signalled*; the image section a mapped
# `.node` sits in is released when the last handle to it goes, which is after the process
# object is reaped, so an immediate retry can still be denied.
HOLDER_RELEASE_SECONDS = 2.0


def evict_box_holders(path: Path, run=sweep.run_windowless) -> list[str]:
    """Kill every process holding an image under `path`. Returns what was killed.

    `taskkill /T` because the server is usually a child of an `npm.cmd` wrapper, and
    killing the wrapper alone leaves the process that holds the file. Best-effort per pid:
    one that has already exited is a success, not a failure, and the retry that follows is
    the only verdict that matters.
    """
    killed: list[str] = []
    for pid, name in box_holders(path, run):
        try:
            run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                creationflags=sweep.NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        killed.append(f"{name} ({pid})")
    return killed


def vacate(
    path: Path, sessions: list[dict], run=sweep.run_windowless, sleep=time.sleep
) -> tuple[list[str], list[str]]:
    """Clear the agent sessions out of the box before it is deleted: `(occupants, notes)`.

    `sessions` is what `claude agents --json` lists (`bg_sessions.listed`). A session's
    working directory is a handle on the box that no eviction above sees -- it maps no
    image under it -- and Windows will not delete a directory a process stands in. So a
    reap of a merged box whose fixer was still alive in it deleted every file, died on
    the empty root with `being used by another process`, and turned the scheduled
    reconcile red until someone found the session (94e52397, three times).

    An idle background session is the pass's own finished fixer: it is stopped, which
    keeps its conversation (`claude attach` reopens it). Anything else -- a session still
    working, a person's interactive one -- is an occupant, and the box is left whole for
    it: `occupants` names each, and nothing is stopped while one remains.
    """
    inside = bg_sessions.in_tree(sessions, path)
    occupants = [_describe(row) for row in inside if not bg_sessions.stoppable(row)]
    if occupants:
        return occupants, []
    notes: list[str] = []
    for row in inside:
        if bg_sessions.stop(str(row.get("id", "")), run):
            notes.append(f"stopped idle background session {row.get('id')} still in the box")
        else:
            occupants.append(_describe(row))
    if notes:
        # `claude stop` returns once the stop is asked for, not once the process is gone.
        sleep(HOLDER_RELEASE_SECONDS)
    return occupants, notes


def _describe(row: dict) -> str:
    return f"{row.get('kind', '?')} session {row.get('id') or '?'} ({row.get('status', '?')})"


def _make_deletable(target: str) -> None:
    """Clear whatever permission bit the filesystem is refusing the delete on.

    **Add the bits, never assign them.** `chmod(S_IWRITE)` is the Windows idiom and says
    exactly the right thing there -- clear the read-only attribute -- but on POSIX the
    same constant is `0o200`, so it also takes away read and execute. A directory this
    hook had touched then became untraversable, and every entry beneath it failed for a
    reason that had nothing to do with the one that got us here.
    """
    try:
        mode = os.stat(target, follow_symlinks=False).st_mode
    except OSError:
        mode = 0
    wanted = stat.S_IWUSR | stat.S_IRUSR
    if stat.S_ISDIR(mode):
        wanted |= stat.S_IXUSR
    try:
        os.chmod(target, mode | wanted)
    except OSError:
        # A permission we cannot even change is the retry's problem to report, not ours.
        pass


def _retry_delete(failed_path: str) -> str:
    """Delete one entry `rmtree` could not, by path. Empty string on success.

    Deliberately **not** the callable `rmtree` hands the hook. On POSIX `rmtree` walks
    with directory file descriptors, so that callable is `os.open`/`os.unlink`/`os.rmdir`
    already spoken for by a `dir_fd` and a bare name -- calling it with a full path is a
    `TypeError` (`open() missing required argument 'flags'`), which is not an `OSError`,
    so it escaped the hook's `except` and aborted the very walk the hook exists to keep
    going. Windows takes the path-based branch and never showed it; CI did, on the first
    run.
    """
    _make_deletable(failed_path)
    try:
        if os.path.isdir(failed_path) and not os.path.islink(failed_path):
            os.rmdir(failed_path)
        else:
            os.unlink(failed_path)
    except OSError as exc:
        return f"{failed_path}: {exc.strerror or exc}"
    return ""


_LONGPATH = "\\\\?\\"


def _longpath(path: Path) -> str:
    """`path` with the prefix that turns Windows' MAX_PATH off; unchanged elsewhere."""
    target = str(path)
    if os.name == "nt" and not target.startswith(_LONGPATH):
        target = _LONGPATH + os.path.abspath(target)
    return target


def unopenable(path: Path, walk=os.walk) -> list[str]:
    """Every directory under `path` this process may not even list, as a reader spells it.

    The one delete failure no eviction and no retry can cure: the entry's ACL grants this
    token nothing. Python's `mkdtemp` makes a directory owner-only, and an owner that is
    an *elevated* process is Administrators -- which an unelevated token holds deny-only
    -- so pytest run elevated in a tree leaves a `.pytest_cache` only an elevated shell
    can open or delete (carameli, 2026-09-29). Any other refusal is somebody else's.
    """
    found: list[str] = []

    def refused(exc: OSError) -> None:
        if isinstance(exc, PermissionError):
            found.append(str(exc.filename).removeprefix(_LONGPATH))

    for _ in walk(_longpath(path), onerror=refused):
        pass
    return found


def remove_tree_longpath(path: Path) -> str:
    """Delete `path` recursively, surviving Windows MAX_PATH. Empty string on success.

    A provisioned box carries a `.venv` whose nesting routinely exceeds MAX_PATH, and
    `git worktree remove` deletes with plain Win32 calls -- so a reap of a perfectly
    clean box died with `Filename too long`, leaving a half-deleted husk that
    classifies as `skipped` and reads as "holding work" forever. The `\\\\?\\` prefix
    turns the limit off; the `onexc` hook clears whatever the filesystem is refusing on
    -- the read-only bit Windows uses for some packaging artifacts -- and retries the
    delete **by path**.

    **The hook records a failure it cannot fix rather than raising it.** Re-raising
    aborts `rmtree` at the first unfixable entry, so one file a live process had mapped
    kept the whole rest of the tree on disk -- and the caller was told about that one
    path as though it were the only thing left. Everything deletable goes now, the
    failures are the return value, and the first one is the deepest: `rmtree` is
    depth-first, so the file itself is reported ahead of the parents that could not go
    because of it.
    """
    target = _longpath(path)
    failures: list[str] = []

    def _clear_and_retry(_func, failed_path, _exc):
        failure = _retry_delete(failed_path)
        if failure:
            failures.append(failure)

    try:
        shutil.rmtree(target, onexc=_clear_and_retry)
    except OSError as exc:
        return str(exc)
    if not failures:
        return ""
    shown = "; ".join(failures[:3])
    return shown if len(failures) <= 3 else f"{shown} (+{len(failures) - 3} more)"


def force_remove_box(
    path: Path, run=sweep.run_windowless, sleep=time.sleep
) -> tuple[str, list[str]]:
    """Delete the box, evicting whatever still runs out of it if that is what refused.

    Returns `(error, notes)` — the error empty on success, the notes already phrased for
    the reap's report. The eviction is asked for **only over a delete that has already
    failed**: enumerating every process's modules costs a second or two, and a reap that
    succeeded owes nobody that. What it buys is the difference between a husk no future
    pass can clear and a box that goes on the same run.
    """
    error = remove_tree_longpath(path)
    if not error:
        return "", []
    evicted = evict_box_holders(path, run)
    if not evicted:
        return error, []
    # Windows releases the image section after the process is reaped rather than when
    # taskkill returns, so an immediate retry can still be denied.
    sleep(HOLDER_RELEASE_SECONDS)
    return remove_tree_longpath(path), [
        f"killed {len(evicted)} process(es) still running out of the box: {', '.join(evicted)}"
    ]


# What a filesystem-level deletion failure says, in each of the spellings this has been
# seen in. `Access is denied` is the live-process case this module exists for; without it
# the very first reap of a box with a dev server still up reported a failure and left the
# husk behind, and only the *second* pass -- by then a husk -- ever reached the fallback
# at all.
_DELETE_FAILED_SAYS = (
    "Filename too long",
    "Directory not empty",
    "Access is denied",
    "Permission denied",
    # Git for Windows' spelling of a delete the filesystem refused mid-walk, from a
    # session tree's reap in devkit on 2026-09-29 (`session_trees.py`).
    "Invalid argument",
)


def fallback_applies(path: Path, error: str) -> bool:
    """Whether a failed `git worktree remove` of `path` may be finished by hand.

    Deliberately narrow: a dirty-tree refusal ("contains modified or untracked
    files") must stay a refusal, because the fallback destroys what git just
    declined to. It applies when the error is a filesystem-level deletion failure,
    or when the box is already a husk -- a directory whose `.git` link is gone
    because a previous removal died partway -- which no `git worktree remove` can
    ever succeed on again.
    """
    if any(said in error for said in _DELETE_FAILED_SAYS):
        return True
    return not (path / ".git").exists()
