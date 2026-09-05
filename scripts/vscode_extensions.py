#!/usr/bin/env python3
"""The VS Code extensions the workspace's tasks need, and whether this machine has them.

`workspace-status.py`'s `toolchain_lines` reports the workstation prerequisites nothing
else mentions -- `uv` on PATH, a git identity -- and this is the third of them, found the
way the other two were: by hand, on a fresh machine, from a failure that named anything
but its cause.

**A `recommendations` entry is a prompt, never an install.** Twenty of the workspace's
tasks resolve their checkout through a `pickStringRemember` or `multiPick` input, and only
`rioj7.command-variable` supplies either, so a machine without it runs every one of them
into `command 'extension.commandvariable.pickStringRemember' not found`. VS Code offers
the recommendation once, in a toast; a machine that dismissed it -- or was provisioned
without ever opening the workspace -- is indistinguishable from one that is set up, and
the failure names a *command* rather than a package, so it cannot be searched for either.

Its own module rather than three more definitions in `workspace-status.py`, on the
precedent `schedule_health.py` set: that file is a reporter of last resort for half a
dozen unrelated subsystems and is twice `file_lines` already, so what belongs in it is the
one-line adapter and not the knowledge of how VS Code records an install.

Two decisions worth keeping:

- **The required list has one source** -- devkit's canonical `workspace.jsonc`, read here
  rather than copied into a constant, so a task that grows a dependency on a new extension
  needs the `recommendations` entry and nothing else. Canonical rather than the live
  workspace file because a fresh machine is precisely the one that has not rendered yet.
- **The installed set comes from VS Code's own registry, not `code --list-extensions`.**
  This runs at every session start, where a subprocess spawn is the cost that gets a hook
  disabled -- and the CLI is not on PATH on a machine where VS Code was installed without
  it, which would report every extension missing on exactly the workstation least able to
  tell that is wrong.

Silent whenever it cannot tell. A checkout with no workspace copy, or a VS Code keeping
its extensions under a custom `--extensions-dir`, has nothing to compare against, and a
session start must never turn "I don't know" into a fix nobody needs.

Tested in `tests/test_vscode_extensions.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc

# devkit's canonical copy, which is the one with a branch and a reviewer. `--render-
# workspace` publishes it to the live file; see `.claude/rules/vscode-tasks.md`.
CANONICAL_WORKSPACE = Path(__file__).resolve().parents[1] / "workspace.jsonc"
# VS Code's own record of what is installed: a JSON array of entries carrying
# `identifier.id`. Rewritten by the editor on every install and uninstall.
EXTENSIONS_JSON = Path.home() / ".vscode" / "extensions" / "extensions.json"


# What each read can actually raise, spelled out rather than caught as `Exception`: an
# absent or unreadable file (OSError), a malformed one (ValueError, which
# JSONDecodeError is), and a well-formed file whose shape is not the one expected --
# `recommendations` holding a string, or a registry entry with no `identifier`
# (AttributeError, KeyError, TypeError). Anything outside that list is a bug here and
# should reach the caller, which is a session start that already refuses to fail on it.
UNREADABLE = (OSError, ValueError, AttributeError, KeyError, TypeError)


def required(workspace: Path = CANONICAL_WORKSPACE) -> list[str]:
    """The extension ids the workspace file recommends; [] when it cannot be read."""
    try:
        parsed = devkit_jsonc.loads(workspace.read_text(encoding="utf-8"))
        return list(parsed.get("extensions", {}).get("recommendations", []))
    except UNREADABLE:
        return []


def installed(registry: Path = EXTENSIONS_JSON) -> set[str] | None:
    """The extension ids VS Code has recorded, lowercased; `None` when it cannot tell.

    `None` rather than an empty set, because the two mean opposite things: a machine with
    no extensions really is missing every recommendation, while a registry this cannot
    find says nothing at all -- and reporting the second as the first would put a fix in
    front of everyone whose VS Code keeps its extensions somewhere else.

    Lowercased because marketplace ids are case-insensitive and the registry keeps
    whichever spelling installed it, so comparing raw would report an extension that is
    sitting right there.
    """
    try:
        entries = json.loads(registry.read_text(encoding="utf-8"))
        return {entry["identifier"]["id"].lower() for entry in entries}
    except UNREADABLE:
        return None


def missing(workspace: Path = CANONICAL_WORKSPACE, registry: Path = EXTENSIONS_JSON) -> list[str]:
    """The recommended extensions this machine has not installed, in the file's order."""
    have = installed(registry)
    if have is None:
        return []
    return [name for name in required(workspace) if name.lower() not in have]


def report_lines(
    workspace: Path = CANONICAL_WORKSPACE, registry: Path = EXTENSIONS_JSON
) -> list[str]:
    """One line naming the missing extensions and the command that installs them; [] when
    there are none.

    A list rather than a string so it concatenates with `toolchain_lines`, which is where
    it is read from and which owns the `[workspace]` prefix.
    """
    absent = missing(workspace, registry)
    if not absent:
        return []
    install = " && ".join(f"code --install-extension {name}" for name in absent)
    return [
        f"VS Code extension not installed: {', '.join(absent)} -- the workspace "
        f"recommends it and a recommendation never installs anything, so every task whose "
        f"picker it supplies fails at click time naming a command rather than a package "
        f"(fix: {install}, then reload the window)"
    ]
