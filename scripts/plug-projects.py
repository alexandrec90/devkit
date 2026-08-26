#!/usr/bin/env python3
"""Plug a project into the workspace, or unplug one, from a checkbox list.

The `folders` array in the workspace file is the project **registry** — `sweep.py`,
`worktree.py`, `worktree-guard.py`, `workspace-status.py`, `preview-task.py` and the
task dispatcher all read it through `devkit_project.known_projects`, and every
`register()`-maintained picker mirrors it. So "is this project part of the workspace"
already has exactly one answer; what it did not have was a verb. Retiring `apt-finder`
(commit `dce470e`) was seven hand-made deletions across five arrays, which is the kind
of edit that half-lands and leaves `sweep.parse_workspace` reporting no checkouts at
all.

Three sources feed the list, because a project can be in any two of them:

| on disk | on GitHub | in the registry | what plugging it does |
| --- | --- | --- | --- |
| yes | yes | no  | registers it |
| no  | yes | no  | clones it first |
| yes | no  | no  | creates the private repo and pushes first |
| yes | yes | yes | nothing — it is already checked |

Unchecking is the inverse and **touches nothing on disk**: unplugging is a registry
edit, so it stays reversible by checking the box again. What it does cost is
visibility — an unregistered project is invisible to every sweep, and to the guard that
routes an agent's edit into a box — which is why `unplug_hazards` refuses over live
boxes and unpushed commits rather than silently orphaning them.

The write goes to devkit's **canonical** `workspace.jsonc` and is then rendered over
the live file by `publish_workspace`, per `.claude/rules/vscode-tasks.md`: the live file
has no branch dimension, so a hand edit there is globally live before anyone reviews it.
`edit_verdict` is the gate on that, and the canonical edit is deliberately left
uncommitted for a task branch — the same shape `--adopt-workspace` has.

**The checkboxes live in VS Code, not in this terminal.** `--refresh-menu` writes
`logs/plug-menu.json`, the `plugSelection` input draws it as a `multiPick` quick-pick
with the registry's own rows pre-ticked, and `--ticked` reads the answer back. The
terminal loop below is what a bare `python scripts/plug-projects.py` still gets, and the
task never reaches it. The task stays unwrapped by `log-wrap.py` all the same: this
script writes `logs/plug-projects.log` itself, and that artifact names the registry it
ended with rather than transcribing what scrolled past.

Pure helpers (`inventory`, `render`, `parse_command`, `plan`, `edit_verdict`,
`menu_payload`, `selection_from_ticks`) carry the decisions and are unit-tested in
`tests/test_plug_projects.py`; `main` is the subprocess-and-prompt shell around them.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import devkit_project
import sweep
import task_branch
import worktree

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_WORKSPACE = devkit_project.DEFAULT_WORKSPACE
WORKSPACE_ROOT = LIVE_WORKSPACE.parent
ARTIFACT = REPO_ROOT / "logs" / "plug-projects.log"
MENU_CACHE = REPO_ROOT / "logs" / "plug-menu.json"

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

    @property
    def where(self) -> str:
        """Which sources hold it, for the listing's right-hand column."""
        if self.on_disk and self.on_github:
            return "folder + repo"
        return "folder only" if self.on_disk else "repo only"


@dataclass(frozen=True)
class Step:
    """One project's worth of work, fully described before anything runs."""

    action: str
    name: str
    clone: bool = False
    create_repo: bool = False
    init_git: bool = False
    hazards: tuple[str, ...] = ()


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


