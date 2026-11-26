#!/usr/bin/env python3
"""The project registry and devkit's settings, read without a VS Code workspace file.

Today the registry is `folders` in `<projects-root>/alex-projects.code-workspace` and
the settings sit under its `settings`, beside VS Code's own. That file is being retired,
so this module is where both move to, as two files with different owners:

- **`projects.toml`, at devkit's root, committed.** The checkouts devkit manages, and
  the settings every PC shares. Shared, so it is versioned with the code that reads it
  and changes on a task branch like `ports.toml` does. TOML rather than JSON because a
  person edits it and it carries comments -- the settings' reasons are paragraphs, and
  the workspace file needed `devkit_jsonc` for exactly that -- and because the repo
  already reads its committed registries (`ports.toml`, `.devkit.toml`) with `tomllib`.
  The stdlib has no TOML writer; a writer edits the `projects` array a line at a time,
  as `new-project.py` already edits `ports.toml`'s `[slots]`, which also keeps its
  comments where a dump-and-rewrite would drop them.
- **`.devkit-machine.json`, beside the checkouts, never committed.** What is true of
  this PC alone: whether the fix pass is on, which projects are on hold, the machine's
  role. JSON because only tools write it and none of it needs a comment, and because a
  flat `{"devkit.fixPass": "off"}` keeps `fix_switch.py`'s parser-free pattern valid
  against the new file unchanged.

**Until every caller has switched, the live workspace file is the fallback** -- per
file for the registry, per key for a setting -- so introducing the module changes what
nothing reads. A missing file is the fallback; a malformed one raises `RegistryError`,
because a committed file is gated by its tests and a silent empty registry is the
failure `sweep.parse_workspace`'s docstring describes.

Every function takes `repo_root` -- the checkout the caller runs in, which may be a box
or an agent CLI's worktree -- and resolves the rest through `sweep.default_workspace`
rather than a copy of its box arithmetic.

Unit-tested in `tests/test_project_registry.py`, which also holds `projects.toml` to
the `folders` of `workspace.jsonc` while both exist.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

REGISTRY_NAME = "projects.toml"
MACHINE_FILE_NAME = ".devkit-machine.json"

# The settings that are one PC's own answer and so belong in the machine file, not the
# committed one. Spelled here rather than imported off their owners (`fix_cycle`,
# `sweep`, `machine_role`) so this module stays cheap to import; the test pins the
# spellings to the owners'.
MACHINE_KEYS: frozenset[str] = frozenset({"devkit.fixPass", "devkit.onHold", "devkit.machineRole"})

_MISSING = object()


class RegistryError(ValueError):
    """A registry or settings file exists but cannot be read as one."""


# --- where the files are ---------------------------------------------------------------


def projects_root(repo_root: Path = REPO_ROOT) -> Path:
    """The directory the checkouts sit in, whether or not a workspace file is there.

    The workspace file's directory, which is all `workspace.parent` ever meant to its
    callers -- `sweep.default_workspace` already walks out of a box or an agent CLI's
    worktree to it, and is pure, so the answer needs no file to exist.
    """
    return sweep.default_workspace(repo_root).parent


def live_workspace(repo_root: Path = REPO_ROOT) -> Path:
    """The live `.code-workspace` file: the fallback until every caller has switched."""
    return sweep.default_workspace(repo_root)


def registry_path(repo_root: Path = REPO_ROOT) -> Path:
    """`projects.toml` in the checkout `repo_root` names -- a box reads its own copy,
    as it does `ports.toml`."""
    return repo_root / REGISTRY_NAME


def machine_path(repo_root: Path = REPO_ROOT) -> Path:
    """`.devkit-machine.json` beside the checkouts."""
    return projects_root(repo_root) / MACHINE_FILE_NAME


# --- parsing ---------------------------------------------------------------------------


def parse_registry(text: str) -> list[str]:
    """Project names from `projects.toml` text, in file order, each once.

    Raises `RegistryError` for text that is not TOML or whose `projects` is not a list
    of names; an absent `projects` is an empty registry.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{REGISTRY_NAME}: {exc}") from exc
    names = data.get("projects", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise RegistryError(f"{REGISTRY_NAME}: `projects` must be a list of names")
    return list(dict.fromkeys(names))


def parse_shared_settings(text: str) -> dict[str, object]:
    """The `[settings]` table of `projects.toml` text, keyed by full setting name."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{REGISTRY_NAME}: {exc}") from exc
    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise RegistryError(f"{REGISTRY_NAME}: `settings` must be a table")
    return settings


def parse_machine(text: str) -> dict[str, object]:
    """The machine file's settings: one JSON object keyed by full setting name."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{MACHINE_FILE_NAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"{MACHINE_FILE_NAME}: must hold one JSON object")
    return data


def workspace_settings(text: str) -> dict[str, object]:
    """The live workspace file's `settings`; empty for text that is not one.

    Silent on malformed text, as every reader of the live file is today -- this is the
    fallback, and it keeps their behaviour rather than improving it.
    """
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    settings = payload.get("settings") if isinstance(payload, dict) else None
    return settings if isinstance(settings, dict) else {}


def _read(path: Path) -> str | None:
    """`path`'s text, or None when there is no file to read."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None


# --- the registry ----------------------------------------------------------------------


def all_projects(repo_root: Path = REPO_ROOT) -> list[str]:
    """Every registered project, checked out here or not, in registry order.

    `projects.toml` when it exists; otherwise the live workspace file's `folders`, read
    by `sweep.parse_workspace` with nothing excluded -- the raw registry, before
    `devkit_project.NOT_PROJECTS` -- and empty when neither file is there.
    """
    text = _read(registry_path(repo_root))
    if text is not None:
        return parse_registry(text)
    live = _read(live_workspace(repo_root))
    return sweep.parse_workspace(live, frozenset()) if live is not None else []


def projects(root: Path | None = None, *, repo_root: Path = REPO_ROOT) -> list[str]:
    """Registered projects with a checkout under `root` (default: `projects_root`).

    The disk decides, as it does in `devkit_project.machine_view`: a project counts on
    this PC when its directory is there. Unlike `machine_view`, a PC holding none of
    them gets none -- the render cannot empty `folders`, but a list of projects can be
    empty, and here it is the true answer.
    """
    base = projects_root(repo_root) if root is None else root
    return [name for name in all_projects(repo_root) if (base / name).is_dir()]


# --- settings --------------------------------------------------------------------------


def setting(key: str, default: object = None, *, repo_root: Path = REPO_ROOT) -> object:
    """A devkit setting's value, or `default` where no file sets it.

    A key in `MACHINE_KEYS` is read from the machine file, any other from
    `projects.toml`'s `[settings]`. A key that file does not set -- or a file that is
    not there -- falls back to the live workspace file's `settings`, so settings can
    move one key at a time. The value is returned as stored; validating it stays with
    the setting's owner.
    """
    if key in MACHINE_KEYS:
        text = _read(machine_path(repo_root))
        found = parse_machine(text).get(key, _MISSING) if text is not None else _MISSING
    else:
        text = _read(registry_path(repo_root))
        found = parse_shared_settings(text).get(key, _MISSING) if text is not None else _MISSING
    if found is not _MISSING:
        return found
    live = _read(live_workspace(repo_root))
    if live is None:
        return default
    return workspace_settings(live).get(key, default)
