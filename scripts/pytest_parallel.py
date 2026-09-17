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
from collections.abc import Mapping

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


def args(
    *,
    workers: str = DEFAULT_WORKERS,
    environ: Mapping[str, str] | None = None,
    have_xdist: bool | None = None,
) -> list[str]:
    """`["-n", workers]` when the run should be parallel, `[]` when it should not.

    `have_xdist` is injected by the tests so the decision can be exercised on a machine
    whose answer is the opposite; production passes nothing and it is probed.
    """
    if disabled(environ):
        return []
    if available() if have_xdist is None else have_xdist:
        return ["-n", workers]
    return []
