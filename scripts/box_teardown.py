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


def box_holders(path: Path, run=subprocess.run) -> list[tuple[int, str]]:
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


def evict_box_holders(path: Path, run=subprocess.run) -> list[str]:
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


def remove_tree_longpath(path: Path) -> str:
    """Delete `path` recursively, surviving Windows MAX_PATH. Empty string on success.

    A provisioned box carries a `.venv` whose nesting routinely exceeds MAX_PATH, and
    `git worktree remove` deletes with plain Win32 calls -- so a reap of a perfectly
    clean box died with `Filename too long`, leaving a half-deleted husk that
    classifies as `skipped` and reads as "holding work" forever. The `\\\\?\\` prefix
    turns the limit off; the `onexc` hook clears the read-only bit that Windows also
    uses to refuse deletion of some packaging artifacts.

    **The hook records a failure it cannot fix rather than raising it.** Re-raising
    aborts `rmtree` at the first unfixable entry, so one file a live process had mapped
    kept the whole rest of the tree on disk -- and the caller was told about that one
    path as though it were the only thing left. Everything deletable goes now, the
    failures are the return value, and the first one is the deepest: `rmtree` is
    depth-first, so the file itself is reported ahead of the parents that could not go
    because of it.
    """
    target = str(path)
    if os.name == "nt" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + os.path.abspath(target)
    failures: list[str] = []

    def _clear_and_retry(func, failed_path, _exc):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError as retry_exc:
            failures.append(f"{failed_path}: {retry_exc.strerror or retry_exc}")

    try:
        shutil.rmtree(target, onexc=_clear_and_retry)
    except OSError as exc:
        return str(exc)
    if not failures:
        return ""
    shown = "; ".join(failures[:3])
    return shown if len(failures) <= 3 else f"{shown} (+{len(failures) - 3} more)"


def force_remove_box(path: Path, run=subprocess.run, sleep=time.sleep) -> tuple[str, list[str]]:
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
