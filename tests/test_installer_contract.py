"""The contract every `scripts/install-*.py` has to satisfy, checked across all of them.

`tests/test_scheduled_jobs.py` holds the *job* contract: registered from XML, names an
artifact, points `schedule_health` at it. This file holds the *installer* contract, and it
exists because the failure that happened was not inside any installer -- it was that
nothing could drive them. Two jobs had never been registered on the workstation that runs
them, two installers had `--check` and four had only `--status`, and the only registry of
what needed installing was a README table a person had to remember to read.

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
    assert len(JOBS) == len(MODULES) - 1, "only install-git-policy.py registers no job"


def test_every_installer_is_discovered_by_the_pass():
    assert [p.name for p in installers.discover(REPO_ROOT)] == IDS


@pytest.mark.parametrize("name", IDS)
def test_every_installer_answers_check_and_yes(name):
    source = source_of(name)
    assert '"--check"' in source, f"{name} has no --check, so nothing can ask it"
    assert '"--yes"' in source, f"{name} has no --yes, so nothing can repair it"


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
