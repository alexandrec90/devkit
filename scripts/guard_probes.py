"""What `worktree-guard.py` asks git about a path before it routes an edit.

Split out of the guard because that module is at its structural ceiling — every entry
`.devkit-structure.txt` records for it (`file_lines`, `definitions`) is pinned at the
value the file already had, so it cannot grow by a line without the ratchet calling it
worse. The seam is the one `structure_check.py` names: these are leaf functions with one
dependency between them and none on the guard's decision logic, so they lift out whole.

Stdlib only, and deliberately so — this runs inside a PreToolUse hook, before any
virtualenv exists.

Every probe here **fails closed**: when git will not answer, the answer is the one that
routes the edit into a box. A hook that cannot read the repo must not start letting
writes through on the strength of a failed subprocess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The same value `sweep.NO_WINDOW` carries, computed the same way rather than imported:
# this module's whole point is to have no dependency worth loading. `CREATE_NO_WINDOW`
# does not exist off Windows, where zero is the correct flag.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `checkout`, capturing stdout. Never raises; never opens a window."""
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        # UTF-8 with replacement, never the platform codec. `text=True` alone decodes
        # through cp1252 here, and a byte it cannot map -- in a branch name, a path, a
        # commit subject -- raises inside subprocess's reader thread, where this
        # function's `check=False` cannot see it. That would crash the guard on a
        # PreToolUse call, which is every edit in the workspace. The full account is the
        # codec note under `VERIFY_IMPORT` in scripts/hooks/stop.py.
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        creationflags=NO_WINDOW,
    )


def path_is_ignored(checkout: Path, target: Path) -> bool:
    """True when `target` is git-ignored inside `checkout`.

    The premise of every block the guard issues is that the edit "would land on the home
    branch" — and for an ignored path that premise is simply false. `.env`, `.local/`,
    `logs/` and the rest of a checkout's untracked local state cannot land on any branch,
    so routing them to a box does not protect a branch from anything. It costs the turn
    and delivers nothing: the box gets its *own* seeded `.env`, so re-issuing the edit
    there writes the value into a worktree that is destroyed without ever shipping it,
    and the file the agent was actually asked to configure stays unchanged.

    `git check-ignore` is the right oracle rather than a hard-coded list of names,
    because what is ignored is per-project and already written down in `.gitignore`.
    It consults the index by default (no `--no-index`), so a *tracked* file that also
    matches an ignore rule reports as not-ignored and still gets a box — tracked is
    tracked, and that is the case the hook exists for.
    """
    try:
        result = git(checkout, "check-ignore", "--quiet", "--", str(target))
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def path_is_modified(checkout: Path, target: Path) -> bool:
    """True when `target` is a **tracked** file already carrying uncommitted changes.

    The premise of a block is that the edit "would land on the home branch with no task
    branch under it" — the thing a later sweep reports as `needs-branch` and that "looks
    like a human left it there". When the file is already dirty, a human (or their
    tooling) *did* leave it there: the verdict is already true of that path, the edit
    cannot make it truer, and the box cannot make it false, because a box cut from
    `origin/<default>` does not contain the change under discussion.

    That last clause is the whole cost, and it is measurable rather than theoretical. On
    2026-08-31 a carameli session was asked to review a `layoutConfig.ts` the user had
    just written from the comic-book skin's in-browser editor and staged on `master`. The
    guard routed all ten of its edits, every one of them carrying
    `not re-aimed automatically: the box's copy of the file does not contain the text this
    edit replaces` — which is not a coincidence but the shape of this case: the box holds
    `origin/<default>`'s copy, so a dirty file's `old_string` is missing *by construction*
    and the re-aim can never fire. The session ended with the user's file unfixed on
    `master`, a second divergent copy of it in a box holding work with no PR, and nothing
    to show for either.

    **Tracked only** — `--untracked-files=no`. An untracked file is not the human's WIP by
    the same argument: the agent's own `Write` can create one, and routing that is a hole
    to close rather than an exemption to widen.
    """
    try:
        result = git(checkout, "status", "--porcelain", "--untracked-files=no", "--", str(target))
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def path_is_exempt(checkout: Path, target: Path) -> bool:
    """True when this path gives a box nothing to protect, for either reason above.

    One predicate rather than two consecutive tests in `redirect_decision`, because the
    two answers are the same answer: *the premise of a block does not hold here*. An
    ignored path can never reach a branch; a dirty one is already on the home branch
    unbranched, which is the outcome a block exists to prevent and cannot undo.

    Ordered so the cheaper, commoner refusal short-circuits: `check-ignore` answers from
    the index, and a `.env` never pays for a status walk. The second probe is reached
    only by a tracked path inside a checkout, which was already about to spawn a whole
    worktree, so one more local git call is not the expensive part of that turn.
    """
    return path_is_ignored(checkout, target) or path_is_modified(checkout, target)


