"""Tests for the VanillaLand E2E launcher task.

Three things carry it, and none of them is the happy path.

**The reference checkout has to be reachable.** Every other workspace tool subtracts it
-- `known_projects` through `NOT_PROJECTS`, `sweep` through `DEFAULT_EXCLUDE` -- because
the actions they dispatch need a harness. This one runs a script the checkout ships, so
it resolves against the raw registry, and that seam is the reason the task is not an
`ACTIONS` entry.

**A missing start script must be diagnosed here, not by PowerShell.** `.local/` is
untracked, so "the file is not there" is the state of every machine nobody has seeded
-- the common outcome, not an edge case -- and the message has to say that rather than
report a path.

**Nothing may launch on a failed check.** The stack takes minutes to come up and starts
detached processes; a task that got as far as spawning IIS Express before noticing it
had resolved the wrong checkout would leave them running.
"""

import json

import pytest
from support import devkit_project, load_script

e2e = load_script("scripts/vanillaland-e2e.py")

every_checkout = e2e.every_checkout
launch_command = e2e.launch_command
main = e2e.main
missing_script = e2e.missing_script
powershell = e2e.powershell
script_in = e2e.script_in
target_repo = e2e.target_repo
StartError = e2e.StartError

# Deliberately not named `alex-projects.code-workspace`: `test_self_hosting.py` reads
# that literal in an unmarked test as a use of the live workstation file, which CI does
# not have. Every test here passes its registry in.
WORKSPACE = json.dumps(
    {"folders": [{"path": "carameli"}, {"path": "devkit"}, {"path": "VanillaLand"}]}
)


class FakeRun:
    """A `Runner` recording what it was asked to launch."""

    def __init__(self, code: int = 0):
        self.code = code
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv, cwd):
        self.calls.append((argv, str(cwd)))
        return self.code


def seeded(tmp_path, *, script: bool = True):
    """A workspace registry on disk, with VanillaLand present and optionally seeded."""
    workspace = tmp_path / "registry.code-workspace"
    workspace.write_text(WORKSPACE, encoding="utf-8")
    repo = tmp_path / "VanillaLand"
    if script:
        runtime = repo / e2e.E2E_SCRIPT
        runtime.parent.mkdir(parents=True)
        runtime.write_text("Write-Output 'stack up'\n", encoding="utf-8")
    else:
        repo.mkdir()
    return workspace, repo


# --- the registry seam ------------------------------------------------------


def test_the_reference_checkout_is_reachable_from_here():
    """The reason this is a workspace task rather than a dispatched action: the
    dispatcher's registry does not contain the only checkout it can run in."""
    assert "VanillaLand" in every_checkout(WORKSPACE)
    assert "VanillaLand" not in devkit_project.known_projects(WORKSPACE)


def test_a_named_checkout_resolves_to_its_directory(tmp_path):
    workspace, repo = seeded(tmp_path)
    assert target_repo("VanillaLand", workspace) == repo


def test_an_unknown_checkout_names_the_real_ones(tmp_path):
    workspace, _ = seeded(tmp_path)
    with pytest.raises(devkit_project.ProjectError, match=r"unknown checkout 'nope'.*VanillaLand"):
        target_repo("nope", workspace)


def test_an_unreadable_registry_names_the_file(tmp_path):
    with pytest.raises(StartError, match=r"cannot read the workspace registry"):
        target_repo("VanillaLand", tmp_path / "nothing-here.code-workspace")


# --- the untracked runtime --------------------------------------------------


def test_a_seeded_checkout_yields_its_start_script(tmp_path):
    _, repo = seeded(tmp_path)
    assert script_in(repo) == repo / e2e.E2E_SCRIPT


def test_an_unseeded_checkout_is_diagnosed_rather_than_run(tmp_path):
    _, repo = seeded(tmp_path, script=False)
    with pytest.raises(StartError) as raised:
        script_in(repo)
    assert "untracked" in str(raised.value)


