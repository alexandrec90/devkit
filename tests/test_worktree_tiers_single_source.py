"""One place spells a worktree root's directory name, and this is what holds it there.

`scripts/hooks/worktree_tiers.py` owns where a worktree is cut on this machine: the two
agent-CLI tiers in `TIERS`, devkit's own box tier in `BOX_TIER`, all three in
`ALL_TIERS`. That ownership was not always real. The agent tiers moved there when a
fourth module got the nested parent-walk wrong, but the box tier's directory stayed a
bare `".worktrees"` in six modules at once -- `sweep.py`, `reclaim.py`, the vendored
`stop_session.py`, `sync-codex-hooks.py`, `structure_check.py` and `untested_symbols.py`
-- each with a comment explaining why *its* copy had to exist, and each explanation true
when it was written and stale by the time the next one was.

The cost was never a rename going wrong. It was that no module could answer "every
directory a worktree lands in", so anything needing the whole list built its own union
and got a different one: `tests/test_worktree_port.py` unioned the box tier onto `TIERS`
by hand, `instruction-budget.py` skipped `.worktrees` -- the one tier its own `rglob`
could never reach -- and so counted 22 `CLAUDE.md` files in devkit where there are 2, and
the reaper gap filed on 2026-09-16 proposed surveying one tier by name, which would have
made a seventh copy and still missed Codex's.

So: a test rather than a convention. Three narrowings, all deliberate.

**It reads production `scripts/` only.** A test is expected to spell the value it
asserts on, and a fixture path under `tmp_path / ".worktrees"` is data, not a second
source of truth.

**It bans the box tier's name and the nested tier's path, not the bare word
`worktrees`.** That word is git's own `.git/worktrees/<name>` bookkeeping directory --
`project_python.py` and `worktree_tiers.git_checkout` both walk it, and they are reading
git's layout rather than devkit's -- and it is also an ordinary plural noun in a message
to the operator. A ban that fires on those trains everyone to ignore it, which is the
one failure mode a lint-shaped test cannot survive.

**Three vendored files keep their literal, and `PINNED` holds each equal instead.**
`structure_check.py` and `untested_symbols.py` list `.worktrees` in a `TOOLING_DIRS` set
alongside `.pytest_cache`, `node_modules` and `dist` -- directories to skip while
scanning, not an answer to where boxes live, and none of the others has an owner module
either. Importing one to share a single string would add an import edge to two gates and
a line to a module already twice the `file_lines` ceiling `.devkit-structure.txt` records
for it; the `--record` escape exists for growth worth taking and a one-line import is not
it.

`sync-codex-hooks.py` is the one where the import was tried and had to come back out, so
the reason is worth keeping: `sync-devkit.py` **spawns it as a subprocess in the
consumer's tree** -- `codex_hooks_stale`, on every `--check`, which is the PR gate. A
sibling import there fails with `ModuleNotFoundError` in any checkout where the rest of
the MANIFEST has not landed, and `--check` reports that as a stale Codex artifact: a red
gate in somebody else's repo, naming the wrong cause. Four tests in
`scripts/hooks/tests/test_sync_devkit.py` catch it, which is how this was found.
"""

from __future__ import annotations

import ast
from pathlib import Path

from support import REPO_ROOT, worktree_tiers

# The module that may spell them, relative to the repo root.
OWNER = "scripts/hooks/worktree_tiers.py"

# The box tier's directory, and the nested tier's path as a script would write it. Both
# derived, so a rename in the owner cannot leave this asserting on a name nothing uses.
BOXES = worktree_tiers.BOXES_DIR_NAME
NESTED = "/".join(worktree_tiers.DEFAULT_TIER.segments)
OWNED_NAMES = frozenset({BOXES, NESTED, NESTED.replace("/", "\\")})

