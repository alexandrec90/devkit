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

**`fix-prs.py` reads from here too, and is not a fourth menu.** It cuts a worktree in
this tier for the PR it was sent at, which is `holder`, `tree_name` and `add_steps` --
where a worktree for a branch goes, and what git is asked to do when the branch already
exists. A private copy of those three in that module would be a second answer to a
question the machine may only have one answer to.

Every function here is pure and tested in `tests/test_agent_worktrees.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import picker_rows
import picker_scan

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


def worktrees_root(project_dir: Path) -> str:
    """`<checkout>/.claude/worktrees/`, in the spelling every comparison here uses.

    Lowercased posix with a trailing slash, rather than `Path.resolve()`, so the callers
    stay pure: git prints forward slashes on Windows too, and the case fold is what makes
    `C:/Users` and `c:/users` the same directory there.
    """
    return (project_dir / WORKTREES_DIR).as_posix().lower().rstrip("/") + "/"


def nested(project_dir: Path, porcelain: str) -> list[tuple[str, str, str]]:
    """`(name, path, branch)` for the worktrees under this checkout's `.claude/worktrees/`.

    Only the immediate children count -- a worktree cut inside another one is that one's
    business, not this menu's.
    """
    root = worktrees_root(project_dir)
    rows = []
    for path, branch in parse_worktree_list(porcelain):
        tail = Path(path).as_posix()
        if not tail.lower().startswith(root):
            continue
        name = tail[len(root) :].strip("/")
        if name and "/" not in name:
            rows.append((name, path, branch))
    return sorted(rows)


def holder(project_dir: Path, porcelain: str, branch: str) -> tuple[str, bool]:
    """`(path, nested)` for the worktree already on `branch`; `("", False)` when none is.

    Git will not check one branch out in two worktrees, so "what holds it" has at most
    one answer and this is the whole of it. The bool separates the two ways a branch can
    be taken, because they need opposite responses: one of this checkout's own
    `.claude/worktrees/` is the worktree the caller was about to cut and should be reused
    instead, while anywhere else -- the checkout itself, a `.worktrees/` box, something
    cut by hand -- belongs to somebody, and `git worktree add` would refuse it with a
    message about a branch rather than about the tree that is the actual obstacle.
    """
    root = worktrees_root(project_dir)
    for path, on in parse_worktree_list(porcelain):
        if on and on == branch:
            return path, Path(path).as_posix().lower().startswith(root)
    return "", False


def tree_name(branch: str, taken: Iterable[str]) -> str:
    """The directory under `.claude/worktrees/` a worktree for `branch` is cut at.

    The branch's last segment, which is `create`'s spelling (`agent/voicemail-0905` ->
    `voicemail-0905`) generalised to a name nobody here chose: a PR head branch is
    written by whoever opened the PR, and may carry no slash at all, several, or
    characters a directory name cannot hold. So anything outside `[A-Za-z0-9._-]`
    becomes a hyphen, and a name already on disk takes a counter -- two PRs whose heads
    end in the same segment want two worktrees, and the second one silently landing in
    the first one's directory is the failure this exists to prevent.

    The name is a label, never an identity: what `holder` matches on is the branch, so a
    worktree cut by `create` or by `claude --worktree` under some other name is still
    found and reused.
    """
    segment = str(branch).rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", segment).strip("-.") or "worktree"
    used = {str(name).lower() for name in taken}
    if cleaned.lower() not in used:
        return cleaned
    counter = 2
    while f"{cleaned}-{counter}".lower() in used:
        counter += 1
    return f"{cleaned}-{counter}"


def add_steps(branch: str, path: str, local: bool) -> tuple[str, ...]:
    """The `git worktree add` argv for a worktree on a branch that already exists.

    `worktree.resume_plan`'s pair, copied deliberately and for its reasons. A branch this
    checkout already has is checked out as it stands: it may carry commits no remote has,
    which is exactly what a box reaped while its work was open leaves behind, and
    re-creating it from `origin` is the one move that discards them. Only a branch never
    seen here is cut from `origin/<branch>` -- with `--track`, where `create` is emphatic
    about `--no-track`, and read the other way round for the same reason: the upstream is
    the branch's own remote, which is precisely where a bare push should land.
    """
    if local:
        return ("worktree", "add", path, branch)
    return ("worktree", "add", "--track", "-b", branch, path, f"origin/{branch}")


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


def tree_entries(trees: dict[str, list[Tree]]) -> list[tuple[str, str]]:
    """The delete dropdown's lines as `(checkout, row)`, the fullest checkout first.

    Ordered by count for the cached menu's reason, read one level down: whoever opened
    this wants to delete something, so the rows of the checkout that has several belong
    above the one that has none. Within a checkout the scan's order stands -- it is `git
    worktree list`'s, which is creation order.

    Named apart from `tree_rows` because two callers need the SAME order for different
    shapes: the dropdown prints it, and the checkout stage records it for stage two to
    filter. A stage two filtering a differently-ordered list would draw the right
    worktrees in the wrong order, which nobody would report.
    """
    return [
        (project, tree_row(project, tree))
        for project in sorted(trees, key=lambda name: (-len(trees[name]), name))
        for tree in trees[project]
    ]


def tree_rows(trees: dict[str, list[Tree]]) -> list[str]:
    """The delete dropdown's lines, or the sentinel when there is nothing to delete."""
    listed = [line for _project, line in tree_entries(trees)]
    return listed or [
        picker_rows.nothing_row("no worktrees", f"nothing under {WORKTREES_DIR} in any checkout")
    ]


def base_entries(bases: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """The base-branch dropdown's lines as `(checkout, row)`.

    Alphabetical by checkout, where `tree_entries` is by count, and the difference is
    the question: this list is read to find a *known* branch name, so a stable position
    is worth more than putting the busiest checkout on top. Split from `base_rows` for
    `tree_entries`' reason -- the checkout stage records this order and stage two
    filters it.
    """
    return [
        (project, base_row(project, ref, note))
        for project in sorted(bases)
        for ref, note in bases.get(project, ())
    ]


def base_rows(bases: dict[str, list[tuple[str, str]]]) -> list[str]:
    """The base-branch dropdown's lines, or the sentinel when origin could not be read."""
    listed = [line for _project, line in base_entries(bases)]
    return listed or [picker_rows.nothing_row("no branches", "origin could not be read")]


def empty_rows(half: str) -> list[str]:
    """The sentinel row for a half whose ticked checkouts turned out to hold nothing.

    Through the two row builders rather than spelled here, because an empty quick-pick
    cannot be told apart from a command that failed to run, and each half words that
    differently -- "no worktrees" against "origin could not be read".
    """
    return tree_rows({}) if half == "trees" else base_rows({})


def tree_project_rows(trees: dict[str, list[Tree]], token: str) -> list[str]:
    """The delete task's checkout stage: one row per checkout, and what it holds.

    A checkout with no worktrees is listed and says so, rather than dropped. The menu
    has to be readable as an answer to "where are my worktrees", and one that silently
    omits the empty checkouts cannot be told apart from one that could not reach them.
    """
    if not trees:
        return [
            picker_rows.nothing_row(
                "no checkouts", "the workspace registry named nothing that could be scanned"
            )
        ]
    listed = []
    for project in sorted(trees, key=lambda name: (-len(trees[name]), name)):
        count = len(trees[project])
        listed.append(
            picker_scan.project_row(
                project,
                token,
                f"{count} worktree{'' if count == 1 else 's'}" if count else "no worktrees",
                "tick as many checkouts as you want -- the next list covers all of them",
            )
        )
    return listed


def base_project_rows(bases: dict[str, list[tuple[str, str]]], token: str) -> list[str]:
    """The new-worktree task's checkout stage: one row per checkout, and its branch count.

    Every checkout contributes at least its default branch, so a count of zero here
    means `recent_bases` could not read that checkout's origin refs at all -- which is
    worth saying in the row rather than leaving as an unexplained short list one stage
    later.
    """
    if not bases:
        return [
            picker_rows.nothing_row(
                "no checkouts", "the workspace registry named nothing that could be scanned"
            )
        ]
    listed = []
    for project in sorted(bases):
        count = len(bases.get(project, ()))
        listed.append(
            picker_scan.project_row(
                project,
                token,
                f"{count} branch{'' if count == 1 else 'es'} to cut from"
                if count
                else "origin could not be read",
                "one checkout or several -- the next list covers all of them",
            )
        )
    return listed
