"""devkit's committed `.agents/skills/` is `.claude/skills/`, byte for byte.

`scripts/sync-codex-context.py` writes the mirror, and nothing ran it when 716bb80 edited
`supervise-fix-pass`: Codex read the old skill on `main` until a fixer happened to run the
script for an unrelated edit (0927-5). A generated file needs a check that compares it
with its source, so this is that check; the remedy it names is the generator.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import load_script

sync = load_script("scripts/sync-codex-context.py")

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".claude" / "skills"
MIRROR = ROOT / ".agents" / "skills"
REMEDY = "run python scripts/sync-codex-context.py"


def test_every_skill_file_is_mirrored_with_the_same_bytes():
    stale = sorted(
        rel.as_posix()
        for rel in sync.relative_files(SOURCE)
        if not (MIRROR / rel).is_file()
        or (MIRROR / rel).read_bytes() != (SOURCE / rel).read_bytes()
    )
    assert stale == [], f"stale in .agents/skills/: {stale} -- {REMEDY}"


def test_the_commit_stage_rewrites_the_mirror_on_every_skill_change():
    """The check above alone was first met in CI: #438 edited `triage-harness` and took a
    fixer. The generator runs at commit time, executable, so `language: script` can."""
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook = config.split("- id: codex-skill-mirror", 1)[1].split("\n\n", 1)[0]
    assert "entry: scripts/sync-codex-context.py" in hook
    assert r"files: ^\.claude/skills/" in hook and "pass_filenames: false" in hook
    staged = subprocess.run(
        ["git", "ls-files", "-s", "scripts/sync-codex-context.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert staged.startswith("100755"), f"not executable in git: {staged!r}"


def test_the_mirror_holds_nothing_the_skills_do_not():
    orphans = sorted(
        rel.as_posix() for rel in sync.relative_files(MIRROR) - sync.relative_files(SOURCE)
    )
    assert orphans == [], f"orphaned in .agents/skills/: {orphans} -- {REMEDY}"
