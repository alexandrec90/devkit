#!/usr/bin/env python3
"""Reap a session worktree whose PR has merged, and the compose project it started.

`claude --worktree` and the fix pass (`fix-prs.py`, `agent-worktree.py new`) cut trees
under `<checkout>/.claude/worktrees/`. `worktree.py reconcile` reaps only the box tier,
and nothing else ever removed one of these: 30 stood in devkit and 20 in carameli on
2026-09-26, most of them for PRs merged days before. Two costs followed.

- **A stack left running.** Each tree gets its own `COMPOSE_PROJECT_NAME` in `.env`, so
  a fixer that brings the stack up leaves a second copy of it behind -- carameli's
  `carameli-fix-nightly-0919` held 1.3 GB for days after its PR merged.
- **A stale copy of the harness.** A tree keeps the vendored rules and skills it was cut
  with, and 21 of 25 trees on 2026-09-20 still told a session to push. A session reopened
  in one reads instructions devkit has since withdrawn.

The verdict is deliberately narrow, because a tree can hold the only copy of work:

- the tree is in an agent tier (never the checkout, never a box: those have owners);
- git does not report it locked, and no session has written a transcript in it for
  `QUIET_HOURS` -- a lock is not enough alone, since a session does not always take one;
- `git status --porcelain` is empty -- nothing uncommitted, nothing untracked;
- its branch has a **merged** PR whose head is the tree's `HEAD`, so every commit in the
  tree is on the remote and in the default branch.

Anything else keeps the tree, with the reason. The compose project goes first, scoped
with `-p` to the tree's own name; a name equal to the checkout's would be the static
checkout's stack and is refused. The worktree is removed without `--force`, so git's
own refusal stands. The branch is left alone.

**A removal Windows refuses partway is finished, not left.** `git worktree remove`
deletes the `.git` link and the registration before the ignored `.venv`, so one file the
filesystem refused (`Invalid argument`, devkit's `fix-harness-ledger-0928` on
2026-09-29) left a husk no later `worktree list` names and no pass would ever clear. A
filesystem-level failure is finished with `box_teardown.force_remove_box`, the box
tier's own fallback, and a husk already standing in the tier -- no `.git`, unregistered,
quiet for `QUIET_HOURS` -- is removed the same way. A dirty-tree refusal keeps its `.git`
and so is never finished by hand.

**What only an administrator can delete is named, not failed.** An elevated session's
pytest left a `.pytest_cache` in a carameli tree that grants Administrators alone
(`box_teardown.unopenable`), and the unelevated scheduled reap failed on it every run --
so the ledger sent a fixer after it every run, and no fixer, unelevated by design, could
act. The line says which directory and gives the elevated command; an elevated pass that
still fails is an ordinary failure.

**A stopped Docker engine keeps the tree, and is not a failure.** `compose down` cannot
reach it, so the scheduled reap failed on every tree with a stack while Docker Desktop
was stopped and the ledger sent a fixer after each run (3dfd297d, five times; one fix
lived only in an uncommitted tree and never shipped). The tree waits whole, as `reconcile`
makes a box wait (`worktree.docker_engine_down`); the signs are
`box_teardown.DAEMON_DOWN_SIGNS`. A wedged engine behind a running Desktop is the same
case in words no sign names, so a failure the signs miss asks the engine itself
(`ENGINE_PROBE`) before it is called the stack's.

Run by `reap-stale.py`, the scheduled pass for what agent sessions leave behind.
Tested in `tests/test_session_trees.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import box_teardown
import rc_machine
import sweep
import worktree_tiers as wt
import wt_profile

Run = Callable[..., subprocess.CompletedProcess[str]]
Remove = Callable[[Path], tuple[str, list[str]]]

# Enough to cover every PR a tree on this machine could still be holding.
MERGED_LOOKBACK = 200

# Untracked files the harness generates into a tree (`codex-session-start.py`, from
# `CLAUDE.md` and the settings), ignored by devkit's `.gitignore` today but not by the
# one a tree cut before that carries. Regenerable, so they hold no work.
GENERATED = frozenset({"AGENTS.md", ".codex/config.toml"})

# How long a tree's sessions must have been silent. A session resumed in a finished tree
# to start the next piece of work is clean and merged until its first edit.
QUIET_HOURS = 12

# `reap`'s answer when `compose down` could not reach Docker's engine. The tree is kept
# whole rather than removed: its stack's containers and volumes would outlive it with
# nothing left to name them, so no later pass could tear them down.
ENGINE_DOWN = (
    "kept -- Docker's engine is not answering, so its stack cannot be torn down; "
    "the next pass with the engine up reaps it, stack and all"
)

# What `down_stack` asks the engine when `compose down` failed in words that name no
# unreachable engine: the cheapest question about every container rather than one. A
# wedged engine fails in spellings no list of signs keeps up with -- a 500 on the
# container listing at 2026-10-09 17:00, under a restart collectors.py held for a live
# scrape (41383e56) -- so a failure is read as the engine's when the engine cannot
# answer this either.
ENGINE_PROBE = ["docker", "ps", "-q"]

# Each way a removal here fails: a marker its artifact line carries, and the kind
# `reap-stale.cause_line` files it under on the ledger. The line names the tree and the
# file the filesystem refused; the kind names neither, so one defect is one ledger group
# whichever tree it hit (9d0f1476). A line's first marker decides it, so the fallback's
# failure is listed before the bare git refusal it also contains.
HUSK_REFUSED = "could not remove its husk"
REAP_REFUSED = "could not reap"
FAILURE_KINDS = {
    HUSK_REFUSED: "a session tree's husk could not be removed",
    f"{REAP_REFUSED}: compose down": "a merged session tree's stack would not come down",
    "and finishing it failed": "a merged session tree could not be removed after git refused it",
    f"{REAP_REFUSED}: git worktree remove": "git refused to remove a merged session tree",
}


@dataclass(frozen=True)
class Tree:
    path: Path
    branch: str
    head: str
    locked: bool


def parse_trees(porcelain: str) -> list[Tree]:
    """Every worktree in `git worktree list --porcelain`, with its lock."""
    found: list[Tree] = []
    fields: dict[str, str] = {}
    for line in [*(porcelain or "").splitlines(), ""]:
        if line.strip():
            key, _, value = line.partition(" ")
            fields[key] = value.strip()
            continue
        if "worktree" in fields:
            found.append(
                Tree(
                    path=Path(fields["worktree"]),
                    branch=fields.get("branch", "").removeprefix("refs/heads/"),
                    head=fields.get("HEAD", ""),
                    locked="locked" in fields,
                )
            )
        fields = {}
    return found


def merged_heads(payload: str) -> dict[str, str]:
    """`{head branch: head sha}` from `gh pr list --state merged --json headRefName,headRefOid`."""
    try:
        rows = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return {}
    return {
        row["headRefName"]: row["headRefOid"]
        for row in rows
        if isinstance(row, dict) and row.get("headRefName") and row.get("headRefOid")
    }


def compose_name(tree: Path) -> str:
    """The tree's own `COMPOSE_PROJECT_NAME` from its `.env`; `""` when it set none."""
    try:
        text = (tree / ".env").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "COMPOSE_PROJECT_NAME":
            return value.strip().strip("'\"")
    return ""