def github_repos(runner) -> list[str]:
    """Every non-archived repo `gh` can see for the authenticated account.

    `runner(argv)` returns a CompletedProcess — injected so the listing is testable
    without a network call. Raises `PlugError` rather than returning an empty list on
    failure: "you have no repos" and "gh is not logged in" produce the same list and
    mean opposite things, and the second one would offer to *create* every repo that
    already exists.
    """
    argv = ["gh", "repo", "list", "--limit", "500", "--no-archived", "--json", "name"]
    result = runner(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise PlugError(f"gh could not list repos: {detail[0] if detail else 'no output'}")
    try:
        return sorted({entry["name"] for entry in json.loads(result.stdout or "[]")})
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PlugError(f"gh returned a repo list this cannot read: {exc}") from exc


def inventory(
    registry_text: str,
    disk: list[str],
    repos: list[str],
    *,
    git_dirs: frozenset[str] = frozenset(),
    harnessed: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """Merge the three sources into one ordered list of candidates.

    Registered projects keep the registry's own order — that array is hand-arranged and
    reordering it in the listing would make the checkbox numbers disagree with the
    workspace tree everyone reads. Everything else is appended alphabetically.

    `NOT_PROJECTS` is subtracted, so a reference checkout cannot be offered for
    unplugging. It is in `folders` on purpose and every consumer of the registry
    already excludes it by name; letting a checkbox retire it would break the one
    task that resolves against the raw registry.
    """
    plugged = [
        n
        for n in devkit_project.known_projects(registry_text)
        if n not in devkit_project.NOT_PROJECTS
    ]
    on_disk, on_github = set(disk), set(repos)
    rest = sorted((on_disk | on_github) - set(plugged) - devkit_project.NOT_PROJECTS, key=str.lower)

    def make(name: str, is_plugged: bool) -> Candidate:
        return Candidate(
            name=name,
            plugged=is_plugged,
            on_disk=name in on_disk,
            on_github=name in on_github,
            is_git=name in git_dirs,
            harnessed=name in harnessed,
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


# --- the same list, drawn by VS Code ----------------------------------------------


def menu_detail(candidate: Candidate, owner: str) -> str:
    """What ticking — or unticking — this row costs, for the quick-pick's second line.

    The pick is the confirmation: the task runs `--ticked ... --yes`, so this string is
    the last thing anyone reads before a private GitHub repo is created. Every row says
    what its *own* tick does, because the consequence of unticking is uniform and the
    consequence of ticking is not.
    """
    if candidate.plugged:
        return "in the registry -- untick to retire it (registry only; nothing on disk is touched)"
    if not candidate.on_disk:
        return f"not on disk -- ticking clones {owner}/{candidate.name} first"
    if not candidate.on_github:
        return f"no repo -- ticking CREATES the private {owner}/{candidate.name} and pushes it"
    return "on disk and on GitHub -- ticking registers it, and nothing else"


def menu_payload(candidates: list[Candidate], owner: str, now: _dt.datetime | None = None) -> list:
    """The quick-pick's option groups, as `pickStringRemember` loads them from a file.

    One group, whose label is where the **timestamp** goes: the extension can only read
    a *file*, so this list is stale by construction and `.claude/rules/vscode-tasks.md`
    requires the reader be told how stale. A group label is drawn as a separator row
    above the options, which is the only line in a quick-pick nothing can tick.

    `picked` is what makes this a checklist rather than a menu: the box opens ticked for
    exactly the projects the registry currently holds, so the pick is an *edit* of the
    live state and an unchanged pick is a no-op. The extension honours the field only
    while it has no remembered tick set of its own, which is why the input carries
    `clearStorage` — see the comment on `plugSelection` in `workspace.jsonc`.

    `fileFormat: "load"` takes this array as the groups verbatim, so nothing here is a
    JS expression evaluated against rising indices. That is deliberate: the templated
    form (`jsonOption`, what `previewRow` uses) ends its list when an expression
    *throws*, so a row missing one field draws ten thousand blank entries instead.
    """
    stamp = now or _dt.datetime.now(_dt.UTC)
    as_of = stamp.astimezone().strftime("%Y-%m-%d %H:%M")
    return [
        {
            "label": f"as of {as_of} -- ticked = in the workspace registry",
            "options": [
                {
                    "value": candidate.name,
                    "label": candidate.name,
                    "description": candidate.where
                    + (
                        ""
                        if candidate.harnessed or not candidate.on_disk
                        else "  (no .devkit.toml)"
                    ),
                    "detail": menu_detail(candidate, owner),
                    "picked": candidate.plugged,
                }
                for candidate in candidates
            ],
        }
    ]


def write_menu(payload: list, path: Path | None = None) -> Path | None:
    """Save the quick-pick's options, atomically. The path on success, None on failure.

    Never raises, for `refresh_menu`'s reason: this runs as a rider on other people's
    passes, and a menu that could not be cached must never fail the work that carried
    it. The destination defaults at CALL time so a test can point `MENU_CACHE` somewhere
    disposable and the callers that pass no path follow it there.
    """
    path = path or MENU_CACHE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(".json.tmp")
        scratch.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")
        scratch.replace(path)
    except OSError:
        return None
    return path


def read_menu(path: Path | None = None) -> list[str] | None:
    """The names the quick-pick offered, in order. None when there is no readable menu.

    The **offered** set, not the ticked one, and it is what makes `--ticked` safe to
    interpret: an answer of "alpha,beta" says nothing about a project registered after
    this file was written, so `selection_from_ticks` must be able to tell an untick from
    a row that was never drawn.
    """
    try:
        groups = json.loads((path or MENU_CACHE).read_text(encoding="utf-8"))
        return [option["value"] for group in groups for option in group["options"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def refresh_menu(path: Path | None = None, owner: str = DEFAULT_OWNER) -> Path | None:
    """Rebuild the quick-pick's options file. The path on success, None on anything else.

    Total, because `worktree.py reconcile` runs it as a rider every fifteen minutes and
    a menu that could not be built must never redden a pass that reaped boxes correctly.

    A run whose `gh` listing failed writes **nothing**, which is the one refusal worth
    spelling out: `gather` degrades to the folder half alone, and a candidate that is
    on GitHub then reads as `folder only` — a row whose detail offers to *create* the
    repo it already has. A stale menu is a wrong list; that one would be a wrong act.
    """
    try:
        candidates, warnings = gather()
        if warnings:
            return None
        return write_menu(menu_payload(candidates, owner), path)
    except Exception:
        return None


def parse_ticks(value: str) -> tuple[str, ...]:
    """Split the checklist's one-string answer. A VS Code input resolves to one string,
    so `separator` joins the ticked values and this is the other half of that."""
    return tuple(name for name in value.replace(",", " ").split() if name)


def picked_nothing(value: str) -> bool:
    """Did the quick-pick never open at all?

    Escape leaves the input unresolved and VS Code passes the literal `${input:...}`
    through to the task — the shape `preview-task.py` treats as a graceful no-op, for
    the reason its input carries no `checkEscapedUI`: the extension's abort flag is
    sticky, so opting into it retires the dropdown for the life of the window.
    """
    return "${input:" in value


def selection_from_ticks(
    ticked: tuple[str, ...], offered: list[str], plugged: set[str]
) -> set[str]:
    """The registry the ticks ask for: current state, edited by the rows that were drawn.

    An unticked row is an unplug **only if the menu offered it**. The file is written by
    a previous pass, so a project registered since is absent from the list and therefore
    absent from the answer — reading the answer as the whole intended registry would
    retire it, silently, on a click that never mentioned it.
    """
    return (plugged | set(ticked)) - (set(offered) - set(ticked))


# --- deciding what to do ----------------------------------------------------------


def plan(
    candidates: list[Candidate],
    checked: set[str],
    hazards: dict[str, tuple[str, ...]] | None = None,
) -> list[Step]:
    """Every ticked-but-unregistered and registered-but-unticked project, as work.

    Unplugs come first. Both halves are one `register`/`unregister` call each, so the
    order only decides what the preview reads like — and a list that retires before it
    adds matches how the numbers in the list will move.
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
                )
            )
    return steps


def describe(step: Step) -> str:
    """One line saying what a step will do, for the confirmation preview."""
    if step.action == UNPLUG:
        tail = f"  ({'; '.join(step.hazards)})" if step.hazards else ""
        return f"  unplug  {step.name}   registry only, nothing on disk is touched{tail}"
    extra = []
    if step.clone:
        extra.append("clone from GitHub")
    if step.init_git:
        extra.append("git init + initial commit")
    if step.create_repo:
        extra.append("create the PRIVATE GitHub repo and push")
    detail = ", ".join(extra) if extra else "registry only"
    return f"  plug    {step.name}   {detail}"


def in_ephemeral_box(root: Path) -> bool:
    """Is `root` one of the workspace's disposable boxes rather than a static checkout?

    Keyed off `worktree.BOXES_DIR_NAME` rather than the literal, so the two cannot
    drift — the same reason `tests/support.py` does it that way.
    """
    return root.parent.name == worktree.BOXES_DIR_NAME


def edit_verdict(*, branch: str, default: str, in_box: bool, live_unstamped: bool) -> str:
    """Why the registry must not be edited from here; "" when it may.

    Pure, so the three ways this run would publish the wrong copy are testable without
    a git tree. The first two are `workspace-status.publish_verdict`'s reasons in a
    different order and for the same reason it has them — a task branch's
    `workspace.jsonc` is a proposal, and a box's is a proposal nobody has even opened a
    PR for yet, while the live file is what every window on the machine reads.

    The third is this script's own. `publish_workspace` would refuse a live file
    carrying a hand edit devkit never wrote, but it refuses *after* the canonical copy
    has already been changed — so the run would leave a registry edit stranded in a
    file nothing renders. Asking first is what makes the refusal recoverable.
    """
    if in_box:
        return (
            "this is an ephemeral box, so its workspace.jsonc is an unmerged proposal -- "
            "run this from the devkit checkout"
        )
    if branch != default:
        return f"devkit is on {branch}, not {default} -- publishing would make an unmerged proposal live"
    if live_unstamped:
        return (
            "the live workspace file carries an edit devkit never wrote -- run "
            "`python scripts/devkit_project.py --adopt-workspace` first, and commit it"
        )
    return ""


def live_carries_a_hand_edit(live: Path) -> bool:
    """Would `publish_workspace` refuse to overwrite `live`?

    The same two terms in the same order, because a second implementation of "is it
    safe to overwrite the file every window on this machine reads" is exactly what
    extracting `publish_workspace` avoided. It is asked *early* here, before anything
    is written, which is the one thing that function cannot do for a caller that edits
    the canonical copy first.

    Both halves matter. Drift alone is the ordinary state — it is what a plug or an
    unplug is about to create. An unmatched stamp alone is the state right after an
    adopt, where the live file is identical to the canonical one and simply not stamped
    yet; refusing there would block a run over a hand edit that never happened.
    """
    text = live.read_text(encoding="utf-8")
    drift = devkit_project.workspace_drift(
        devkit_jsonc.loads(text), devkit_jsonc.loads(devkit_project.canonical_text())
    )
    return bool(drift) and devkit_project.semantic_digest(text) != devkit_project.read_stamp(live)


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
    """Create the PRIVATE GitHub repo for an existing folder and push it.

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
        "--private",
        "--source=.",
        "--remote=origin",
        "--push",
    ]
    if runner(argv, cwd=path).returncode != 0:
        raise PlugError(f"could not create {owner}/{name} on GitHub")


def apply_registry(text: str, steps: list[Step]) -> str:
    """Apply every step's registry half to `text`, in one pass each way.

    `register` and `unregister` both verify their own result and raise
    `RegistryEditError` on a half-applied edit, which is the failure this whole tier
    exists for: `sweep.parse_workspace` reads an unparseable workspace file as "no
    checkouts", so a broken registry is silent everywhere it matters.
    """
    retiring = [s.name for s in steps if s.action == UNPLUG]
    adding = [s.name for s in steps if s.action == PLUG]
    if retiring:
        text = devkit_project.unregister(text, retiring)
    if adding:
        text = devkit_project.register(text, adding)
    return text


def write_artifact(problems: list[str]) -> None:
    """Persist this run's failures under `logs/`, per `.claude/rules/engineering.md`.

    The script writes its own artifact instead of the task being wrapped in
    `log-wrap.py`, which is the ordinary way. That wrapper pipes the child's stdout and
    reads it a line at a time, so a prompt that has not ended its line never reaches the
    terminal and the checkbox loop below hangs waiting for input nobody can see — true
    of a bare `python scripts/plug-projects.py` whichever way the task is spelled. One
    artifact covering both entry points is also the only version of this that stays
    right when someone runs the CLI, which is where the confirmations still live.

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
    """
    registry = LIVE_WORKSPACE.read_text(encoding="utf-8")
    disk = disk_projects(WORKSPACE_ROOT)
    warnings: list[str] = []
    try:
        repos = github_repos(_run)
    except PlugError as exc:
        warnings.append(f"{exc} -- listing folders only, so nothing can be cloned this run")
        repos = []
    git_dirs = frozenset(n for n in disk if (WORKSPACE_ROOT / n / ".git").exists())
    harnessed = frozenset(n for n in disk if (WORKSPACE_ROOT / n / ".devkit.toml").is_file())
    return inventory(registry, disk, repos, git_dirs=git_dirs, harnessed=harnessed), warnings


def _apply(steps: list[Step], owner: str, out) -> None:
    """Run every step's disk half, then write and publish the registry."""
    for step in steps:
        if step.clone:
            out(f"  clone   {owner}/{step.name}")
            clone_repo(owner, step.name, WORKSPACE_ROOT, _run)
        if step.create_repo:
            out(f"  create  {owner}/{step.name} (private)")
            create_repo(owner, step.name, WORKSPACE_ROOT / step.name, _run, init=step.init_git)

    canonical = devkit_project.CANONICAL_WORKSPACE
    canonical.write_text(
        apply_registry(canonical.read_text(encoding="utf-8"), steps),
        encoding="utf-8",
        newline="\n",
    )
    out(f"  update  {canonical.name} (folders + every maintained picker)")

    outcome, changes = devkit_project.publish_workspace(LIVE_WORKSPACE)
    if outcome == devkit_project.RENDER_REFUSED:
        raise PlugError(
            f"{canonical.name} is written, but the live file carries an edit devkit never "
            "wrote and was not published -- `--render-workspace --force` discards it"
        )
    out(f"  publish {LIVE_WORKSPACE.name} ({len(changes)} change(s)) -- reload the window")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true", help="print the checkbox list and exit")
    parser.add_argument("--json", action="store_true", help="with --list, emit JSON")
    parser.add_argument(
        "--refresh-menu",
        action="store_true",
        help=f"rebuild {MENU_CACHE.name}, the VS Code quick-pick's options, and exit",
    )
    parser.add_argument(
        "--ticked",
        metavar="NAMES",
        help="the VS Code checklist's answer: the ticked names, comma-separated",
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
        write_artifact([f"could not read the workspace: {exc}"])
        print(f"ERROR   could not read the workspace: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(f"  NOTE    {warning}")

    if args.refresh_menu:
        if warnings:
            print("ERROR   the menu was not rewritten: it would offer to create repos that exist")
            write_artifact(["gh could not list repos, so the menu was left as it was"])
            return 2
        written = write_menu(menu_payload(candidates, args.owner))
        if written is None:
            print(f"ERROR   could not write {MENU_CACHE}", file=sys.stderr)
            write_artifact([f"could not write {MENU_CACHE}"])
            return 2
        print(f"  wrote {written} ({len(candidates)} row(s))")
        write_artifact([])
        return 0

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

    # Resolved before the gate below, so that Escaping the quick-pick from a box costs a
    # line rather than an error about branches: nothing was picked, so nothing is being
    # published and none of the gate's three reasons is about to be true.
    ticks: tuple[str, ...] = ()
    offered: list[str] = []
    if args.ticked is not None:
        if picked_nothing(args.ticked):
            print("  nothing was picked -- nothing changed")
            write_artifact([])
            return 0
        from_file = read_menu()
        if from_file is None:
            problem = (
                f"the checklist has not been built yet ({MENU_CACHE.name} is missing or "
                "unreadable) -- run `python scripts/plug-projects.py --refresh-menu`"
            )
            print(f"ERROR   {problem}", file=sys.stderr)
            write_artifact([problem])
            return 2
        offered = from_file
        picked = parse_ticks(args.ticked)
        # A menu row is only as fresh as the pass that wrote it, and the world moved on
        # after that: a name that has since stopped being a candidate is dropped with a
        # note rather than failing the run, because the *other* ticks are still true.
        stale = sorted(set(picked) - known)
        if stale:
            print(f"  NOTE    no longer on disk, on GitHub or in the registry: {', '.join(stale)}")
        ticks = tuple(name for name in picked if name in known)
        if not ticks:
            problem = (
                "nothing is ticked -- that would retire the whole registry. Untick one "
                "project at a time, or use --unplug NAME"
            )
            print(f"ERROR   {problem}", file=sys.stderr)
            write_artifact([problem])
            return 1

    git = sweep.git_for(REPO_ROOT)
    # Asked before the list is drawn, not after it is ticked: every reason it refuses is
    # true of the checkout rather than of the choice, so making someone tick twelve boxes
    # first would only lose the ticking.
    verdict = edit_verdict(
        branch=(git("rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip(),
        default=task_branch.detect_default_branch(git),
        in_box=in_ephemeral_box(REPO_ROOT),
        live_unstamped=live_carries_a_hand_edit(LIVE_WORKSPACE),
    )
    if verdict:
        print(f"ERROR   {verdict}", file=sys.stderr)
        print("        `--list` still works from here.", file=sys.stderr)
        write_artifact([verdict])
        return 1

    checked = {c.name for c in candidates if c.plugged}
    if args.ticked is not None:
        checked = selection_from_ticks(ticks, offered, checked)
    elif args.plug or args.unplug:
        checked = (checked | set(args.plug)) - set(args.unplug)
    else:
        print("")
        chosen = interactive(candidates, checked, input)
        if chosen is None:
            print("  nothing changed")
            write_artifact([])
            return 0
        checked = chosen

    hazards = {
        c.name: unplug_hazards(c.name, WORKSPACE_ROOT, sweep.git_for(WORKSPACE_ROOT / c.name))
        for c in candidates
        if c.plugged and c.name not in checked
    }
    steps = plan(candidates, checked, hazards)
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
        _apply(steps, args.owner, print)
    except (PlugError, devkit_project.RegistryEditError, OSError) as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        write_artifact([str(exc)])
        return 1

    # The registry just moved, so the cached quick-pick is now wrong in the one way that
    # matters: its rows are pre-ticked from the state this run replaced. `reconcile`
    # would fix it within the quarter hour, and a second click inside that window is
    # exactly when someone is most likely to look.
    if refresh_menu(owner=args.owner) is None:
        print(f"  NOTE    {MENU_CACHE.name} was not rewritten -- the next pick may be stale")

    print("")
    print(f"  commit {devkit_project.CANONICAL_WORKSPACE.name} on a task branch -- it is devkit's")
    print("  copy of the registry, and an uncommitted one stops the session-start publish.")
    if any(s.action == PLUG for s in steps):
        print("  a project with a stack also needs a slot in ports.toml; add it by hand.")
    write_artifact([])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
