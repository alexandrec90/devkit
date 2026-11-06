"""`scripts/install-elevated-worker.py`: the one job an administrator registers.

Nothing here touches `C:\\ProgramData` or the scheduler: every path the installer holds is
pointed at `tmp_path` by the `planted` fixture, and every `run` is a fake.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from support import ELEVATED_EXEMPTION, REPO_ROOT, load_script

installer = load_script("scripts/install-elevated-worker.py")
request = load_script("scripts/elevated_request.py")
worker = load_script("scripts/elevated_worker.py")
schtasks = installer.devkit_schtasks
installers = load_script("scripts/installers.py")

SOURCE = (REPO_ROOT / "scripts" / "install-elevated-worker.py").read_text(encoding="utf-8")
SID = "S-1-5-21-1-2-3-1001"
LOCKED = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
# A real Windows interpreter: the local tier, which CI deselects and `scripts/e2e.py` runs.
windows_only = pytest.mark.local


class Fake:
    """A `Runner` that records every argv and answers from a table of substrings."""

    def __init__(self, fail: str = ""):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if self.fail and self.fail in joined:
            return subprocess.CompletedProcess(list(argv), 1, "", f"{self.fail} refused")
        if "whoami" in argv[0]:
            return subprocess.CompletedProcess(list(argv), 0, f'"pc\\me","{SID}"\n', "")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def said(self, text: str) -> bool:
        return any(text in " ".join(argv) for argv in self.calls)


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """The installer's folders under `tmp_path`, and an interpreter folder to copy."""
    root = tmp_path / "devkit"
    home = root / "elevated"
    spool = root / "requests"
    interpreter = home / "python"
    for name, value in (
        ("ROOT", root),
        ("HOME", home),
        ("SPOOL", spool),
        ("INTERPRETER", interpreter),
        ("PYTHON", interpreter / "python.exe"),
    ):
        monkeypatch.setattr(installer, name, str(value))
    source = tmp_path / "base"
    for folder in ("Lib/site-packages/evil", "Lib/encodings", "Lib/test", "DLLs"):
        (source / folder).mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe", "python314.dll", "python3.dll", "vcruntime140.dll"):
        (source / name).touch()
    (source / "Scripts").mkdir()
    (source / "Scripts" / "pip.exe").touch()
    (source / "Lib" / "os.py").touch()
    (source / "Lib" / "site-packages" / "evil.pth").touch()
    (source / "DLLs" / "_ctypes.pyd").touch()
    monkeypatch.setattr(installer, "windowless", lambda _python: str(source / "pythonw.exe"))
    monkeypatch.setattr(installer.harness_state, "stood_down", frozenset)
    monkeypatch.setattr(installer.elevated_request, "descriptor", lambda _path: LOCKED)
    return root


def registered(run: Fake) -> bool:
    return run.said("/Create")


def document() -> str:
    return installer.task_document()


# --- the task --------------------------------------------------------------------


def test_it_runs_every_five_minutes_highest_as_whoever_is_logged_on():
    text = document()
    assert "<Interval>PT5M</Interval>" in text
    assert "<RunLevel>HighestAvailable</RunLevel>" in text
    assert "<GroupId>S-1-5-32-545</GroupId>" in text
    assert '<Actions Context="Author">' in text
    assert '<Principal id="Author">' in text


def test_it_is_registered_from_the_shared_document_with_its_laptop_settings():
    text = document()
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in text
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in text
    assert text.index("<Principals>") < text.index("<Settings>")


def test_it_needs_no_console_window():
    assert schtasks.parse_task(document()).command.lower().endswith("pythonw.exe")


def test_the_task_runs_the_admin_only_copy_and_the_private_interpreter():
    """Neither may be in a folder the user can write: not the auto-updating checkout, and
    not the interpreter, which on a typical PC is under the user's AppData."""
    parsed = schtasks.parse_task(document())
    home = r"C:\ProgramData\devkit\elevated"
    assert parsed.command == home + r"\python\pythonw.exe"
    assert parsed.arguments.endswith(f'"{home}\\elevated_worker.py"')
    assert f"<WorkingDirectory>{home}</WorkingDirectory>" in document()
    assert str(REPO_ROOT) not in document()
    assert "AppData" not in document()