def branch_has_own_commits(checkout: Path, detect_default) -> bool:
    """True when `checkout`'s HEAD carries commits `origin/<default>` does not.

    One of the two producers of `needs_box`'s `protects_open_work`: a task branch with
    commits of its own has somewhere for an edit to belong -- an open PR, or one about
    to exist. A task branch with none is either freshly cut or already merged, and in
    both cases a box strands nothing.

    Local only, deliberately. The honest question is "has this branch got an open PR",
    and asking GitHub would answer it exactly -- but this runs in a PreToolUse hook on
    every edit that reaches a static checkout, where a network round trip is latency the
    agent experiences as a hang. `git rev-list` against the *already fetched*
    `origin/<default>` costs milliseconds and agrees with the PR in every case that
    matters; the disagreement is a branch pushed and merged since the last fetch, which
    the next fetch resolves.

    `detect_default` is injected rather than imported so this module keeps its one
    property: nothing here loads anything. The guard passes `task_branch`'s resolver.

    **Fails closed** -- every error returns True, meaning "decline, leave the edit
    alone". A hook that cannot read the repo must not start diverting edits into boxes
    on the strength of a failed subprocess.
    """
    try:
        default = detect_default(lambda *args: git(checkout, *args))
        probe = git(checkout, "rev-list", "--count", f"origin/{default}..HEAD")
    except (OSError, subprocess.SubprocessError, ValueError):
        return True
    if probe.returncode != 0:
        return True
    try:
        return int(probe.stdout.strip()) > 0
    except ValueError:
        return True


def branch_is_a_sweep_park(checkout: Path, marker: str) -> bool:
    """True when `sweep.py --branch` parked this checkout's uncommitted work here.

    The other producer of `protects_open_work`, and the two tools disagreed without it.
    `--branch` cuts an `agent/...` branch **in place**, from HEAD, precisely so a dirty
    tree comes along untouched -- and the branch then has no commits, so the very next
    edit was routed into a box cut from `origin/<default>`, which is the one place that
    work is not. The sanctioned "park stranded work on a branch, then commit it" flow
    could not reach its own second step.

    Two terms, and both are needed. `marker` is the anchor file `--branch` writes and
    nothing else does, so it names this exact flow rather than any task branch (the
    guard passes `sweep.ANCHOR_MARKER_NAME`, so there is no second copy of the name).
    Dirtiness is what makes a box strand something: once the work is committed,
    `branch_has_own_commits` answers on its own, and a *clean* checkout left on a spent
    task branch is the shared-unguarded-space case `needs_box` was rewritten to close --
    so it must still be routed.

    Fails to False, unlike every other probe here: this is the widening term, and a
    probe that cannot read the repo must not be the thing that turns the guard off.
    `branch_has_own_commits` is asked first and fails closed, so a broken repo declines
    before this is ever reached.
    """
    try:
        anchor = git(checkout, "rev-parse", "--git-path", marker)
        if anchor.returncode != 0:
            return False
        path = Path(anchor.stdout.strip())
        recorded = path if path.is_absolute() else checkout / path
        if not recorded.is_file() or not recorded.read_text(encoding="utf-8").strip():
            return False
        dirty = git(checkout, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return dirty.returncode == 0 and bool(dirty.stdout.strip())


def branch_protects_open_work(checkout: Path, detect_default, marker: str) -> bool:
    """`needs_box`'s `protects_open_work`, resolved: either producer is enough.

    One name rather than a second probe injected into `redirect_decision`, which takes
    more arguments than anything should already. Asked in cost order -- a `rev-list`
    against an already-fetched ref, then a marker read and a `status` only if that came
    back empty.
    """
    return branch_has_own_commits(checkout, detect_default) or branch_is_a_sweep_park(
        checkout, marker
    )
