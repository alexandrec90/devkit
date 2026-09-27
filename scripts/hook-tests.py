#!/usr/bin/env python3
"""Run a checkout's vendored-harness tests and write failures to a parseable artifact.

The backend for the workspace's "Test: Harness Hook Tests" task. This used to be a
task in devkit's own `.vscode/tasks.json`, a second copy in
`templates/core/dot-vscode/tasks.json.tmpl` that every generated project inherited,
and a third spelling inside carameli's `run-tests.py` as `--target=hook-tests` — three
definitions of one invocation that is byte-identical everywhere.

It is identical everywhere because the vendored tier is: `scripts/hooks/tests/` is at
the same path in every consumer (it is in `sync-devkit.py`'s `MANIFEST`), and pytest's
`testpaths` deliberately excludes it in every consumer, which is exactly why it needs a
runner of its own rather than riding along with `run-tests.py`.

**Runs with the cwd set to the chosen checkout**, not to devkit — `devkit_project.py`
invokes it that way for every DEVKIT-owned action. So the target repo is `Path.cwd()`
and nothing here may resolve a target path through `Path(__file__)`.

Usage:
    python scripts/hook-tests.py          # in the checkout to test
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

# Relative to the target checkout, never to devkit. Both are `MANIFEST` paths, so
# this is the vendoring contract rather than a convention.
HOOK_TESTS = Path("scripts") / "hooks" / "tests"
ARTIFACT = Path("logs") / "hook-test-failures.log"

# Total artifact cap. The vendored suite is small and stdlib-only, so a run that
# produces more than this is a collection error dumping the same trace repeatedly,
# not twenty real failures worth reading.
MAX_LINES = 200


def cap(text: str, limit: int = MAX_LINES) -> str:
    """Truncate to `limit` lines, saying so — pure, so it is testable without pytest."""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.strip()
    kept = lines[:limit]
    kept.append(f"... ({len(lines)} lines total, truncated)")
    return "\n".join(kept).strip()


def vendors_harness(root: Path) -> bool:
    """Whether `root` carries the vendored tier this script exists to run."""
    return (root / HOOK_TESTS).is_dir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="checkout to test (default: the cwd, which is how the task invokes it)",
    )
    args = parser.parse_args(argv)
    root = (args.root or Path.cwd()).resolve()

    if not vendors_harness(root):
        # NOT a clean skip. `.claude/rules/engineering.md` and devkit's own CLAUDE.md
        # both name the silent-skip failure — a gate that reports green having run
        # nothing — as the quietest bug in this harness, and the vendored contract
        # test exists because devkit once shipped exactly that. A checkout without
        # the tier is a real answer to give loudly, not a reason to exit 0.
        print(
            f"hook-tests: {root.name} has no {HOOK_TESTS.as_posix()} — "
            f"it does not vendor the devkit harness, so there is nothing to test here.",
            file=sys.stderr,
        )
        return 2

    cmd = [sys.executable, "-m", "pytest", HOOK_TESTS.as_posix(), "-q", "--tb=short"]
    print(f"hook-tests: {' '.join(cmd[2:])}")
    # A temp root of this run's own, as `run-tests.py` gives its suite: under the
    # machine-wide one, another session's run holding its `pytest-current` link failed a
    # green suite at pytest's exit (97d20f01).
    with tempfile.TemporaryDirectory(prefix="pytest-", ignore_cleanup_errors=True) as tmp:
        result = subprocess.run(
            [*cmd, f"--basetemp={tmp}"], cwd=root, capture_output=True, text=True
        )

    artifact = root / ARTIFACT
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if result.returncode == 0:
        # Cleared on pass, so a stale artifact never sends the next agent chasing a
        # failure that is already fixed.
        artifact.write_text("", encoding="utf-8")
        print(f"hook-tests: passed (artifact cleared: {ARTIFACT.as_posix()})")
        return 0

    body = cap(result.stdout + result.stderr)
    artifact.write_text(
        "# source: devkit scripts/hook-tests.py\n"
        f"# fix: python -m pytest {HOOK_TESTS.as_posix()} <the failing test id> --tb=long\n"
        + body
        + "\n",
        encoding="utf-8",
    )
    print(f"hook-tests: FAILED — details in {ARTIFACT.as_posix()}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
