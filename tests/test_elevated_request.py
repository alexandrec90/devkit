"""`scripts/elevated_request.py`: the one way to ask, and whether there is anyone to ask."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

request = load_script("scripts/elevated_request.py")
worker = load_script("scripts/elevated_worker.py")

VERB = "restart-wsl-service"
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


def ask(verb, spool, copy, checkout=Path("C:/devkit")):
    return request.request(verb, str(spool), str(copy), checkout)


def test_the_verb_list_is_defined_once_and_the_workers_copy_matches():
    assert request.VERBS == (VERB,)
    assert set(request.VERBS) == set(worker.VERBS)


def test_a_request_is_an_empty_file_of_the_exact_name(installed):
    spool, copy = installed
    assert ask(VERB, spool, copy) == f"{VERB} requested: the worker takes it within five minutes"
    assert [path.name for path in spool.iterdir()] == [f"{VERB}.request"]
    assert (spool / f"{VERB}.request").stat().st_size == 0
    assert worker.verb_of(f"{VERB}.request") == VERB, "the worker reads what this writes"


@pytest.mark.parametrize(
    "verb",
    [
        "",
        "reboot-when-idle",
        "restart-docker-service",
        "restart-wsl-service.request",
        "..\\x",
        "RESTART-WSL-SERVICE",
    ],
)
def test_a_verb_off_the_list_is_a_bug_and_writes_nothing(installed, verb):
    spool, copy = installed
    with pytest.raises(ValueError, match="is not one of"):
        ask(verb, spool, copy)
    assert list(spool.iterdir()) == []


def test_asking_twice_is_one_request(installed):
    spool, copy = installed
    ask(VERB, spool, copy)
    assert ask(VERB, spool, copy) == f"{VERB} is already waiting for the worker"
    assert len(list(spool.iterdir())) == 1


def test_no_worker_says_so_and_names_the_exact_command(tmp_path, monkeypatch):
    monkeypatch.setattr(request, "WINDOWS", True)
    checkout = tmp_path / "devkit"
    said = ask(VERB, tmp_path / "absent", tmp_path / "absent.py", checkout)
    assert said == request.not_installed(checkout)
    assert "python -I -S" in said
    assert "--yes`" in said
    assert "install-elevated-worker.py" in said
    assert "elevated terminal" in said
    assert "no job can" in said


def test_a_spool_with_no_worker_copy_is_not_installed(installed, tmp_path):
    spool, _copy = installed
    said = ask(VERB, spool, tmp_path / "gone.py", tmp_path / "devkit")
    assert "not installed" in said


def test_a_request_with_no_worker_leaves_a_dated_note_for_the_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(request, "WINDOWS", True)
    checkout = tmp_path / "devkit"
    ask(VERB, tmp_path / "absent", tmp_path / "w.py", checkout)
    assert request.read_waiting(checkout, time.time()) == [VERB]


def test_a_request_a_worker_took_leaves_no_note(installed, tmp_path):
    spool, copy = installed
    checkout = tmp_path / "devkit"
    ask(VERB, spool, copy, checkout)
    assert not (checkout / request.WAITING).exists()


def test_a_note_lands_in_the_static_checkout_the_pass_reads(tmp_path, monkeypatch):
    """A request from a box or a session tree has to reach the permanent checkout's logs."""
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    assert request.static_checkout() == tmp_path


def test_a_note_falls_back_to_this_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path / "gone"))
    assert request.static_checkout() == request.CHECKOUT


def test_a_note_ages_out(tmp_path):
    request.note_waiting(tmp_path, VERB, 1_000.0)
    assert request.read_waiting(tmp_path, 1_000.0 + request.WAITING_FOR - 1) == [VERB]
    assert request.read_waiting(tmp_path, 1_000.0 + request.WAITING_FOR) == []


def test_a_note_for_a_verb_off_the_list_means_nothing(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / request.WAITING).write_text(json.dumps({"reboot-when-idle": 1.0}), encoding="utf-8")
    assert request.read_waiting(tmp_path, 2.0) == []


@pytest.mark.parametrize("text", ["not json", "[1]", '{"restart-wsl-service": "soon"}'])
def test_a_note_that_will_not_read_is_no_note(tmp_path, text):
    (tmp_path / "logs").mkdir()
    (tmp_path / request.WAITING).write_text(text, encoding="utf-8")
    assert request.read_waiting(tmp_path, 2.0) == []


def test_no_note_is_no_note_and_clearing_none_is_fine(tmp_path):
    assert request.read_waiting(tmp_path, 2.0) == []
    request.clear_waiting(tmp_path)


