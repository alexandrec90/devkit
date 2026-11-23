#!/usr/bin/env python3
"""Which delivery flow a project is in: `[delivery] flow` in its `.devkit.toml`.

`direct` is every project's flow by the owner's decision, and what the template writes:
a PR merges as it opens -- Claude Code's built-in `/ship`'s unlabelled, `ship-pass.py`'s
labelled `merge-now` -- and every check runs after the merge and repairs what it finds.
`gated` is the fix pass running the commit-stage fixers and a PR the gate decides, kept
for code that moves real money.

**Anything that cannot be read is `gated`.** A missing file, a broken one, a misspelt
value: the direct flow takes a gate away, so it is only ever entered on a clear yes.
`harness_config.DeliveryConfig` documents the field in the manifest's schema; this reads
it on its own because it is asked once per project on a two-minute job, and because the
checkout's `harness_config.py` is a vendored copy that may predate the field.

Devkit-only and not vendored: everything that asks is the fix pass or the ship task, which
run from devkit against every project's checkout. Tested in `tests/test_delivery.py`.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path

MANIFEST = ".devkit.toml"
GATED = "gated"
DIRECT = "direct"


def flow(root: Path) -> str:
    """The flow the project at `root` is in: `DIRECT` when its manifest says so exactly,
    else `GATED`."""
    try:
        with (root / MANIFEST).open("rb") as manifest:
            data = tomllib.load(manifest)
    except (OSError, ValueError):  # `TOMLDecodeError` and a bad encoding are ValueErrors
        return GATED
    table = data.get("delivery")
    value = table.get("flow") if isinstance(table, dict) else None
    return DIRECT if value == DIRECT else GATED


def is_direct(root: Path) -> bool:
    """Whether the project at `root` is in the direct flow."""
    return flow(root) == DIRECT


def direct_projects(root: Path, projects: Iterable[str]) -> frozenset[str]:
    """The names in `projects` whose checkout under `root` is in the direct flow."""
    return frozenset(name for name in projects if is_direct(root / name))
