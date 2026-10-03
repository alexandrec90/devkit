"""The contract every `scripts/install-*.py` has to satisfy, checked across all of them.

`tests/test_scheduled_jobs.py` holds the *job* contract: registered from XML, names an
artifact, points `schedule_health` at it. This file holds the *installer* contract, and it
exists because the failure that happened was not inside any installer -- it was that
nothing could drive them. Two jobs had never been registered on the workstation that runs
them, two installers had `--check` and four had only `--status`, and the only registry of
what needed installing was a README table a person had to remember to read. Not every
installer registers a *job* -- the git policy and the Windows Terminal profile are machine
state this pass keeps current with no schedule of their own -- so the properties below
split into the ones every installer owes and the ones only a job does.

So the properties are asserted for every installer at once, found rather than listed:

1. `installers.py` discovers it, so the scheduled pass reaches it.
2. It answers `--check` and `--yes`, and `--check` answers 1 when nothing is registered.
3. If it registers a job, it says which group that job belongs to, it checks through the
   one shared implementation, and it consults the ledger so a stood-down job lands
   disabled.
4. `harness-switch.py`'s list of delivery jobs is exactly the installers that say so.
5. The README's jobs table names it.
6. Its task runs under `log-wrap.py --always`, so every failure lands on the
   harness-events ledger -- or it is in `OFF_THE_LEDGER` with the reason.
"""

from __future__ import annotations

import inspect
import re
from xml.sax.saxutils import unescape

import pytest
from support import REPO_ROOT, load_script

installers = load_script("scripts/installers.py")
switch = load_script("scripts/harness-switch.py")
log_wrap = load_script("scripts/log-wrap.py")

INSTALLERS = sorted(REPO_ROOT.glob("scripts/install-*.py"))
MODULES = [(path.name, load_script(f"scripts/{path.name}")) for path in INSTALLERS]
IDS = [name for name, _module in MODULES]
JOBS = [(name, module) for name, module in MODULES if getattr(module, "TASK_NAME", "")]
JOB_IDS = [name for name, _module in JOBS]

GROUPS = frozenset({"delivery", "maintenance"})


