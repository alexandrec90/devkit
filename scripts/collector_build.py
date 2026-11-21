#!/usr/bin/env python3
"""Stage a collector's build context from commits rather than from the checkouts on disk.

`collectors.redeploy` builds a container whose image copies in a sibling checkout
(`buildsFrom`, ibkr_trader's `COPY data-lake`). Built from the checkouts as they stand,
the image takes the sibling at *its* HEAD -- data-lake's main -- while the consumer
that owns the schema is validated against the commit its own PR gate pins
(`worktree_env.pinned_ref`). 2026-10-11 and again 2026-10-12 01:00Z (8e3c4a67): the
redeploy baked data-lake c0aa963 into ibkr_trader's image before ibkr_trader had merged
the migration its models needed, and four jobs failed with `UndefinedColumn` until it
did. So a target with a pinned sibling is built from a staged context instead: each
checkout exported at the commit it is meant to run (`git archive`, so no worktree is
registered and no checkout hook runs), laid out as the siblings sit in the workspace,
and handed to compose by an override file that replaces only `build.context`. Every
other path in the project's compose file still resolves against its own checkout.

Stdlib only, and nothing here spawns: `collectors.Git` runs git, `collectors.Docker`
runs compose.
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

# The names compose looks for, in its own order; the override is merged on top when it
# exists. Naming any file with `-f` turns that lookup off, so both are named explicitly.
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
COMPOSE_OVERRIDES = (
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
)
# Written into the staged context. JSON is YAML, so no YAML writer is needed.
OVERRIDE_NAME = "compose.staged-context.yaml"

# `(checkout, rev, destination)` -> "" once `destination` holds `rev`'s tree, else why not.
Export = Callable[[Path, str, Path], str]


def compose_files(checkout: Path) -> list[Path]:
    """The compose files `docker compose` would read in `checkout`: its first base file,
    then its first override file; [] when it has no base file."""
    base = next((checkout / n for n in COMPOSE_FILES if (checkout / n).is_file()), None)
    if base is None:
        return []
    extra = next((checkout / n for n in COMPOSE_OVERRIDES if (checkout / n).is_file()), None)
    return [base, extra] if extra else [base]


def override_text(service: str, context: Path) -> str:
    """A compose file that moves `service`'s build context to `context` and nothing else:
    its `dockerfile`, relative to the context, finds the same file in the staged copy."""
    return json.dumps({"services": {service: {"build": {"context": str(context)}}}}, indent=2)


def unpack(archive: Path, dest: Path) -> str:
    """Extract the tar `git archive` wrote into `dest`; "" on success, else why not. The
    `data` filter refuses a member that would land outside `dest`."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
    except (OSError, tarfile.TarError) as exc:
        return f"could not unpack {archive.name}: {exc}"
    return ""


def stage(
    checkout: Path,
    service: str,
    sources: Sequence[tuple[Path, str]],
    into: Path,
    export: Export,
) -> tuple[list[Path], str]:
    """Export each `(checkout, rev)` in `sources` to `into/<its directory name>` and write
    the override pointing `service`'s build at `into`. Returns the compose files to pass
    to `docker compose -f`, or `([], why)` when the context could not be staged."""
    files = compose_files(checkout)
    if not files:
        return [], f"{checkout.name} has no compose file to build from"
    for root, rev in sources:
        why = export(root, rev, into / root.name)
        if why:
            return [], f"could not stage {root.name} at {rev[:12]}: {why}"
    override = into / OVERRIDE_NAME
    try:
        override.write_text(override_text(service, into), encoding="utf-8")
    except OSError as exc:
        return [], f"could not write {override.name}: {exc}"
    return [*files, override], ""