def changes(tree: Path, run: Run) -> tuple[list[str], list[str]] | None:
    """`(work, noise)` in the tree; None when its status cannot be read.

    Noise is what holds no work: an untracked `GENERATED` file, and a tracked file whose
    only change is its line endings -- `git diff` shows nothing for it, but `status`
    lists it (carameli's `.secrets.baseline` rewritten CRLF, in six trees).
    """
    status = run(["git", "-C", str(tree), "status", "--porcelain"])
    if status.returncode != 0:
        return None
    work: list[str] = []
    noise: list[str] = []
    for line in status.stdout.splitlines():
        code, path = line[:2], line[3:].strip().strip('"')
        if code == "??" and path in GENERATED:
            noise.append(path)
        elif (
            code == " M"
            and run(["git", "-C", str(tree), "diff", "--quiet", "--", path]).returncode == 0
        ):
            noise.append(path)
        elif line.strip():
            work.append(path)
    return work, noise


def idle_seconds(tree: Path, store: Path | None = None, now: float | None = None) -> float | None:
    """Seconds since a session last wrote a transcript in `tree`; None when unknowable."""
    last = rc_machine.last_activity(tree, rc_machine.sessions_store() if store is None else store)
    return None if last is None else (time.time() if now is None else now) - last


def verdict(
    tree: Tree, checkout: Path, dirty: bool | None, merged: dict[str, str], idle: float | None
) -> str:
    """`""` when the tree may be reaped, else why it is kept. None for `dirty` or `idle`
    means the question could not be answered, and an unanswered question keeps the tree."""
    if wt.same_dir(tree.path, checkout) or wt.tier_of(tree.path) is None:
        return "not a session worktree"
    if tree.locked:
        return "locked -- a session is using it"
    if idle is None or idle < QUIET_HOURS * 3600:
        return f"a session wrote in it within {QUIET_HOURS}h, or the store could not be read"
    if dirty is None:
        return "its status could not be read"
    if dirty:
        return "holds uncommitted or untracked files"
    if not tree.branch:
        return "detached HEAD"
    if tree.branch not in merged:
        return f"no merged PR for {tree.branch}"
    if merged[tree.branch] != tree.head:
        return "HEAD is not the head its PR merged"
    return ""


