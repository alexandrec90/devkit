#!/usr/bin/env python3
"""Plug a project into the registry, or unplug one, from a checkbox list.

`projects.toml` is the project **registry** — `sweep.py`, `worktree.py`,
`worktree-guard.py`, `workspace-status.py`, `preview-task.py` and the dispatcher all read
it through `project_registry`. So "is this project registered" already has exactly one
answer; what it did not have was a verb. Retiring `apt-finder` (commit `dce470e`) was
seven hand-made deletions across five arrays, which is the kind of edit that half-lands.

Three sources feed the list, because a project can be in any two of them:

| on disk | on GitHub | in the registry | what plugging it does |
| --- | --- | --- | --- |
| yes | yes | no  | registers it |
| no  | yes | no  | clones it first |
| yes | no  | no  | creates the public repo and pushes first |
| no  | yes | yes | clones it onto this PC; the registry is untouched |
| yes | yes | yes | nothing — it is already checked |

**The registry is global and the checkout is per machine.** The registry is committed,
so a project plugged in on one workstation is registered on all of them, while only
some hold its checkout. That is why the fourth row exists: on the other machines the
project is registered and absent, the list offers it as one row saying so, and
ticking that row clones it here without editing the registry at all. Two PCs holding
two different subsets of the registry is the ordinary state, not a half-applied sync.
To drop a project from one PC only, delete its checkout. Unchecking a project that
*is* on this PC retires it from the registry — on
every machine — and **touches nothing on disk**, so it stays reversible by checking
the box again. What it does cost is visibility — an unregistered project is invisible
to every sweep, and to the guard that routes an agent's edit into a box — which is why
`unplug_hazards` refuses over live boxes and unpushed commits rather than silently
orphaning them.

The write goes to devkit's committed `projects.toml`, never to the retired live
workspace file. `edit_verdict` gates where it is made from. The edit then goes onto a
task branch in a worktree of its own, with a ship intent (`hand_off_registry`), and the
static checkout's copy is put back: the fix pass runs from that checkout, and an edit
left there for someone to commit was committed on a branch in it, which the pass then
ran (a5ec492b).

A bare `python scripts/plug-projects.py` gets the terminal checkbox loop below; `--list`
prints the list, and `--picks NAMES` flips the named rows without the loop. This
script's own `logs/plug-projects.log` names the registry it ended with.

**A tick is a toggle**: `--picks` means the same thing whenever it is applied, it
matches what the terminal loop's `toggle` verb does, and naming nothing is a no-op in
both directions instead of a request to retire every project.

**By name, `--unplug` is the whole retirement and `--plug` its reverse.** A tick stays
the registry alone, as its row says, but `--unplug NAME` then runs `project_retire`: this
PC's collector stopped, its claimed tasks exported and deleted, `docker compose down`
(volumes kept), and the GitHub repo archived, which is what stops Dependabot opening PRs
for a fixer to chase. `--plug NAME` unarchives the repo before it clones, and puts back
what this PC's record says was running. On a second PC, the same flag does the local
half alone (`RETIRE_HERE`, `RESTORE_HERE`): the registry and the repo are global.

Pure helpers (`inventory`, `render`, `parse_command`, `plan`, `edit_verdict`,
`toggled_selection`, `clone_picks`, `scripted_selection`, `with_local_halves`) carry the
decisions and are unit-tested in `tests/test_plug_projects.py`; `main` is the shell
around them.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import canonical_handoff
import devkit_project
import project_registry
import project_retire
import sweep
import task_branch
import worktree

REPO_ROOT = Path(__file__).resolve().parents[1]
# The projects root, where the checkouts are cloned and plugged.
WORKSPACE_ROOT = project_registry.projects_root(REPO_ROOT)
# devkit's committed registry, the one file a plug or an unplug edits.
REGISTRY = project_registry.registry_path(REPO_ROOT)
ARTIFACT = REPO_ROOT / "logs" / "plug-projects.log"

# The GitHub account these checkouts live under. Hard-coded exactly as
# `new-project.py --github-owner` hard-codes it, and overridable the same way: `gh`
# reports the owner of every repo it lists, so this is only consulted when *creating*
# one for a folder that has no repo yet.
DEFAULT_OWNER = "alexandrec90"

# Directories under the workspace root that are not, and cannot become, a checkout.
# Everything else is judged by whether it looks like one — see `disk_projects`.
SKIP_DIRS = frozenset({"logs"})

PLUG = "plug"
UNPLUG = "unplug"
# The registry already names it; the disk does not have it. Distinct from PLUG because
# the registry half is already true, so `apply_projects` must not re-register it.
CLONE = "clone"
# This PC's half of a retire or a restore, for a project whose registry half is already
# true -- the second PC that ran it. Neither edits the registry.
RETIRE_HERE = "retire-here"
RESTORE_HERE = "restore-here"
REGISTRY_EDITS = (PLUG, UNPLUG)


class PlugError(RuntimeError):
    """A step could not be carried out. Carries the message the user should read."""


# --- the three sources ------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One project, as the three sources between them describe it."""

    name: str
    plugged: bool
    on_disk: bool
    on_github: bool
    is_git: bool = False
    harnessed: bool = False
    archived: bool = False

    @property
    def where(self) -> str:
        """Which sources hold it, for the listing's right-hand column."""
        if self.on_disk and self.on_github:
            found = "folder + repo"
        else:
            found = "folder only" if self.on_disk else "repo only"
        return found + (" (archived)" if self.archived else "")


