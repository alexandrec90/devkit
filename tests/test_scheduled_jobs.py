"""The contract every unattended devkit job has to satisfy, checked across all of them.

Each installer has its own suite for what it *does*. This one exists because the failure
that actually happened was not inside any job -- it was a job that no installer here had
ever registered, and so no suite was looking at:

    devkit-docker-prune, registered by hand with `schtasks /Create /SC DAILY`,
    running `pythonw.exe docker-maint.py prune --generic`.

Every property the other two jobs were carefully given, that one lacked. It skipped
every fire on battery, waited for ten minutes of idle, never caught up a missed run --
and, the part that made it undiagnosable, it wrote nothing anywhere. It had been exiting
1 for a day when someone asked where a fresh agent would go to find out why, and the
honest answer was: nowhere. `schtasks` had a `Last Result` of 1 and that was the entire
record.

So the properties are asserted **for every job at once**, from the installers rather than
from a list written here, and a new job that forgets one fails this file rather than
waiting to be noticed:

1. It is registered by an installer in this repo -- not by hand, and not by a flag
   spelling that cannot express the settings a laptop needs.
2. It goes through `devkit_schtasks`, which is where those settings live.
3. It names the artifact it leaves (`ARTIFACT`), and that artifact is under `logs/`.
4. `schedule_health.ARTIFACTS` points at that same file, so the session-start line that
   reports the failure also says where to read about it.

What this cannot check is that a live machine's registered task still matches its
installer -- `schedule_health` answers a different question (is it running at all), and
the registered command is not readable from CI. Re-running an installer is idempotent
(`/F`), which is the cheap way to make a machine agree with the repo again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

schedule_health = load_script("scripts/schedule_health.py")

# Every installer that registers a scheduled task, found rather than listed: a new
# `scripts/install-*.py` is in scope the moment it defines a `TASK_NAME`.
INSTALLERS = sorted(REPO_ROOT.glob("scripts/install-*.py"))


def scheduled_installers() -> list[tuple[str, object]]:
    """`(path stem, module)` for every installer that registers a devkit job.

    `install-git-policy.py` defines no `TASK_NAME` -- it installs git hooks, not a
    scheduled task -- and drops out here without needing to be named.
    """
    found = []
    for path in INSTALLERS:
        module = load_script(f"scripts/{path.name}")
        name = getattr(module, "TASK_NAME", "")
        if isinstance(name, str) and name.startswith(schedule_health.PREFIX):
            found.append((path.name, module))
    return found


JOBS = scheduled_installers()
IDS = [name for name, _module in JOBS]


def test_there_is_at_least_one_job_to_check():
    """A discovery-based suite that finds nothing passes vacuously, which is the one
    way a test like this rots without anyone noticing."""
    assert JOBS, f"no scheduled-job installers found under {REPO_ROOT / 'scripts'}"


@pytest.mark.parametrize(("name", "module"), JOBS, ids=IDS)
def test_a_job_is_registered_through_the_document_builder(name, module):
    """`schtasks /Create /SC ...` cannot express the three settings that decide whether a
    job on a laptop runs at all, so it silently inherits the server defaults. That is
    what the hand-registered prune task did."""
    assert (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8").count("devkit_schtasks"), (
        f"{name} does not go through devkit_schtasks"
    )
    assert hasattr(module, "task_document")


@pytest.mark.parametrize(("name", "module"), JOBS, ids=IDS)
def test_a_job_names_the_artifact_it_leaves(name, module):
    """An unattended job runs windowless: its stdout goes nowhere at all. A job with no
    artifact is a job whose only record of a failure is an integer in the scheduler."""
    artifact = getattr(module, "ARTIFACT", "")
    assert artifact, (
        f"{name} registers {module.TASK_NAME} but names no ARTIFACT. Wrap the command in "
        f"`log-wrap.py --always` (see install-docker-prune.py) or point ARTIFACT at the "
        f"log its runner already writes."
    )
    assert artifact.startswith("logs/"), f"{name}: {artifact} is not under logs/"


@pytest.mark.parametrize(("name", "module"), JOBS, ids=IDS)
def test_the_session_start_report_points_at_that_artifact(name, module):
    """`schedule_health` prints the failure line; the pointer is the only thing on it a
    reader can act on."""
    assert schedule_health.ARTIFACTS.get(module.TASK_NAME) == module.ARTIFACT


def test_the_pointer_table_has_no_entries_for_jobs_that_no_longer_exist():
    """The other direction: a stale entry sends a reader to a file nothing writes."""
    known = {module.TASK_NAME for _name, module in JOBS}
    assert set(schedule_health.ARTIFACTS) == known


# --- each job's artifact really is the file its runner writes -------------------
#
# The assertions above prove the three declarations agree with each other. They cannot
# prove any of them is true, because the runner is a different script. One test per job
# closes that, against the runner's own constant.


def test_reconcile_writes_the_file_its_installer_advertises():
    installer = load_script("scripts/install-reconcile-task.py")
    worktree = load_script("scripts/worktree.py")
    assert installer.ARTIFACT == worktree.RECONCILE_LOG


def test_the_upgrade_pass_writes_the_file_its_installer_advertises():
    installer = load_script("scripts/install-upgrade-schedule.py")
    upgrade = load_script("scripts/upgrade-project.py")
    assert installer.ARTIFACT == upgrade.ARTIFACT.as_posix()


def test_the_prune_writes_the_file_its_installer_advertises():
    """`docker-maint.py` writes no artifact of its own -- it is a script with several
    callers, most of them interactive. The wrapper is what gives the scheduled caller
    one, so here the claim is about the label the wrapper is given."""
    installer = load_script("scripts/install-docker-prune.py")
    log_wrap = load_script("scripts/log-wrap.py")
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"
    assert "--always" in installer.prune_arguments(r"C:\py\pythonw.exe", root=Path(r"C:\ws"))


def test_the_vanillaland_merge_writes_the_file_its_installer_advertises():
    """Same shape as the prune: `git-merge-default.py` has several callers -- a VS Code
    task among them -- and writes no artifact of its own, so the claim here is about the
    label the wrapper is given. The label differs from the clicked task's on purpose, so
    a click cannot overwrite the unattended run's only record."""
    installer = load_script("scripts/install-vanillaland-merge.py")
    log_wrap = load_script("scripts/log-wrap.py")
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"
    assert "--always" in installer.merge_arguments(r"C:\py\pythonw.exe", root=Path(r"C:\ws"))