def down_stack(tree: Tree, checkout: Path, run: Run) -> str:
    """Tear down the tree's own compose project. `""` when done or there is none.

    `ENGINE_DOWN` when Docker's engine could not be reached -- its error says so, or it
    cannot answer `ENGINE_PROBE` either -- before the tree is touched, so a kept tree is
    kept whole. A name equal to the checkout's is the static checkout's stack and is
    never downed.
    """
    name = compose_name(tree.path)
    if not name or name == checkout.name:
        return ""
    down = run(["docker", "compose", "-p", name, "down", "-v"])
    if down.returncode == 0:
        return ""
    said = f"{down.stderr or ''}\n{down.stdout or ''}"
    if box_teardown.engine_unreachable(said) or run(ENGINE_PROBE).returncode != 0:
        return ENGINE_DOWN
    return f"compose down -p {name} failed: {(down.stderr or '').strip()[-200:]}"


def reap(
    tree: Tree,
    checkout: Path,
    run: Run,
    noise: Sequence[str] = (),
    remove: Remove | None = None,
) -> str:
    """Tear the tree's stack down and remove the tree. `""` on success, else the error.

    `noise` is cleared first -- generated files deleted, line-ending-only changes checked
    out -- so the removal needs no `--force`, and git still refuses anything else. A
    refusal from the filesystem rather than from git is finished with `remove`; one whose
    directory is gone by then -- delete-pending when git asked, released since -- has
    nothing left to finish, and only the registration is pruned (ad9e1a06).
    """
    error = down_stack(tree, checkout, run)
    if error:
        return error
    for path in noise:
        if path in GENERATED:
            (tree.path / path).unlink(missing_ok=True)
        else:
            run(["git", "-C", str(tree.path), "checkout", "--", path])
    removed = run(["git", "-C", str(checkout), "worktree", "remove", str(tree.path)])
    if removed.returncode == 0:
        return ""
    said = (removed.stderr or "").strip()[-200:]
    if box_teardown.delete_refused(said) and not tree.path.exists():
        run(["git", "-C", str(checkout), "worktree", "prune"])
        return ""
    if not (tree.path.is_dir() and box_teardown.fallback_applies(tree.path, said)):
        return f"git worktree remove refused: {said}"
    error, _notes = (remove or box_teardown.force_remove_box)(tree.path)
    run(["git", "-C", str(checkout), "worktree", "prune"])
    return (
        f"git worktree remove refused ({said}), and finishing it failed: {error}" if error else ""
    )


def admin_only(path: Path) -> str:
    """Why only an administrator can finish removing `path`, or "" when that is not why.

    Asked only after a removal failed, and never of an elevated pass: one of those that
    still fails has hit something else. The command removes the whole directory, since
    everything this process could delete is already gone.
    """
    if wt_profile.is_elevated() or not path.exists():
        return ""
    locked = box_teardown.unopenable(path)
    if not locked:
        return ""
    more = f" (+{len(locked) - 1} more)" if len(locked) > 1 else ""
    command = (
        f"Remove-Item -LiteralPath '{path}' -Recurse -Force"
        if sys.platform == "win32"
        else f"sudo rm -rf '{path}'"
    )
    return (
        f"only an administrator can remove it -- an elevated process left {locked[0]}{more} "
        f"that this unelevated pass may not open; from an elevated shell run: {command}"
    )


def husks(checkout: Path, listed: Sequence[Tree]) -> list[Path]:
    """Directories in the checkout's own tier that a removal died partway through.

    No `.git` entry and no registration: git has already let go of it, so nothing in it
    is a commit, and no `git worktree remove` can succeed on it again.
    """
    root = wt.default_root(checkout)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [
        path
        for path in entries
        if path.is_dir()
        and not (path / ".git").exists()
        and not any(wt.same_dir(path, tree.path) for tree in listed)
    ]


