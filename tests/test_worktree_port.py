"""`frontend/src/worktreePort.ts`: devkit's one TypeScript file, and the two lists it
mirrors from the Python side.

Vendored into every project whose frontend tier is on, so a wrong constant here is a
wrong port in every one of them. devkit's CI has no Node, so this reads the source as
text -- the shapes asserted on are ones the file owns, and a change to them is a
change to what the assertions parse.

Two mirrors are held here, and a third property:

- `WORKTREE_DIRS` against `worktree_tiers.TIERS` plus the box tier -- "where a worktree
  can be" has one owner on the Python side and this is the only place the TS copy is
  compared to it.
- `SLOT_SPAN` against `ports.toml`'s `registry.max_slots` -- the derived dev port only
  stays inside the frontend base's reserved span while the two agree.
- Dependency-free: the config loads this file, so its only imports are Node builtins.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from support import REPO_ROOT, devkit_ports, load_script, worktree, worktree_tiers

sync = load_script("scripts/sync-devkit.py")

SOURCE = REPO_ROOT / "frontend" / "src" / "worktreePort.ts"
TEST = REPO_ROOT / "frontend" / "src" / "worktreePort.test.ts"


def _worktree_dirs(text: str) -> set[tuple[tuple[str, ...], int]]:
    """`{(segments, depth)}` parsed off the `WORKTREE_DIRS` literal."""
    block = re.search(r"const WORKTREE_DIRS[^=]*=\s*\[(.*?)\n\]", text, re.DOTALL)
    assert block, "WORKTREE_DIRS literal not found"
    found = set()
    for segments, depth in re.findall(r"\[\[([^\]]*)\],\s*(\d+)\]", block.group(1)):
        found.add((tuple(re.findall(r"'([^']+)'", segments)), int(depth)))
    return found


def _as_marker(tier) -> tuple[tuple[str, ...], int]:
    """A Python tier in the TS list's terms.

    A nested tier is its segments at its depth. A detached one anchors on a runtime
    home the Python side reads from the environment (`CODEX_HOME`); the TS side, which
    stays dependency-free and never asks the environment, spells that home's default
    directory name as the outermost segment -- so `~/.codex` becomes `.codex`. That is
    the one place the mirror is narrower than the original, and it is deliberate.
    """
    segments = tier.segments
    if tier.detached:
        segments = (PurePosixPath(tier.home_default).name, *segments)
    return segments, tier.depth


def test_the_typescript_tier_list_mirrors_the_python_one():
    """Every tier `worktree_tiers.TIERS` names, at the same depth, plus the box tier the
    Python side keeps in `worktree.BOXES_DIR_NAME` -- and nothing else."""
    expected = {_as_marker(tier) for tier in worktree_tiers.TIERS}
    expected.add(((worktree.BOXES_DIR_NAME,), 1))
    assert _worktree_dirs(SOURCE.read_text(encoding="utf-8")) == expected


def test_the_slot_span_is_the_registrys_slot_ceiling():
    text = SOURCE.read_text(encoding="utf-8")
    match = re.search(r"export const SLOT_SPAN = (\d+)", text)
    assert match, "SLOT_SPAN literal not found"
    assert int(match.group(1)) == devkit_ports.load(REPO_ROOT).max_slots


def test_the_helper_and_its_test_stay_dependency_free():
    """The config loads the helper, so a framework import would make it un-droppable
    into half the projects it is vendored into. The test may add vitest and itself."""
    imports = re.findall(r"^import .* from '([^']+)'", SOURCE.read_text(encoding="utf-8"), re.M)
    assert imports == ["node:path"], imports
    test_imports = re.findall(r"^import .* from '([^']+)'", TEST.read_text(encoding="utf-8"), re.M)
    assert set(test_imports) == {"node:path", "vitest", "./worktreePort.ts"}, test_imports


def test_the_gated_manifest_names_exactly_these_two_files():
    """The gate and the files are one thing: a rename on either side is a file the
    pull stops delivering with nothing red."""
    assert sync.GATED_MANIFEST == {"frontend": ("worktreePort.ts", "worktreePort.test.ts")}
    assert set(sync.gated_source_paths()) == {
        SOURCE.relative_to(REPO_ROOT).as_posix(),
        TEST.relative_to(REPO_ROOT).as_posix(),
    }


def test_strict_port_is_the_documented_pairing():
    """The docstring tells the consumer to keep `strictPort: true`; the hash gives up
    collision-freedom on the strength of it. If the prose stops saying so, the trade
    the module makes is no longer written anywhere the wiring is done."""
    assert "strictPort: true" in SOURCE.read_text(encoding="utf-8")