def test_the_missing_script_message_points_at_the_checkouts_own_procedure(tmp_path):
    """The remedy lives in VanillaLand and cannot be carried out from devkit, so the
    message has to name it. A message that only said "not found" would read as this
    task being broken."""
    said = missing_script(tmp_path / "VanillaLand", e2e.E2E_SCRIPT)
    assert "seed-vs-sql.ps1" in said
    assert "README.md" in said
    assert str(e2e.E2E_SCRIPT) in said


# --- the command ------------------------------------------------------------


def test_powershell_7_is_preferred_over_windows_powershell():
    assert powershell(which=lambda name: f"C:/{name}.exe") == "C:/pwsh.exe"


def test_windows_powershell_is_the_fallback():
    found = powershell(which=lambda name: "C:/powershell.exe" if name == "powershell" else None)
    assert found == "C:/powershell.exe"


def test_no_powershell_at_all_says_so(tmp_path):
    with pytest.raises(StartError, match=r"no PowerShell on PATH"):
        powershell(which=lambda name: None)


def test_the_script_is_run_unsigned_and_without_a_profile(tmp_path):
    """Both flags are load-bearing: the file is untracked (so unsigned), and a profile
    that resets `$ErrorActionPreference` would turn a failed health check into a task
    that reports success."""
    command = launch_command("pwsh", tmp_path / "start.ps1")
    assert command[0] == "pwsh"
    assert "-NoProfile" in command
    assert command[command.index("-ExecutionPolicy") + 1] == "Bypass"
    assert command[-2:] == ["-File", str(tmp_path / "start.ps1")]


# --- the entrypoint ---------------------------------------------------------


def test_main_runs_the_start_script_in_the_checkout(tmp_path, monkeypatch):
    workspace, repo = seeded(tmp_path)
    monkeypatch.setattr(e2e.shutil, "which", lambda name: f"C:/{name}.exe")
    fake = FakeRun()
    assert main(["--workspace", str(workspace)], run=fake) == 0
    ((argv, cwd),) = fake.calls
    assert argv[-1] == str(repo / e2e.E2E_SCRIPT)
    assert cwd == str(repo)


def test_main_propagates_the_scripts_exit_code(tmp_path, monkeypatch):
    """`start.ps1` throws when a site does not become healthy, and the whole point of
    the task's icon is that a stack which came up half-way reads as failed."""
    workspace, _ = seeded(tmp_path)
    monkeypatch.setattr(e2e.shutil, "which", lambda name: f"C:/{name}.exe")
    assert main(["--workspace", str(workspace)], run=FakeRun(code=1)) == 1


def test_main_launches_nothing_when_the_checkout_is_unseeded(tmp_path, monkeypatch):
    workspace, _ = seeded(tmp_path, script=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda name: f"C:/{name}.exe")
    fake = FakeRun()
    assert main(["--workspace", str(workspace)], run=fake) == 2
    assert not fake.calls


def test_main_launches_nothing_when_the_checkout_is_unknown(tmp_path, monkeypatch):
    workspace, _ = seeded(tmp_path)
    monkeypatch.setattr(e2e.shutil, "which", lambda name: f"C:/{name}.exe")
    fake = FakeRun()
    assert main(["--workspace", str(workspace), "--checkout", "nope"], run=fake) == 2
    assert not fake.calls


def test_the_default_checkout_needs_no_argument(tmp_path, monkeypatch):
    """The picker feeds `--checkout`, but the task carries no picker: there is one
    legacy monolith, and a required argument would be a token to keep in sync."""
    workspace, repo = seeded(tmp_path)
    monkeypatch.setattr(e2e.shutil, "which", lambda name: f"C:/{name}.exe")
    fake = FakeRun()
    assert main(["--workspace", str(workspace)], run=fake) == 0
    assert fake.calls[0][1] == str(repo)
