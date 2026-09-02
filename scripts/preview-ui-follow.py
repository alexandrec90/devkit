#!/usr/bin/env python3
"""Keep every host UI preview on the branch it was opened at.

`preview-ui-host.py` serves each picked ref from a **detached copy** under
`.ui-previews/<project>/<slug>`. The copy is a snapshot: it is detached at the ref the
run resolved, and nothing moves it afterwards. So a preview of `main` left open across
three merges keeps serving the commit it started on, and a reviewer reloading the tab
gets the same stale frame every time -- the browser cannot fix a server whose files are
old. Measured on 2026-09-02: a preview of roguelike `main` opened at `30aec57` was still
serving it after #11, #12 and #13 had merged, and the hard reloads spent on it could
never have worked.

This process closes that: every `--interval` seconds it fetches each served copy's ref
and, when the branch has moved, re-detaches the copy onto the new tip. Vite's watcher
sees a working tree full of changed files and reloads the page, which is the whole of
the fix -- verified against a live 5300 server, where a copy fast-forwarded under a
running Vite began serving modules that did not exist when it started.

Two things it will not do, both deliberate:

- **A copy with local edits is never moved.** `?edit=1`'s layout editor saves into the
  serving copy, and `git checkout` would discard that. Such a copy is reported once and
  then left alone -- the refusal is latched, because a message every 20 seconds about a
  state nobody has changed is noise that trains a reader to ignore the line.
- **A box is never touched.** `preview-ui-host.py` serves a live box's worktree *as it
  stands*, unpushed work included; that is the only place the work exists, and following
  a branch into it would destroy the very thing the preview was opened to show. Boxes
  are excluded structurally rather than by a flag: this only ever considers directories
  under `.ui-previews/`, and a box's worktree is not one.

**Why a separate process rather than a tick inside `preview-ui-host.py`'s watch loop**,
which is where this belongs: that file is 1110 lines and carries
`file_lines::scripts/preview-ui-host.py = 1110` in `.devkit-structure.txt`, so it cannot
grow by one line; and it cannot be split either, because relocating any of its
`# pragma: no cover` platform guards into a new file creates a new `suppressions::` key,
which `structure_check.verdict` counts as `worse`. Same for the task registration in
`devkit_project.py` (`file_lines = 1566`). The gate is right that those files are full
and wrong that relocation is new debt; that is filed as a harness defect, and when it is
fixed the loop below is three lines inside `watch()` and this file is deleted.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import sweep
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The host script owns where the copies live, how a ref becomes a directory name, and
# the registry of what is being served. Loaded rather than re-derived: a second copy of
# `ref_slug` would be a fork that agrees today and disagrees the first time either moves.
host = load_by_path("preview_ui_host", Path(__file__).resolve().parent / "preview-ui-host.py")

# How often each copy's ref is fetched. Twenty seconds because the thing being waited on
# is a human merging a PR, and the cost of a tick is one `git fetch` per open preview --
# so this is set by how soon a reviewer should stop seeing an old frame, not by how
# cheap the poll is.
FOLLOW_SECONDS = 20.0

# `git status` with untracked files excluded on purpose. What must block a checkout is a
# tracked file the checkout would overwrite; an untracked one it would leave alone --
# and Vite drops caches into the tree that would otherwise read as "somebody is editing
# here" and stop the following forever.
DIRTY_ARGS = ("status", "--porcelain", "--untracked-files=no")


@dataclass(frozen=True)
class Copy:
    """One served copy that may be moved: where it is, and the ref it should be on."""

    project: str
    ref: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.project} {self.ref}"


def copies(root: Path, entries: list[dict]) -> list[Copy]:
    """The `.ui-previews` copies named by the registry, in the order first recorded.

    An entry whose directory does not exist is dropped rather than reported: that is
    exactly what a box-served preview looks like from here -- the row is real, the box
    is real, and there is deliberately nothing under `.ui-previews` for it. Two servers
    on one ref (a second run picked it again) share a copy, so the list is deduplicated
    by path; syncing the same directory twice per tick would only race itself.
    """
    seen: dict[Path, Copy] = {}
    for entry in entries:
        project = str(entry.get("project") or "")
        ref = str(entry.get("ref") or "")
        if not project or not ref:
            continue
        path = root / host.UI_PREVIEWS_DIR_NAME / project / host.ref_slug(ref)
        if path.is_dir() and path not in seen:
            seen[path] = Copy(project=project, ref=ref, path=path)
    return list(seen.values())


def git(path: Path, args, run=subprocess.run):
    """One git call against a copy, captured. Never raises: every caller reads returncode."""
    return run(["git", "-C", str(path), *args], capture_output=True, text=True)


def first_line(result) -> str:
    """The most useful line of a failed git call, for a one-line report."""
    text = (result.stderr or result.stdout or "").strip()
    return text.splitlines()[0] if text else "no output"


def head_of(copy: Copy, run=subprocess.run) -> str:
    """The commit the copy is serving right now; empty when git will not say."""
    result = git(copy.path, ("rev-parse", "--verify", "HEAD"), run)
    return result.stdout.strip() if result.returncode == 0 else ""


def tip_of(copy: Copy, run=subprocess.run) -> tuple[str, str]:
    """`(sha, error)` for the branch's current tip, fetched fresh.

    `FETCH_HEAD` rather than `origin/<ref>` because it is what this fetch just brought
    down: it cannot be a remote-tracking ref left behind by some other run's refspec,
    and it is defined even where the fetch did not update one.
    """
    fetched = git(copy.path, ("fetch", "--quiet", "origin", copy.ref), run)
    if fetched.returncode != 0:
        return "", f"could not fetch origin {copy.ref} -- {first_line(fetched)}"
    result = git(copy.path, ("rev-parse", "--verify", "FETCH_HEAD"), run)
    if result.returncode != 0:
        return "", f"fetched origin {copy.ref} but could not read it -- {first_line(result)}"
    return result.stdout.strip(), ""


def is_dirty(copy: Copy, run=subprocess.run) -> bool:
    """Whether the copy carries tracked changes a checkout would discard."""
    result = git(copy.path, DIRTY_ARGS, run)
    return bool(result.stdout.strip()) if result.returncode == 0 else True


def advance(copy: Copy, run=subprocess.run) -> str:
    """Move one copy onto its branch's tip. The line to print, or empty for "nothing to say".

    Empty is the ordinary answer: a preview whose branch has not moved says nothing on
    every tick, which is what makes the lines this does print worth reading.
    """
    current = head_of(copy, run)
    if not current:
        return f"[warn] {copy.label}: not a git worktree at {copy.path}"
    tip, error = tip_of(copy, run)
    if error:
        return f"[warn] {copy.label}: {error}"
    if tip == current:
        return ""
    if is_dirty(copy, run):
        return (
            f"[held] {copy.label} has local edits -- not following. Commit or discard "
            f"them in {copy.path} to resume."
        )
    moved = git(copy.path, ("checkout", "--detach", tip), run)
    if moved.returncode != 0:
        return f"[warn] {copy.label}: could not move onto {tip[:7]} -- {first_line(moved)}"
    return f"[followed] {copy.label}: {current[:7]} -> {tip[:7]} -- reload the tab"


def sync_all(found: list[Copy], seen: dict[Path, str], run=subprocess.run) -> list[str]:
    """Advance every copy, returning only the lines a reader has not already been told.

    `seen` is the latch, and it is keyed by path and valued by the last line that path
    produced: a refusal repeats every tick until the state changes, and printing it 180
    times an hour would bury the `[followed]` lines that are the point of this process.
    A copy that goes quiet is forgotten, so the next refusal is reported afresh.
    """
    lines = []
    for copy in found:
        line = advance(copy, run)
        if line and seen.get(copy.path) != line:
            lines.append(line)
        if line:
            seen[copy.path] = line
        else:
            seen.pop(copy.path, None)
    return lines


def follow(root: Path, interval: float, once=False, sleep=time.sleep, run=subprocess.run) -> int:
    """Poll until interrupted, re-reading the registry each tick.

    The registry is re-read rather than captured once so this can be started before any
    preview is open, and survive every one of them being stopped and others started --
    which is the difference between something you leave running and something you have
    to remember to restart alongside the task it follows.
    """
    seen: dict[Path, str] = {}
    while True:
        for line in sync_all(copies(root, host.read_registry()), seen, run):
            host.echo(line)
        if once:
            return 0
        sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preview-ui-follow.py",
        description="Keep every open host UI preview on the tip of the branch it shows.",
    )
    parser.add_argument("--workspace", type=Path, default=None, help="the .code-workspace registry")
    parser.add_argument(
        "--interval",
        type=float,
        default=FOLLOW_SECONDS,
        metavar="SECONDS",
        help=f"how often each copy's ref is fetched (default {FOLLOW_SECONDS:.0f})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="sync every copy once and exit, rather than holding the terminal",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = args.workspace or sweep.default_workspace(REPO_ROOT)
    if not workspace.is_file():
        host.echo(f"no workspace registry at {workspace}")
        return 2
    root = workspace.parent
    open_now = copies(root, host.read_registry())
    if not args.once:
        following = ", ".join(copy.label for copy in open_now) or "nothing yet"
        host.echo(f"Following {following}. Ctrl+C stops. New previews are picked up as they open.")
    return follow(root, args.interval, once=args.once)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        host.echo("\nStopped following. Every preview keeps serving whatever it is on.")
        raise SystemExit(0) from None
