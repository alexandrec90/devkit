"""Suite-wide: no test waits on how long the machine running it has been up.

`machine_clock.awake_since` gates real waits: `collectors.wait_for_engine` polls a silent
Docker engine until the machine has been up `ENGINE_STARTUP` seconds, and
`workspace-status.py --notify` sleeps out `catch_up_wait`. A CI runner has always just
booted, so a test reaching either through the real clock sleeps for real -- one did for
549 seconds of every PR gate's nine-and-a-half-minute shard (run 45719636635), passing
all the while, while the same test took milliseconds on a workstation that had been up
for days. Every test therefore sees a machine that cannot say when it came up, which is
"one answer is the answer" to each caller; a test about the waiting passes `awake` or
patches `awake_since` itself, as before.

A module that tests the clock itself opts out with `READS_THE_REAL_MACHINE_CLOCK = True`.

A plugin, loaded by `addopts`, for the reason `claude_config_plugin.py` gives: this
directory cannot hold a `conftest.py`, and not every test module imports `support`.
"""

from __future__ import annotations

import machine_clock
import pytest

OPT_OUT = "READS_THE_REAL_MACHINE_CLOCK"


@pytest.fixture(autouse=True)
def _machine_cannot_say_when_it_came_up(request, monkeypatch):
    if not getattr(request.module, OPT_OUT, False):
        monkeypatch.setattr(machine_clock, "awake_since", lambda now=None: None)
