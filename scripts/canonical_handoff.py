"""Move an edit of devkit's canonical `workspace.jsonc` off the static checkout onto a
task branch, with a ship intent for the fix pass to ship.

The static checkout is the one the scheduled fix pass runs from. Its watchdog
fast-forwards it before every fire and refuses a dirty one, and a checkout someone moved
onto a branch to commit the edit there runs that branch's code instead of `main`'s. Both
held the pass and filed `pass-stale`: `new-project.py`'s registration left uncommitted
(851308ef), and an unplug committed on `agent/unplug-carameli-1111` in the checkout
itself, the branch `plug-projects.py` told the person to make (a5ec492b). So each tool
that writes the canonical copy from the static checkout hands its edit off here.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))

import task_branch as tb
import worktree_tiers

INTENT = Path("logs") / "ship-intent.md"


@dataclass(frozen=True)
class Cut:
    """What `hand_off` did: the branch and tree it cut, or why it cut none (`said`)."""

    branch: str = ""
    tree: Path | None = None
    said: str = ""


def git_in(
    checkout: Path, runner=subprocess.run
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """`git(*args)` bound to `checkout`, never raising."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return runner(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    return git


def dirty(path: Path, runner=subprocess.run) -> bool:
    """Whether git reports `path` changed in its checkout; False where git cannot say."""
    done = git_in(path.parent, runner)("status", "--porcelain", "--", path.name)
    return done.returncode == 0 and bool(done.stdout.strip())


def hand_off(
    canonical: Path,
    slug: str,
    intent: str,
    runner=subprocess.run,
    carry: Callable[[Path], str] | None = None,
) -> Cut:
    """Put the uncommitted edit of `canonical` on `agent/<slug>-<mmdd>`, in a
    `.claude/worktrees/` tree off the checkout's `HEAD` -- the commit the edit was made
    against -- with `intent` as its ship intent, and put the static checkout's copy back.

    `carry(tree)` writes whatever else the branch takes and returns the text to add to
    the intent. An empty `Cut` when there is nothing to hand off: a linked worktree or
    a task branch (the edit is on a branch already), a checkout git cannot read, or no
    edit. A cut that failed says so in `said`, with the file left as it was.
    """
    checkout = canonical.parent
    git = git_in(checkout, runner)
    if not (checkout / ".git").is_dir() or not dirty(canonical, runner):
        return Cut()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != tb.detect_default_branch(git, fallback="main"):
        return Cut()
    heads = git("for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split()
    task = tb.branch_name(tb.slugify(slug), set(heads))
    tree = worktree_tiers.default_root(checkout) / task.partition("/")[2]
    cut = git("worktree", "add", "--quiet", "--no-track", "-b", task, str(tree), "HEAD")
    if cut.returncode != 0:
        said = (cut.stderr or cut.stdout or "").strip().splitlines()
        return Cut(said=f"could not cut {task}: {said[-1] if said else cut.returncode}")
    edited = canonical.read_text(encoding="utf-8")
    (tree / canonical.name).write_text(edited, encoding="utf-8", newline="\n")
    message = intent + (carry(tree) if carry else "")
    (tree / INTENT).parent.mkdir(parents=True, exist_ok=True)
    (tree / INTENT).write_text(message, encoding="utf-8", newline="\n")
    git("checkout", "--", canonical.name)
    return Cut(branch=task, tree=tree)
