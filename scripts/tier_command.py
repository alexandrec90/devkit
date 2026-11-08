#!/usr/bin/env python3
"""What the medium tier runs at a commit, read before there is a tree, and where it cannot.

Off the text of `.devkit.toml` and `scripts/run-tests.py` at the commit, because the
answer decides whether a tree is cut at all. Pure, apart from a PATH lookup.

**A tree is not given a project's real `.env`.** One project's settings reach `DROP
DATABASE`, and a suite that needs a database gets none here. So such a project is not run,
rather than run and recorded red every time (`not_applicable`): photolab's conftest raises
on an unset `DATABASE_URL`, productivity-tracker's calls `pytest.fail`, and carameli's
`--all` execs into a Docker stack by the tree's own project name. A project that knows a
subset of its suite needs neither opts back in with `medium_tier`.

Tested in `tests/test_tier_command.py`.
"""

from __future__ import annotations

import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))  # the vendored tier
import e2e_results
import harness_config

# `run-tests.py`'s flag for the whole suite, which its bare run is not (`tier-pass.py`).
WHOLE_SUITE = "--all"

# A runner that drives the stack, as code: `["docker", "compose", ...]` or the command
# line. A comment that merely says "Docker" is no such thing.
DRIVES_STACK = re.compile(r"""["']docker["']\s*,\s*["']compose["']|docker[ -]compose\s+\w""")


def _manifest(toml_text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError:
        return {}


def medium_command(toml_text: str) -> str:
    """`[delivery] medium_tier` off a `.devkit.toml` body; "" for absent or unreadable.

    Off the text rather than `harness_config.load`, which reads a directory: this is
    read at a commit, before there is a tree.
    """
    delivery = _manifest(toml_text).get("delivery")
    command = delivery.get("medium_tier") if isinstance(delivery, dict) else None
    return command.strip() if isinstance(command, str) else ""


def not_applicable(toml_text: str, script: str) -> str:
    """Why a project with no `medium_tier` cannot run here, or "" when it can."""
    if harness_config.from_dict(_manifest(toml_text)).db.enabled:
        return "[db] enabled: its suite needs a database a disposable tree is not given"
    if DRIVES_STACK.search(script):
        return f"{e2e_results.MEDIUM_CONTRACT} drives the Docker stack, which no tree has"
    return ""


def suite_args(script: str) -> tuple[str, ...]:
    """What makes a `run-tests.py` with this source run its whole suite."""
    return (WHOLE_SUITE,) if WHOLE_SUITE in script else ()


def argv_for(tree: Path, command: str, python: str) -> list[str]:
    """The run: the project's command, else its `run-tests.py` over the whole suite.

    A `python` in the command means the tree's own, as a contract script runs under it.
    Anything else is found on PATH, since `npm` is `npm.cmd` here and nothing but a shell
    resolves that, and a shell is a window this job does not open.
    """
    if not command:
        script = tree / e2e_results.MEDIUM_CONTRACT
        return [python, str(script), *suite_args(script.read_text(encoding="utf-8"))]
    head, *rest = shlex.split(command)
    program = python if head in ("python", "python3") else shutil.which(head) or head
    return [program, *rest]
