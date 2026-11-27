#!/usr/bin/env python3
"""The project registry and devkit's settings, read without a VS Code workspace file.

The registry was `folders` in `<projects-root>/alex-projects.code-workspace` and the
settings sat under its `settings`, beside VS Code's own. That file is retired, and both
live here now, as two files with different owners:

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

**The live workspace file is read by nothing here but `migrate_machine_settings`**, the
one-shot that copies a PC's machine keys out of it if it is still there. A missing or
malformed `projects.toml` raises `RegistryError` naming the file: it is committed in
every checkout, box and agent tree, so its absence is a broken checkout, and a silent
empty registry is the failure that reads as "nothing to do". A missing machine file is
a PC that never set anything, so its keys read as their defaults.

Every function takes `repo_root` -- the checkout the caller runs in, which may be a box
or an agent CLI's worktree -- and resolves the rest through `projects_root.py` rather
than a copy of its box arithmetic.

Unit-tested in `tests/test_project_registry.py`.

Imports nothing of devkit's but `devkit_jsonc` and `projects_root`, so `sweep` and the
rest can read the registry without an import cycle.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import projects_root as _roots

REPO_ROOT = Path(__file__).resolve().parents[1]

REGISTRY_NAME = "projects.toml"
MACHINE_FILE_NAME = ".devkit-machine.json"

# The settings that are one PC's own answer and so belong in the machine file, not the
# committed one. Spelled here rather than imported off their owners (`fix_cycle`,
# `sweep`, `machine_role`) so this module stays cheap to import; the test pins the
# spellings to the owners'.
MACHINE_KEYS: frozenset[str] = frozenset({"devkit.fixPass", "devkit.onHold", "devkit.machineRole"})


class RegistryError(ValueError):
    """A registry or settings file exists but cannot be read as one."""


# --- where the files are ---------------------------------------------------------------


def projects_root(repo_root: Path = REPO_ROOT) -> Path:
    """The directory the checkouts sit in (`projects_root.projects_root`), which is all
    `workspace.parent` ever meant to its callers. Pure: no file has to exist."""
    return _roots.projects_root(repo_root)


def live_workspace(repo_root: Path = REPO_ROOT) -> Path:
    """The retired live `.code-workspace` file, read only by `migrate_machine_settings`."""
    return _roots.default_workspace(repo_root)


def _machine_dir(repo_root: Path, root: Path | None, workspace: Path | None) -> Path:
    """Where the machine file is: `root`, a deprecated `workspace`'s directory, else
    `projects_root(repo_root)`."""
    return _roots.from_flags(root, workspace, repo_root)


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

    Silent on malformed text: its one reader is `migrate_machine_settings`, for which a
    frozen file it cannot read simply has nothing left to copy.
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


def _registry_text(repo_root: Path) -> str:
    """`projects.toml`'s text; `RegistryError` naming the file when it is not there."""
    path = registry_path(repo_root)
    text = _read(path)
    if text is None:
        raise RegistryError(f"no {REGISTRY_NAME} at {path}")
    return text


def all_projects(repo_root: Path = REPO_ROOT) -> list[str]:
    """Every registered project, checked out here or not, in registry order.

    `projects.toml`'s `projects`, nothing excluded -- the raw registry, before
    `devkit_project.NOT_PROJECTS`. Raises `RegistryError` when the file is missing or
    malformed: an empty answer there would read as "no projects", which is the one
    answer a sweep over them must never give wrongly.
    """
    return parse_registry(_registry_text(repo_root))


def projects(root: Path | None = None, *, repo_root: Path = REPO_ROOT) -> list[str]:
    """Registered projects with a checkout under `root` (default: `projects_root`).

    The disk decides: a project counts on this PC when its directory is there, and a
    PC holding none of them gets none -- here an empty list is the true answer.
    """
    base = projects_root(repo_root) if root is None else root
    return [name for name in all_projects(repo_root) if (base / name).is_dir()]


# --- settings --------------------------------------------------------------------------


def setting(
    key: str,
    default: object = None,
    *,
    repo_root: Path = REPO_ROOT,
    root: Path | None = None,
    workspace: Path | None = None,
) -> object:
    """A devkit setting's value, or `default` where its file does not set it.

    A key in `MACHINE_KEYS` is read from the machine file -- a PC with none has set
    nothing, so every key reads as `default` -- any other from `projects.toml`'s
    `[settings]`, whose absence raises `RegistryError` as `all_projects` does. The value
    is returned as stored; validating it stays with the setting's owner.

    `root` is the projects root a caller was pointed at (`--root`, a test's), when it is
    not the one `repo_root` resolves to: the machine file is looked for there.
    `workspace` is the deprecated spelling of the same thing, whose directory is used;
    the file itself is never read.
    """
    if key in MACHINE_KEYS:
        text = _read(_machine_dir(repo_root, root, workspace) / MACHINE_FILE_NAME)
        return parse_machine(text).get(key, default) if text is not None else default
    return parse_shared_settings(_registry_text(repo_root)).get(key, default)


