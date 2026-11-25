"""Suite-wide isolation from this machine's own Claude Code config.

`fix_reports.trust` writes Claude Code's `.claude.json` -- the pass answers its trust
prompt -- and reads where Claude Code does, under `CLAUDE_CONFIG_DIR` when that is set.
A test that reached it through a pass with `trusted` faked would otherwise write the
workstation's real file, so every test gets a config directory of its own.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _own_claude_config(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
