"""`scripts/elevated_request.py`: the one way to ask, and whether there is anyone to ask."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

request = load_script("scripts/elevated_request.py")
worker = load_script("scripts/elevated_worker.py")

# A real Windows descriptor: the local tier, which CI deselects and `scripts/e2e.py` runs.
windows_only = pytest.mark.local


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A spool and a worker copy under a temporary root, on a machine that reads as Windows."""
    monkeypatch.setattr(request, "WINDOWS", True)
    spool = tmp_path / "requests"
    spool.mkdir()
    copy = tmp_path / "elevated_worker.py"
    copy.touch()
    return spool, copy


def ask(verb, spool, copy):
    return request.request(verb, str(spool), str(copy), Path("C:/devkit"))


def test_the_verb_list_is_defined_once_and_the_workers_copy_matches():
    assert set(request.VERBS) == set(worker.VERBS)
    assert len(request.VERBS) == len(set(request.VERBS))


@pytest.mark.parametrize("verb", request.VERBS)
def test_a_request_is_an_empty_file_of_the_exact_name(installed, verb):
    spool, copy = installed
    assert ask(verb, spool, copy) == f"{verb} requested: the worker takes it within five minutes"
    assert [path.name for path in spool.iterdir()] == [f"{verb}.request"]
    assert (spool / f"{verb}.request").stat().st_size == 0
    assert worker.verb_of(f"{verb}.request") == verb, "the worker reads what this writes"


@pytest.mark.parametrize(
    "verb", ["", "reboot", "restart-wsl-service.request", "..\\x", "REBOOT-WHEN-IDLE"]
)
def test_a_verb_off_the_list_is_a_bug_and_writes_nothing(installed, verb):
    spool, copy = installed
    with pytest.raises(ValueError, match="is not one of"):
        ask(verb, spool, copy)
    assert list(spool.iterdir()) == []


def test_asking_twice_is_one_request(installed):
    spool, copy = installed
    ask("reboot-when-idle", spool, copy)
    assert ask("reboot-when-idle", spool, copy) == (
        "reboot-when-idle is already waiting for the worker"
    )
    assert len(list(spool.iterdir())) == 1


def test_no_worker_says_so_and_names_the_exact_command(tmp_path, monkeypatch):
    monkeypatch.setattr(request, "WINDOWS", True)
    said = ask("restart-wsl-service", tmp_path / "absent", tmp_path / "absent.py")
    assert said == request.not_installed(Path("C:/devkit"))
    assert "--yes`" in said
    assert "install-elevated-worker.py" in said
    assert "elevated terminal" in said
    assert "no job can" in said


def test_a_spool_with_no_worker_copy_is_not_installed(installed, tmp_path):
    spool, _copy = installed
    assert "not installed" in ask("restart-wsl-service", spool, tmp_path / "gone.py")


def test_asking_never_creates_the_spool(tmp_path, monkeypatch):
    """A folder a non-admin makes under ProgramData is theirs, ACL and all, and the
    installer refuses to adopt one. So the unelevated side may never be what made it."""
    monkeypatch.setattr(request, "WINDOWS", True)
    spool = tmp_path / "requests"
    ask("restart-wsl-service", spool, tmp_path / "w.py")
    assert not spool.exists()


def test_nothing_unelevated_makes_a_folder_there():
    source = (REPO_ROOT / "scripts" / "elevated_request.py").read_text(encoding="utf-8")
    assert "mkdir" not in source
    assert "makedirs" not in source


