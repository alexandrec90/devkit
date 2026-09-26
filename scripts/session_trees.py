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
import rc_machine
import sweep
import worktree_tiers as wt

Run = Callable[..., subprocess.CompletedProcess[str]]

# Enough to cover every PR a tree on this machine could still be holding.
MERGED_LOOKBACK = 200

# Untracked files the harness generates into a tree (`codex-session-start.py`, from
# `CLAUDE.md` and the settings), ignored by devkit's `.gitignore` today but not by the
# one a tree cut before that carries. Regenerable, so they hold no work.
GENERATED = frozenset({"AGENTS.md", ".codex/config.toml"})

# How long a tree's sessions must have been silent. A session resumed in a finished tree
# to start the next piece of work is clean and merged until its first edit.
QUIET_HOURS = 12


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


def reap(tree: Tree, checkout: Path, run: Run, noise: Sequence[str] = ()) -> str:
    """Tear the tree's stack down and remove the tree. `""` on success, else the error.

    `noise` is cleared first -- generated files deleted, line-ending-only changes checked
    out -- so the removal needs no `--force`, and git still refuses anything else.
    """
    for path in noise:
        if path in GENERATED:
            (tree.path / path).unlink(missing_ok=True)
        else:
            run(["git", "-C", str(tree.path), "checkout", "--", path])
    name = compose_name(tree.path)
    if name and name != checkout.name:
        down = run(["docker", "compose", "-p", name, "down", "-v"])
        if down.returncode != 0:
            return f"compose down -p {name} failed: {(down.stderr or '').strip()[-200:]}"
    removed = run(["git", "-C", str(checkout), "worktree", "remove", str(tree.path)])
    if removed.returncode != 0:
        return f"git worktree remove refused: {(removed.stderr or '').strip()[-200:]}"
    return ""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            creationflags=sweep.NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def sweep_checkout(
    checkout: Path,
    apply: bool,
    say: Callable[[str], None],
    run: Run = _run,
    gh_for: Callable[[Path], Run] = sweep.gh_for,
    idle: Callable[[Path], float | None] = idle_seconds,
) -> int:
    """Assess (and with `apply`, reap) every session tree of one checkout. Failures.

    One `gh` call per checkout, not per tree: the pass runs every few minutes.
    """
    listed = run(["git", "-C", str(checkout), "worktree", "list", "--porcelain"])
    if listed.returncode != 0:
        return 0  # not a git checkout: nothing to assess
    trees = [t for t in parse_trees(listed.stdout) if wt.tier_of(t.path) is not None]
    if not trees:
        return 0
    fields = "headRefName,headRefOid"
    prs = gh_for(checkout)(
        "pr", "list", "--state", "merged", "--limit", str(MERGED_LOOKBACK), "--json", fields
    )
    if prs.returncode != 0:
        say(f"session trees in {checkout.name}: merged PRs could not be read -- all kept")
        return 0
    merged = merged_heads(prs.stdout)
    failures = 0
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
        error = reap(tree, checkout, run, noise)
        if error:
            failures += 1
            say(f"{label}: could not reap: {error}")
        else:
            say(f"{label}: its PR merged at this HEAD -- reaped")
    return failures


def sweep_workspace(workspace: Path, apply: bool, say: Callable[[str], None]) -> int:
    """Every checkout the workspace file lists. The count of trees that failed to reap."""
    try:
        names = sweep.parse_workspace(workspace.read_text(encoding="utf-8"))
    except OSError:
        return 0
    return sum(sweep_checkout(workspace.parent / name, apply, say) for name in names)