# The vendored files that keep a private spelling, and the module-level name each keeps
# it under. Every one is justified in this module's docstring; adding a row needs the
# same, because the row is the exemption.
PINNED = {
    "scripts/hooks/structure_check.py": "TOOLING_DIRS",
    "scripts/hooks/untested_symbols.py": "TOOLING_DIRS",
    "scripts/sync-codex-hooks.py": "BOXES_DIR_NAME",
}


def _production_scripts() -> list[Path]:
    """Every script under `scripts/`, minus the vendored tier's own test suite and the
    `PINNED` files `test_the_pinned_copies_agree_with_the_owner` covers instead."""
    return sorted(
        path
        for path in (REPO_ROOT / "scripts").rglob("*.py")
        if "__pycache__" not in path.parts
        and "tests" not in path.parts
        and path.relative_to(REPO_ROOT).as_posix() not in PINNED
    )


def _named_strings(source: str, name: str) -> set[str]:
    """Every string in the module-level assignment to `name` -- a bare constant or the
    members of a set literal, which is the difference between the three pinned files."""
    for node in ast.walk(ast.parse(source)):
        targets = getattr(node, "targets", [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return {
                element.value
                for element in ast.walk(node)
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    raise AssertionError(f"no module-level {name} found")


def _string_constants(source: str) -> list[tuple[int, str]]:
    """Every `str` constant in `source`, with its line. Docstrings included, harmlessly:
    a paragraph *about* `.worktrees/` is never a constant equal to `.worktrees`, which
    is exactly the difference between naming a directory and being the name of one."""
    return [
        (node.lineno, node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_the_names_under_ban_are_the_ones_the_owner_actually_holds():
    """Guards the test below against the owner being renamed or restructured out from
    under it, which would leave it passing while nothing owned anything."""
    assert (REPO_ROOT / OWNER).is_file()
    held = {value for _line, value in _string_constants((REPO_ROOT / OWNER).read_text("utf-8"))}
    assert BOXES in held
    assert NESTED == ".claude/worktrees"


def test_no_script_outside_the_owner_spells_a_worktree_root():
    """The property the six comments each promised on their own and could not keep.

    Fails on a *constant*, so prose is free: a module may explain `.worktrees/` at any
    length, and only assigning the string counts as a second answer to where boxes live.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line} -- {value!r}"
        for path in _production_scripts()
        if path.relative_to(REPO_ROOT).as_posix() != OWNER
        for line, value in _string_constants(path.read_text(encoding="utf-8"))
        if value in OWNED_NAMES
    ]
    assert not offenders, (
        "read the name off `worktree_tiers` instead -- it is stdlib-only and vendored, "
        "so a hook running before any virtualenv can import it: " + "; ".join(offenders)
    )


def test_the_pinned_copies_agree_with_the_owner():
    """The three vendored files spell `.worktrees` rather than import it, each for the
    reason in this module's docstring. This is what keeps a spelling from being a second
    answer: rename the box tier and these fail by name, which is the whole thing an
    import would have bought."""
    for rel, name in PINNED.items():
        held = _named_strings((REPO_ROOT / rel).read_text(encoding="utf-8"), name)
        assert BOXES in held, f"{rel}: {name} no longer carries {BOXES!r}"
        assert not held & (OWNED_NAMES - {BOXES}), (
            f"{rel}: only the box tier belongs in {name} -- the nested tier is two "
            "segments and is matched by shape, never by a directory name"
        )


def test_every_tier_reaches_the_typescript_mirror_through_one_list():
    """`ALL_TIERS` exists so "every worktree root" has an answer to import. Without it
    the only way to ask was to union `TIERS` with a constant from a workspace script
    that half the callers -- every vendored hook -- cannot import at all."""
    assert worktree_tiers.ALL_TIERS == (*worktree_tiers.TIERS, worktree_tiers.BOX_TIER)
    assert {tier.segments[-1] for tier in worktree_tiers.ALL_TIERS} == worktree_tiers.MARKER_NAMES