# --- writing the machine file -----------------------------------------------------------


def write_machine(path: Path, values: dict[str, object]) -> None:
    """Replace the machine file at `path` with `values`, atomically.

    Written beside the target and renamed over it, so a reader -- `fix_switch`'s
    pattern on a scheduled fire -- sees the old file or the new one, never half of one.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def set_machine_setting(
    key: str,
    value: object,
    *,
    repo_root: Path = REPO_ROOT,
    root: Path | None = None,
    workspace: Path | None = None,
) -> Path:
    """Set one `MACHINE_KEYS` setting in the machine file, keeping the others; the path.

    Raises `ValueError` for a key that is not this PC's alone -- a shared one written
    here would be read by nothing -- and `RegistryError` for a machine file that is
    there and malformed, which is left as found rather than overwritten. `root` and the
    deprecated `workspace` say where, as for `setting`.
    """
    if key not in MACHINE_KEYS:
        raise ValueError(f"{key} is not a machine setting ({', '.join(sorted(MACHINE_KEYS))})")
    path = _machine_dir(repo_root, root, workspace) / MACHINE_FILE_NAME
    text = _read(path)
    values = parse_machine(text) if text is not None else {}
    values[key] = value
    write_machine(path, values)
    return path


def migrate_machine_settings(
    *, repo_root: Path = REPO_ROOT, root: Path | None = None, workspace: Path | None = None
) -> list[str]:
    """Copy each `MACHINE_KEYS` setting the live workspace file holds and the machine
    file lacks into the machine file; the keys copied, sorted.

    The one reader of the retired live file left, and only while a PC may not have run
    it yet: no live file is nothing to copy. `root` is the directory both files sit in;
    `workspace`, when given, names the live file itself.

    One-shot by construction: a key already in the machine file is never overwritten,
    so a second run copies nothing and a value set since with `machine_role.py --set`
    survives. Nothing to copy writes nothing. Raises `RegistryError` for a malformed
    machine file, which is left as found.
    """
    if workspace is not None:
        live_path = workspace
    elif root is not None:
        live_path = root / _roots.WORKSPACE_FILE_NAME
    else:
        live_path = live_workspace(repo_root)
    live = _read(live_path)
    if live is None:
        return []
    found = workspace_settings(live)
    path = live_path.parent / MACHINE_FILE_NAME
    text = _read(path)
    values = parse_machine(text) if text is not None else {}
    copied = sorted(key for key in MACHINE_KEYS if key in found and key not in values)
    if copied:
        write_machine(path, {**values, **{key: found[key] for key in copied}})
    return copied


# --- writing the registry --------------------------------------------------------------
#
# The `projects` array is edited a line at a time, as `new-project.py`'s `register_slot`
# edits `ports.toml`'s `[slots]`: the stdlib has no TOML writer, and a dump-and-rewrite
# would drop the comments this file exists to carry. Every edit is reparsed before it is
# returned, because a half-applied one is a registry every reader sees as wrong. This
# file is the only registry a writer edits: `workspace.jsonc` is deleted.

_ARRAY_OPEN = re.compile(r"^\s*projects\s*=\s*\[")
_ENTRY = re.compile(r'^(?P<indent>\s*)"(?P<name>[^"\\]*)"\s*(?P<comma>,?)\s*(?P<comment>#.*)?$')
_DEFAULT_INDENT = "    "


def _code(line: str) -> str:
    """`line` without its newline or a trailing `#` comment (names hold no `#`)."""
    body = line.rstrip("\r\n")
    in_string = False
    for i, char in enumerate(body):
        if char == '"':
            in_string = not in_string
        elif char == "#" and not in_string:
            return body[:i].rstrip()
    return body.rstrip()


def _checked_name(name: str) -> str:
    if not name or any(c in name for c in '"\\#\r\n'):
        raise RegistryError(f"{REGISTRY_NAME}: {name!r} is not a project name")
    return name


def _array_span(lines: list[str]) -> tuple[int, int] | None:
    """`(open, close)` line indices of the multi-line `projects = [` array, or None.

    A one-line array (`projects = []`, `projects = ["a"]`) is first laid out one name
    per line by `_spread`, so every edit below works on the one shape.
    """
    open_at = None
    for i, line in enumerate(lines):
        if _ARRAY_OPEN.match(line):
            open_at = i
            break
        if line.lstrip().startswith("["):
            return None  # a table header: `projects` would have had to come before it
    if open_at is None:
        return None
    for j in range(open_at + 1, len(lines)):
        if _code(lines[j]).lstrip().startswith("]"):
            return open_at, j
    raise RegistryError(f"{REGISTRY_NAME}: the `projects` array is never closed")


def _spread(lines: list[str]) -> list[str]:
    """`lines` with a one-line `projects = [...]` rewritten one name per line."""
    for i, line in enumerate(lines):
        if _ARRAY_OPEN.match(line) and "]" in _code(line):
            names = parse_registry(_code(line) + "\n")
            comment = line.rstrip("\r\n")[len(_code(line)) :].strip()
            opener = "projects = [" + (f"  {comment}" if comment else "") + "\n"
            body = [f'{_DEFAULT_INDENT}"{n}",\n' for n in names]
            return [*lines[:i], opener, *body, "]\n", *lines[i + 1 :]]
        if line.lstrip().startswith("["):
            break
    return lines


def _entries(lines: list[str], open_at: int, close_at: int) -> list[tuple[int, re.Match[str]]]:
    """The array's name lines; a line that is neither a name, a comment nor blank is
    refused rather than guessed at."""
    found = []
    for i in range(open_at + 1, close_at):
        if not _code(lines[i]).strip():
            continue
        match = _ENTRY.match(lines[i].rstrip("\r\n"))
        if match is None:
            raise RegistryError(f"{REGISTRY_NAME}: lay out `projects` one name per line")
        found.append((i, match))
    return found


def _verified(edited: str, expected: list[str]) -> str:
    got = parse_registry(edited)
    if sorted(got) != sorted(expected):
        raise RegistryError(f"{REGISTRY_NAME}: the edit did not take effect")
    return edited


def register_text(text: str, *names: str, before: frozenset[str] = frozenset()) -> str:
    """`projects.toml` text with each of `names` added to `projects`.

    Appended at the end of the array -- before the first name in `before`, when one is
    there, so a `NOT_PROJECTS` reference checkout stays last. A name
    already registered is skipped, so a re-run changes nothing; comments and blank lines
    are kept. A file with no `projects` gets one, ahead of its first table.
    """
    current = parse_registry(text)
    adding = [_checked_name(n) for n in dict.fromkeys(names) if n not in current]
    if not adding:
        return text
    lines = _spread(text.splitlines(keepends=True))
    span = _array_span(lines)
    if span is None:
        _add_array(lines, adding)
    else:
        _add_entries(lines, span, adding, before)
    return _verified("".join(lines), current + adding)


def _add_array(lines: list[str], adding: list[str]) -> None:
    """Give `lines` a `projects` array holding `adding`, ahead of the first table."""
    block = ["projects = [\n", *(f'{_DEFAULT_INDENT}"{n}",\n' for n in adding), "]\n"]
    header = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), None)
    if header is not None:
        lines[header:header] = [*block, "\n"]
        return
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines += (["\n"] if lines else []) + block


def _add_entries(
    lines: list[str], span: tuple[int, int], adding: list[str], before: frozenset[str]
) -> None:
    """Insert `adding` into the array at `span`: before the first name in `before`, else
    at the end, giving the last name the comma it then needs."""
    open_at, close_at = span
    entries = _entries(lines, open_at, close_at)
    indent = entries[0][1]["indent"] if entries else _DEFAULT_INDENT
    at = next((i for i, m in entries if m["name"] in before), close_at)
    if at == close_at and entries:
        last, match = entries[-1]
        if not match["comma"]:
            comment = f"  {match['comment']}" if match["comment"] else ""
            lines[last] = f'{match["indent"]}"{match["name"]}",{comment}\n'
    lines[at:at] = [f'{indent}"{n}",\n' for n in adding]


def unregister_text(text: str, *names: str) -> str:
    """`projects.toml` text with each of `names` dropped from `projects`.

    The inverse of `register_text`: a name not registered is skipped, so a re-run
    changes nothing, and every other line -- a comment above a dropped name included --
    is kept, since whose comment it was is not something a line edit can tell.
    """
    current = parse_registry(text)
    dropping = {n for n in names if n in current}
    if not dropping:
        return text
    lines = _spread(text.splitlines(keepends=True))
    span = _array_span(lines)
    if span is None:  # parse_registry found names, so the array is there
        raise RegistryError(f"{REGISTRY_NAME}: cannot find the `projects` array to edit")
    gone = {i for i, m in _entries(lines, *span) if m["name"] in dropping}
    edited = "".join(line for i, line in enumerate(lines) if i not in gone)
    return _verified(edited, [n for n in current if n not in dropping])


def _write_edit(path: Path, edit: Callable[[str], str]) -> bool:
    """Apply `edit` to the registry at `path`; whether the file changed.

    A missing file is left missing: a file created here holding only the new name would
    read as a registry of one, and `all_projects` names the missing one instead.
    """
    text = _read(path)
    if text is None:
        return False
    edited = edit(text)
    if edited == text:
        return False
    path.write_text(edited, encoding="utf-8", newline="\n")
    return True


def register_file(path: Path, *names: str, before: frozenset[str] = frozenset()) -> bool:
    """`register_text` applied to the file at `path`; whether it was written."""
    return _write_edit(path, lambda text: register_text(text, *names, before=before))


def unregister_file(path: Path, *names: str) -> bool:
    """`unregister_text` applied to the file at `path`; whether it was written."""
    return _write_edit(path, lambda text: unregister_text(text, *names))


# --- end of writing the registry -------------------------------------------------------