def test_no_environment_variable_can_reach_the_command():
    """A task in the user's session expands `%windir%` from the user's environment."""
    assert "%" not in document()


def test_the_interpreter_runs_isolated_with_no_site():
    """`-I` is what stops `PYTHONINSPECT`; the `._pth` alone left it at a prompt."""
    arguments = schtasks.parse_task(document()).arguments.split()
    assert arguments[:3] == ["-I", "-S", "-B"]


def test_it_is_the_one_job_with_no_user_write_on_the_task():
    """`secured` grants the registering user full access, which on a task that runs
    elevated is an administrator's rights for the asking: they could rewrite its action."""
    after = schtasks.secured(document(), SID)
    assert f"(A;;FA;;;{SID})" not in after
    assert SID not in after
    assert installer.SECURITY in after
    assert after.count("<SecurityDescriptor>") == 1


def test_the_descriptor_gives_every_non_admin_read_and_nothing_more():
    aces = re.findall(r"\(A;;([A-Z]+);;;([A-Z]{2})\)", installer.SECURITY)
    assert aces == [("FA", "BA"), ("FA", "SY"), ("FR", "AU")]


def test_the_check_compares_the_document_register_would_register():
    run = Fake()

    def query(argv):
        if "/XML" in argv:
            registered = schtasks.secured(document(), SID)
            return subprocess.CompletedProcess(list(argv), 0, registered, "")
        return run(argv)

    code, _message = schtasks.run_check(installer.TASK_NAME, document(), query)
    assert code == schtasks.CHECK_CURRENT


def test_a_task_someone_opened_to_the_user_is_stale():
    """Drift the unelevated check can see, on the property that matters most."""
    run = Fake()
    opened = document().replace(installer.SECURITY, f"D:(A;;FA;;;{SID})")

    def query(argv):
        if "/XML" in argv:
            return subprocess.CompletedProcess(list(argv), 0, opened, "")
        return run(argv)

    code, message = schtasks.run_check(installer.TASK_NAME, document(), query)
    assert code == schtasks.CHECK_STALE
    assert "is secured" in message


def test_a_stood_down_job_lands_disabled(monkeypatch):
    monkeypatch.setattr(
        installer.harness_state, "stood_down", lambda: frozenset({installer.TASK_NAME})
    )
    assert "<Enabled>false</Enabled>" in document()[document().index("<Settings>") :]


def test_it_stays_out_of_maintain_as_the_named_exception():
    assert "install-elevated-worker.py" in ELEVATED_EXEMPTION
    assert "install-elevated-worker.py" not in {
        path.name for path in installers.discover(REPO_ROOT)
    }


def test_nothing_it_copies_or_registers_runs_through_the_log_wrapper():
    assert "log-wrap" not in schtasks.parse_task(document()).arguments
    assert "devkit_schtasks.logged(" not in SOURCE


# --- the folders -----------------------------------------------------------------


def test_the_admin_only_folder_cuts_what_programdata_gives_every_user():
    argv = installer.lock_argv(r"C:\x")
    assert "/inheritance:r" in argv, "or `BUILTIN\\Users:(CI)(WD,AD,WEA,WA)` is inherited"
    assert "/grant:r" in argv
    assert "*S-1-5-32-544:(OI)(CI)F" in argv
    assert "*S-1-5-18:(OI)(CI)F" in argv
    assert "*S-1-5-32-545:(OI)(CI)RX" in argv
    assert [part for part in argv if part.startswith("*") and ":" in part][2].endswith("RX")


def test_its_owner_is_administrators_not_whoever_ran_it():
    assert installer.own_argv(r"C:\x") == ["icacls", r"C:\x", "/setowner", "*S-1-5-32-544"]


def test_the_spool_lets_the_user_add_a_file_and_nothing_else():
    """No `AD`, so no subfolder and no junction; no delete, so not the sentinel; no
    `(OI)`, so the grant does not reach any file."""
    assert installer.spool_argv(r"C:\x", SID) == ["icacls", r"C:\x", "/grant", f"*{SID}:(WD)"]


def test_the_folders_are_the_contracts():
    assert installer.ROOT == request.ROOT == worker.ROOT
    assert installer.SENTINEL == worker.SENTINEL


