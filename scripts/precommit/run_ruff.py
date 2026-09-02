#!/usr/bin/env python3
"""Run `ruff` through the project's own interpreter, for pre-commit.

The two ruff hooks were `language: system` with a bare `ruff` entry, which resolves
against `PATH`. That is correct for a human in an activated shell and wrong for every
other caller: **an agent's shell is never activated**, so `git commit` from one failed
with ``Executable `ruff` not found`` and refused the commit — a gate blocking on the
absence of a tool that was installed three directories away.

`language: system` is still right, and switching these hooks to a pinned
`ruff-pre-commit` rev would be the wrong repair: `lint-fix.py` runs the same fixers on
every agent edit, and the point of that pairing is that a human commit and an agent
commit converge on the same formatting. Two independently pinned ruffs would drift, and
the drift would show up as a file that one of them reformats on every pass.

So the entry point moves rather than the version: this resolves the interpreter that
holds the project's ruff and spawns `-m ruff`, which reaches the same executable `ruff`
would have. One ruff, no `PATH` dependency.

Exits with ruff's own code, and with 1 plus a readable line when ruff genuinely is not
installed anywhere — the one case where the old message was telling the truth.

Stdlib only. Tested in `tests/test_run_ruff_hook.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    import project_python
except ImportError:  # pragma: no cover - a checkout missing its own helper
    project_python = None  # type: ignore[assignment]


def ruff_command(args: list[str], root: Path = REPO_ROOT) -> list[str]:
    """The argv to spawn. Falls back to a bare `ruff` when the helper is absent.

    The fallback reproduces exactly the old behaviour rather than inventing a new one:
    if this file is somehow shipped without `project_python.py`, a `PATH` lookup is
    still better than refusing to run.
    """
    if project_python is None:
        return ["ruff", *args]
    return project_python.tool_command(root, "ruff", args)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        return subprocess.run(ruff_command(args), cwd=REPO_ROOT, check=False).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"run_ruff: could not start ruff ({exc}). It is declared in pyproject.toml — "
            f"`uv sync --all-extras --all-groups` installs it.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
