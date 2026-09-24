"""The workstation bootstrap is PowerShell -- it runs before Python exists -- so nothing
here can execute it. What these pin is its shape: the steps a fresh machine depends on
are present, in an order where each one's input already exists."""

from __future__ import annotations

import re
from pathlib import Path

from support import sweep

SCRIPT = (Path(sweep.__file__).parent / "bootstrap-machine.ps1").read_text(encoding="utf-8")


def _step_at(title: str) -> int:
    at = SCRIPT.find(f"Step '{title}'")
    assert at >= 0, f"bootstrap-machine.ps1 has no {title!r} step"
    return at


def test_the_bootstrap_renders_the_workspace_file():
    """It told the operator to open `alex-projects.code-workspace` and never created it:
    nothing else writes that file until `devkit-workspace-status`'s first daily pass, so
    a new machine had no VS Code tasks at all -- including the plug picker that clones
    the projects -- until someone found `--render-workspace` by hand."""
    render_step = _step_at("Workspace file")
    assert re.search(r"python \$render --render-workspace", SCRIPT[render_step:])


def test_the_render_runs_after_the_clone_and_before_the_summary():
    """The renderer is a file in the clone, and the summary points at what it wrote."""
    assert _step_at("Scheduled jobs") < _step_at("Workspace file") < _step_at("Left for you")


def test_the_summary_names_the_file_the_render_writes():
    """The note spells the file name for the operator; the render takes it from
    `sweep`. Two spellings are one rename away from pointing at different files."""
    assert f"'{sweep.WORKSPACE_FILE_NAME}'" in SCRIPT