def test_a_fresh_install_makes_the_folder_then_owns_and_locks_it(planted):
    run = Fake()
    assert installer.claim_root(run) == ""
    assert planted.is_dir()
    assert run.calls == [installer.own_argv(str(planted)), installer.lock_argv(str(planted))]


def test_a_lock_that_exited_0_and_left_a_hole_is_a_refusal(planted, monkeypatch):
    """Probed: `/grant:r` keeps an ACE it did not name, and `icacls` still exits 0."""
    held = f"{LOCKED}(A;OICI;FA;;;OW)"
    monkeypatch.setattr(installer.elevated_request, "descriptor", lambda _path: held)
    run = Fake()
    assert "did not end up admin-only (OW holds FA)" in installer.claim_root(run)
    assert installer.install(Fake()) == 1, "and so nothing is copied or registered"


def test_a_folder_a_user_made_first_is_refused_and_not_adopted(planted, monkeypatch):
    """Any user may create this folder and own it, and an owner can undo any ACL set here."""
    planted.mkdir()
    held = f"O:{SID}D:(A;;FA;;;{SID})"
    monkeypatch.setattr(installer.elevated_request, "descriptor", lambda _path: held)
    run = Fake()
    said = installer.claim_root(run)
    assert f"owned by {SID}" in said
    assert "delete it" in said
    assert run.calls == [], "not chowned, not ACLed: whatever is in it is not trusted"


def test_an_earlier_install_is_kept(planted):
    planted.mkdir()
    run = Fake()
    assert installer.claim_root(run) == ""
    assert run.calls == []


def test_a_link_at_the_root_is_refused(planted, monkeypatch):
    planted.mkdir()
    monkeypatch.setattr(installer.os.path, "isjunction", lambda _path: True)
    run = Fake()
    assert "is a link" in installer.claim_root(run)
    assert run.calls == []


def test_a_folder_appearing_after_the_look_is_a_refusal(planted, monkeypatch):
    monkeypatch.setattr(installer.os.path, "lexists", lambda _path: False)
    planted.mkdir()
    run = Fake()
    assert "appeared while this ran" in installer.claim_root(run)
    assert run.calls == []


# --- the copies ------------------------------------------------------------------


def test_the_interpreter_copy_is_the_stdlib_and_nothing_a_user_installed(planted, tmp_path):
    target = tmp_path / "copy"
    installer.copy_interpreter(tmp_path / "base", target)
    assert (target / "pythonw.exe").is_file()
    assert (target / "python314.dll").is_file()
    assert (target / "vcruntime140.dll").is_file()
    assert (target / "DLLs" / "_ctypes.pyd").is_file()
    assert (target / "Lib" / "os.py").is_file()
    assert not (target / "Lib" / "site-packages").exists()
    assert not (target / "Lib" / "test").exists()
    assert not (target / "Scripts").exists()


def test_the_copy_pins_its_own_path(planted, tmp_path):
    target = tmp_path / "copy"
    installer.copy_interpreter(tmp_path / "base", target)
    [pth] = target.glob("python*._pth")
    assert pth.name == f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    assert pth.read_text(encoding="ascii").split() == ["Lib", "DLLs"]


