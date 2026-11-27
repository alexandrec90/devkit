#!/usr/bin/env python3
"""Where the checkouts are, resolved from any checkout: static, box or agent CLI tree.

The projects root is the directory every registered checkout sits in, one level above a
static checkout. It used to be spelled `<root>/alex-projects.code-workspace`'s parent,
and `sweep.default_workspace` was the resolver; the workspace file is retired, so the
directory is the answer and the file name survives only for what still reads that file
-- `project_registry.migrate_machine_settings`'s one-shot, and the VS Code task layer
until it is deleted.

Its own module, stdlib only, because both `sweep` and `project_registry` need it and
`sweep` reads the registry: kept in either, the other import was a cycle.

Pure: no answer here needs a file to exist, except that a detached agent CLI worktree is
recognised by its own `.git` pointer (`worktree_tiers.owning_checkout`).

Tested in `tests/test_projects_root.py`; `tests/test_sweep.py` still drives the
resolvers through `sweep`'s re-exports.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))  # the vendored tier
import worktree_tiers

REPO_ROOT = Path(__file__).resolve().parents[1]

# The retired VS Code workspace file's name, beside the checkouts.
WORKSPACE_FILE_NAME = "alex-projects.code-workspace"
BOXES_DIR_NAME = worktree_tiers.BOXES_DIR_NAME
# The separator between project and topic in a box name (`devkit--fix-thing-0810`).
NAME_SEP = "--"


def cli_worktree_checkout(repo_root: Path) -> Path | None:
    """The checkout an agent CLI's worktree belongs to; None when `repo_root` is not one.

    A **nested** tier is matched on the directory names above `repo_root` rather than on
    anything git reports, so it stays pure and answers for a path that no longer exists.
    A **detached** one cannot: `codex --worktree` cuts `<CODEX_HOME>/worktrees/<hash>/`,
    where the digest names no checkout, so `worktree_tiers.owning_checkout` reads the
    worktree's own `.git` pointer instead. A Codex worktree already removed answers None
    and the caller treats it as an ordinary checkout, which by then it is.
    """
    return worktree_tiers.owning_checkout(repo_root)


def projects_root(repo_root: Path = REPO_ROOT) -> Path:
    """The directory the checkouts sit in, for a checkout at `repo_root`, box or not.

    One level above a static checkout -- but a **box** sits one level deeper, under
    `<root>/.worktrees/`, and an agent CLI's worktree under its checkout or outside it
    entirely. A box and an agent tree are exactly where an agent runs these scripts, and
    the naive `repo_root.parent` there named a directory holding no checkouts: five
    scripts broke that way, one of them silently, before this was one function.
    """
    inside = cli_worktree_checkout(repo_root)
    if inside is not None:
        return inside.parent
    parent = repo_root.parent
    return parent.parent if parent.name == BOXES_DIR_NAME else parent


def default_workspace(repo_root: Path = REPO_ROOT, name: str = WORKSPACE_FILE_NAME) -> Path:
    """The retired workspace file's path for a checkout at `repo_root`: `projects_root`
    joined to `name`. Only for what still reads that file; nothing needs it to exist."""
    return projects_root(repo_root) / name


def source_checkout(repo_root: Path) -> Path:
    """The permanent checkout `repo_root` belongs to -- itself, unless it is a box or an
    agent CLI's worktree.

    A script asking "which registered checkout am I?" from a box gets the box's name
    (`devkit--topic-0810`), which matches nothing in the registry -- and from one,
    `workspace-status.py` once reported **devkit itself** as "never vendored devkit".
    The project name is read off the box name rather than assumed, so this holds for a
    box cut from any project.
    """
    inside = cli_worktree_checkout(repo_root)
    if inside is not None:
        return inside
    parent = repo_root.parent
    if parent.name != BOXES_DIR_NAME:
        return repo_root
    return parent.parent / repo_root.name.split(NAME_SEP, 1)[0]


def from_flags(
    root: Path | None = None, workspace: Path | None = None, repo_root: Path = REPO_ROOT
) -> Path:
    """The projects root a CLI was pointed at: `--root`, else the deprecated
    `--workspace`'s directory (scheduled tasks registered before `--root` still pass
    it), else `projects_root(repo_root)`. Neither path has to exist."""
    if root is not None:
        return root
    if workspace is not None:
        return workspace.parent
    return projects_root(repo_root)


ROOT_HELP = "the directory the checkouts sit in (default: this checkout's)"
WORKSPACE_HELP = "deprecated: names the projects root by a file in it, which is never read"


def add_flags(parser: argparse.ArgumentParser) -> None:
    """`--root` and its deprecated spelling `--workspace` on `parser`, both defaulting to
    None; `from_flags` or `legacy_workspace` turns the pair into a path."""
    parser.add_argument("--root", type=Path, default=None, help=ROOT_HELP)
    parser.add_argument("--workspace", type=Path, default=None, help=WORKSPACE_HELP)


def legacy_workspace(
    root: Path | None = None, workspace: Path | None = None, repo_root: Path = REPO_ROOT
) -> Path:
    """A `workspace` path for code that still threads one and reads only its directory:
    the deprecated `--workspace` as given, else `WORKSPACE_FILE_NAME` in `from_flags`'s
    root. Never a file anything opens -- a CLI's internals switch to the root itself
    once the task layer is gone, and this goes with them."""
    if workspace is not None:
        return workspace
    return from_flags(root, None, repo_root) / WORKSPACE_FILE_NAME
