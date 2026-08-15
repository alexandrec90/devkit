#!/usr/bin/env python3
"""pre-commit hook: the vendored harness in this repo matches the devkit rev it pins.

This is the drift check `scripts/sync-devkit.py --check` performs, with the awkward part
removed. That script resolves its source from `$DEVKIT_DIR`, so **on a machine with no
devkit clone it cannot run at all**: it now says so and exits 1 once the project is
stamped, rather than reporting a comparison that never happened, but a loud refusal is
still not a check. This hook needs no variable and no local clone — pre-commit has
already fetched devkit at the rev the consumer pins — so it is the one that works on a
second machine.

Run through pre-commit there is nothing to configure, because pre-commit has already
cloned devkit at the `rev` the config pins: this file *is* the source of truth for that
comparison, so the version being compared against is the one written down in
`.pre-commit-config.yaml` and updated by `pre-commit autoupdate`.

In devkit itself the comparison is vacuous (the clone and the working tree are the same
repo), so the hook reports that and exits 0 rather than pretending to verify something.

Usage (no filenames; the manifest decides what is compared):
    python scripts/precommit/check_harness_drift.py

stdlib only.
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_by_path

DEVKIT_ROOT = Path(__file__).resolve().parents[2]


def _load_sync_harness():
    """Import `scripts/sync-devkit.py` by path — the hyphen makes it unimportable."""
    return load_by_path("_sync_harness", DEVKIT_ROOT / "scripts" / "sync-devkit.py")


def is_same_repo(a: Path, b: Path) -> bool:
    """True when both paths are the same directory, resolving links."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    # pre-commit captures hook output through a pipe, so stdout takes the platform's
    # legacy codepage (cp1252 on a Windows consumer), not UTF-8. A glyph the codepage
    # cannot map raises mid-print and truncates the report to whatever was flushed
    # before it -- the header, minus the filenames that are the whole point. Markers
    # below are ASCII for that reason; this keeps prose from ever crashing the hook
    # on a codepage that is narrower still.
    if isinstance(sys.stdout, io.TextIOWrapper):
        with contextlib.suppress(OSError, LookupError):
            sys.stdout.reconfigure(errors="backslashreplace")

    # pre-commit runs hooks from the root of the repo being committed.
    repo_root = Path.cwd()

    if is_same_repo(DEVKIT_ROOT, repo_root):
        print("devkit-drift: this IS devkit — nothing to compare against. Skipped.")
        return 0

    sync = _load_sync_harness()
    drifted, missing_upstream, ok = sync.classify(DEVKIT_ROOT, repo_root, sync.MANIFEST)

    if missing_upstream:
        # devkit does not have a file its own manifest lists: an upstream packaging bug,
        # not something this repo can fix. Report it rather than counting it as in-sync.
        print(f"devkit-drift: {len(missing_upstream)} manifest file(s) missing from devkit:")
        for rel in missing_upstream:
            print(f"  ? {rel}")

    if drifted:
        print(f"devkit-drift: {len(drifted)} vendored file(s) differ from devkit:")
        for rel in drifted:
            state = "absent here" if not (repo_root / rel).exists() else "modified"
            print(f"  x {rel} ({state})")
        print(
            "\nThe vendored harness is upstream code; edit it in devkit, not here.\n"
            "  adopt upstream:        python scripts/sync-devkit.py --pull\n"
            "  send a change up:      python scripts/sync-devkit.py --push\n"
            "(both need DEVKIT_DIR pointing at a devkit checkout)"
        )
        return 1

    if missing_upstream:
        return 1

    print(f"devkit-drift: all {len(ok)} vendored files match devkit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