@dataclass(frozen=True)
class Step:
    """One project's worth of work, fully described before anything runs."""

    action: str
    name: str
    clone: bool = False
    create_repo: bool = False
    init_git: bool = False
    hazards: tuple[str, ...] = ()
    # Its archived GitHub repo is unarchived first, before any clone.
    unarchive: bool = False
    # This PC's half too: `project_retire.retire` after an unplug, `restore` after a plug.
    local: bool = False


def disk_projects(root: Path) -> list[str]:
    """Directories under the workspace root that are a checkout, registered or not.

    "Looks like a checkout" is `.git` (a directory in a clone, a *file* in a worktree)
    or a `.devkit.toml`, rather than "is a directory" — the workspace root also holds
    `logs/`, the tool caches, and `.worktrees/` full of ephemeral boxes, none of which
    is a project and one of which would offer a dozen rows that reap themselves.
    """
    found = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if (entry / ".git").exists() or (entry / ".devkit.toml").is_file():
            found.append(entry.name)
    return found


def github_repos(runner) -> tuple[list[str], frozenset[str]]:
    """`(every repo gh can see for the account, the archived ones among them)`.

    Archived repos are listed rather than left out, because a retired project's repo is
    archived and its folder is still here: left out, its row would read `no repo` and
    offer to CREATE the repository it already has. `inventory` decides which to offer.

    `runner(argv)` returns a CompletedProcess — injected so the listing is testable
    without a network call. Raises `PlugError` rather than returning an empty list on
    failure: "you have no repos" and "gh is not logged in" produce the same list and
    mean opposite things, and the second one would offer to *create* every repo that
    already exists.
    """
    argv = ["gh", "repo", "list", "--limit", "500", "--json", "name,isArchived"]
    result = runner(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise PlugError(f"gh could not list repos: {detail[0] if detail else 'no output'}")
    try:
        entries = json.loads(result.stdout or "[]")
        names = sorted({entry["name"] for entry in entries})
        archived = frozenset(entry["name"] for entry in entries if entry.get("isArchived"))
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        raise PlugError(f"gh returned a repo list this cannot read: {exc}") from exc
    return names, archived


def inventory(
    registered: list[str],
    disk: list[str],
    repos: list[str],
    *,
    git_dirs: frozenset[str] = frozenset(),
    harnessed: frozenset[str] = frozenset(),
    archived: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """Merge the three sources into one ordered list of candidates.

    `registered` is the raw registry, in its own order. Registered projects keep that
    order — the array is hand-arranged and reordering it in the listing would make the
    checkbox numbers disagree with the file everyone reads. Everything else is appended
    alphabetically.

    An archived repo is offered only when it is registered or on disk: a retired
    project's checkout is kept, so its row is there to plug back, while every repo
    archived long ago and never cloned here stays out of the list.

    `NOT_PROJECTS` is subtracted, so a reference checkout cannot be offered for
    unplugging. It is registered on purpose and every consumer of the registry
    already excludes it by name; letting a checkbox retire it would break the one
    script that resolves against the raw registry (`git-merge-default.py`).
    """
    plugged = [n for n in dict.fromkeys(registered) if n not in devkit_project.NOT_PROJECTS]
    on_disk, on_github = set(disk), set(repos)
    offered = on_disk | (on_github - (set(archived) - set(plugged)))
    rest = sorted(offered - set(plugged) - devkit_project.NOT_PROJECTS, key=str.lower)

    def make(name: str, is_plugged: bool) -> Candidate:
        return Candidate(
            name=name,
            plugged=is_plugged,
            on_disk=name in on_disk,
            on_github=name in on_github,
            is_git=name in git_dirs,
            harnessed=name in harnessed,
            archived=name in archived,
        )

    return [make(n, True) for n in plugged] + [make(n, False) for n in rest]


# --- the checkbox list ------------------------------------------------------------


def render(candidates: list[Candidate], checked: set[str]) -> list[str]:
    """The checkbox list, one line per candidate, numbered from 1.

    A row whose box no longer matches the registry carries the verb that will run, so
    the pending change is legible without scrolling back to what was ticked.
    """
    if not candidates:
        return ["  (no projects found on disk, on GitHub or in the registry)"]
    width = max(len(c.name) for c in candidates)
    lines = []
    for index, candidate in enumerate(candidates, start=1):
        ticked = candidate.name in checked
        pending = ""
        if ticked != candidate.plugged:
            pending = "  <- will plug" if ticked else "  <- will unplug"
        elif ticked and needs_clone(candidate):
            pending = "  <- registered, not on this PC: will clone"
        note = "" if candidate.harnessed or not candidate.on_disk else "  (no .devkit.toml)"
        mark = "x" if ticked else " "
        lines.append(
            f"  {index:>2}. [{mark}] {candidate.name.ljust(width)}   {candidate.where}{note}{pending}"
        )
    return lines


HELP = (
    "  numbers (or names) toggle a box -- `3`, `1 4`, `apt-finder`\n"
    "  a  apply the pending changes      q  quit without changing anything\n"
    "  l  redraw the list                ?  this help"
)


def parse_command(line: str, names: list[str]) -> tuple[str, tuple[str, ...]]:
    """Read one line of input into `(verb, names to toggle)`.

    Numbers and names are both accepted because the numbers move: plugging a project
    reorders the list on the next redraw, and a name is the only stable handle.
    Anything unrecognised is `error` with the message to print — never a silent no-op,
    which in a list of checkboxes reads as "that one is not togglable".
    """
    text = line.strip()
    if not text:
        return "noop", ()
    lowered = text.lower()
    if lowered in {"q", "quit", "exit"}:
        return "quit", ()
    if lowered in {"a", "apply", "y", "yes"}:
        return "apply", ()
    if lowered in {"l", "list", "ls"}:
        return "list", ()
    if lowered in {"?", "h", "help"}:
        return "help", ()

    by_lower = {n.lower(): n for n in names}
    picked: list[str] = []
    for token in text.replace(",", " ").split():
        if token.isdigit():
            index = int(token)
            if not 1 <= index <= len(names):
                return "error", (f"there is no line {index}",)
            picked.append(names[index - 1])
        elif token.lower() in by_lower:
            picked.append(by_lower[token.lower()])
        else:
            return "error", (f"not a line number or a project name: {token}",)
    return "toggle", tuple(picked)


def interactive(
    candidates: list[Candidate],
    checked: set[str],
    ask,
    out=print,
) -> set[str] | None:
    """Run the toggle loop. Returns the final tick set, or None if the user quit.

    `ask` is `input`'s shape, injected so the loop is testable. It is also why this
    task cannot be wrapped in `log-wrap.py` — see `write_artifact`.
    """
    names = [c.name for c in candidates]
    state = set(checked)
    while True:
        for line in render(candidates, state):
            out(line)
        out("")
        try:
            verb, picked = parse_command(ask("  toggle / [a]pply / [q]uit / [?] > "), names)
        except EOFError:
            # A task terminal closed, or stdin was never a terminal. Quitting is the
            # only safe reading: an EOF is not consent to edit the registry.
            out("\n  no input -- nothing changed")
            return None
        out("")
        if verb == "quit":
            return None
        if verb == "apply":
            return state
        if verb == "help":
            out(HELP)
            out("")
        elif verb == "error":
            out(f"  {picked[0]}")
            out("")
        elif verb == "toggle":
            for name in picked:
                state.symmetric_difference_update({name})


# --- the scripted answer ---------------------------------------------------------


def parse_ticks(value: str) -> tuple[str, ...]:
    """Split `--picks`'s one-string answer, comma- or space-separated."""
    return tuple(name for name in value.replace(",", " ").split() if name)


def toggled_selection(picked: tuple[str, ...], plugged: set[str]) -> set[str]:
    """The registry the picks ask for: the live one, with every ticked row flipped.

    A symmetric difference, and it needs no `offered` list and no guard against a row
    whose premise moved: a toggle is relative, so the worst a raced registry edit can do
    is flip a row the person did mean to flip -- not a silent retirement of a project
    nobody mentioned.

    Names the scan did not offer are dropped by `main` before this is reached. Nor can a clone pick: `clone_picks` takes those out first, because
    flipping a registered-elsewhere row would read as its retirement.
    """
    return set(plugged).symmetric_difference(picked)


def scripted_selection(
    candidates: list[Candidate],
    *,
    ticks: tuple[str, ...] | None,
    plug: list[str],
    unplug: list[str],
) -> tuple[set[str], set[str]]:
    """`(registry the run asks for, projects to clone here)` for the two scripted entries.

    The `--picks` answer (`ticks`) and the `--plug`/`--unplug` flags read "clone it
    here" differently, which is why `plan` takes the clone set explicitly. A tick on a
    registered-elsewhere row is a clone and not a toggle, so it is taken out before the
    toggles are read; `--plug` of such a project is a clone; and `--unplug` of one says
    nothing about the others, where it used to clone every one of them as a side effect.
    """
    checked = {c.name for c in candidates if c.plugged}
    if ticks is not None:
        clone_here = clone_picks(ticks, candidates)
        rest = tuple(name for name in ticks if name not in clone_here)
        return toggled_selection(rest, checked), clone_here
    return (checked | set(plug)) - set(unplug), set(plug)


def after_apply_notes(steps: list[Step], handed: bool = False) -> list[str]:
    """What still needs a human once the steps have run; empty for a clone-only run.

    A clone edits nothing in the registry -- the project was registered already, from
    another workstation -- so there is nothing to commit and the note would send
    someone to ship a `projects.toml` that has not changed. Nor does an edit `_apply`
    handed off onto a task branch: the fix pass ships that one. The note that remains is
    for a cut that failed, and says where the branch goes, since "commit it on a task
    branch" in the static checkout is how the pass came to run one (a5ec492b).
    """
    notes = []
    if any(s.action in REGISTRY_EDITS for s in steps) and not handed:
        name = REGISTRY.name
        notes.append(f"  ship {name} from a worktree, never a branch in devkit's own checkout:")
        notes.append("  the fix pass runs from that checkout, on whatever branch it is on.")
    if any(s.action == PLUG for s in steps):
        notes.append("  a project with a stack also needs a slot in ports.toml; add it by hand.")
    return notes


def clone_picks(picked: tuple[str, ...], candidates: list[Candidate]) -> set[str]:
    """The ticks that mean "clone it onto this PC" rather than "flip its registration".

    A registered project with no checkout here is the one row where a toggle is the
    wrong reading: the registry already has it, so flipping it would retire it on every
    workstation, when the person ticking it on *this* one almost always wants the
    checkout. Its tick is therefore a clone, and the registry is left alone.
    """
    by_name = {c.name: c for c in candidates}
    return {name for name in picked if name in by_name and needs_clone(by_name[name])}


# --- deciding what to do ----------------------------------------------------------


def plan(
    candidates: list[Candidate],
    checked: set[str],
    hazards: dict[str, tuple[str, ...]] | None = None,
    clone: set[str] | frozenset[str] = frozenset(),
) -> list[Step]:
    """Every ticked-but-unregistered and registered-but-unticked project, as work.

    Unplugs come first. Both halves are one registry edit each, so the
    order only decides what the preview reads like — and a list that retires before it
    adds matches how the numbers in the list will move.

    `clone` names the registered-elsewhere projects to clone onto this PC. It is
    explicit rather than "every checked row that `needs_clone`" because every caller
    means something different by an untouched row: `--picks` names only
    what was ticked, `--unplug` names nothing about the rest, and only the terminal
    loop's `apply` is a statement about every box it drew.
    """
    found = hazards or {}
    steps = []
    for candidate in candidates:
        ticked = candidate.name in checked
        if ticked == candidate.plugged:
            continue
        if not ticked:
            steps.append(Step(UNPLUG, candidate.name, hazards=tuple(found.get(candidate.name, ()))))
    for candidate in candidates:
        if candidate.name in checked and not candidate.plugged:
            steps.append(
                Step(
                    PLUG,
                    candidate.name,
                    clone=not candidate.on_disk,
                    create_repo=not candidate.on_github,
                    init_git=candidate.on_disk and not candidate.is_git,
                    unarchive=candidate.archived,
                )
            )
    # A registered project that is not on disk is the fresh-workstation case, and the
    # other-workstation case -- and it is the one row where the tick and the registry
    # already agree, so both loops above skip it and no verb offered the clone. Four
    # projects were registered, none was on disk, and the answer was a hand-written
    # `gh repo clone` loop. This keeps one verb for "make the registry true here": the
    # registry half is already done, so the step carries only the disk half.
    steps.extend(
        Step(CLONE, c.name, clone=True, unarchive=c.archived)
        for c in candidates
        if c.name in checked and c.name in clone and needs_clone(c)
    )
    return steps


def with_local_halves(
    steps: list[Step],
    candidates: list[Candidate],
    *,
    plug: list[str],
    unplug: list[str],
    retired: frozenset[str] = frozenset(),
) -> list[Step]:
    """`steps`, with this PC's half of a retire or a restore added for every project
    named by `--plug` or `--unplug`.

    Only the flags reach it. A tick is a toggle of the registry alone, with nothing on
    disk touched, so `--picks` and the terminal loop pass no names and are left exactly
    as they were.

    A named project whose registry half is already true still gets its local half: the
    second PC that ran a retired project unplugs it to stop it there, and a project
    plugged back from another PC is restored here when this PC has a record of retiring
    it (`retired`) or its repo is still archived.
    """
    named = set(plug) | set(unplug)
    if not named:
        return steps

    def local(step: Step) -> bool:
        if step.action == UNPLUG:
            return step.name in named
        return step.name in named and (step.unarchive or step.name in retired)

    out = [replace(s, local=True) if local(s) else s for s in steps]
    acted = {s.name for s in steps}
    idle = [c for c in candidates if c.name not in acted and c.on_disk]
    return out + local_only_halves(idle, plug=plug, unplug=unplug, retired=retired)


def local_only_halves(
    idle: list[Candidate], *, plug: list[str], unplug: list[str], retired: frozenset[str]
) -> list[Step]:
    """The retire or restore this PC owes a named project on disk whose registry half
    is already true, so no step in the plan acts on it (`with_local_halves`)."""
    by_name = {c.name: c for c in idle}
    out = [
        Step(RETIRE_HERE, name, local=True)
        for name in dict.fromkeys(unplug)
        if name in by_name and not by_name[name].plugged
    ]
    for name in dict.fromkeys(plug):
        found = by_name.get(name)
        if found and found.plugged and (found.archived or name in retired):
            out.append(Step(RESTORE_HERE, name, unarchive=found.archived, local=True))
    return out


def needs_clone(candidate: Candidate) -> bool:
    """Is this registered project missing from disk, with a repo to clone it from?

    Both halves matter. Without `on_github` there is nothing to clone *from*, and
    emitting the step anyway would turn a fresh workstation's first run into a failed
    `gh repo clone` -- a registry entry naming a repo that no longer exists is a
    different problem, and one an unplug fixes rather than a clone.
    """
    return candidate.plugged and not candidate.on_disk and candidate.on_github


def describe(step: Step) -> str:
    """One line saying what a step will do, for the confirmation preview."""
    if step.action in (RETIRE_HERE, RESTORE_HERE, CLONE):
        return _describe_registered(step)
    if step.action == UNPLUG:
        tail = f"  ({'; '.join(step.hazards)})" if step.hazards else ""
        what = (
            f"registry, then {RETIRING}"
            if step.local
            else "registry only, nothing on disk is touched"
        )
        return f"  unplug  {step.name}   {what}{tail}"
    extra = []
    if step.unarchive:
        extra.append("unarchive the GitHub repo")
    if step.clone:
        extra.append("clone from GitHub")
    if step.init_git:
        extra.append("git init + initial commit")
    if step.create_repo:
        extra.append("create the PUBLIC GitHub repo and push")
    if step.local:
        extra.append(RESTORING)
    detail = ", ".join(extra) if extra else "registry only"
    return f"  plug    {step.name}   {detail}"


RETIRING = "stop its collector, tasks and containers here (volumes kept), archive its GitHub repo"
RESTORING = "put back what this PC retired"


def _describe_registered(step: Step) -> str:
    """`describe` for the steps whose registry half is already true."""
    if step.action == RETIRE_HERE:
        return f"  retire  {step.name}   not in the registry; {RETIRING}"
    undo = "unarchive its GitHub repo, " if step.unarchive else ""
    if step.action == RESTORE_HERE:
        return f"  restore {step.name}   already registered; {undo}{RESTORING}"
    back = f", {RESTORING}" if step.local else ""
    return (
        f"  clone   {step.name}   registered elsewhere, {undo}clone from GitHub onto this PC{back}"
    )


def in_ephemeral_box(root: Path) -> bool:
    """Is `root` one of the workspace's disposable boxes rather than a static checkout?

    Keyed off `worktree.BOXES_DIR_NAME` rather than the literal, so the two cannot
    drift — the same reason `tests/support.py` does it that way.
    """
    return root.parent.name == worktree.BOXES_DIR_NAME


def edit_verdict(*, branch: str, default: str, in_box: bool) -> str:
    """Why the registry must not be edited from here; "" when it may.

    Pure, so both are testable without a git tree. The registry edit is cut onto a task
    branch off the static checkout's default branch (`hand_off_registry`), so a box's
    copy, or a task branch's, is the wrong base: it carries an unmerged proposal the
    registry branch would inherit.
    """
    if in_box:
        return (
            "this is an ephemeral box, so its registry is an unmerged proposal -- "
            "run this from the devkit checkout"
        )
    if branch != default:
        return f"devkit is on {branch}, not {default} -- the registry edit would carry an unmerged proposal"
    return ""


def unplug_hazards(name: str, root: Path, git) -> tuple[str, ...]:
    """What unplugging `name` would make invisible. Empty when it costs nothing.

    Unplugging deletes nothing, so neither of these is about losing files — it is about
    losing the *readers*. An unregistered project is outside `known_projects`, so
    `sweep.py` stops reporting its stranded work, `worktree-guard.py` stops routing an
    agent's edit into a box, and `worktree.py reconcile` stops reaping the boxes it
    already has: their port slots and volume sets leak with nothing left to notice.

    `git(*args)` is bound to the checkout, `sweep.git_for`'s shape.
    """
    found = []
    boxes_dir = root / worktree.BOXES_DIR_NAME
    boxes = (
        [b for b in sorted(boxes_dir.iterdir()) if worktree.project_of(b.name) == name]
        if boxes_dir.is_dir()
        else []
    )
    if boxes:
        found.append(f"{len(boxes)} live box(es) reconcile would stop reaping")
    if not (root / name / ".git").exists():
        return tuple(found)
    dirty = git("status", "--porcelain")
    if dirty.returncode == 0 and dirty.stdout.strip():
        found.append(f"{len(dirty.stdout.strip().splitlines())} uncommitted path(s)")
    ahead = git("log", "--oneline", "@{u}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip():
        found.append(f"{len(ahead.stdout.strip().splitlines())} unpushed commit(s)")
    return tuple(found)


# --- carrying it out --------------------------------------------------------------


def scripted_env() -> dict[str, str]:
    """The ambient environment with devkit's branch policy waived.

    Creating a repo for an existing folder pushes its default branch, which is the
    "scripted repo setup" case that hatch exists for — `new-project.py` waives it for
    the same `gh repo create --push` and says why.
    """
    return {**os.environ, "DEVKIT_SKIP_BRANCH_POLICY": "1"}


def clone_repo(owner: str, name: str, root: Path, runner) -> None:
    """Clone `owner/name` into the workspace root. Raises `PlugError` on failure."""
    result = runner(["gh", "repo", "clone", f"{owner}/{name}", str(root / name)])
    if result.returncode != 0:
        raise PlugError(f"could not clone {owner}/{name}: {(result.stderr or '').strip()}")


def create_repo(owner: str, name: str, path: Path, runner, *, init: bool) -> None:
    """Create the public GitHub repo for an existing folder and push it
    (`devkit_project.CREATE_VISIBILITY`).

    `gh repo create --source` needs a commit to push, so a folder that is not a repo at
    all — or is one with nothing committed — gets `git init` and one commit first. This
    is the only outward-facing step in the script, which is why it is never reached
    without the confirmation in `main`.
    """
    if init and runner(["git", "init", "-b", "main"], cwd=path).returncode != 0:
        raise PlugError(f"could not initialise a git repo in {path}")
    if runner(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=path).returncode != 0:
        if runner(["git", "add", "-A"], cwd=path).returncode != 0:
            raise PlugError(f"could not stage {path} for its first commit")
        if runner(["git", "commit", "-m", "Initial commit"], cwd=path).returncode != 0:
            raise PlugError(f"could not make the first commit in {path}")
    argv = [
        "gh",
        "repo",
        "create",
        f"{owner}/{name}",
        devkit_project.CREATE_VISIBILITY,
        "--source=.",
        "--remote=origin",
        "--push",
    ]
    if runner(argv, cwd=path).returncode != 0:
        raise PlugError(f"could not create {owner}/{name} on GitHub")


def apply_projects(text: str, steps: list[Step]) -> str:
    """Apply every step's registry half to `projects.toml` text, retirements first.

    Raises `project_registry.RegistryError` on an edit that did not take: a half-applied
    registry is one every reader sees as wrong.
    """
    retiring = [s.name for s in steps if s.action == UNPLUG]
    adding = [s.name for s in steps if s.action == PLUG]
    text = project_registry.unregister_text(text, *retiring)
    return project_registry.register_text(text, *adding, before=devkit_project.NOT_PROJECTS)


def write_registry(registry: Path, steps: list[Step]) -> bool:
    """Write `steps`' registry half to `registry` (`projects.toml`); whether it changed.

    A missing or unreadable file is a `PlugError`, never a silent skip: `projects.toml`
    is the whole registry, and a file created here holding one name would read as a
    registry of one.
    """
    try:
        before = registry.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlugError(f"cannot read {registry}: {exc}") from exc
    try:
        after = apply_projects(before, steps)
    except project_registry.RegistryError as exc:
        raise PlugError(f"{exc} -- nothing was written") from exc
    if after == before:
        return False
    registry.write_text(after, encoding="utf-8", newline="\n")
    return True


def write_artifact(problems: list[str]) -> None:
    """Persist this run's failures under `logs/`, per `.claude/rules/engineering.md`.

    The script writes its own artifact instead of being wrapped in `log-wrap.py`, which
    is the ordinary way. That wrapper pipes the child's stdout and reads it a line at a
    time, so a prompt that has not ended its line never reaches the terminal and the
    checkbox loop below would hang waiting for input nobody can see.

    Emptied on a clean run, the way `log-wrap.py` empties its own log, so it never
    describes a failure already fixed.
    """
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(problems)
    ARTIFACT.write_text(body + "\n" if body else "", encoding="utf-8", newline="\n")


# --- the shell around it ----------------------------------------------------------


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=scripted_env(),
        creationflags=sweep.NO_WINDOW,
    )


def gather() -> tuple[list[Candidate], list[str]]:
    """Read all three sources. Returns the candidates and any warnings to print.

    A GitHub listing that fails is a warning rather than a failure: the folder half of
    the list is still true and still togglable, and the only thing lost is the ability
    to clone or to create — both of which the plan names explicitly, so a step that
    needed the missing half cannot be reached by accident.

    The registry is devkit's committed `projects.toml`, which holds a project registered
    on another PC -- exactly the row this list has to draw as "registered, not on this
    PC". A malformed one is an `OSError` for `main` to report: guessing would offer to
    plug every registered project back in.
    """
    try:
        registry = project_registry.parse_registry(REGISTRY.read_text(encoding="utf-8"))
    except project_registry.RegistryError as exc:
        raise OSError(str(exc)) from exc
    disk = disk_projects(WORKSPACE_ROOT)
    warnings: list[str] = []
    archived: frozenset[str] = frozenset()
    try:
        repos, archived = github_repos(_run)
    except PlugError as exc:
        warnings.append(f"{exc} -- listing folders only, so nothing can be cloned this run")
        repos = []
    git_dirs = frozenset(n for n in disk if (WORKSPACE_ROOT / n / ".git").exists())
    harnessed = frozenset(n for n in disk if (WORKSPACE_ROOT / n / ".devkit.toml").is_file())
    found = inventory(
        registry, disk, repos, git_dirs=git_dirs, harnessed=harnessed, archived=archived
    )
    return found, warnings


REGISTRY_INTENT = """{subject}

`scripts/plug-projects.py` {what} on a confirmed pick. This is devkit's registry,
`projects.toml`, which every machine shares.

The script cut this branch itself. It used to leave the edit uncommitted in devkit's
static checkout and say to commit it on a task branch, and a session did, in that
checkout: the fix pass runs from it, so the pass ran the task branch's code and filed
`pass-stale` (a5ec492b).

It merges once green, as every unheld PR does: the pick was the decision.
"""


def registry_change(steps: list[Step]) -> tuple[str, str]:
    """`(slug, intent)` for the registry edit `steps` make; ("", "") for a clone-only run."""
    plugged = sorted(s.name for s in steps if s.action == PLUG)
    unplugged = sorted(s.name for s in steps if s.action == UNPLUG)
    verbs = [(PLUG, plugged), (UNPLUG, unplugged)]
    parts = [f"{verb} {', '.join(names)}" for verb, names in verbs if names]
    if not parts:
        return "", ""
    subject = " and ".join(parts).capitalize() + " in the project registry"
    what = " and ".join(f"{verb}ged {', '.join(names)}" for verb, names in verbs if names)
    slug = "-".join(f"{verb}-{'-'.join(names)}" for verb, names in verbs if names)
    return slug, REGISTRY_INTENT.format(subject=subject, what=what)


def hand_off_registry(steps: list[Step], registry: Path, runner=subprocess.run) -> str:
    """Put the registry edit on a task branch off the static checkout: the line to print,
    or "" when nothing was handed off and the person still has to commit it."""
    slug, intent = registry_change(steps)
    if not slug:
        return ""
    cut = canonical_handoff.hand_off(registry, slug, intent, runner)
    if cut.said:
        return f"  WARNING {cut.said}"
    if cut.tree is None:
        return ""
    return (
        f"  branch  {cut.branch}: {registry.name}, for the fix pass to ship and merge once "
        f"green\n          {cut.tree}"
    )


def _apply(steps: list[Step], owner: str, out) -> bool:
    """Run every step's disk half, then write the registry; whether its edit was handed
    off onto a task branch (`hand_off_registry`). This PC's half of a retire or a
    restore runs last (`local_halves`), once the registry edit is safe."""
    for step in steps:
        if step.unarchive:
            out(f"  unarchive {owner}/{step.name}")
            unarchive_repo(owner, step.name, _run)
        if step.clone:
            out(f"  clone   {owner}/{step.name}")
            clone_repo(owner, step.name, WORKSPACE_ROOT, _run)
        if step.create_repo:
            out(f"  create  {owner}/{step.name} (public)")
            create_repo(owner, step.name, WORKSPACE_ROOT / step.name, _run, init=step.init_git)

    # A clone-only run edits nothing in the registry: the project was registered already.
    registry = REGISTRY
    edits = any(s.action in REGISTRY_EDITS for s in steps)
    # Asked before the write, so an edit already sitting there is never carried off onto
    # a branch under this pick's name.
    clean_before = edits and not canonical_handoff.dirty(registry)
    if edits and write_registry(registry, steps):
        out(f"  update  {registry.name} (projects)")

    handed = hand_off_registry(steps, registry) if clean_before else ""
    if handed:
        out(handed)
    local_halves(steps, owner, out)
    return handed.startswith("  branch")


def unarchive_repo(owner: str, name: str, runner) -> None:
    """Make an archived repo writable again, before anything clones or registers it."""
    result = runner(list(project_retire.archive_argv(f"{owner}/{name}", undo=True)))
    if result.returncode != 0:
        raise PlugError(f"could not unarchive {owner}/{name}: {(result.stderr or '').strip()}")


def local_halves(steps: list[Step], owner: str, out, run=sweep.run_bounded) -> None:
    """Run `project_retire` for every step that carries this PC's half.

    After the registry edit, because that is where the refusals are: a live box or
    unpushed work stops the unplug before anything here has been stopped. A failure
    raises with the step named; running the same command again skips what is done."""
    for step in steps:
        if not step.local:
            continue
        checkout = WORKSPACE_ROOT / step.name
        if step.action in (UNPLUG, RETIRE_HERE):
            out(f"  retire  {step.name} on this PC")
            failed = project_retire.retire(step.name, checkout, owner, run, out)
        else:
            out(f"  restore {step.name} on this PC")
            failed = project_retire.restore(step.name, checkout, run, out)
        if failed:
            raise PlugError(f"{step.name}: {failed} -- fix that and run the same command again")


def named_halves(
    steps: list[Step], candidates: list[Candidate], args: argparse.Namespace
) -> list[Step]:
    """`with_local_halves` for this run's flags; a pick names nothing, so adds nothing."""
    if args.picks is not None:
        return steps
    retired = frozenset(n for n in args.plug if project_retire.is_retired(n))
    return with_local_halves(steps, candidates, plug=args.plug, unplug=args.unplug, retired=retired)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true", help="print the checkbox list and exit")
    parser.add_argument("--json", action="store_true", help="with --list, emit JSON")
    parser.add_argument(
        "--picks",
        metavar="NAMES",
        help="names (comma- or space-separated) whose registry state is flipped",
    )
    parser.add_argument("--plug", action="append", default=[], metavar="NAME")
    parser.add_argument("--unplug", action="append", default=[], metavar="NAME")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    parser.add_argument(
        "--force",
        action="store_true",
        help="unplug a project even when it has live boxes or unpushed work",
    )
    parser.add_argument(
        "--owner", default=DEFAULT_OWNER, help="GitHub owner for a repo this creates"
    )
    args = parser.parse_args(argv)

    try:
        candidates, warnings = gather()
    except OSError as exc:
        write_artifact([f"could not read the registry: {exc}"])
        print(f"ERROR   could not read the registry: {exc}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"  NOTE    {warning}")

    if args.list:
        if args.json:
            print(json.dumps([asdict(c) | {"where": c.where} for c in candidates], indent=2))
        else:
            print("")
            for line in render(candidates, {c.name for c in candidates if c.plugged}):
                print(line)
            print("")
        write_artifact([])
        return 0

    known = {c.name for c in candidates}
    unknown = sorted(set(args.plug + args.unplug) - known)
    if unknown:
        print(
            f"ERROR   not on disk, on GitHub or in the registry: {', '.join(unknown)}",
            file=sys.stderr,
        )
        write_artifact([f"unknown project(s): {', '.join(unknown)}"])
        return 2

    # Resolved before the gate below, so that naming nothing from a box costs a line
    # rather than an error about branches: nothing is being edited and neither of the
    # gate's reasons is about to be true.
    ticks: tuple[str, ...] = ()
    if args.picks is not None:
        picked = parse_ticks(args.picks)
        # A tick is a toggle, so no ticks is simply no change.
        if not picked:
            print("  nothing was picked -- nothing changed")
            write_artifact([])
            return 0
        # A name that is not a candidate is dropped with a note rather than failing the
        # run, because the *other* ticks are still true.
        stale = sorted(set(picked) - known)
        if stale:
            print(f"  NOTE    no longer on disk, on GitHub or in the registry: {', '.join(stale)}")
        ticks = tuple(name for name in picked if name in known)
        if not ticks:
            print("  nothing left to act on -- nothing changed")
            write_artifact([])
            return 0

    git = sweep.git_for(REPO_ROOT)
    # Asked before the list is drawn, not after it is ticked: every reason it refuses is
    # true of the checkout rather than of the choice, so making someone tick twelve boxes
    # first would only lose the ticking.
    verdict = edit_verdict(
        branch=(git("rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip(),
        default=task_branch.detect_default_branch(git),
        in_box=in_ephemeral_box(REPO_ROOT),
    )
    if verdict:
        print(f"ERROR   {verdict}", file=sys.stderr)
        print("        `--list` still works from here.", file=sys.stderr)
        write_artifact([verdict])
        return 1

    if args.picks is not None or args.plug or args.unplug:
        checked, clone_here = scripted_selection(
            candidates,
            ticks=ticks if args.picks is not None else None,
            plug=args.plug,
            unplug=args.unplug,
        )
    else:
        print("")
        chosen = interactive(candidates, {c.name for c in candidates if c.plugged}, input)
        if chosen is None:
            print("  nothing changed")
            write_artifact([])
            return 0
        checked, clone_here = chosen, set(chosen)

    hazards = {
        c.name: unplug_hazards(c.name, WORKSPACE_ROOT, sweep.git_for(WORKSPACE_ROOT / c.name))
        for c in candidates
        if c.plugged and c.name not in checked
    }
    steps = named_halves(plan(candidates, checked, hazards, clone=clone_here), candidates, args)
    if not steps:
        print("  nothing to change")
        write_artifact([])
        return 0

    print("  this run will:")
    for step in steps:
        print(describe(step))
    print("")

    blocked = [s for s in steps if s.hazards] if not args.force else []
    if blocked:
        problems = [f"{s.name}: {'; '.join(s.hazards)}" for s in blocked]
        for line in problems:
            print(f"ERROR   {line}", file=sys.stderr)
        print("        ship or reap that work first, or re-run with --force", file=sys.stderr)
        write_artifact(problems)
        return 1

    if not args.yes:
        try:
            if input("  apply? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("  nothing changed")
                write_artifact([])
                return 0
        except EOFError:
            print("\n  no input -- nothing changed")
            write_artifact([])
            return 0

    try:
        handed = _apply(steps, args.owner, print)
    except (PlugError, project_registry.RegistryError, OSError) as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        write_artifact([str(exc)])
        return 1

    print("")
    for line in after_apply_notes(steps, handed):
        print(line)
    write_artifact([])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