def test_a_note_that_cannot_be_written_is_not_the_callers_problem(tmp_path):
    blocked = tmp_path / "file"
    blocked.touch()
    request.note_waiting(blocked, VERB, 1.0)


def test_asking_never_creates_the_spool(tmp_path, monkeypatch):
    """A folder a non-admin makes under ProgramData is theirs, ACL and all, and the
    installer refuses to adopt one. So the unelevated side may never be what made it."""
    monkeypatch.setattr(request, "WINDOWS", True)
    spool = tmp_path / "requests"
    ask(VERB, spool, tmp_path / "w.py", tmp_path / "devkit")
    assert not spool.exists()


def test_the_only_folder_this_makes_is_the_checkouts_logs():
    source = (REPO_ROOT / "scripts" / "elevated_request.py").read_text(encoding="utf-8")
    assert source.count("mkdir") == 1
    assert "path.parent.mkdir" in source
    assert "makedirs" not in source


def test_a_spool_that_refuses_says_so(installed, monkeypatch):
    spool, copy = installed

    def refused(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(request.os, "open", refused)
    assert ask(VERB, spool, copy).startswith(
        f"{VERB} was not requested: the spool refused this user"
    )


def test_it_says_windows_only_elsewhere_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(request, "WINDOWS", False)
    assert "Windows-only" in ask(VERB, tmp_path, tmp_path / "w.py", tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert request.state(tmp_path, lambda _a: pytest.fail("asked"))[0] == request.CURRENT


def test_the_command_line_validates_against_the_list(capsys):
    with pytest.raises(SystemExit):
        request.main(["reboot-when-idle"])
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


def test_the_spool_alone_may_grant_adding_a_file():
    sddl = f"{ADMIN_ONLY}(A;;DC;;;{USER})"
    assert request.acl_problems(sddl, rights=request.SPOOL_RIGHTS) == []
    assert request.acl_problems(f"{ADMIN_ONLY}(A;;FA;;;{USER})", rights=request.SPOOL_RIGHTS)


def test_the_ace_programdata_hands_every_user_is_a_problem():
    """`BUILTIN\\Users:(CI)(WD,AD,WEA,WA)`, inherited unless the descriptor is protected."""
    assert request.acl_problems("O:BAD:AI(A;CIID;0x116;;;BU)") == ["BU holds 0x116"]


def test_a_deny_ace_is_no_grant():
    assert request.acl_problems(f"O:BAD:(D;;FA;;;{USER})(A;;FA;;;BA)") == []


def test_an_unreadable_descriptor_is_a_problem():
    assert request.acl_problems("") != []


def test_the_aces_alone_are_judged_with_no_owner_needed():
    """A task's descriptor carries a DACL and no owner, so the ACEs are their own check."""
    assert request.ace_problems(READ_ONLY) == []
    held = f"{READ_ONLY}(A;;FA;;;{USER})(A;;0x1301bf;;;BU)"
    assert request.ace_problems(held) == [f"{USER} holds FA", "BU holds 0x1301bf"]


def test_an_ace_is_judged_against_the_rights_and_admins_it_is_given():
    held = f"D:(A;;DC;;;{USER})(D;;FA;;;{USER})"
    assert request.ace_problems(held) == [f"{USER} holds DC"]
    assert request.ace_problems(held, rights=request.SPOOL_RIGHTS) == []
    assert request.ace_problems(held, admins=frozenset({USER})) == []


@windows_only
def test_a_real_folder_a_user_made_reads_as_not_admin_only(tmp_path):
    """The real descriptor through the real API, for a folder this user owns."""
    held = request.descriptor(str(tmp_path))
    assert held.startswith("O:")
    assert request.acl_problems(held)


def test_a_path_nothing_can_read_is_no_descriptor():
    assert request.descriptor(r"C:\no\such\folder\anywhere") == ""


def test_off_windows_no_descriptor_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(request.sys, "platform", "linux")
    assert request.descriptor(str(tmp_path)) == ""


# --- the task's own descriptor -------------------------------------------------------

READ_ONLY = "D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;FR;;;AU)"


def task(descriptor: str = READ_ONLY) -> str:
    """A task document as `schtasks /Query /XML` prints one, CRCRLF line ends and all."""
    held = f"<SecurityDescriptor>{descriptor}</SecurityDescriptor>\r\r\n" if descriptor else ""
    return f"<Task>\r\r\n  <RegistrationInfo>\r\r\n    {held}  </RegistrationInfo>\r\r\n</Task>"


def test_the_read_only_descriptor_has_no_problems():
    assert request.task_problems(task()) == []


def test_a_descriptor_secured_handed_the_user_is_a_problem():
    """`devkit_schtasks.secured`'s, which a task keeps through `/Create /F`."""
    held = f"D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;FA;;;{USER})"
    assert request.task_problems(task(held)) == [f"{USER} holds FA"]


def test_a_task_with_no_descriptor_is_a_problem():
    assert request.task_problems(task("")) == ["it carries no security descriptor"]
    assert request.task_problems("") == ["it carries no security descriptor"]


# --- state ----------------------------------------------------------------------------


def answer(code: int, document: str = ""):
    return lambda argv: subprocess.CompletedProcess(argv, code, document or task(), "")


@pytest.fixture
def on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(request, "WINDOWS", True)
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / request.WORKER_NAME).write_text("worker v2\n", encoding="utf-8")
    copy = tmp_path / "installed.py"
    copy.write_text("worker v2\n", encoding="utf-8")
    return checkout, copy


def state_of(checkout, copy, code=0, held=ADMIN_ONLY, document=""):
    return request.state(checkout, answer(code, document), str(copy), ("C:/x",), lambda _p: held)


def test_a_task_and_a_matching_copy_are_current(on_disk):
    assert state_of(*on_disk) == (request.CURRENT, "installed and current")


def test_the_query_it_makes_is_the_document(on_disk):
    checkout, copy = on_disk
    seen = []

    def run(argv):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, task(), "")

    request.state(checkout, run, str(copy), ("C:/x",), lambda _p: ADMIN_ONLY)
    assert seen == [["schtasks", "/Query", "/TN", request.TASK_NAME, "/XML"]]


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


