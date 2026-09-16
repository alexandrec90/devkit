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
"""

from __future__ import annotations

import inspect
import re

import pytest
from support import REPO_ROOT, load_script

installers = load_script("scripts/installers.py")
switch = load_script("scripts/harness-switch.py")

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


def _never_spawn(argv):
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
