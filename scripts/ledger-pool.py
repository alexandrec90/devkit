#!/usr/bin/env python3
"""Share this machine's harness-events ledger with another machine's, through one folder.

The ledger is one shard per machine (`scripts/hooks/harness_events.py`) so that two
machines can write into one directory without a sync tool ever seeing a conflict. What
was missing is the directory: the shards lived in the devkit checkout's `logs/`, which
holds every job's per-machine artifact too and so cannot be shared. A second machine ran
the scheduled jobs with its defects on a ledger this one never read, and the fix pass
here took that for an empty backlog.

    python scripts/ledger-pool.py                 # where this machine's shard lives
    python scripts/ledger-pool.py --set <dir>     # pool it: a folder every machine syncs
    python scripts/ledger-pool.py --unset         # back to the checkout's own logs/

Run `--set` with the same folder on every machine -- a OneDrive or Dropbox folder, a
network share. It writes `harness_events.POOL_POINTER` and moves this machine's shard
into the folder, so its history arrives with it; each machine only ever appends to its
own file there. The unsharded legacy `harness-events.log` stays behind: every machine
has one under that name, and the read half keeps reading the local one.

Exit 0 when the ledger is where it should be, 1 when a pool is named but unreachable --
rows then go to the checkout's `logs/` and no other machine sees them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import harness_events

UNREACHABLE = 1


def move_shard(source: Path, dest: Path) -> int:
    """Append `source` to `dest` and delete it; the number of rows moved.

    Appended rather than renamed, because `dest` may already hold rows: a pool set
    twice, or rows written locally while it was offline. Nothing is lost either way --
    `source` goes only once its bytes are in `dest`.
    """
    if source == dest or not source.is_file():
        return 0
    data = source.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    held = dest.read_bytes() if dest.is_file() else b""
    joint = b"\n" if held and not held.endswith(b"\n") and data else b""
    with dest.open("ab") as out:
        out.write(joint + data)
    source.unlink()
    return len(data.splitlines())


def own_shards(base: Path) -> list[Path]:
    """Every place this machine's shard can be: the checkout's `logs/`, and the pool."""
    name = harness_events.ledger_name()
    pool = harness_events.pool_dir(base)
    places = [base / harness_events.LEDGER_DIR / name]
    return places + ([pool / name] if pool is not None else [])


def set_pool(base: Path, pool: Path) -> str:
    """Point `base`'s ledger at `pool` and move this machine's shard into it."""
    pool = pool.expanduser().resolve()
    pool.mkdir(parents=True, exist_ok=True)
    dest = pool / harness_events.ledger_name()
    sources = own_shards(base)
    pointer = base / harness_events.POOL_POINTER
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(f"{pool}\n", encoding="utf-8")
    moved = sum(move_shard(source, dest) for source in sources)
    return f"ledger pooled at {pool}: {moved} row(s) of this machine's moved into {dest.name}"


def unset_pool(base: Path) -> str:
    """Stop pooling: this machine's shard comes back to the checkout's `logs/`."""
    dest = base / harness_events.LEDGER_DIR / harness_events.ledger_name()
    sources = own_shards(base)
    (base / harness_events.POOL_POINTER).unlink(missing_ok=True)
    moved = sum(move_shard(source, dest) for source in sources)
    return f"ledger not pooled: {moved} row(s) moved back into {dest}"


def status(base: Path) -> tuple[int, str]:
    """`(exit code, line)` saying where this machine's rows go and whose it reads."""
    named = harness_events.pool_named(base)
    if named is None:
        return 0, (
            f"ledger not pooled: rows go to {base / harness_events.LEDGER_DIR}, and another "
            "machine's are not read here (share one with --set <dir> on each machine)"
        )
    if harness_events.pool_dir(base) is None:
        return UNREACHABLE, (
            f"ledger pool {named} unreachable: rows go to the checkout's logs/ until it is "
            "back, and no other machine sees them"
        )
    hosts = sorted(
        path.stem.removeprefix(f"{harness_events.LEDGER_STEM}-")
        for path in named.glob(harness_events.LEDGER_GLOB)
    )
    return 0, f"ledger pooled at {named}: shard(s) from {', '.join(hosts) or 'no machine yet'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--set", type=Path, metavar="DIR", help="pool the ledger in DIR")
    which.add_argument("--unset", action="store_true", help="stop pooling")
    parser.add_argument("--root", type=Path, help="the devkit checkout (default: resolved)")
    args = parser.parse_args(argv)
    base = args.root or harness_events.ledger_root()
    if base is None:
        print("ledger-pool: no devkit checkout to hold a ledger here", file=sys.stderr)
        return 2
    if args.set is not None:
        print(set_pool(base, args.set))
    elif args.unset:
        print(unset_pool(base))
    code, line = status(base)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
