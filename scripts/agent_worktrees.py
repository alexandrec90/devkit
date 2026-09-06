"""What a `.claude/worktrees/` worktree is, and what the two dropdowns draw for it.

The pure half of `scripts/agent-worktree.py`: parsing `git worktree list`, deciding
whether a worktree can be removed without losing anything, and building the option file
its two tasks read. Split out rather than written inline because the CLI half spawns git
and opens terminals, and every decision here is worth asserting without either.

**These are not boxes.** `worktree.py`'s tier lives at `<workspace>/.worktrees/`, holds
a port lease and a `COMPOSE_PROJECT_NAME`, and is reaped by a scheduled pass. This tier
is the one Claude Code's `--worktree` flag cuts: a plain git worktree inside the
checkout, gitignored, with no lease and no reaper. The location is not a preference --
it is where remote Claude sessions spawn, so anything that only understands one of the
two directories is blind to half the worktrees on the machine.

Every function here is pure and tested in `tests/test_agent_worktrees.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import picker_rows

# Relative to a checkout. Spelled with a forward slash because every comparison below is
# made on `as_posix()` output, which is what `git worktree list --porcelain` prints too.
WORKTREES_DIR = ".claude/worktrees"

# `<project>:<name>`, one token because a VS Code input resolves to one string. Both
# halves are directory names, and a colon is not legal in either on Windows.
PICK_SEP = ":"

# What joins several ticked rows into that one string. A space, matching `previewRow` and
# `brokenPrRow`: neither half can contain one.
PICK_LIST_SEP = " "

# The value a row carries when picking it should run nothing, re-exported from
# `picker_rows` rather than spelled again: `parse_pick` reads it and the row builders
# write it, and a second copy would drift the first time either moved.
NOTHING = picker_rows.NOTHING

# How many recent branches the base picker offers per checkout. The default branch is
# always the first row and does not count against it.
BASE_LIMIT = 10

# What `removal_decision` answers with.
REMOVE = "remove"  # nothing would be lost; `git worktree remove` will take it
FORCE = "force"  # something would be lost, and the operator asked for that
KEEP = "keep"  # something would be lost and nobody asked


@dataclass(frozen=True)
class Tree:
    """One worktree under a checkout's `.claude/worktrees/`, as a menu row would name it."""

    name: str  # the directory under `.claude/worktrees/`, which is also the pick's tail
    path: str
    branch: str  # "" when the worktree is on a detached HEAD
    dirty: int = 0  # `git status --porcelain` lines: tracked edits AND untracked files
    unpushed: int = 0  # commits the remote does not have

    def state(self) -> str:
        """The half of a row that says what ticking it would cost."""
        parts = []
        if self.dirty:
            parts.append(f"{self.dirty} uncommitted path(s)")
        if self.unpushed:
            parts.append(f"{self.unpushed} unpushed commit(s)")
        return ", ".join(parts) or "clean and pushed"


def parse_worktree_list(porcelain: str) -> list[tuple[str, str]]:
    """`(path, branch)` for every worktree in `git worktree list --porcelain` output.

    A detached worktree yields an empty branch rather than being dropped: it still
    occupies the directory, and a delete menu that could not see it would be a menu that
    cannot remove the one worktree somebody is most likely to have finished with.
    """
    found: list[tuple[str, str]] = []
    path, branch = "", ""
    for line in (porcelain or "").splitlines():
        if line.startswith("worktree "):
            if path:
                found.append((path, branch))
            path, branch = line[len("worktree ") :].strip(), ""
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/") :].strip()
    if path:
        found.append((path, branch))
    return found


def nested(project_dir: Path, porcelain: str) -> list[tuple[str, str, str]]:
    """`(name, path, branch)` for the worktrees under this checkout's `.claude/worktrees/`.

    Compared as lowercased posix strings rather than with `Path.resolve()`, so this stays
    pure: git prints forward slashes on Windows too, and the case fold is what makes
    `C:/Users` and `c:/users` the same directory there. Only the immediate children
    count -- a worktree cut inside another one is that one's business, not this menu's.
    """
    root = (project_dir / WORKTREES_DIR).as_posix().lower().rstrip("/") + "/"
    rows = []
    for path, branch in parse_worktree_list(porcelain):
        tail = Path(path).as_posix()
        if not tail.lower().startswith(root):
            continue
        name = tail[len(root) :].strip("/")
        if name and "/" not in name:
            rows.append((name, path, branch))
    return sorted(rows)


