"""The workstation bootstrap is PowerShell -- it runs before Python exists -- so nothing
here can execute it. What these pin is its shape: the steps a fresh machine depends on
are present, in an order where each one's input already exists."""

from __future__ import annotations

from pathlib import Path

from support import sweep

SCRIPT = (Path(sweep.__file__).parent / "bootstrap-machine.ps1").read_text(encoding="utf-8")


def _step_at(title: str) -> int:
    at = SCRIPT.find(f"Step '{title}'")
    assert at >= 0, f"bootstrap-machine.ps1 has no {title!r} step"
    return at


def test_the_bootstrap_renders_no_workspace_file():
    """The registry is `projects.toml`, committed in the clone, so a fresh machine has
    nothing to render: the step that ran `--render-workspace` went with VS Code, and
    the flag it ran no longer exists."""
    assert "Step 'Workspace file'" not in SCRIPT
    assert "--render-workspace" not in SCRIPT


def test_the_steps_run_clone_first_and_the_summary_last():
    """Every later step reads a file in the clone, and the summary reports them all."""
    assert (
        _step_at("Python")
        < _step_at("DEVKIT_DIR")
        < _step_at("Scheduled jobs")
        < _step_at("Left for you")
    )


def test_the_bootstrap_installs_and_mentions_no_vs_code():
    """VS Code is retired: no winget entry for it, no extension step, and no warning
    about a VS Code that was open while tools were installed."""
    assert "Microsoft.VisualStudioCode" not in SCRIPT
    assert "Step 'VS Code extensions'" not in SCRIPT
    assert "--install-extension" not in SCRIPT
    assert "VS Code" not in SCRIPT


def test_the_summary_names_the_cli_that_clones_the_registered_projects():
    """With no plug picker to click, the summary points at the script behind it."""
    tail = SCRIPT[_step_at("Left for you") :]
    assert "plug-projects.py --plug" in tail