def source_of(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_there_are_installers_to_hold_to_this():
    assert len(MODULES) >= 10, IDS
    scheduleless = {"install-git-policy.py", "install-wt-profile.py"}
    assert {name for name, module in MODULES if not getattr(module, "TASK_NAME", "")} == (
        scheduleless
    ), "an installer that registers no job is a deliberate exception; name it here"


def test_every_installer_is_discovered_by_the_pass():
    assert [p.name for p in installers.discover(REPO_ROOT)] == IDS


@pytest.mark.parametrize("name", IDS)
def test_every_installer_answers_check_and_yes(name):
    source = source_of(name)
    assert '"--check"' in source, f"{name} has no --check, so nothing can ask it"
    assert '"--yes"' in source, f"{name} has no --yes, so nothing can repair it"


@pytest.mark.parametrize("name", IDS)
def test_every_installer_answers_uninstall(name):
    """Decommissioning a machine is a verb the whole set has to answer, not five of it.

    While eight installers had no `--uninstall`, the workspace could not honestly offer
    the verb for any of them: a checklist that silently did nothing for eight of thirteen
    ticks is worse than no checklist.
    """
    assert '"--uninstall"' in source_of(name), (
        f"{name} has no --uninstall, so this machine cannot be decommissioned from it"
    )


@pytest.mark.parametrize("name", IDS)
def test_no_installer_mutates_the_machine_before_yes(name):
    """`--uninstall` on its own prints a plan; it does not act.

    `install-global-tools.py` shipped the other way -- `--uninstall` shared the
    mutually-exclusive group with `--yes`, so the dry run was unspellable and the bare
    verb deleted the live task. It cost a registered `devkit-global-tools` during the
    audit that added this. The tell is structural and cheap to check: `--yes` must not be
    in the same exclusive group as the verbs, or the two cannot be combined at all.
    """
    source = source_of(name)
    verbs = re.search(
        r"mode\s*=\s*parser\.add_mutually_exclusive_group\(\)(.*?)\n    args\s*=",
        source,
        re.S,
    )
    assert verbs is not None, f"{name}: no verb group found to check"
    # `(?<![\w])` so this is the verb group itself and not `apply_mode`, which is the
    # separate group two installers use to keep `--dry-run` and `--yes` exclusive of
    # each other while both stay combinable with a verb.
    in_verb_group = re.search(r"(?<![\w])mode\.add_argument\(\s*\n?\s*\"--yes\"", verbs.group(1))
    assert in_verb_group is None, (
        f"{name}: --yes is one of the mutually-exclusive verbs, so `--uninstall --yes` "
        "cannot be spelled and the uninstall has no dry run"
    )


SHARED_ARGV = (["--check"], ["--uninstall"], ["--uninstall", "--yes"], ["--yes"])


@pytest.mark.parametrize("argv", SHARED_ARGV, ids=[" ".join(a) for a in SHARED_ARGV])
@pytest.mark.parametrize(("name", "module"), MODULES, ids=IDS)
def test_every_installer_parses_the_shared_verbs(name, module, argv):
    """The four spellings every installer owes, asserted through its real parser.

    `--uninstall --yes` is the one that matters. While `--yes` sat in the same
    mutually-exclusive group as the verbs, argparse *rejected* that combination -- which
    is why `install-global-tools.py` had no dry run and its bare `--uninstall` deleted a
    live scheduled task. argparse signals a rejected command line by raising `SystemExit`,
    so parsing without one is the whole assertion.

    Asserted here rather than by reading the source, and parametrized per argv so a
    failure names the spelling that broke.
    """
    module.build_parser().parse_args(argv)


def _never_spawn(argv, **_kwargs):
    raise AssertionError(f"--check reached the scheduler directly: {argv}")


@pytest.mark.parametrize(("name", "module"), JOBS, ids=JOB_IDS)
def test_check_reaches_the_shared_check_with_its_own_document(name, module, monkeypatch, capsys):
    """The exit code is the whole interface `installers.py` reads, and the shared check
    is the only thing allowed to produce it. Run through each installer's real `main`
    with `devkit_schtasks.run_check` replaced, so a mode that parses but never reaches
    the shared check -- or hands it something other than its own task document -- fails
    here rather than on the machine. The scheduler itself is never consulted: the six
    Schedule-based installers bind their runner as a default argument, so a monkeypatch
    on the module attribute would not have kept them off it."""
    monkeypatch.setattr(module, "WINDOWS", True)
    seen: dict[str, object] = {}

    def run_check(task_name, document, run):
        seen.update(name=task_name, document=document, run=run)
        return 1, f"schedule: {task_name} nothing is scheduled. Re-run with --yes."

    monkeypatch.setattr(module.devkit_schtasks, "run_check", run_check)
    kwargs = (
        {"runner": _never_spawn} if "runner" in inspect.signature(module.main).parameters else {}
    )
    assert module.main(["--check"], **kwargs) == 1
    assert "nothing is scheduled" in capsys.readouterr().err
    assert seen["name"] == module.TASK_NAME
    assert isinstance(seen["document"], str) and "<Task" in seen["document"]
    assert callable(seen["run"])


@pytest.mark.parametrize(("name", "module"), JOBS, ids=JOB_IDS)
def test_every_job_is_registrable_without_elevation(name, module, monkeypatch):
    """Every job is kept current by a non-elevated `--yes`, and the scheduler refuses a
    non-administrator two trigger shapes: a `BootTrigger`, and a `LogonTrigger` naming no
    user (probed: `ERROR: Access is denied.`). The three jobs carrying one failed every
    scheduled repair, and one repair deleted two of them for good (9385b3d7). Read off the
    document each installer's own `--check` hands the shared check, as `register` would
    register it.

    The stub answers *not* current: on "current" `install-tray` goes on to ask the real
    scheduler when its resident process started, and CI's Linux runner has no `schtasks`
    (the job went red on exactly that). Any spawn at all fails here on every OS."""
    monkeypatch.setattr(module, "WINDOWS", True)
    monkeypatch.setattr(module.subprocess, "run", _never_spawn)
    seen: dict[str, str] = {}

    def run_check(task_name, document, run):
        seen["document"] = document
        return 1, "not current"

    monkeypatch.setattr(module.devkit_schtasks, "run_check", run_check)
    kwargs = (
        {"runner": _never_spawn} if "runner" in inspect.signature(module.main).parameters else {}
    )
    module.main(["--check"], **kwargs)
    registered = module.devkit_schtasks.secured(seen["document"], "S-1-5-21-1-2-3-1001")
    assert "<BootTrigger>" not in registered, name
    for trigger in re.findall(r"<LogonTrigger>.*?</LogonTrigger>", registered, re.S):
        assert "<UserId>" in trigger, name


# --- every failure of every job reaches the ledger ------------------------------------
#
# Four jobs ran under `log-wrap.py --always`, which files a `scheduled-job-failed` event
# per failed run; the other eight were seen only through `fix_loop.job_findings`, which
# reads the scheduler's *current* `Last Result` once per fix pass. A failure the next run
# overwrote was lost: `logs/reconcile.failed.log` held a 2026-10-02 exit-1 reconcile
# that never reached the ledger. Read off the document each installer's own `--check`
# hands the shared check, so the wrapping is asserted where it is registered.

# A job whose task is not wrapped, and why. The reason is the whole exemption.
OFF_THE_LEDGER: dict[str, str] = {
    "devkit-tray": (
        "resident, not a pass: it starts at logon and runs until logoff, so the wrapper "
        "would hold everything it prints in memory for the whole session and could report "
        "only once it exits. Its one failure with no other signal -- not starting -- is "
        "what tray.py writes logs/tray.log for, and schedule_health reads its Last Result"
    ),
    "devkit-fix-pass": (
        "files its own: the pass files every step that fails, and the watchdog files a "
        "pass that crashed, hung or ran stale (file_once). It exits non-zero mostly for "
        "outcomes already on the ledger, so a wrapper filed each a second time under a "
        "generic group. Its own crash, the one thing neither files, is a failed run of an "
        "unwrapped job, which fix_loop.job_findings files off the scheduler"
    ),
}

LEDGER_WRAPPED = re.compile(
    r'^"(?P<wrapper>[^"]*[\\/]scripts[\\/]log-wrap\.py)"\s+--always\s+"(?P<label>[^"]+)"'
    r'\s+--\s+"(?P<python>[^"]+)"\s+\S'
)


def checked_document(module, monkeypatch) -> str:
    """The task document `module`'s `--check` hands the shared check, with the scheduler
    kept out of it (see `test_every_job_is_registrable_without_elevation`)."""
    monkeypatch.setattr(module, "WINDOWS", True)
    monkeypatch.setattr(module.subprocess, "run", _never_spawn)
    seen: dict[str, str] = {}

    def run_check(task_name, document, run):
        seen["document"] = document
        return 1, "not current"

    monkeypatch.setattr(module.devkit_schtasks, "run_check", run_check)
    kwargs = (
        {"runner": _never_spawn} if "runner" in inspect.signature(module.main).parameters else {}
    )
    module.main(["--check"], **kwargs)
    return seen["document"]


def _slashed(path: str) -> str:
    """A Windows path compared the way the scheduler compares it: case and separator
    blind. The documents are built on CI's Linux runner too."""
    return path.replace("\\", "/").rstrip("/").lower()


@pytest.mark.parametrize(("name", "module"), JOBS, ids=JOB_IDS)
def test_every_job_files_each_failure_on_the_ledger(name, module, monkeypatch):
    document = checked_document(module, monkeypatch)
    registered = module.devkit_schtasks.parse_task(document)
    assert registered is not None, name
    wrapped = LEDGER_WRAPPED.match(registered.arguments)
    if module.TASK_NAME in OFF_THE_LEDGER:
        assert wrapped is None, f"{name} is wrapped now; drop its OFF_THE_LEDGER entry"
        assert OFF_THE_LEDGER[module.TASK_NAME].strip()
        return
    assert wrapped, (
        f"{name} registers `{registered.arguments}`, which is not run under "
        f"`log-wrap.py --always`: a failed run is then recorded nowhere but the "
        f"scheduler's Last Result, which the next run overwrites. Build the arguments "
        f"with devkit_schtasks.logged, or add the job to OFF_THE_LEDGER with the reason."
    )
    assert not wrapped["python"].lower().endswith("pythonw.exe"), (
        f"{name} wraps a pythonw.exe; log-wrap spawns it with CREATE_NO_WINDOW, which "
        f"Windows ignores for a GUI-subsystem child (scripts/windowless-jobs.md)"
    )
    # `log-wrap.py` resolves `logs/` -- the artifact the ledger row names -- from the cwd,
    # and a task with no `<WorkingDirectory>` starts in `system32`.
    working = re.search(r"<WorkingDirectory>(.*?)</WorkingDirectory>", document, re.S)
    assert working, f"{name} has no <WorkingDirectory>, so log-wrap writes into system32"
    checkout = _slashed(wrapped["wrapper"]).removesuffix("/scripts/log-wrap.py")
    assert _slashed(unescape(working.group(1))) == checkout, name


def test_no_two_jobs_wrap_into_one_artifact(monkeypatch):
    """`log-wrap.py` names its files after the label. Two jobs sharing one would overwrite
    each other's kept failure, and a label slugging to a runner's own artifact would
    replace that runner's account of its run with the captured console."""
    artifacts = {module.ARTIFACT for _name, module in JOBS}
    seen: dict[str, str] = {}
    for name, module in JOBS:
        registered = module.devkit_schtasks.parse_task(checked_document(module, monkeypatch))
        wrapped = LEDGER_WRAPPED.match(registered.arguments)
        if wrapped is None:
            continue
        written = f"logs/{log_wrap.slug(wrapped['label'])}.log"
        assert written not in seen, f"{name} and {seen.get(written)} both write {written}"
        seen[written] = name
        assert written == module.ARTIFACT or written not in artifacts, (
            f"{name}'s wrapper writes {written}, which another job's runner owns"
        )


@pytest.mark.parametrize(("name", "module"), JOBS, ids=JOB_IDS)
def test_every_job_declares_its_group(name, module):
    assert getattr(module, "GROUP", None) in GROUPS, (
        f"{name}: GROUP must be one of {sorted(GROUPS)}"
    )


def test_the_switch_stands_down_exactly_the_delivery_jobs():
    """`BRANCH_DELIVERY_JOBS` used to be a hand list beside a comment listing the other
    six, and the comment was already one job behind."""
    delivery = {module.TASK_NAME for _name, module in JOBS if module.GROUP == "delivery"}
    assert set(switch.BRANCH_DELIVERY_JOBS) == delivery


@pytest.mark.parametrize("name", JOB_IDS)
def test_every_job_checks_through_the_shared_implementation(name):
    """Six private copies of the check were wrong in the same way at once; a seventh
    would be too."""
    assert "devkit_schtasks.run_check(" in source_of(name), name


@pytest.mark.parametrize("name", JOB_IDS)
def test_every_job_installer_consults_the_ledger(name):
    """It must ask `harness_state.stood_down()` and pass the answer to `task_xml` as
    `enabled=`, so a job stood down by name lands disabled rather than skipped."""
    source = source_of(name)
    assert "harness_state.stood_down()" in source, name
    assert "enabled=" in source, name


@pytest.mark.parametrize(("name", "module"), JOBS, ids=JOB_IDS)
def test_every_job_is_in_the_readme_table(name, module):
    """The table is the one place a person sees the whole set; a job missing from it is
    a job nobody knows to expect."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"| `{module.TASK_NAME}` | `scripts/{name}` |" in readme, name