def sweep_husks(
    checkout: Path,
    listed: Sequence[Tree],
    apply: bool,
    say: Callable[[str], None],
    idle: Callable[[Path], float | None],
    remove: Remove | None = None,
) -> int:
    """Remove (with `apply`) every quiet husk of one checkout. The count that failed."""
    failures = 0
    for path in husks(checkout, listed):
        quiet = idle(path)
        if quiet is None or quiet < QUIET_HOURS * 3600:
            continue
        label = f"session tree {checkout.name}:{path.name}"
        if not apply:
            say(f"{label}: a husk a removal left behind -- would remove")
            continue
        error, _notes = (remove or box_teardown.force_remove_box)(path)
        why = admin_only(path) if error else ""
        if why:
            say(f"{label}: {why}")
        elif error:
            failures += 1
            say(f"{label}: {HUSK_REFUSED}: {error}")
        else:
            say(f"{label}: a husk a removal left behind -- removed")
    return failures


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`argv` bounded with its whole tree (`sweep.run_bounded`): a `docker compose` here
    runs the compose plugin as a child holding the pipes, which `subprocess.run(timeout=)`
    would wait on past its timeout for as long as the engine is wedged
    (`worktree.compose`). A timeout or a missing program is an exit 1 saying so."""
    try:
        done = sweep.run_bounded(list(argv), 300)
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))
    if done.returncode == sweep.TIMED_OUT:
        return subprocess.CompletedProcess(list(argv), 1, done.stdout, done.stderr)
    return done


def sweep_checkout(
    checkout: Path,
    apply: bool,
    say: Callable[[str], None],
    run: Run = _run,
    gh_for: Callable[[Path], Run] = sweep.gh_for,
    idle: Callable[[Path], float | None] = idle_seconds,
) -> int:
    """Assess (and with `apply`, reap) every session tree and husk of one checkout.
    The count of failures.

    One `gh` call per checkout, not per tree: the pass runs every few minutes.
    """
    listed = run(["git", "-C", str(checkout), "worktree", "list", "--porcelain"])
    if listed.returncode != 0:
        return 0  # not a git checkout: nothing to assess
    every = parse_trees(listed.stdout)
    failures = sweep_husks(checkout, every, apply, say, idle)
    trees = [t for t in every if wt.tier_of(t.path) is not None]
    if not trees:
        return failures
    fields = "headRefName,headRefOid"
    prs = gh_for(checkout)(
        "pr", "list", "--state", "merged", "--limit", str(MERGED_LOOKBACK), "--json", fields
    )
    if prs.returncode != 0:
        say(f"session trees in {checkout.name}: merged PRs could not be read -- all kept")
        return failures
    merged = merged_heads(prs.stdout)
    for tree in trees:
        found = changes(tree.path, run)
        work, noise = found if found is not None else ([], [])
        dirty = None if found is None else bool(work)
        why = verdict(tree, checkout, dirty, merged, idle(tree.path))
        label = f"session tree {checkout.name}:{wt.label(tree.path)}"
        if why:
            continue  # kept trees are the ordinary case; the artifact names only reaps
        if not apply:
            say(f"{label}: its PR merged at this HEAD -- would reap")
            continue
        line, failed = reap_outcome(tree.path, reap(tree, checkout, run, noise))
        failures += failed
        say(f"{label}: {line}")
    return failures


def reap_outcome(path: Path, error: str) -> tuple[str, bool]:
    """What a reap's `error` reads as in the artifact, and whether it is a failure.

    Two refusals keep the tree without counting one, because no fixer can act on
    either and the scheduled job going red sends one anyway: a stopped Docker engine
    (`ENGINE_DOWN`; the next pass with it up reaps the tree whole), and a directory only
    an administrator may delete (`admin_only`).
    """
    if not error:
        return "its PR merged at this HEAD -- reaped", False
    if error == ENGINE_DOWN:
        return error, False
    why = admin_only(path)
    if why:
        return why, False
    return f"{REAP_REFUSED}: {error}", True


def sweep_workspace(workspace: Path, apply: bool, say: Callable[[str], None]) -> int:
    """Every checkout the workspace file lists. The count of trees that failed to reap."""
    try:
        names = sweep.parse_workspace(workspace.read_text(encoding="utf-8"))
    except OSError:
        return 0
    return sum(sweep_checkout(workspace.parent / name, apply, say) for name in names)