def removal_decision(tree: Tree, forced: bool) -> tuple[str, str]:
    """Whether this worktree may be removed, and the sentence that says why not.

    Uncommitted paths and unpushed commits are treated the same way and that is
    deliberate: both are work that exists in exactly one place, and a checkbox list is
    the worst possible surface for losing either. `git worktree remove` makes the first
    half of that judgement itself; it knows nothing about the second, so a branch with
    three unpushed commits and a clean tree is one it would remove without a word.

    `forced` is the operator saying they meant it, which is a different act from ticking
    a box -- it is a second dropdown, on a task whose rows already state what each pick
    holds.
    """
    if forced:
        return FORCE, ""
    if tree.dirty or tree.unpushed:
        return KEEP, f"{tree.name} has {tree.state()}"
    return REMOVE, ""


def pick_value(project: str, name: str) -> str:
    """The one token a ticked row resolves to."""
    return f"{project}{PICK_SEP}{name}"


def split_picks(text: str) -> list[str]:
    """The ticked tokens, in the order the extension joined them. Duplicates dropped."""
    return list(dict.fromkeys(token for token in str(text).split(PICK_LIST_SEP) if token))


def parse_pick(token: str) -> tuple[str, str] | None:
    """`<project>:<name>` as its two halves; None for the sentinel or for nonsense."""
    project, separator, name = str(token).partition(PICK_SEP)
    if not separator or not project or not name or name == NOTHING:
        return None
    return project, name


def tree_row(project: str, tree: Tree) -> str:
    """One row of the delete dropdown.

    The checkout rides in the description because the list is flat: one input runs one
    command, so the "which checkout, then which of its worktrees" pair the cached menu
    nested has nowhere to live -- and "where are my worktrees" was always a question
    about the machine rather than about one checkout.
    """
    return picker_rows.row(
        pick_value(project, tree.name),
        tree.name,
        f"{project} -- {tree.state()}",
        f"{tree.branch or 'detached HEAD'} -- {tree.path}",
    )


def base_row(project: str, ref: str, note: str) -> str:
    """One row of the base-branch dropdown.

    The value is the branch name, not `origin/` plus it: the CLI takes a branch and
    resolves which ref it means, so the same string works whether it was ticked here or
    typed.
    """
    return picker_rows.row(
        pick_value(project, ref),
        ref,
        f"{project} -- {note}",
        f"cut the new branch from origin/{ref}",
    )


def tree_rows(trees: dict[str, list[Tree]]) -> list[str]:
    """The delete dropdown's lines: every checkout's worktrees, the fullest checkout first.

    Ordered by count for the cached menu's reason, read one level down: whoever opened
    this wants to delete something, so the rows of the checkout that has several belong
    above the one that has none. Within a checkout the scan's order stands -- it is `git
    worktree list`'s, which is creation order.
    """
    listed = [
        tree_row(project, tree)
        for project in sorted(trees, key=lambda name: (-len(trees[name]), name))
        for tree in trees[project]
    ]
    return listed or [
        picker_rows.nothing_row("no worktrees", f"nothing under {WORKTREES_DIR} in any checkout")
    ]


def base_rows(bases: dict[str, list[tuple[str, str]]]) -> list[str]:
    """The base-branch dropdown's lines: every checkout's recent branches.

    Alphabetical by checkout, where `tree_rows` is by count, and the difference is the
    question: this list is read to find a *known* branch name, so a stable position is
    worth more than putting the busiest checkout on top.
    """
    listed = [
        base_row(project, ref, note)
        for project in sorted(bases)
        for ref, note in bases.get(project, ())
    ]
    return listed or [picker_rows.nothing_row("no branches", "origin could not be read")]
