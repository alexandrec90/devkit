#!/usr/bin/env python3
"""The VHDX half of `docker-maint.py prune`: compacting Docker's disk, and whether it can.

`docker image prune` frees space *inside* the WSL VM; the VHDX holding it is a
dynamically-expanding file that never shrinks on its own, so only `Optimize-VHD` hands
the space back to Windows -- and it needs an elevated token and exclusive access, which
means `wsl --shutdown` first.

The scheduled prune is registered at the least privilege, so it can never compact: it
used to stop every container anyway, fail `sc start` (exit 5) and `Optimize-VHD`, and
still print PRUNE COMPLETE with exit 0. `is_elevated` is what lets the prune say which
half it can run before it costs anything.

Stdlib only. Tested in `tests/test_docker_maint.py`, beside the prune that calls it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

# Newest layout first; older Docker Desktop kept the disk under `wsl/data`.
LAYOUTS = (
    "AppData/Local/Docker/wsl/disk/docker_data.vhdx",
    "AppData/Local/Docker/wsl/data/ext4.vhdx",
)


def is_elevated() -> bool:
    """Whether this process holds an elevated token. True off Windows: no VHDX there."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def find(home: Path) -> Path | None:
    """Docker's disk under `home`, or None when no known layout has one."""
    return next((home / rel for rel in LAYOUTS if (home / rel).is_file()), None)


def compact(run: Callable[..., int], stop: Callable[[], None]) -> str:
    """Stop Docker and compact its disk: `compacted`, `failed`, or `none` when there is none."""
    print("\n  Stopping Docker for exclusive VHDX access ...")
    stop()
    vhdx = find(Path.home())
    if vhdx is None:
        print("  [skip] No Docker WSL VHDX found at the expected paths.")
        return "none"
    print(f"  Compacting {vhdx}")
    command = f"Optimize-VHD -Path '{vhdx}' -Mode Full"
    if run(["powershell", "-NoProfile", "-Command", command], timeout=900):
        print("  [warn] Optimize-VHD failed -- it needs an ELEVATED shell and Hyper-V tools.")
        print("         The prune above still freed space inside the VM.")
        return "failed"
    return "compacted"
