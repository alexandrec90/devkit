#!/usr/bin/env python3
"""Whether a suite run should be handed to `pytest-xdist`, and with what flag.

The push gate runs pytest three times -- `run-tests.py`, the vendored hook tier, and
`posix-rehearsal.py`, which is the whole suite a second time under a faked platform.
Serially on this workstation that measured 438s, 186s and 320s: fifteen minutes of a
`git push` spent on eight idle cores. With `-n auto` the same three are 109s, 55s and
111s, all green -- the suite is already parallel-safe because its fixtures build
throwaway directories rather than sharing one.

Decided here rather than in `pyproject.toml`'s `addopts` on purpose. `addopts` would
reach **every** pytest invocation, including the single-test debug run an agent makes
while fixing something -- where xdist adds worker startup to a two-second run, breaks
`--pdb`, and reorders live output. The runners in this directory are exactly the "run
the whole thing" entry points, so the flag belongs on them and a bare `pytest` stays
serial and debuggable.

Not vendored. `run_push_gate.py` asks the same question about a *different*
interpreter -- the project's, which it spawns -- so it probes with a subprocess of its
own rather than importing this; sharing a module across that boundary would mean
shipping this file to every consumer to answer a question it cannot answer there.

Tested in `tests/test_pytest_parallel.py`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_memory

# An operator turning the parallel run off -- for a suite that has grown a shared
# resource, or to read an interleaved failure in order. Same asymmetry as
# `git_policy`'s `SKIP_ENV_VAR`: the values that read as "off" to a human must not
# switch parallelism off, because `DEVKIT_PYTEST_WORKERS=0` means "no workers" to
# whoever typed it and honouring it as "disable the opt-out" would be backwards.
DISABLE_ENV = "DEVKIT_NO_XDIST"
_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})

# `auto` is xdist's own spelling for "one worker per CPU". Kept as a constant because
# two runners and their tests name it, not because it is expected to change.
DEFAULT_WORKERS = "auto"


def disabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the operator has switched the parallel run off."""
    env = os.environ if environ is None else environ
    return env.get(DISABLE_ENV, "").strip().lower() not in _OFF_VALUES


def available() -> bool:
    """Whether *this* interpreter can import xdist.

    `find_spec` rather than an import: the answer is wanted before pytest starts, and
    importing the plugin here would load it into a process that is about to spawn a
    different one. A project that has not installed it is not a failure -- the flag is
    simply left off, which is the behaviour every consumer had before this existed.
    """
    try:
        return importlib.util.find_spec("xdist") is not None
    except (ImportError, ValueError):
        # A half-installed distribution can leave `find_spec` raising rather than
        # answering. "Cannot import it" is the answer wanted in that case too.
        return False


# What one worker running a suite is assumed to hold, an estimate in the spirit of
# `tier-pass.RUN_MB`. `-n auto` alone is one worker per CPU whatever is free, and on a
# 16 GB PC beside running sessions the push gate's workers got the gate killed for low
# memory three runs in a row, so devkit's red main never left the machine (90f90636).
WORKER_MB = 512


def workers_for(free_mb: int | None, cpus: int | None) -> str:
    """xdist's `-n` for a machine with `free_mb` free: `auto` while a worker per CPU fits
    above `host_memory.FLOOR_MB`, fewer when they do not, "" (serial) under two. A probe
    that failed is `auto`, read as no guard the way `host_memory` asks."""
    if free_mb is None:
        return DEFAULT_WORKERS
    fit = (free_mb - host_memory.FLOOR_MB) // WORKER_MB
    if cpus is not None and fit >= cpus:
        return DEFAULT_WORKERS
    return str(fit) if fit >= 2 else ""


_PROBE = object()


def args(
    *,
    workers: str | None = None,
    environ: Mapping[str, str] | None = None,
    have_xdist: bool | None = None,
    free_mb: int | object | None = _PROBE,
) -> list[str]:
    """`["-n", workers]` when the run should be parallel, `[]` when it should not.

    `workers` unset is decided by free memory (`workers_for`). `have_xdist` and `free_mb`
    are injected by the tests so the decision can be exercised on a machine whose answer
    is the opposite; production passes neither and both are probed.
    """
    if disabled(environ):
        return []
    if not (available() if have_xdist is None else have_xdist):
        return []
    if workers is None:
        free = host_memory.available_mb() if free_mb is _PROBE else free_mb
        workers = workers_for(free if isinstance(free, int) else None, os.cpu_count())
    return ["-n", workers] if workers else []