def test_the_smoke_imports_everything_the_worker_imports():
    """A stdlib module left behind would fail here rather than at 03:00 under pythonw."""
    tree = ast.parse((REPO_ROOT / "scripts" / "elevated_worker.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            imported.add(node.module.split(".")[0])
    builtin_enough = {"collections", "io", "os", "sys", "time"}
    assert imported - builtin_enough <= set(installer.SMOKE.removeprefix("import ").split(", "))


def test_nothing_the_worker_imports_is_left_behind():
    assert not {"csv", "ctypes", "json", "subprocess", "dataclasses"} & set(installer.LEFT_BEHIND)


@pytest.fixture(scope="module")
def real_copy(tmp_path_factory):
    """The real interpreter, copied once: a real `_pth`, real DLLs, 36 MB or so."""
    target = tmp_path_factory.mktemp("private") / "python"
    source = Path(schtasks.windowless(sys.executable)).parent
    installer.copy_interpreter(source, target)
    return target


def hostile_env(planted_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(planted_dir),
        "PYTHONHOME": str(planted_dir),
        "PYTHONSTARTUP": str(planted_dir / "json.py"),
        "PYTHONINSPECT": "1",
    }


@windows_only
def test_the_private_interpreter_ignores_a_hostile_environment_and_cwd(real_copy, tmp_path):
    """Run rather than read. `PYTHONINSPECT` is the one the `._pth` alone did not stop: it
    hung at a prompt, so this has a timeout and a closed stdin."""
    (tmp_path / "json.py").write_text("raise SystemExit('planted')\n", encoding="ascii")
    code = "import sys, json; print(sys.path); print(json.__file__)"
    done = subprocess.run(
        [str(real_copy / "python.exe"), *installer.INTERPRETER_FLAGS, "-c", code],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
        env=hostile_env(tmp_path),
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert str(tmp_path) not in done.stdout.replace(str(real_copy), "")
    assert str(real_copy / "Lib") in done.stdout


@windows_only
def test_the_private_interpreter_can_load_the_worker(real_copy, tmp_path):
    """The smoke an install runs, plus the worker itself, without entering its `main`."""
    copy = tmp_path / "elevated_worker.py"
    copy.write_bytes((REPO_ROOT / "scripts" / "elevated_worker.py").read_bytes())
    code = (
        f"{installer.SMOKE}; import runpy; "
        f"print(sorted(runpy.run_path({str(copy)!r}, run_name='loaded')['VERBS']))"
    )
    done = subprocess.run(
        [str(real_copy / "python.exe"), *installer.INTERPRETER_FLAGS, "-c", code],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "reboot-when-idle" in done.stdout


def test_a_full_install_copies_then_registers(planted, monkeypatch, tmp_path):
    run = Fake()
    monkeypatch.setattr(schtasks, "write_task_file", lambda xml: tmp_path / "t.xml")
    (tmp_path / "t.xml").touch()
    assert installer.install(run) == 0
    assert (planted / "elevated" / "elevated_worker.py").read_bytes() == (
        REPO_ROOT / "scripts" / "elevated_worker.py"
    ).read_bytes()
    assert (planted / "elevated" / "python" / "pythonw.exe").is_file()
    assert (planted / "requests" / installer.SENTINEL).is_file()
    assert run.said(f"*{SID}:(WD)")
    assert registered(run)
    order = [" ".join(argv) for argv in run.calls]
    smoke = next(i for i, line in enumerate(order) if "-c" in line.split())
    create = next(i for i, line in enumerate(order) if "/Create" in line)
    assert smoke < create, "nothing is scheduled against an interpreter that did not start"


def test_an_interpreter_that_does_not_start_registers_nothing(planted):
    run = Fake(fail=installer.SMOKE)
    assert installer.install(run) == 1
    assert not registered(run)


def test_a_spool_that_could_not_be_granted_registers_nothing(planted):
    run = Fake(fail=":(WD)")
    assert installer.install(run) == 1
    assert not registered(run)


def test_no_sid_registers_nothing(planted):
    run = Fake(fail="whoami")
    assert installer.install(run) == 1
    assert not registered(run)


def test_a_copy_refused_mid_run_says_how_to_recover(planted, monkeypatch, capsys):
    def locked(*_a, **_k):
        raise PermissionError("in use")

    monkeypatch.setattr(installer, "copy_interpreter", locked)
    run = Fake()
    assert installer.install(run) == 1
    assert not registered(run)
    assert "re-run in a minute" in capsys.readouterr().err


def test_a_reinstall_replaces_the_interpreter_and_keeps_the_rate_limit(
    planted, monkeypatch, tmp_path
):
    """Upgrading is the deliberate way back in, and it must not hand a loop a fresh window."""
    monkeypatch.setattr(schtasks, "write_task_file", lambda xml: tmp_path / "t.xml")
    (tmp_path / "t.xml").touch()
    assert installer.install(Fake()) == 0
    (planted / "elevated" / "rate-limit.json").write_text('{"x": 1}', encoding="utf-8")
    (planted / "elevated" / "python" / "stale.dll").touch()
    monkeypatch.setattr(installer, "claim_root", lambda _run: "")
    assert installer.install(Fake()) == 0
    assert not (planted / "elevated" / "python" / "stale.dll").exists()
    assert (planted / "elevated" / "rate-limit.json").is_file()


def test_uninstall_takes_the_task_and_the_folder(planted):
    planted.mkdir()
    (planted / "x").touch()
    run = Fake()
    assert installer.uninstall(run) == 0
    assert not planted.exists()


def test_uninstall_stops_at_a_task_it_could_not_delete(planted):
    planted.mkdir()
    assert installer.uninstall(Fake(fail="/Delete")) == 1
    assert planted.exists()


# --- the command line ------------------------------------------------------------


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)


def test_bare_prints_the_plan_and_touches_nothing(windows, monkeypatch, capsys):
    monkeypatch.setattr(installer, "install", lambda: pytest.fail("installed"))
    monkeypatch.setattr(installer.subprocess, "run", lambda *_a, **_k: pytest.fail("spawned"))
    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "Dry run." in out
    assert "elevated terminal" in out
    assert "One administrator action per PC" in out


def test_yes_without_elevation_says_so_clearly_and_touches_nothing(windows, monkeypatch, capsys):
    monkeypatch.setattr(installer.docker_vhdx, "is_elevated", lambda: False)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda _root: installer.REPO_ROOT)
    monkeypatch.setattr(installer, "install", lambda: pytest.fail("installed"))
    assert installer.main(["--yes"]) == 1
    err = capsys.readouterr().err
    assert "needs an elevated terminal" in err
    assert "--yes" in err


def test_yes_elevated_from_a_temporary_checkout_is_refused(windows, monkeypatch, capsys):
    """What runs as admin comes from what landed, not from a session's tree."""
    monkeypatch.setattr(installer.docker_vhdx, "is_elevated", lambda: True)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda _root: Path("C:/static"))
    monkeypatch.setattr(installer, "install", lambda: pytest.fail("installed"))
    assert installer.main(["--yes"]) == 1
    assert "temporary checkout" in capsys.readouterr().err


def test_yes_elevated_from_the_static_checkout_installs(windows, monkeypatch):
    monkeypatch.setattr(installer.docker_vhdx, "is_elevated", lambda: True)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda _root: installer.REPO_ROOT)
    monkeypatch.setattr(installer, "install", lambda: 0)
    assert installer.main(["--yes"]) == 0