def test_a_spool_that_refuses_says_so(installed, monkeypatch):
    spool, copy = installed

    def refused(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(request.os, "open", refused)
    assert ask("restart-wsl-service", spool, copy).startswith(
        "restart-wsl-service was not requested: the spool refused this user"
    )


def test_it_says_windows_only_elsewhere_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(request, "WINDOWS", False)
    assert "Windows-only" in ask("reboot-when-idle", tmp_path, tmp_path / "w.py")
    assert list(tmp_path.iterdir()) == []
    assert request.state(tmp_path, lambda _a: pytest.fail("asked"))[0] == request.CURRENT


def test_the_command_line_validates_against_the_list(capsys):
    with pytest.raises(SystemExit):
        request.main(["reboot"])
    assert "invalid choice" in capsys.readouterr().err


# --- a folder that holds what an administrator runs ---------------------------------

ADMIN_ONLY = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
USER = "S-1-5-21-1-2-3-1001"


def test_an_admin_only_folder_has_no_problems():
    assert request.acl_problems(ADMIN_ONLY) == []
    assert request.acl_problems("O:SYD:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;AU)") == []


def test_a_folder_a_user_owns_is_a_problem_whatever_its_dacl_says():
    """An owner can rewrite the DACL; `C:\\ProgramData` lets anyone create one and own it."""
    sddl = ADMIN_ONLY.replace("O:BA", f"O:{USER}")
    assert request.acl_problems(sddl) == [f"owned by {USER}"]


@pytest.mark.parametrize("rights", ["FA", "GA", "GW", "FW", "0x1301bf", "WD", "SD", "WO", "DC"])
def test_a_non_admin_who_can_write_is_a_problem(rights):
    sddl = f"{ADMIN_ONLY}(A;;{rights};;;{USER})"
    assert request.acl_problems(sddl) == [f"{USER} holds {rights}"]


def test_the_ace_programdata_hands_every_user_is_a_problem():
    """`BUILTIN\\Users:(CI)(WD,AD,WEA,WA)`, inherited unless the install cuts it."""
    assert request.acl_problems("O:BAD:AI(A;CIID;0x116;;;BU)") == ["BU holds 0x116"]


def test_a_deny_ace_is_no_grant():
    assert request.acl_problems(f"O:BAD:(D;;FA;;;{USER})(A;;FA;;;BA)") == []


def test_an_unreadable_descriptor_is_a_problem():
    assert request.acl_problems("") != []


@windows_only
def test_a_real_folder_a_user_made_reads_as_not_admin_only(tmp_path):
    """The real descriptor through the real API, for a folder this user owns."""
    held = request.descriptor(str(tmp_path))
    assert held.startswith("O:")
    assert request.acl_problems(held)


@windows_only
def test_a_real_locked_folder_parses_as_the_install_leaves_it(tmp_path):
    """The ACL `lock_argv` sets, as the OS spells it back. This user owns the folder and
    pytest's `0o700` temp root hands down an `OW` ACE `/grant:r` keeps, so those two are
    what is left to complain about, and each is `acl_problems` doing its job."""
    installer = load_script("scripts/install-elevated-worker.py")
    folder = tmp_path / "locked"
    folder.mkdir()
    done = subprocess.run(
        installer.lock_argv(str(folder)), capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    held = request.descriptor(str(folder))
    subprocess.run(["icacls", str(folder), "/reset"], capture_output=True, check=False)
    assert "(A;OICI;0x1200a9;;;BU)" in held
    assert ":PAI" in held, "protected: nothing inherited from ProgramData"
    assert [line.split()[0] for line in request.acl_problems(held)] == ["owned", "OW"]


def test_a_path_nothing_can_read_is_no_descriptor():
    assert request.descriptor(r"C:\no\such\folder\anywhere") == ""


# --- state ----------------------------------------------------------------------------


def answer(code: int):
    return lambda argv: subprocess.CompletedProcess(argv, code, "", "")


@pytest.fixture
def on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(request, "WINDOWS", True)
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / request.WORKER_NAME).write_text("worker v2\n", encoding="utf-8")
    copy = tmp_path / "installed.py"
    copy.write_text("worker v2\n", encoding="utf-8")
    return checkout, copy


def state_of(checkout, copy, code=0, held=ADMIN_ONLY):
    return request.state(checkout, answer(code), str(copy), ("C:/x",), lambda _path: held)


def test_a_task_and_a_matching_copy_are_current(on_disk):
    assert state_of(*on_disk) == (request.CURRENT, "installed and current")


def test_no_task_is_missing(on_disk):
    assert state_of(*on_disk, code=request.NOT_REGISTERED) == (request.MISSING, "not installed")


def test_no_copy_is_missing(on_disk, tmp_path):
    checkout, _copy = on_disk
    assert state_of(checkout, tmp_path / "gone.py")[0] == request.MISSING


def test_a_copy_that_differs_from_the_checkout_is_outdated(on_disk):
    """The auto-updating checkout moved and the admin-only copy deliberately did not."""
    checkout, copy = on_disk
    copy.write_text("worker v1\n", encoding="utf-8")
    assert state_of(checkout, copy) == (
        request.OUTDATED,
        f"its copy differs from scripts/{request.WORKER_NAME}",
    )


def test_a_folder_someone_opened_up_is_outdated(on_disk):
    held = f"{ADMIN_ONLY}(A;;FA;;;{USER})"
    assert state_of(*on_disk, held=held) == (request.OUTDATED, "C:/x is not admin-only")


def test_a_scheduler_that_did_not_answer_is_unknown(on_disk):
    """Not "missing": that would tell the operator to install something that is there."""
    assert state_of(*on_disk, code=3)[0] == request.UNKNOWN


def test_a_descriptor_that_could_not_be_read_is_unknown(on_disk):
    assert state_of(*on_disk, held="")[0] == request.UNKNOWN


def test_a_failed_spawn_cannot_read_as_not_registered(monkeypatch):
    def refused(*_a, **_k):
        raise OSError("no schtasks")

    monkeypatch.setattr(request.subprocess, "run", refused)
    assert request.run_query(["schtasks"]).returncode not in (0, request.NOT_REGISTERED)
