"""`scripts/install-elevated-worker.py`: the one job an administrator registers.

Nothing here touches `C:\\ProgramData` or the scheduler: every path the installer holds is
pointed at `tmp_path` by the `planted` fixture, and every `run` is a fake.
"""

from __future__ import annotations

import ast
import ntpath
import os
import re
import stat
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
LOCKED = "O:BAD:P(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;0x1200a9;;;BU)"
SPOOL_ACL = f"{LOCKED}(A;;DC;;;{SID})"
OPENED = f"D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;FA;;;{SID})"
# A real Windows interpreter: the local tier, which CI deselects and `scripts/e2e.py` runs.
windows_only = pytest.mark.local


class Fake:
    """A `Runner` over a fake scheduler: it records every argv, holds one registered
    document, and fails whatever argv contains `fail`."""

    def __init__(self, fail: str = "", held: str | None = None, reads_back: str | None = None):
        self.calls: list[list[str]] = []
        self.fail = fail
        self.held = held
        self.reads_back = reads_back
        self.created_from: list[tuple[Path, bool]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if self.fail and self.fail in joined:
            return subprocess.CompletedProcess(list(argv), 1, "", f"{self.fail} refused")
        if "whoami" in argv[0]:
            return subprocess.CompletedProcess(list(argv), 0, f'"pc\\me","{SID}"\n', "")
        if "/Query" in argv and "/XML" in argv:
            return subprocess.CompletedProcess(
                list(argv), 0 if self.held else 1, self.held or "ERROR: not found", ""
            )
        if "/Delete" in argv:
            self.held = None
        if "/Create" in argv:
            path = Path(argv[argv.index("/XML") + 1])
            self.created_from.append((path, path.is_file()))
            self.held = self.reads_back or installer.task_document()
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def said(self, text: str) -> bool:
        return any(text in " ".join(argv) for argv in self.calls)

    def order(self, *texts: str) -> list[int]:
        lines = [" ".join(argv) for argv in self.calls]
        return [next(i for i, line in enumerate(lines) if text in line) for text in texts]


def descriptors(root: Path, bad: dict[str, str] | None = None):
    """A `descriptor` reading every path as an install leaves it, bar those in `bad`."""

    def read(path: str) -> str:
        for name, held in (bad or {}).items():
            if str(path).endswith(name):
                return held
        return SPOOL_ACL if Path(path) == root / "requests" else LOCKED

    return read


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """The installer's folders under `tmp_path`, an interpreter folder to copy, and a
    `make_protected` that is `os.mkdir` with the semantics of the real one."""
    root = tmp_path / "devkit"
    home = root / "elevated"
    spool = root / "requests"
    interpreter = home / "python"
    for name, value in (
        ("ROOT", root),
        ("HOME", home),
        ("SPOOL", spool),
        ("SCRATCH", home / "tmp"),
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
    monkeypatch.setattr(installer.elevated_request, "descriptor", descriptors(root))

    def make(path: str, _sddl: str) -> int:
        try:
            os.mkdir(path)
        except FileExistsError:
            return installer.ERROR_ALREADY_EXISTS
        return 0

    monkeypatch.setattr(installer, "make_protected", make)
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


def test_its_time_limit_sits_above_the_workers_worst_run():
    """The health check, the kill, the settle, the update and the check after it, each at
    its own bound. `IgnoreNew` makes this finite on purpose."""
    worst = (
        2 * worker.QUERY_TIMEOUT
        + worker.WSL_SETTLE
        + worker.WSL_ANSWER_TIMEOUT
        + worker.KILL_TIMEOUT
        + (worker.WSL_SETTLE_WAIT // worker.WSL_POLL) * (worker.QUERY_TIMEOUT + worker.WSL_POLL)
        + worker.WSL_UPDATE_TIMEOUT
        + worker.WSL_ANSWER_TIMEOUT
    )
    minutes = int(re.fullmatch(r"PT(\d+)M", installer.TIME_LIMIT)[1])
    assert worst < minutes * 60


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
    assert request.task_problems(document()) == []


def test_the_check_compares_the_document_register_would_register():
    run = Fake(held=schtasks.secured(document(), SID))
    code, _message = schtasks.run_check(installer.TASK_NAME, document(), run)
    assert code == schtasks.CHECK_CURRENT


def test_a_task_someone_opened_to_the_user_is_stale():
    """Drift the unelevated check can see, on the property that matters most."""
    run = Fake(held=document().replace(installer.SECURITY, OPENED))
    code, message = schtasks.run_check(installer.TASK_NAME, document(), run)
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


# --- finding 1: no document in a folder the user can write ----------------------------


def test_the_document_is_written_in_the_locked_folder_and_nowhere_else(planted, monkeypatch):
    """Review: a watcher swapped `devkit-task.xml` in the user's temp 20 rounds of 20."""
    monkeypatch.setattr(
        schtasks.tempfile, "gettempdir", lambda: pytest.fail("reached the user's temp")
    )
    run = Fake()
    assert installer.install(run) == 0
    [(path, existed)] = run.created_from
    assert path.parent == planted / "elevated"
    assert existed, "schtasks was handed a file that was there"
    assert not path.exists(), "and it does not stay"


def test_the_document_goes_even_when_registering_fails(planted):
    run = Fake(fail="/Create")
    assert installer.install(run) == 1
    assert not list((planted / "elevated").glob("*.xml"))


def test_the_shared_register_is_not_used_and_is_unchanged():
    assert "devkit_schtasks.register(" not in SOURCE
    assert "secured(xml, sid)" in (REPO_ROOT / "scripts" / "devkit_schtasks.py").read_text(
        encoding="utf-8"
    )


# --- finding 2: ROOT is created locked, and everything is read back -------------------


def test_root_is_never_made_then_locked():
    tree = ast.parse(SOURCE)
    claim = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "claim_root"
    )
    text = ast.get_source_segment(SOURCE, claim)
    assert "make_protected(ROOT, ROOT_DESCRIPTOR)" in text
    assert "mkdir" not in text
    assert "icacls" not in SOURCE.split("def claim_root")[1].split("def _each")[0]


def test_roots_descriptor_is_protected_owned_by_administrators_and_admin_only():
    assert installer.ROOT_DESCRIPTOR.startswith("O:BAD:P(")
    assert request.acl_problems(installer.ROOT_DESCRIPTOR) == []


@windows_only
def test_a_real_folder_is_created_with_its_descriptor_in_one_call(tmp_path):
    """Unelevated, so with no owner in the descriptor: `O:BA` needs the token the
    installer has (probed: 1307 here). The DACL and its protection are what is proven."""
    folder = str(tmp_path / "root")
    sddl = installer.ROOT_DESCRIPTOR.removeprefix("O:BA")
    assert installer.make_protected(folder, sddl) == 0
    held = request.descriptor(folder)
    assert "D:P(" in held
    assert "(A;OICI;0x1200a9;;;BU)" in held
    assert installer.make_protected(folder, sddl) == installer.ERROR_ALREADY_EXISTS
    assert installer.make_protected(str(tmp_path / "x"), installer.ROOT_DESCRIPTOR) == 1307


def test_an_unusable_descriptor_makes_nothing(tmp_path):
    assert installer.make_protected(str(tmp_path / "x"), "not sddl") != 0
    assert not (tmp_path / "x").exists()


def test_off_windows_nothing_is_made_and_the_answer_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    sddl = installer.ROOT_DESCRIPTOR
    assert installer.make_protected(str(tmp_path / "x"), sddl) == installer.ERROR_NOT_SUPPORTED
    assert not (tmp_path / "x").exists()


def test_a_fresh_install_makes_root_with_the_protected_descriptor(planted, monkeypatch):
    made = []
    real = installer.make_protected
    monkeypatch.setattr(
        installer,
        "make_protected",
        lambda path, sddl: made.append((path, sddl)) or real(path, sddl),
    )
    assert installer.claim_root() == ""
    assert made == [(str(planted), installer.ROOT_DESCRIPTOR)]


def test_a_root_that_read_back_wrong_is_a_refusal(planted, monkeypatch):
    held = f"{LOCKED}(A;OICI;FA;;;OW)"
    monkeypatch.setattr(installer.elevated_request, "descriptor", lambda _path: held)
    assert "did not end up admin-only" in installer.claim_root()
    run = Fake()
    assert installer.install(run) == 1, "and so nothing is copied or registered"
    assert not registered(run)


def test_a_root_that_could_not_be_made_is_a_refusal(planted, monkeypatch):
    monkeypatch.setattr(installer, "make_protected", lambda _p, _s: 5)
    assert "could not be created (error 5)" in installer.claim_root()


def test_a_folder_a_user_made_first_is_refused_and_not_adopted(planted, monkeypatch):
    """Any user may create this folder and own it, and an owner can undo any ACL set here."""
    planted.mkdir()
    held = f"O:{SID}D:(A;;FA;;;{SID})"
    monkeypatch.setattr(installer.elevated_request, "descriptor", lambda _path: held)
    run = Fake()
    assert installer.install(run) == 1
    assert run.calls == [], "not chowned, not ACLed: whatever is in it is not trusted"


def test_an_earlier_install_is_kept(planted):
    planted.mkdir()
    assert installer.claim_root() == ""


def test_a_link_at_the_root_is_refused(planted, monkeypatch):
    planted.mkdir()
    monkeypatch.setattr(installer, "is_link", lambda path: Path(path) == planted)
    assert "is a link" in installer.claim_root()


def test_a_folder_appearing_after_the_look_is_a_refusal(planted, monkeypatch):
    monkeypatch.setattr(installer.os.path, "lexists", lambda _path: False)
    planted.mkdir()
    assert "appeared while this ran" in installer.claim_root()


@pytest.mark.parametrize(
    "where",
    ["elevated", "requests", "elevated/python", "elevated/python/Lib", "elevated/python/Lib/os.py"],
)
def test_a_planted_owner_anywhere_is_a_refusal_on_a_reinstall(planted, monkeypatch, where):
    """Review: the gap after `mkdir` let the user plant `HOME` or `requests` and keep them."""
    assert installer.install(Fake()) == 0
    name = where.replace("/", os.sep)
    bad = {name: f"O:{SID}D:(A;;FA;;;BA)"}
    monkeypatch.setattr(installer.elevated_request, "descriptor", descriptors(planted, bad))
    keep = planted / "elevated" / "python" / "Lib" / "os.py"
    run = Fake()
    assert installer.install(run) == 1
    assert run.calls == [], "refused rather than reused or repaired"
    assert keep.exists(), "and nothing was replaced first"


def test_a_file_a_user_can_write_is_a_refusal_on_a_reinstall(planted, monkeypatch):
    assert installer.install(Fake()) == 0
    bad = {"python314.dll": f"{LOCKED}(A;;FA;;;{SID})"}
    monkeypatch.setattr(installer.elevated_request, "descriptor", descriptors(planted, bad))
    assert installer.install(Fake()) == 1


def test_a_link_under_home_is_a_refusal(planted, monkeypatch):
    assert installer.install(Fake()) == 0
    target = planted / "elevated" / "python" / "Lib"
    monkeypatch.setattr(installer, "is_link", lambda path: Path(path) == target)
    problems = installer.tree_problems()
    assert [problem for problem in problems if "is a link" in problem]


def test_a_folder_in_the_spool_is_a_refusal(planted):
    assert installer.install(Fake()) == 0
    (planted / "requests" / "planted").mkdir()
    assert [p for p in installer.tree_problems() if "does not belong in the spool" in p]


def test_a_request_waiting_in_the_spool_is_not_a_mismatch(planted):
    """Those are the user's by design, and nothing runs or reads them."""
    assert installer.install(Fake()) == 0
    (planted / "requests" / "restart-wsl-service.request").touch()
    assert installer.tree_problems() == []
    assert installer.install(Fake()) == 0


def test_everything_is_verified_before_anything_runs_from_it(planted, monkeypatch):
    """Read back after the copy: the owner set on what was just copied, not assumed."""
    real = installer.tree_problems
    seen = []

    def spy(complete=False):
        seen.append(complete)
        return real(complete)

    monkeypatch.setattr(installer, "tree_problems", spy)
    run = Fake()
    assert installer.install(run) == 0
    assert seen == [False, True]
    smoke, create = run.order(installer.SMOKE, "/Create")
    assert smoke < create


def test_a_tree_that_reads_back_wrong_after_the_copy_runs_nothing_from_it(planted, monkeypatch):
    calls = iter([[], ["a mismatch"]])
    monkeypatch.setattr(installer, "tree_problems", lambda complete=False: next(calls))
    run = Fake()
    assert installer.install(run) == 1
    assert not run.said(installer.SMOKE)
    assert not registered(run)


def test_a_verification_with_nothing_there_says_what_is_missing(planted):
    planted.mkdir()
    assert [p for p in installer.tree_problems(complete=True) if "is missing" in p]
    assert installer.tree_problems() == [], "a first install has not made them yet"


def test_the_owner_is_set_recursively_and_never_through_a_link():
    assert installer.own_argv(r"C:\x") == [
        "icacls",
        r"C:\x",
        "/setowner",
        "*S-1-5-32-544",
        "/T",
        "/C",
        "/L",
    ]


def test_the_owner_is_set_on_the_whole_tree_before_it_is_read_back(planted, monkeypatch):
    real = installer.tree_problems
    run = Fake()
    set_first = []

    def spy(complete=False):
        if complete:
            set_first.append(run.said("/setowner"))
        return real(complete)

    monkeypatch.setattr(installer, "tree_problems", spy)
    assert installer.install(run) == 0
    assert set_first == [True]
    assert [argv for argv in run.calls if "/setowner" in argv] == [installer.own_argv(str(planted))]


def test_the_spool_lets_the_user_add_a_file_and_nothing_else():
    """No `AD`, so no subfolder and no junction; no delete, so not the sentinel; no
    `(OI)`, so the grant does not reach any file."""
    assert installer.spool_argv(r"C:\x", SID) == ["icacls", r"C:\x", "/grant", f"*{SID}:(WD)"]


def test_the_folders_are_the_contracts():
    assert installer.ROOT == request.ROOT == worker.ROOT
    assert installer.SCRATCH == worker.SCRATCH
    assert installer.SENTINEL == worker.SENTINEL


# --- finding 3: the install trusts its moment, and says so ---------------------------


@pytest.mark.parametrize("tool", sorted(installer.TOOLS))
def test_every_tool_is_run_from_system32(tool):
    assert installer.absolute([tool, "x"]) == [rf"C:\Windows\System32\{tool}.exe", "x"]


def test_the_private_interpreter_may_run_and_nothing_else_may():
    python = rf"{installer.ROOT}\elevated\python\python.exe"
    assert installer.absolute([python, "-c", "x"])[0] == python
    assert installer.absolute([r"c:\windows\system32\sc.exe"])
    for argv in (["git"], ["python"], [r"C:\Users\me\evil.exe"], ["whoami.exe"]):
        with pytest.raises(ValueError, match="is not a program"):
            installer.absolute(argv)


def test_every_argv_the_shared_modules_hand_it_is_covered():
    """A bare name no one mapped would raise on the elevated run, not in a test."""
    for argv in (
        schtasks.register_argv("t", Path("x")),
        schtasks.query_xml_argv("t"),
        schtasks.delete_argv("t"),
        list(schtasks.WHOAMI_ARGV),
        installer.installer_cli.query_argv("t"),
        installer.installer_cli.uninstall_argv("t"),
        installer.own_argv("x"),
        installer.spool_argv("x", SID),
    ):
        assert ntpath.isabs(installer.absolute(argv)[0])


def test_a_child_gets_a_scrubbed_environment_and_a_fixed_cwd(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\evil")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    installer._run_argv(["schtasks", "/Query"])
    assert seen["argv"][0] == r"C:\Windows\System32\schtasks.exe"
    assert seen["env"] == installer.CLEAN_ENV
    assert seen["cwd"] == installer.SYSTEM32


def test_a_run_not_isolated_re_executes_under_i_s(monkeypatch):
    class Flags:
        isolated = 0
        no_site = 0

    monkeypatch.setattr(installer.sys, "flags", Flags)
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 7)

    assert installer.reexec("x.py", ["--yes"], run) == 7
    assert seen["argv"][1:] == ["-I", "-S", "-B", "x.py", "--yes"]
    assert seen["env"] == installer.CLEAN_ENV
    assert seen["cwd"] == installer.SYSTEM32


def test_an_isolated_run_does_not_re_execute(monkeypatch):
    class Flags:
        isolated = 1
        no_site = 1

    monkeypatch.setattr(installer.sys, "flags", Flags)
    assert installer.reexec("x.py", ["--yes"], lambda *_a, **_k: pytest.fail("ran")) is None


def test_the_re_exec_comes_before_anything_of_ours_is_imported():
    lines = SOURCE.splitlines()
    assert lines.index("    _code = reexec(__file__, sys.argv[1:])") < lines.index(
        "import devkit_schtasks"
    )


def test_the_person_is_told_the_isolated_command():
    """Isolation has to start with the first interpreter: `site` runs before any script."""
    assert request.install_command(Path("C:/d")).startswith("python -I -S ")


def test_the_false_claim_is_gone_and_the_residual_risk_is_stated():
    false_claim = "Nothing elevated comes from a folder the user can write"
    for name in ("install-elevated-worker.py", "CLAUDE.md"):
        text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        flat = " ".join(text.split())
        assert false_claim not in flat, name
        assert "assumes a clean machine at that moment" in flat, name
    assert false_claim not in " ".join((REPO_ROOT / "README.md").read_text("utf-8").split())


def test_the_plan_says_what_a_person_is_trusting():
    assert "assumes a clean machine right now" in installer.plan()


@windows_only
def test_it_really_runs_isolated_with_its_own_imports(tmp_path):
    """The re-exec and `-I -S` together, against a hostile environment. `--status` only
    reads the scheduler, and this installer's imports all have to resolve under `-I`."""
    (tmp_path / "harness_state.py").write_text("raise SystemExit('planted')\n", "ascii")
    done = subprocess.run(
        [
            sys.executable,
            "-S",
            str(REPO_ROOT / "scripts" / "install-elevated-worker.py"),
            "--status",
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        cwd=tmp_path,
    )
    assert "planted" not in done.stdout + done.stderr
    assert "Traceback" not in done.stderr


# --- item 4: the old task goes, and the descriptor is read back ------------------------


def test_an_existing_task_is_deleted_before_it_is_created(planted):
    """`/Create /F` keeps the access a task already has, whatever the document says."""
    run = Fake(held=document().replace(installer.SECURITY, OPENED))
    assert installer.install(run) == 0
    deleted, created = run.order("/Delete", "/Create")
    assert deleted < created


def test_a_task_that_would_not_delete_is_a_refusal(planted):
    run = Fake(held=document(), fail="/Delete")
    assert installer.install(run) == 1
    assert not registered(run)


def test_a_first_install_deletes_nothing(planted):
    run = Fake()
    assert installer.install(run) == 0
    assert not run.said("/Delete")


def test_the_descriptor_is_read_back_after_registering(planted):
    run = Fake()
    assert installer.install(run) == 0
    last_query = max(i for i, argv in enumerate(run.calls) if "/Query" in argv)
    assert last_query > run.order("/Create")[0]


def test_a_task_that_reads_back_open_is_refused_and_taken_away(planted, capsys):
    """A task the user can rewrite is worse than none."""
    run = Fake(reads_back=document().replace(installer.SECURITY, OPENED))
    assert installer.install(run) == 1
    assert run.held is None
    assert "read back wrong" in capsys.readouterr().err


def test_a_task_that_reads_back_with_no_descriptor_is_refused(planted):
    run = Fake(reads_back="<Task><Exec><Command>x</Command></Exec></Task>")
    assert installer.install(run) == 1
    assert run.held is None


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
    builtin_enough = {"collections", "os", "sys", "time"}
    assert imported - builtin_enough <= set(installer.SMOKE.removeprefix("import ").split(", "))


def test_nothing_the_worker_imports_is_left_behind():
    assert not {"ctypes", "json", "re", "subprocess", "dataclasses"} & set(installer.LEFT_BEHIND)


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
    assert "restart-wsl-service" in done.stdout


def test_a_full_install_copies_then_registers(planted):
    run = Fake()
    assert installer.install(run) == 0
    assert (planted / "elevated" / "elevated_worker.py").read_bytes() == (
        REPO_ROOT / "scripts" / "elevated_worker.py"
    ).read_bytes()
    assert (planted / "elevated" / "python" / "pythonw.exe").is_file()
    assert (planted / "elevated" / "tmp").is_dir(), "where `wsl --update` downloads"
    assert (planted / "requests" / installer.SENTINEL).is_file()
    assert run.said(f"*{SID}:(WD)")
    assert registered(run)


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


def test_a_reinstall_replaces_the_interpreter_and_keeps_the_rate_limit(planted):
    """Upgrading is the deliberate way back in, and it must not hand a loop a fresh window."""
    assert installer.install(Fake()) == 0
    (planted / "elevated" / "rate-limit.json").write_text('{"x": 1}', encoding="utf-8")
    (planted / "elevated" / "python" / "stale.dll").touch()
    assert installer.install(Fake(held=document())) == 0
    assert not (planted / "elevated" / "python" / "stale.dll").exists()
    assert (planted / "elevated" / "rate-limit.json").is_file()


def test_uninstall_takes_the_task_and_the_folder(planted):
    planted.mkdir()
    (planted / "x").touch()
    run = Fake()
    assert installer.uninstall(run) == 0
    assert not planted.exists()


def test_uninstall_is_not_jammed_by_a_read_only_file(planted):
    planted.mkdir()
    jam = planted / "x"
    jam.touch()
    jam.chmod(stat.S_IREAD)
    assert installer.uninstall(Fake()) == 0
    assert not planted.exists()


def test_uninstall_stops_at_a_task_it_could_not_delete(planted):
    planted.mkdir()
    assert installer.uninstall(Fake(fail="/Delete", held="x")) == 1
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


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ("outdated", "its copy differs"),
        ("missing", "not installed"),
        ("exposed", "the task is not read-only"),
    ],
)
def test_check_reports_what_nothing_else_prompts_for(windows, monkeypatch, capsys, state, why):
    monkeypatch.setattr(schtasks, "run_check", lambda *_a: (0, "current"))
    monkeypatch.setattr(installer.elevated_request, "state", lambda *_a: (state, why))
    assert installer.main(["--check"]) == schtasks.CHECK_STALE
    assert f"{state}: {why}" in capsys.readouterr().err


def test_it_says_windows_only_elsewhere(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out


def test_an_elevated_run_writes_no_bytecode_into_the_checkout():
    """Set before the first import of ours: an elevated `__pycache__` in a tree is what
    `elevated_writes.py` exists to catch."""
    lines = SOURCE.splitlines()
    assert lines.index("sys.dont_write_bytecode = True") < lines.index("import devkit_schtasks")


# --- the pieces an install is built from ----------------------------------------------


class NotIsolated:
    isolated = 0
    no_site = 0


def test_the_base_python_is_the_real_interpreter_behind_a_virtualenv(tmp_path, monkeypatch):
    (tmp_path / "python.exe").touch()
    monkeypatch.setattr(installer.sys, "base_prefix", str(tmp_path))
    assert installer.base_python() == os.path.join(str(tmp_path), "python.exe")


def test_with_no_base_interpreter_it_is_this_one(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.sys, "base_prefix", str(tmp_path))
    assert installer.base_python() == sys.executable


def test_the_isolated_argv_is_the_base_python_under_its_guards(tmp_path, monkeypatch):
    (tmp_path / "python.exe").touch()
    monkeypatch.setattr(installer.sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(installer.sys, "flags", NotIsolated)
    assert installer.isolated_argv("x.py", ["--check"]) == [
        os.path.join(str(tmp_path), "python.exe"),
        "-I",
        "-S",
        "-B",
        "x.py",
        "--check",
    ]


def test_an_isolated_run_needs_no_argv(monkeypatch):
    class Isolated:
        isolated = 1
        no_site = 1

    monkeypatch.setattr(installer.sys, "flags", Isolated)
    assert installer.isolated_argv("x.py", ["--yes"]) == []
    half = type("Half", (), {"isolated": 1, "no_site": 0})
    monkeypatch.setattr(installer.sys, "flags", half)
    assert installer.isolated_argv("x.py", [])[1:4] == ["-I", "-S", "-B"], "both, or re-exec"


def test_the_task_arguments_are_the_guards_then_the_installed_copy():
    assert installer.worker_arguments() == (
        r'-I -S -B "C:\ProgramData\devkit\elevated\elevated_worker.py"'
    )


def test_elevating_adds_the_descriptor_principal_and_context_once():
    base = (
        "<Task>\n  <RegistrationInfo>\n  </RegistrationInfo>\n"
        "  <Settings>\n  </Settings>\n  <Actions><Exec/></Actions>\n</Task>"
    )
    text = installer.elevate(base)
    assert text.count(f"<SecurityDescriptor>{installer.SECURITY}</SecurityDescriptor>") == 1
    assert text.index("</SecurityDescriptor>") < text.index("</RegistrationInfo>")
    assert text.count("<RunLevel>HighestAvailable</RunLevel>") == 1
    assert text.index("<Principals>") < text.index("<Settings>")
    assert '<Actions Context="Author">' in text
    assert request.task_problems(text) == []


def test_each_elevating_replacement_is_made_once():
    """Each replacement is bounded to one, so an `<Actions>` elsewhere is untouched."""
    base = "<RegistrationInfo></RegistrationInfo>\n  <Settings>\n<Actions><Actions>"
    text = installer.elevate(base)
    assert text.count("<Principals>") == 1
    assert text.count('<Actions Context="Author">') == 1
    assert text.count("<Actions>") == 1


def test_a_plain_file_a_folder_and_nothing_are_not_links(tmp_path):
    (tmp_path / "f").touch()
    assert not installer.is_link(str(tmp_path / "f"))
    assert not installer.is_link(str(tmp_path))
    assert not installer.is_link(str(tmp_path / "absent"))


@pytest.mark.parametrize(
    ("mode", "attributes"),
    [
        (stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT),
        (stat.S_IFLNK, 0),
    ],
    ids=["junction", "symlink"],
)
def test_a_reparse_point_or_a_symlink_is_a_link(monkeypatch, mode, attributes):
    info = type("Info", (), {"st_mode": mode, "st_file_attributes": attributes})
    monkeypatch.setattr(installer.os, "lstat", lambda _path: info)
    linked = installer.is_link("x")
    monkeypatch.undo()
    assert linked


def test_judging_reads_the_descriptor_back_and_names_the_path(planted, monkeypatch):
    planted.mkdir()
    assert installer.judged(str(planted)) == []
    held = f"O:{SID}D:(A;;FA;;;{SID})"
    monkeypatch.setattr(installer.elevated_request, "descriptor", lambda _path: held)
    assert installer.judged(str(planted)) == [
        f"{planted}: owned by {SID}",
        f"{planted}: {SID} holds FA",
    ]
    assert installer.judged(str(planted), frozenset({"FA"})) == [f"{planted}: owned by {SID}"]


def test_a_link_is_judged_without_reading_through_it(planted, monkeypatch):
    planted.mkdir()
    monkeypatch.setattr(
        installer.elevated_request, "descriptor", lambda _path: pytest.fail("read it")
    )
    monkeypatch.setattr(installer, "is_link", lambda _path: True)
    assert installer.judged(str(planted)) == [f"{planted} is a link"]


def test_the_walk_reaches_every_path_and_follows_no_link(tmp_path, monkeypatch):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f").touch()
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / "beyond").touch()
    monkeypatch.setattr(installer, "is_link", lambda path: Path(path).name == "linked")
    found = {Path(path).relative_to(tmp_path).as_posix() for path in installer.walk(str(tmp_path))}
    assert found == {"a", "a/b", "a/b/f", "linked"}


def test_an_installed_spool_has_no_problems(planted):
    assert installer.install(Fake()) == 0
    (planted / "requests" / "restart-wsl-service.request").touch()
    assert installer.spool_problems() == []


def test_a_spool_judges_its_own_grant_and_the_sentinel_strictly(planted, monkeypatch):
    """The spool may grant adding a file; the sentinel in it may not grant even that."""
    assert installer.install(Fake()) == 0
    spool = planted / "requests"
    bad = {installer.SENTINEL: SPOOL_ACL}
    monkeypatch.setattr(installer.elevated_request, "descriptor", descriptors(planted, bad))
    assert installer.spool_problems() == [f"{spool / installer.SENTINEL}: {SID} holds DC"]


def test_what_does_not_belong_in_the_spool_is_named(planted, monkeypatch):
    assert installer.install(Fake()) == 0
    spool = planted / "requests"
    (spool / "folder").mkdir()
    (spool / "link").touch()
    monkeypatch.setattr(installer, "is_link", lambda path: Path(path).name == "link")
    assert sorted(installer.spool_problems()) == [
        f"{spool / 'folder'} does not belong in the spool",
        f"{spool / 'link'} does not belong in the spool",
    ]


def test_installing_the_files_lays_out_the_tree_and_sets_owners(planted, tmp_path):
    planted.mkdir()
    run = Fake()
    assert installer.install_files(tmp_path / "base", SID, run) == ""
    home = planted / "elevated"
    assert (home / installer.WORKER_NAME).read_bytes() == (
        REPO_ROOT / "scripts" / installer.WORKER_NAME
    ).read_bytes()
    assert (home / "tmp").is_dir()
    assert (home / "python" / "pythonw.exe").is_file()
    assert (planted / "requests" / installer.SENTINEL).is_file()
    assert run.calls == [
        installer.own_argv(str(planted)),
        installer.spool_argv(str(planted / "requests"), SID),
    ]


def test_installing_the_files_stops_at_the_first_refusal(planted, tmp_path):
    planted.mkdir()
    run = Fake(fail="/setowner")
    assert installer.install_files(tmp_path / "base", SID, run) == "/setowner refused"
    assert len(run.calls) == 1, "the spool is not granted on a tree whose owner is unset"


def test_registering_a_first_task_leaves_no_document(planted):
    (planted / "elevated").mkdir(parents=True)
    run = Fake()
    assert installer.register_task(run) == ""
    assert not run.said("/Delete")
    assert registered(run)
    assert not list((planted / "elevated").iterdir())


def test_registering_over_a_task_that_will_not_go_registers_nothing(planted):
    (planted / "elevated").mkdir(parents=True)
    run = Fake(held=document(), fail="/Delete")
    said = installer.register_task(run)
    assert said == f"the existing {installer.TASK_NAME} could not be deleted: /Delete refused"
    assert not registered(run)


def test_a_registration_the_scheduler_refuses_is_its_answer(planted):
    (planted / "elevated").mkdir(parents=True)
    assert installer.register_task(Fake(fail="/Create")) == "/Create refused"
    assert not list((planted / "elevated").iterdir())


def test_a_task_that_reads_back_open_is_registered_and_deleted_again(planted):
    (planted / "elevated").mkdir(parents=True)
    run = Fake(reads_back=document().replace(installer.SECURITY, OPENED))
    said = installer.register_task(run)
    assert said.startswith(f"{installer.TASK_NAME} read back wrong ({SID} holds FA)")
    assert run.held is None


def test_a_refusal_names_five_problems_and_runs_nothing(capsys):
    problems = [f"problem {index}" for index in range(8)]
    assert installer.refused(problems) == 1
    err = capsys.readouterr().err
    assert "problem 4" in err
    assert "problem 5" not in err
    assert f"Nothing was run from {installer.ROOT}" in err


def test_why_a_run_may_not_install():
    assert installer.refusal(elevated=True, static_checkout=True) == ""
    unelevated = installer.refusal(elevated=False, static_checkout=True)
    assert "needs an elevated terminal" in unelevated
    assert request.install_command(installer.REPO_ROOT) in unelevated
    assert "needs an elevated terminal" in installer.refusal(False, False), "elevation first"
    assert "temporary checkout" in installer.refusal(elevated=True, static_checkout=False)


def test_the_parser_reads_each_mode_and_the_yes():
    parser = installer.build_parser()
    assert parser.parse_args([]).apply is False
    args = parser.parse_args(["--uninstall", "--yes"])
    assert (args.uninstall, args.apply, args.check, args.status) == (True, True, False, False)
    assert parser.parse_args(["--check"]).check


def test_the_parser_allows_one_mode_at_a_time(capsys):
    with pytest.raises(SystemExit):
        installer.build_parser().parse_args(["--check", "--status"])
    assert "not allowed with" in capsys.readouterr().err


def test_the_check_is_current_when_the_task_and_the_copy_are(monkeypatch, capsys):
    run = Fake(held=schtasks.secured(document(), SID))
    asked = []

    def state(checkout, runner):
        asked.append((checkout, runner))
        return request.CURRENT, "installed and current"

    monkeypatch.setattr(installer.elevated_request, "state", state)
    assert installer.check(run) == schtasks.CHECK_CURRENT
    assert asked == [(installer.REPO_ROOT, run)]
    assert capsys.readouterr().err == ""


def test_the_check_reports_a_task_that_is_not_registered_before_the_copy(monkeypatch, capsys):
    monkeypatch.setattr(
        installer.elevated_request, "state", lambda *_a: pytest.fail("asked about the copy")
    )
    assert installer.check(Fake()) == schtasks.CHECK_STALE
    assert installer.TASK_NAME in capsys.readouterr().err


def test_the_check_reports_an_outdated_copy_as_stale(monkeypatch, capsys):
    run = Fake(held=schtasks.secured(document(), SID))
    monkeypatch.setattr(
        installer.elevated_request, "state", lambda *_a: (request.OUTDATED, "its copy differs")
    )
    assert installer.check(run) == schtasks.CHECK_STALE
    assert f"{installer.TASK_NAME} is outdated: its copy differs." in capsys.readouterr().err
