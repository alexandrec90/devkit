#!/usr/bin/env python3
"""What `harness-switch.py` has stood down, and the instruction files it holds.

Split out of `harness-switch.py`, and the seam is the one its readers already show. The
switch is a *verb* run from a task; this is a *record* three other things read —
`agent-box.py` asks whether the hooks are off before it opens a terminal,
`tests/test_doc_claims.py` and `scripts/instruction-budget.py` ask where an instruction
file currently lives. A verb nobody imports and a record everybody does are two
responsibilities, and keeping them in one module put it past `definitions` and
`file_lines` on the day it was written.

The underscore in the name is the practical half of that: this is importable by name,
so its three readers say `import harness_state` rather than resolving a dashed script
through `precommit/_loader.py`.

## What "moved aside" means

Each instruction file goes to `logs/harness-switch/files/<root key>/<path>` — in the
checkout `$DEVKIT_DIR` names, never in whichever copy this module happens to be imported
from; `_state_root` carries why that distinction is destructive rather than cosmetic. The
ledger records where it came from, so restoring is a move back rather than a
reconstruction. A **tracked** file additionally gets `git update-index --skip-worktree`.

That second half is not tidiness. Without it the file reads as deleted: `git status`
shows it, and — much worse — `git add -A` stages it, so the first `Agent: Ship PR` run in
a box with the group off would carry "delete CLAUDE.md" into a pull request. With it, git
reports the tree clean and the deletion cannot be committed by accident.

Nothing here decides *whether* to switch anything. `harness-switch.py` owns that, and
owns the reason the group moves files at all rather than setting a flag.

Tested in `tests/test_harness_state.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _state_root() -> Path:
    """The checkout whose `logs/` holds the ledger and the stash.

    `$DEVKIT_DIR` first, this copy second -- `harness_events.ledger_root`'s resolution and
    its reason. It matters more here than there, and the difference is destructive rather
    than cosmetic: an ephemeral box is a devkit checkout too, so a switch run from one
    would stash six repositories' instruction files into a directory `reconcile` deletes
    the moment the box's PR merges. The files would then be recoverable only from git, and
    the untracked ones -- the workspace root's `CLAUDE.md` and `~/.claude/CLAUDE.md` --
    not at all.
    """
    named = (os.environ.get("DEVKIT_DIR") or "").strip()
    return Path(named) if named and Path(named).is_dir() else REPO_ROOT


STATE_DIR = _state_root() / "logs" / "harness-switch"
LEDGER = STATE_DIR / "ledger.json"
STASH = STATE_DIR / "files"

# What Claude Code injects without being asked. `.claude/skills/` is absent by design --
# a skill costs nothing until a session invokes one by name, which is the opposite of the
# always-loaded tier the switch exists to stand down. So is `templates/`, whose
# `CLAUDE.md.tmpl` files are devkit's *output* and are never loaded into a session.
RULES_GLOB = ".claude/rules/*.md"
MEMORY_NAME = "CLAUDE.md"
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".worktrees",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "templates",
        "logs",
    }
)


@dataclass
class StashedFile:
    """One instruction file that has been moved aside, and how to put it back."""

    root: str
    relpath: str
    stash: str
    tracked: bool

    def live(self) -> Path:
        return Path(self.root) / self.relpath

    def held(self) -> Path:
        return STASH / self.stash


@dataclass
class Ledger:
    """Everything a restore needs, and the only record that a group is off at all."""

    hooks: bool = False
    jobs: tuple[str, ...] = ()
    instructions: list[StashedFile] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> Ledger:
        """Never raises: a corrupt or absent ledger reads as "nothing is switched off",
        which is the state that makes `--off` safe to re-run and `--on` a no-op."""
        try:
            raw = json.loads((path or LEDGER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        files = [
            StashedFile(
                root=str(entry.get("root", "")),
                relpath=str(entry.get("relpath", "")),
                stash=str(entry.get("stash", "")),
                tracked=bool(entry.get("tracked")),
            )
            for entry in raw.get("instructions", [])
            if isinstance(entry, dict)
        ]
        return cls(
            hooks=bool(raw.get("hooks")),
            jobs=tuple(str(name) for name in raw.get("jobs", [])),
            instructions=[f for f in files if f.root and f.relpath and f.stash],
        )

    def save(self, path: Path | None = None) -> None:
        target = path or LEDGER
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "hooks": self.hooks,
            "jobs": list(self.jobs),
            "instructions": [
                {"root": f.root, "relpath": f.relpath, "stash": f.stash, "tracked": f.tracked}
                for f in self.instructions
            ],
        }
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def root_key(root: Path) -> str:
    """A filename-safe name for a root, stable across runs.

    The drive letter is kept (`c/Users/...` -> `c-Users-...`) because two roots differing
    only by drive are two roots, and a key that collided would restore one checkout's file
    into another.
    """
    text = root.resolve().as_posix()
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in text).strip("-")


def instruction_files(root: Path) -> list[Path]:
    """Every file under `root` that Claude Code injects without being asked.

    Nested `CLAUDE.md` files count: they are loaded when a session touches that subtree,
    which is injection deferred rather than injection avoided.
    """
    if not root.is_dir():
        return []
    found = [path for path in root.glob(RULES_GLOB) if path.is_file()]
    for path in root.rglob(MEMORY_NAME):
        relative = path.relative_to(root)
        if not any(part in SKIP_DIRS for part in relative.parts) and path.is_file():
            found.append(path)
    return sorted(set(found))


def is_tracked(root: Path, relpath: str, runner=subprocess.run) -> bool:
    """Whether git in `root` has this path in its index. False for anything not a repo."""
    try:
        done = runner(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relpath],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return done.returncode == 0


def skip_worktree_argv(root: Path, relpath: str, skip: bool) -> list[str]:
    """The call that hides a tracked file's absence from git, or stops hiding it.

    See the module docstring: this is what keeps the deletion out of `git add -A`.
    """
    flag = "--skip-worktree" if skip else "--no-skip-worktree"
    return ["git", "-C", str(root), "update-index", flag, "--", relpath]


def switch_root(root: Path, ledger: Ledger, runner=subprocess.run) -> list[str]:
    """Move every instruction file under `root` aside. Returns one report line each."""
    lines: list[str] = []
    resolved = root.resolve()
    already = {(Path(f.root), f.relpath) for f in ledger.instructions}
    for path in instruction_files(resolved):
        relpath = path.relative_to(resolved).as_posix()
        if (resolved, relpath) in already:
            continue
        tracked = is_tracked(resolved, relpath, runner)
        held = STASH / root_key(resolved) / relpath
        held.parent.mkdir(parents=True, exist_ok=True)
        held.write_bytes(path.read_bytes())
        path.unlink()
        if tracked:
            runner(skip_worktree_argv(resolved, relpath, True), capture_output=True)
        ledger.instructions.append(
            StashedFile(
                root=str(resolved),
                relpath=relpath,
                stash=held.relative_to(STASH).as_posix(),
                tracked=tracked,
            )
        )
        lines.append(f"  moved aside: {resolved.name}/{relpath}")
    return lines


def restore_root(ledger: Ledger, root: Path | None = None, runner=subprocess.run) -> list[str]:
    """Put back everything the ledger holds, for `root` or for every root.

    An entry whose root no longer exists is dropped rather than reported as a failure: a
    reaped box is the ordinary way for one to disappear, and a ledger that can never be
    emptied is one nobody trusts.
    """
    lines: list[str] = []
    keep: list[StashedFile] = []
    wanted = None if root is None else root.resolve()
    for entry in ledger.instructions:
        if wanted is not None and Path(entry.root) != wanted:
            keep.append(entry)
        else:
            lines.append(_restore_one(entry, runner))
    ledger.instructions = keep
    return lines


def _restore_one(entry: StashedFile, runner=subprocess.run) -> str:
    """One entry back where it came from, as a report line. Never raises."""
    name = f"{Path(entry.root).name}/{entry.relpath}"
    if not Path(entry.root).is_dir():
        return f"  gone, dropped: {entry.root}/{entry.relpath}"
    if entry.tracked:
        runner(skip_worktree_argv(Path(entry.root), entry.relpath, False), capture_output=True)
    if not entry.held().is_file():
        # The stash is gone but the index entry is not, so the file is recoverable from
        # git and the only thing that must not survive is the skip-worktree bit.
        return f"  stash missing, index cleared: {name}"
    target = entry.live()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(entry.held().read_bytes())
    entry.held().unlink()
    return f"  restored: {name}"


def stashed_for(path: Path, ledger: Ledger | None = None) -> Path | None:
    """Where to read `path` from while it is switched off, or None when it is not.

    The lookup is by the file's *logical* path, so a caller keeps saying `REPO_ROOT /
    "CLAUDE.md"` and never learns the stash layout. That matters more than it sounds: the
    gates that use this also relativise those paths for their messages, and a stash path
    is not under the repo.
    """
    wanted = path.resolve()
    for entry in (ledger or Ledger.load()).instructions:
        if (Path(entry.root) / entry.relpath).resolve() == wanted and entry.held().is_file():
            return entry.held()
    return None


def instruction_text(path: Path, ledger: Ledger | None = None) -> str:
    """`path`'s content, live or stashed; "" when it is neither."""
    if path.is_file():
        return path.read_text(encoding="utf-8")
    held = stashed_for(path, ledger)
    return held.read_text(encoding="utf-8") if held else ""


def instruction_exists(path: Path, ledger: Ledger | None = None) -> bool:
    return path.is_file() or stashed_for(path, ledger) is not None


def instruction_sources(root: Path, ledger: Ledger | None = None) -> list[tuple[str, Path]]:
    """`(relpath, where to read it)` for every instruction file of `root`, switched or not.

    The listing, not just the read, has to go through here: a gate that globs for
    `CLAUDE.md` finds nothing once the group is off, and a check that silently stops
    checking anything is worse than one that fails.
    """
    resolved = root.resolve()
    live = {path.relative_to(resolved).as_posix(): path for path in instruction_files(resolved)}
    for entry in (ledger or Ledger.load()).instructions:
        if Path(entry.root) == resolved and entry.relpath not in live and entry.held().is_file():
            live[entry.relpath] = entry.held()
    return sorted(live.items())
