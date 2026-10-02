"""devkit's commit stage rewrites `.agents/skills/` whenever `.claude/skills/` changes.

`scripts/sync-codex-context.py` writes the mirror, and nothing ran it when 716bb80 edited
`supervise-fix-pass`: Codex read the old skill on `main` until a fixer happened to run the
script for an unrelated edit (0927-5). The check that compares the mirror with its source
is vendored, in `scripts/hooks/tests/test_sync_codex_context.py`, so every project's suite
holds its own mirror to it; this file holds devkit's commit-time generator.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_commit_stage_rewrites_the_mirror_on_every_skill_change():
    """The check alone was first met in CI: #438 edited `triage-harness` and took a
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
