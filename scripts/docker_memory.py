#!/usr/bin/env python3
"""Docker's VM memory, as the collectors pass sees it: whether the containers this
machine keeps up fit inside the VM, and -- once its engine has gone silent -- what
Windows can still say about whether running out of memory is why.

Lived on 2026-10-09. Docker's WSL VM is capped (`.wslconfig`, 4 GB on the 16 GB
workstation), and ibkr_trader's `app` and `db` ran with no `mem_limit` beside the
other collectors. Docker Desktop logged an OOM kill inside the VM at 19:32 UTC, its
engine froze for three hours, and the VM booted eight times that day. Each time the
collectors pass saw only "docker is not answering" and restarted it, and the ledger
went on to make the restart more forceful three times without anyone naming memory.

So the pass now does two things with this module:

- **While the engine answers** (`budget`): every container it keeps up -- one whose
  compose working directory is a checkout this machine runs a collector from -- must
  carry a memory limit, and the limits together must fit the VM with `HEADROOM` left
  for the engine itself. A container with a limit that outgrows it is OOM-killed alone
  and its restart policy brings it back; one without grows until the whole VM runs out
  and every stack on the machine freezes with it.
- **Once the engine is silent** (`silent_vm_evidence`): asking the engine is exactly
  what does not work then, so only Windows is read -- Docker Desktop's own log of OOM
  kills reported from inside the VM, and the VM process's working set against the
  `.wslconfig` cap. Both stay readable while the VM is frozen.

And a third, so the VM holds no more of Windows' memory than its containers use
(`reclaim_argv`): it drops the VM's file cache and compacts what that freed.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

GIB = 2**30
MIB = 2**20
# What the VM needs beyond its containers' limits: dockerd, containerd, BuildKit and
# Docker Desktop's in-VM services. A sum of limits that leaves less than this is a VM
# that can still run out with every container inside its own limit.
HEADROOM = 512 * MIB
# The working set, as a share of the `.wslconfig` cap, at which a silent engine reads
# as one starved of memory. Not 100%: the cap counts memory the kernel reserves for
# itself, so a VM that has run out shows a little under it -- 98% minutes before the
# 2026-10-09 freeze, 94% while frozen.
NEAR_CAP = 0.9
# How far back a silent pass looks for an OOM kill. The pass runs every 15 minutes,
# but a freeze can delay it (`WEDGE_CONFIRM`, a held restart), and a kill that froze
# the engine is the cause however late the pass arrives to read it.
OOM_LOOKBACK = 2 * 3600

# Docker Desktop's backend logs every request its in-VM `init` makes. `init` posts here
# once per OOM kill it sees inside the VM -- the one record of one that survives on
# the Windows side. The request row, not its `S->C` answer, so each kill counts once.
OOM_REQUEST = re.compile(r"^\[(?P<when>[^\]]+)\].*S<-C \S+ POST /analytics/track/oom-kills\s*$")
BACKEND_LOG = "com.docker.backend.exe.log"
# `{{.HostConfig.Memory}}` is the limit in bytes, 0 for none.
INSPECT_FORMAT = "{{.Id}}\t{{.HostConfig.Memory}}"
VM_IMAGE = "vmmem"
WSL_MEMORY = re.compile(r"^\s*memory\s*=\s*(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?B?)\s*$", re.I)
_UNITS = {"": 1, "B": 1, "K": 2**10, "M": MIB, "G": GIB, "T": 2**40}
_FRACTION = re.compile(r"\.\d+")
# Drop the VM's file cache, then compact the memory that freed. Measured 2026-10-10 with
# the containers using 950 MiB: the VM held 3.81 GB of Windows' memory, 2.2 GB of it
# file cache. `autoMemoryReclaim=dropcache` in `.wslconfig` never fired, since WSL drops
# only once the VM's CPU idles and one running Postgres and a collector never does. The
# drop alone gave Windows back 0.6 GB of the 1.75 GB it freed: the VM reports free
# memory only in whole 2 MiB blocks (`page_reporting_order` 9). Compacting gave back
# 0.34 GB more. `echo 1` drops the page cache alone, never dentries or a process's memory.
RECLAIM = "sync; echo 1 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory"


def gib(size: float) -> str:
    return f"{size / GIB:.1f} GiB"


def log_dir() -> Path:
    """Where Docker Desktop writes its host-side logs on this machine."""
    return (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Docker/log/host"
    )


def parse_limits(text: str) -> dict[str, int]:
    """`docker inspect --format INSPECT_FORMAT` rows as `{full id: limit bytes}`."""
    found = {}
    for line in text.splitlines():
        cid, _, limit = line.strip().partition("\t")
        if cid and limit.strip().isdigit():
            found[cid] = int(limit)
    return found


def limit_of(cid: str, limits: dict[str, int]) -> int | None:
    """`cid`'s limit, matched as a prefix: `docker ps` prints the short id that
    `docker inspect` answers with in full. None when it was not answered for."""
    return next((limit for full, limit in limits.items() if full.startswith(cid)), None)


def capacity(text: str) -> int | None:
    """`docker info --format {{.MemTotal}}` as bytes, or None. A failing engine prints
    `0` with exit 0 (2026-10-09, over a 500 from the API), so zero is no answer."""
    text = text.strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


@dataclass(frozen=True)
class Problem:
    # The failure line: stable across passes, so the ledger files one group for it.
    line: str
    # The measured numbers, said beside it rather than in it.
    detail: str = ""


def budget(
    capped: Sequence[tuple[str, int | None]], vm: int | None, headroom: int = HEADROOM
) -> list[Problem]:
    """What is wrong with the memory limits of the containers this machine keeps up.

    `capped` is `(label, limit bytes)` per container, a limit of 0 meaning none and None
    meaning the engine did not say; `vm` is the VM's memory, None when unknown. A sum is
    judged only when every limit is known and set: an unlimited container already makes
    the sum meaningless, and is reported for itself.
    """
    problems = [
        Problem(
            f"{label} has no memory limit -- unbounded, it can run Docker's whole VM out of "
            "memory and freeze every stack in it"
        )
        for label, limit in capped
        if limit == 0
    ]
    limits = [limit for _, limit in capped]
    if problems or vm is None or any(limit is None for limit in limits):
        return problems
    total = sum(limit for limit in limits if limit)
    if total > vm - headroom:
        problems.append(
            Problem(
                "the memory limits of the containers this machine keeps up do not fit "
                "Docker's VM -- lower them, or run fewer collectors here",
                f"limits total {gib(total)}; the VM has {gib(vm)}, less {gib(headroom)} "
                "for the engine itself",
            )
        )
    return problems


def parse_when(text: str) -> float | None:
    """A backend log timestamp (`2026-10-09T19:32:33.837887400Z`) as a POSIX time."""
    try:
        when = _dt.datetime.fromisoformat(_FRACTION.sub("", text.strip()).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.timestamp() if when.tzinfo else None


def oom_kills(text: str, since: float) -> list[float]:
    """When each OOM kill `init` reported at or after `since` happened, oldest first."""
    found = []
    for line in text.splitlines():
        hit = OOM_REQUEST.match(line)
        when = parse_when(hit["when"]) if hit else None
        if when is not None and when >= since:
            found.append(when)
    return sorted(found)


def backend_logs(where: Path, since: float) -> str:
    """Every backend log -- the live one and its rotations -- written since `since`."""
    texts = []
    try:
        paths = sorted(where.glob(f"{BACKEND_LOG}*"))
    except OSError:
        return ""
    for path in paths:
        try:
            if path.stat().st_mtime >= since:
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(texts)


def wsl_cap(text: str) -> int | None:
    """The `memory=` a `.wslconfig` sets for the VM, in bytes; None when it sets none.

    WSL reads `4GB` as 4 GiB. Only the `[wsl2]` section counts."""
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.strip("[]").strip().lower()
            continue
        hit = WSL_MEMORY.match(line) if section == "wsl2" else None
        if hit:
            unit = hit["unit"].upper().rstrip("B")
            return int(float(hit["n"]) * _UNITS[unit])
    return None


def vm_working_set(tasklist_csv: str) -> int | None:
    """The VM process's working set from `tasklist /FO CSV /NH`, in bytes; None when it
    is not running. Its memory column is `"3,937,576 K"` with the locale's separator,
    so every non-digit goes."""
    total = None
    for line in tasklist_csv.splitlines():
        cells = [cell.strip('"') for cell in line.strip().split('","')]
        if len(cells) < 5 or not cells[0].lower().startswith(VM_IMAGE):
            continue
        kib = re.sub(r"\D", "", cells[4])
        if kib:
            total = (total or 0) + int(kib) * 1024
    return total


def read_silent_vm(
    run: Callable[[list[str]], tuple[int, str]], logs: Path, wslconfig: Path, since: float
) -> str:
    """`silent_vm_evidence` from what Windows holds: the backend logs under `logs`, the VM
    process through `run` (`tasklist`), and the cap in `wslconfig`. Asks the engine
    nothing, since this is read exactly when it does not answer."""
    code, out = run(["tasklist", "/FI", f"IMAGENAME eq {VM_IMAGE}*", "/FO", "CSV", "/NH"])
    try:
        cap = wsl_cap(wslconfig.read_text(encoding="utf-8"))
    except OSError:
        cap = None
    return silent_vm_evidence(
        oom_kills(backend_logs(logs, since), since),
        vm_working_set(out) if code == 0 else None,
        cap,
    )


def silent_vm_evidence(kills: Sequence[float], working_set: int | None, cap: int | None) -> str:
    """What says a silent engine's VM ran out of memory, or "" when nothing does."""
    said = []
    if kills:
        last = _dt.datetime.fromtimestamp(kills[-1], _dt.timezone.utc).strftime("%H:%M UTC")
        said.append(
            f"Docker Desktop logged {len(kills)} OOM kill(s) inside its VM, the last at {last}"
        )
    if working_set is not None and cap and working_set >= NEAR_CAP * cap:
        said.append(f"the VM holds {gib(working_set)} of its {gib(cap)} cap")
    return "; ".join(said)


def reclaim_argv(distro: str) -> list[str]:
    """The `wsl` call that runs `RECLAIM` as root inside `distro`."""
    return ["wsl", "-d", distro, "-u", "root", "-e", "sh", "-c", RECLAIM]