def test_a_task_the_user_can_rewrite_is_exposed(on_disk):
    held = task(f"D:(A;;FA;;;BA)(A;;FA;;;{USER})")
    assert state_of(*on_disk, document=held) == (
        request.EXPOSED,
        "the devkit-elevated task is not read-only to the user",
    )


def test_exposure_is_asked_before_age(on_disk):
    """An outdated copy is nothing to act on, so it may not hide a task that is."""
    checkout, copy = on_disk
    copy.write_text("worker v1\n", encoding="utf-8")
    held = task(f"D:(A;;FA;;;{USER})")
    assert state_of(checkout, copy, document=held)[0] == request.EXPOSED


def test_a_folder_someone_opened_up_is_exposed(on_disk):
    held = f"{ADMIN_ONLY}(A;;FA;;;{USER})"
    assert state_of(*on_disk, held=held) == (request.EXPOSED, "C:/x is not admin-only")


def test_a_scheduler_that_did_not_answer_is_unknown(on_disk):
    """Not "missing": that would say something false about a PC that may have one."""
    assert state_of(*on_disk, code=3)[0] == request.UNKNOWN


def test_a_descriptor_that_could_not_be_read_is_unknown(on_disk):
    assert state_of(*on_disk, held="")[0] == request.UNKNOWN


def test_a_copy_differs_only_when_its_bytes_do(tmp_path):
    repo, installed = tmp_path / "repo.py", tmp_path / "installed.py"
    repo.write_bytes(b"worker v2\n")
    installed.write_bytes(b"worker v2\n")
    assert not request.copy_differs(repo, installed)
    installed.write_bytes(b"worker v2\r\n")
    assert request.copy_differs(repo, installed), "a line ending is a different copy"


def test_a_copy_that_cannot_be_read_raises_for_state_to_answer(tmp_path):
    (tmp_path / "repo.py").write_bytes(b"x")
    with pytest.raises(OSError):
        request.copy_differs(tmp_path / "repo.py", tmp_path / "gone.py")


def test_a_failed_spawn_cannot_read_as_not_registered(monkeypatch):
    def refused(*_a, **_k):
        raise OSError("no schtasks")

    monkeypatch.setattr(request.subprocess, "run", refused)
    assert request.run_query(["schtasks"]).returncode not in (0, request.NOT_REGISTERED)


# --- results ----------------------------------------------------------------------------


def test_results_read_what_the_worker_wrote(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps({VERB: {"result": "failed", "detail": "why", "at": 1.0}}), encoding="utf-8"
    )
    assert request.results(str(path)) == {VERB: ("failed", "why")}


@pytest.mark.parametrize("text", ["not json", "[1]"])
def test_results_that_will_not_read_are_no_results(tmp_path, text):
    path = tmp_path / "results.json"
    path.write_text(text, encoding="utf-8")
    assert request.results(str(path)) == {}


def test_no_results_are_no_results(tmp_path):
    assert request.results(str(tmp_path / "absent.json")) == {}
