#!/usr/bin/env python3
"""What no change may land with, checked in the seconds before it is pushed.

In the direct flow nothing gates a merge and every check runs after it, so a broken
change is fixed forward. Three breakages are worth stopping first, because each leaves a
file nothing can read and the session that wrote it is still here to fix it: Python that
does not parse, TOML or JSON that does not parse (`.devkit.toml` among them), and a
conflict marker left in a file.

**It must never be in the way of a change that is fine.** It installs nothing, runs
nothing, asks the network nothing, and reads only the files the change touches. Any doubt
passes: a file it cannot read, one that was already broken on the base, a syntax newer
than the interpreter running this, JSON that may carry comments.

`land.py` runs it for a session and `ship_intent.py` for an intent, so both roads into
the default branch hold the same thing. Vendored from devkit (`sync-devkit.py`'s
MANIFEST), stdlib only. Tested in `scripts/hooks/tests/test_land_check.py`.

**Version skew** is the other half, and it holds rather than refuses: a project pins a
devkit release, so a `.devkit.toml` key that only devkit's `main` knows reddens the
project's vendored `test_manifest_has_no_unknown_keys` the moment it merges, and stays
red until a release carrying it is adopted (2026-11-26: `[delivery] e2e`, five projects).
`skew` names the keys a change adds that the project's own vendored `harness_config.py`
does not know; `land.py` opens such a PR held, and the fix pass releases it once an
adopted release knows them (`fix_release.release_skew_holds`). Any doubt reads as no skew.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import sys
import tomllib
import types
import warnings
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

# A bigger file is data, not something a person resolved a conflict in or wrote by hand.
MAX_BYTES = 1_000_000

# All three of a conflict's lines, in order, each alone at the start of its line: prose
# and tests that mention one are not a conflict.
CONFLICT = re.compile(
    r"^<{7} [^\n]*\n(?:(?!^>{7} )[^\n]*\n)*?={7}\r?\n(?:(?!^<{7} )[^\n]*\n)*?^>{7} [^\n]*$", re.M
)

# JSON by name, comments by habit: each of these tools reads JSON with comments.
JSONC = re.compile(
    r"(^|/)(\.vscode|\.devcontainer)/|(^|/)[tj]sconfig[^/]*\.json$|\.code-workspace$"
)

BaseText = Callable[[str], "str | None"]


def python_version(root: Path) -> tuple[int, int]:
    """`[python] version` in `.devkit.toml`; `(0, 0)` when nothing readable says."""
    try:
        settings = tomllib.loads((root / ".devkit.toml").read_text(encoding="utf-8"))
        major, minor = str(settings["python"]["version"]).split(".")[:2]
        return int(major), int(minor)
    except (OSError, ValueError, KeyError, TypeError):  # TOMLDecodeError is a ValueError
        return 0, 0


def python_problem(text: str) -> str:
    if "\0" in text:
        return ""  # data, not source; a ValueError or a SyntaxError by the patch release
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape is the linter's
            ast.parse(text)
    except SyntaxError as broken:
        return f"does not parse: {broken.msg} (line {broken.lineno})"
    except (ValueError, RecursionError, MemoryError):
        return ""  # a file too deep to read: not this check's to judge
    return ""


def toml_problem(text: str) -> str:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as broken:
        return f"is not valid TOML: {broken}"
    return ""


def json_problem(text: str) -> str:
    try:
        json.loads(text)
    except ValueError as broken:
        return f"is not valid JSON: {broken}"
    return ""


def problem_in(path: str, text: str, judges_python: bool = True) -> str:
    """What is wrong with `text`, the contents of `path`, or ""."""
    text = text.removeprefix("\N{BYTE ORDER MARK}")
    if CONFLICT.search(text):
        return "has an unresolved merge conflict"
    if path.endswith(".py"):
        return python_problem(text) if judges_python else ""
    if path.endswith(".toml"):
        return toml_problem(text)
    if path.endswith(".json") and not JSONC.search(path):
        return json_problem(text) if text.strip() else ""
    return ""


def read(root: Path, path: str) -> str | None:
    """`path` as text; None for one that is deleted, large, binary or not UTF-8."""
    file = root / path
    try:
        if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_BYTES:
            return None
        return file.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _nothing(_path: str) -> str | None:
    return None


def problems(root: Path, paths: Iterable[str], base_text: BaseText = _nothing) -> list[str]:
    """`path: what is wrong` for each changed file that must not land.

    `base_text` reads a path as the base has it: a file broken the same way there is not
    this change's doing, and a fixture that is broken on purpose is exactly that.
    """
    judges_python = python_version(root) <= sys.version_info[:2]
    found = []
    for path in sorted(set(paths)):
        text = read(root, path)
        if text is None or not (why := problem_in(path, text, judges_python)):
            continue
        before = base_text(path)
        if before is not None and problem_in(path, before, judges_python):
            continue
        found.append(f"{path}: {why}")
    return found


# --- version skew ---------------------------------------------------------------------

HARNESS_CONFIG = "scripts/hooks/harness_config.py"
# The label a skew hold carries beside `hold`, so the fix pass releases its own holds and
# never a person's.
SKEW_LABEL = "devkit-skew"
SKEW_LINE = "Devkit skew:"
SKEW_TAIL = " -- waits for the devkit release that adds them."
# The tables `harness_config.from_dict` reads by hand, renaming their keys; every other
# table maps 1:1 onto its dataclass. The same split as `test_repo_contract._toml_schema`,
# which a test holds this to.
HAND_MAPPED = {
    "project": frozenset({"env_prefix"}),
    "paths": frozenset({"app", "tests", "unit_tests"}),
}
DATACLASS_TABLES = {
    "db": "DbConfig",
    "frontend": "FrontendConfig",
    "desktop": "DesktopConfig",
    "python": "PythonConfig",
    "bash": "BashConfig",
    "docker": "DockerConfig",
    "worktree": "WorktreeConfig",
    "test_contract": "TestContractConfig",
    "structure": "StructureConfig",
    "delivery": "DeliveryConfig",
    "schedule": "ScheduleConfig",
}

Schema = Mapping[str, frozenset[str]]


def schema_of(module: object) -> dict[str, frozenset[str]]:
    """Legal keys per `.devkit.toml` table, as a loaded `harness_config` module has them.
    A table whose dataclass the module lacks is a table that release does not know."""
    schema = dict(HAND_MAPPED)
    for table, name in DATACLASS_TABLES.items():
        cls = getattr(module, name, None)
        if cls is not None and dataclasses.is_dataclass(cls):
            schema[table] = frozenset(f.name for f in dataclasses.fields(cls))
    return schema


def schema_from_source(source: str) -> dict[str, frozenset[str]]:
    """`schema_of` a `harness_config.py` given as text: a project's own copy, from disk or
    from a ref. Registered in `sys.modules` before it runs (scripts/CLAUDE.md), and
    dropped after."""
    name = "_land_check_harness_config"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(source, HARNESS_CONFIG, "exec"), module.__dict__)  # noqa: S102 - the project's own vendored file
        return schema_of(module)
    finally:
        sys.modules.pop(name, None)


def unknown_keys(settings: Mapping[str, object], schema: Schema) -> list[str]:
    """`[table] key` for each key `schema` does not know, `[table]` for a whole table."""
    found = []
    for table, section in settings.items():
        if table not in schema:
            found.append(f"[{table}]")
        elif isinstance(section, Mapping):
            found.extend(f"[{table}] {key}" for key in section if key not in schema[table])
    return sorted(found)


def new_unknown_keys(
    head: Mapping[str, object], base: Mapping[str, object], schema: Schema
) -> list[str]:
    """The unknown keys `head` adds over `base`. One the base already carries is that
    branch's red, not this change's, and holding every change behind it would stop the
    project landing anything."""
    return sorted(set(unknown_keys(head, schema)) - set(unknown_keys(base, schema)))


def skew(root: Path, base_settings: Mapping[str, object]) -> list[str]:
    """The `.devkit.toml` keys this checkout adds that its own vendored `harness_config.py`
    does not know. Fail-open: no copy, an unreadable one, or any error is no skew."""
    try:
        source = (root / HARNESS_CONFIG).read_text(encoding="utf-8")
        head = tomllib.loads((root / ".devkit.toml").read_text(encoding="utf-8"))
        return new_unknown_keys(head, base_settings, schema_from_source(source))
    except Exception:  # fail-open by contract: a doubt never holds a change
        return []


def skew_line(keys: Iterable[str]) -> str:
    return f"{SKEW_LINE} {', '.join(keys)}{SKEW_TAIL}"


def with_skew_line(body: str, keys: list[str]) -> str:
    """`body` ending in the skew line, once."""
    if not keys or named_keys(body):
        return body
    return f"{body.rstrip()}\n\n{skew_line(keys)}\n".lstrip()


def named_keys(body: str) -> list[str]:
    """The keys a skew line in `body` names; nothing when it has none."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(SKEW_LINE):
            named = line.removeprefix(SKEW_LINE).removesuffix(SKEW_TAIL.strip()).strip()
            return [key.strip() for key in named.split(",") if key.strip()]
    return []


def known(key: str, schema: Schema) -> bool:
    """Whether `schema` knows `[table] key`, or `[table]`."""
    match = re.fullmatch(r"\[([^\]]+)\](?:\s+(\S+))?", key)
    if not match:
        return False
    table, name = match.groups()
    return table in schema and (name is None or name in schema[table])


def releasable(body: str, schema: Schema) -> bool:
    """Whether a skew-held PR's keys are all known to `schema` now. Pure. A body naming
    none stays held: there is nothing to say the release arrived."""
    keys = named_keys(body)
    return bool(keys) and all(known(key, schema) for key in keys)