def test_uninstall_yes_needs_elevation_too(windows, monkeypatch, capsys):
    monkeypatch.setattr(installer.docker_vhdx, "is_elevated", lambda: False)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda _root: installer.REPO_ROOT)
    monkeypatch.setattr(installer, "uninstall", lambda: pytest.fail("uninstalled"))
    assert installer.main(["--uninstall", "--yes"]) == 1
    assert "needs an elevated terminal" in capsys.readouterr().err


def test_uninstall_alone_is_a_dry_run(windows, monkeypatch, capsys):
    monkeypatch.setattr(installer, "uninstall", lambda: pytest.fail("uninstalled"))
    assert installer.main(["--uninstall"]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_check_needs_no_elevation(windows, monkeypatch):
    monkeypatch.setattr(
        installer.docker_vhdx, "is_elevated", lambda: pytest.fail("asked about elevation")
    )
    monkeypatch.setattr(schtasks, "run_check", lambda *_a: (0, "current"))
    monkeypatch.setattr(installer.elevated_request, "state", lambda *_a: ("current", "ok"))
    assert installer.main(["--check"]) == 0


def test_check_reports_a_copy_the_repo_has_moved_past(windows, monkeypatch, capsys):
    monkeypatch.setattr(schtasks, "run_check", lambda *_a: (0, "current"))
    monkeypatch.setattr(
        installer.elevated_request, "state", lambda *_a: ("outdated", "its copy differs")
    )
    assert installer.main(["--check"]) == schtasks.CHECK_STALE
    assert "outdated: its copy differs" in capsys.readouterr().err


def test_it_says_windows_only_elsewhere(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out


def test_an_elevated_run_writes_no_bytecode_into_the_checkout():
    """Set before the first import of ours: an elevated `__pycache__` in a tree is what
    `elevated_writes.py` exists to catch."""
    lines = SOURCE.splitlines()
    assert lines.index("sys.dont_write_bytecode = True") < lines.index("import devkit_schtasks")
