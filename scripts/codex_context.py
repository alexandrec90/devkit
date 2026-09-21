#!/usr/bin/env python3
"""Whether this machine's Codex is wired to read a repo's instruction tier at all.

`workspace-status.py`'s `toolchain_lines` reports the workstation prerequisites nothing
else mentions -- `uv` on PATH, a git identity, a VS Code extension -- and this is the
fourth, found the way the third was: by hand, from a claim that had quietly stopped
being true.

**Two machine-local files carry devkit's whole Codex story, and neither is in any
repository.** `README.md` states both as mitigations already in place:

| file | what it has to carry | what its absence costs |
| --- | --- | --- |
| `<CODEX_HOME>/config.toml` | `project_doc_fallback_filenames = ["CLAUDE.md"]` | Codex reads no project instructions at all |
| `<CODEX_HOME>/AGENTS.md` | the rule bridge: inspect `.claude/rules/` frontmatter, read the unscoped rules and the scoped ones whose `paths` match | Codex gets none of `.claude/rules/` |

Neither has a symptom. A Codex session missing both starts, answers, and edits code --
it just does so having read none of the policy every Claude session in the same repo is
held to, and the two runtimes then disagree about what the project requires in a way
that reads as one of them being wrong rather than as a machine being unconfigured.

That is why this is reported rather than tested. The claim is unverifiable from inside
the repo by construction -- `tests/test_doc_claims.py` can check that every path README
cites exists, and cannot check the contents of a file on somebody's workstation -- and
on 2026-09-18 a session found `~/.codex/AGENTS.md` holding only the no-hooks policy,
with the bridge paragraph gone and README still asserting it. Nothing had changed in
git, so nothing anywhere was red.

**Never writes, and silent when Codex is not set up.** A machine with no `CODEX_HOME`
directory is a machine that does not run Codex, and a prerequisite reported to somebody
who does not have the tool is the line that teaches them to skip the rest. Restoring
either file is the operator's edit to their own home directory, so the report names the
file and what belongs in it.

Tested in `tests/test_codex_context.py`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import worktree_tiers

# The setting that points Codex at `CLAUDE.md`, and the substring that says the rule
# bridge is present. `.claude/rules` rather than a sentence of the paragraph: the bridge
# is prose an operator may reword, and what cannot be reworded away is the directory it
# has to name. A file that never mentions it cannot be telling Codex to read it.
FALLBACK_SETTING = "project_doc_fallback_filenames"
BRIDGE_MARKER = ".claude/rules"

CONFIG_NAME = "config.toml"
AGENTS_NAME = "AGENTS.md"

# Same list and the same reason as `vscode_extensions.UNREADABLE`: an absent or
# unreadable file, and one whose bytes are not text. Anything else is a bug here.
UNREADABLE = (OSError, ValueError, UnicodeDecodeError)


def codex_home(env: dict | None = None) -> Path:
    """`<CODEX_HOME>`, or `~/.codex`. Resolved through `worktree_tiers` rather than
    spelled again here -- that module already owns where Codex keeps its state, for the
    worktree tier, and two answers to "where is CODEX_HOME" is one too many.

    `home_of` answers `None` only for a tier anchored at a *checkout* rather than at a
    home directory, which the Codex tier is not -- so the fallback is that tier's own
    declared default. Written as a fallback rather than an assert because this signature
    should not be an optional every caller re-checks, and because an assertion is a
    claim the structural gate counts as somebody giving up.
    """
    codex = next(tier for tier in worktree_tiers.TIERS if tier.agent == "codex")
    home = worktree_tiers.home_of(codex, dict(os.environ) if env is None else env)
    return home or Path(codex.home_default).expanduser()


def _carries(path: Path, marker: str) -> bool:
    """Whether `path` contains `marker`. A file that is absent or cannot be read answers
    False rather than "cannot tell": the whole `CODEX_HOME` directory being absent is
    the "cannot tell" case and is handled one level up, and inside a directory that does
    exist, a setting nobody can read is a setting that is not in effect."""
    try:
        return marker in path.read_text(encoding="utf-8")
    except UNREADABLE:
        return False


def report_lines(home: Path | None = None) -> list[str]:
    """One line per machine-local Codex file that is absent or has lost its contract.

    [] on a machine with no `CODEX_HOME` directory, which is the ordinary state for a
    workstation that runs only Claude Code. A list rather than a string so it
    concatenates with `toolchain_lines`, which owns the `[workspace]` prefix.
    """
    root = home or codex_home()
    if not root.is_dir():
        return []
    lines = []
    if not _carries(root / CONFIG_NAME, FALLBACK_SETTING):
        lines.append(
            f"{root / CONFIG_NAME} does not set {FALLBACK_SETTING} -- Codex then reads no "
            f"project instructions at all, because devkit repos carry CLAUDE.md and no "
            f'AGENTS.md (fix: add {FALLBACK_SETTING} = ["CLAUDE.md"])'
        )
    if not _carries(root / AGENTS_NAME, BRIDGE_MARKER):
        lines.append(
            f"{root / AGENTS_NAME} carries no rule bridge -- Codex does not discover "
            f"{BRIDGE_MARKER}/, so it runs on none of the engineering policy every Claude "
            f"session in the same repo is held to, and README.md says this bridge is in "
            f"place (fix: tell it to read each rule's frontmatter, take the unscoped rules "
            f"in full and the scoped ones whose `paths` match the files being edited)"
        )
    return lines
