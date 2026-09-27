#!/usr/bin/env python3
"""Retire a friction row that an open PR's detector no longer files, against that PR.

The harvest runs the default branch's detectors, so while a fix to one is in review it
keeps filing the very rows the fix exists to stop, and `session_friction.outdated` --
which asks the same detectors -- cannot retire them. The ledger then sent a sweep at
fc786188 while open #420 already suppressed it, and that fixer spent four turns
replaying the evidence through both detectors to find out (20fe4a86).

So the pass asks every open devkit PR that changes the detector the same question
`outdated` asks, with that PR's copy of it: a row it would no longer file is resolved
with the PR as its `pr=`. `fix_verify` then holds the group in flight until the PR
merges, and reopens it if the PR closes unmerged -- the ordinary life of a resolution.

A PR's detector is loaded on its own, beside the default branch's other modules. One
that cannot run that way -- it needs a sibling the PR also changed -- answers nothing,
and its rows stay open for a session to judge, as they did before this existed.

Tested in `tests/test_friction_pending.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import harness_triage as triage

# Resolved by the second insert above; `scripts/precommit/` is not a package.
from _loader import load_by_path

DETECTOR = "scripts/session_friction.py"
# What a PR's detector raises beside the default branch's modules when it needs a sibling
# the PR also changed: a name gone, a signature moved, a field renamed, a file it expects.
CANNOT_RUN_ALONE = (
    ImportError,
    SyntaxError,
    AttributeError,
    TypeError,
    KeyError,
    ValueError,
    OSError,
)

Run = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Fix:
    """An open PR that changes the detector, with its copy of it."""

    number: str
    branch: str
    source: str


def detector_fixes(gh: Run, git: Run) -> list[Fix]:
    """Every open PR in the checkout `gh` and `git` are bound to that changes `DETECTOR`.

    A PR whose head cannot be fetched or read is left out: nothing is resolved on its
    word, so a `gh` or `git` that cannot answer costs a sweep, never a wrong retirement.
    """
    listing = gh("pr", "list", "--state", "open", "--json", "number,headRefName,files")
    try:
        rows = json.loads(listing.stdout or "[]") if listing.returncode == 0 else []
    except ValueError:
        return []
    fixes = []
    for row in rows if isinstance(rows, list) else []:
        files = {str(f.get("path", "")) for f in row.get("files") or [] if isinstance(f, dict)}
        branch = str(row.get("headRefName", ""))
        if DETECTOR not in files or not branch:
            continue
        git("fetch", "--quiet", "origin", branch)
        shown = git("show", f"origin/{branch}:{DETECTOR}")
        if shown.returncode == 0 and shown.stdout:
            fixes.append(Fix(str(row.get("number", "")), branch, shown.stdout))
    return fixes


def pending(items: Iterable[triage.Item], fixes: Iterable[Fix]) -> list[tuple[str, str, str]]:
    """`(id, note, pr)` for each open friction row some PR's detector no longer files.

    The first PR to retire a row takes it: should that one close unmerged, the row is
    reopened and the next pass finds the other.
    """
    items = list(items)
    found: list[tuple[str, str, str]] = []
    taken: set[str] = set()
    for fix in fixes:
        for ref, why in outdated_on(fix, [i for i in items if i.id not in taken]):
            taken.add(ref)
            note = f"not merged yet: #{fix.number} ({fix.branch}) fixes the detector -- {why}"
            found.append((ref, note, fix.number))
    return found


def outdated_on(fix: Fix, items: list[triage.Item]) -> list[tuple[str, str]]:
    """`session_friction.outdated` as `fix`'s copy of the detector answers it."""
    name = f"session_friction_pr{fix.number or 'x'}"
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "session_friction.py"
        path.write_text(fix.source, encoding="utf-8")
        before = list(sys.path)
        try:
            module = load_by_path(name, path)
            return list(module.outdated(items))
        except CANNOT_RUN_ALONE:
            # A PR's detector that cannot run alone answers nothing (see the docstring).
            return []
        finally:
            sys.modules.pop(name, None)
            sys.path[:] = before
