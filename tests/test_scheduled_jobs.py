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

import ast
import os
import sys
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


def test_the_stop_idle_pass_writes_the_file_its_installer_advertises():
    """Same shape as the prune, same runner even: `docker-maint.py` writes no artifact
    of its own, so the claim is about the label the wrapper is given."""
    installer = load_script("scripts/install-docker-stop-idle.py")
    log_wrap = load_script("scripts/log-wrap.py")
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"
    assert "--always" in installer.stop_idle_arguments(r"C:\py\pythonw.exe", root=Path(r"C:\ws"))


def test_the_vanillaland_merge_writes_the_file_its_installer_advertises():
    """Same shape as the prune: `git-merge-default.py` has several callers -- a VS Code
    task among them -- and writes no artifact of its own, so the claim here is about the
    label the wrapper is given. The label differs from the clicked task's on purpose, so
    a click cannot overwrite the unattended run's only record."""
    installer = load_script("scripts/install-vanillaland-merge.py")
    log_wrap = load_script("scripts/log-wrap.py")
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"
    assert "--always" in installer.merge_arguments(r"C:\py\pythonw.exe", root=Path(r"C:\ws"))


# --- an unattended job puts no window on the desktop ----------------------------
#
# `pythonw.exe` is what keeps the job itself from opening a console. What it does not do
# is stop Windows giving a brand new console **window** to every console child a
# console-less process spawns -- so suppressing the parent's window turns one window per
# fire into dozens of flickering ones unless each spawn says otherwise.
#
# That was fixed once, at the spawn sites in `sweep.py`, `worktree.py` and
# `worktree-guard.py`, and the check that pinned it looked at exactly those three files.
# The flicker came back anyway, from `git` spawned by `sync-devkit.py` on the nightly
# upgrade pass, because **the fix stopped at a process boundary the check could not
# see**: a job's reach is longer than the script the scheduler names. `upgrade-project.py`
# spawns `sync-devkit.py`, which spawns `git`; the flag on the first hop is even ignored,
# because the interpreter it launches is `pythonw.exe` and Windows drops
# `CREATE_NO_WINDOW` for a GUI-subsystem child.
#
# So the check now covers the reachable set rather than the named one, and `UNATTENDED`
# below is the list a new job has to join. Only the *outermost* spawn in each script
# needs the flag -- a window-less console is inherited -- but "outermost" is not a
# property a reader can check, so every spawn site carries it.
#
# Source-level, because the symptom exists only on a Windows desktop under a scheduler:
# there is no runtime assertion that would have caught either round of this, and both
# were reported by a human watching windows flash.

# Modules the scheduler can reach, and why each is here. The entry points are checked
# against the installers by `test_every_script_a_job_launches_is_covered`; the rest are
# reached from them and have to be listed, because no static check can follow a spawn
# into another interpreter.
UNATTENDED: dict[str, str] = {
    # entry points, named directly in an installer's argv
    "scripts/worktree.py": "devkit-worktree-reconcile runs it every 15 minutes",
    "scripts/upgrade-project.py": "devkit-upgrade-projects runs it nightly",
    "scripts/docker-maint.py": "devkit-docker-prune and devkit-docker-stop-idle run it nightly",
    "scripts/git-merge-default.py": "devkit-vanillaland-merge runs it nightly",
    "scripts/log-wrap.py": "the wrapper three of those jobs are launched through",
    # reached from an entry point
    "scripts/sweep.py": "the git and gh IO for reconcile, upgrade and the merge",
    "scripts/sync-devkit.py": "upgrade-project.py spawns it per project, once per pass",
    "scripts/git_policy.py": "the single spawn point git-merge-default.py runs git through",
    # not scheduled, but the same failure: an agent hook's parent is whatever launched
    # the agent, and an editor's extension host has no console either.
    "scripts/worktree-guard.py": "PreToolUse hook, parent may itself be console-less",
}

# A module in `UNATTENDED` that spawns nothing passes vacuously, so each one has to say
# which module does the spawning for it.
DELEGATES_ITS_SPAWNS: dict[str, str] = {
    "scripts/git-merge-default.py": "scripts/git_policy.py",
}

SPAWN_ATTRS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def _is_subprocess(node: ast.expr) -> bool:
    """True for `subprocess` and for a qualified `<module>.subprocess`.

    `worktree-guard.py` reaches it as `worktree.subprocess.run`, having loaded the
    module by path -- which a check matching only a bare `subprocess.` would miss.
    """
    if isinstance(node, ast.Name):
        return node.id == "subprocess"
    return isinstance(node, ast.Attribute) and node.attr == "subprocess"


def spawn_sites(source: str) -> list[ast.Call]:
    """Every `subprocess.<spawn>(...)` call in `source`, however it is spelled."""
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SPAWN_ATTRS
        and _is_subprocess(node.func.value)
    ]


@pytest.mark.parametrize("rel", sorted(UNATTENDED), ids=sorted(UNATTENDED))
def test_every_spawn_a_scheduled_job_can_reach_suppresses_its_console(rel):
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    sites = spawn_sites(source)
    if not sites:
        assert rel in DELEGATES_ITS_SPAWNS, (
            f"{rel} spawns nothing, so this test passes without checking anything. "
            f"Either it gained a spawn that this scan cannot see, or it belongs in "
            f"DELEGATES_ITS_SPAWNS naming the module that spawns for it."
        )
        return
    for site in sites:
        flagged = any(keyword.arg == "creationflags" for keyword in site.keywords)
        assert flagged, (
            f"{rel}:{site.lineno} spawns a process without creationflags. Under "
            f"pythonw.exe this opens a console window; pass creationflags=NO_WINDOW."
        )


def test_the_delegation_note_names_a_module_that_really_does_spawn():
    """Otherwise the exemption is just a way to opt out of the check."""
    for rel, delegate in DELEGATES_ITS_SPAWNS.items():
        assert delegate in UNATTENDED, f"{rel} delegates to {delegate}, which is unchecked"
        assert spawn_sites((REPO_ROOT / delegate).read_text(encoding="utf-8")), (
            f"{rel} names {delegate} as its spawner, but {delegate} spawns nothing"
        )


# --- and the flag does not cost the job its artifact ---------------------------
#
# The other half of `NO_WINDOW`, which is easy to ship without noticing: the flag gives
# the child a console of its own, and a child that captures nothing writes to *that*
# console instead of to the handles it inherited. Every spawn the reconcile pass makes
# captures, so this never bit -- but `docker-maint.py` streams, and flagging it without
# naming the streams would have replaced a flickering window with a prune log that says
# `# exit: 0` and nothing else.

docker_maint = load_script("scripts/docker-maint.py")


def test_the_streams_are_named_when_there_is_something_to_name(tmp_path, monkeypatch):
    handle = (tmp_path / "out.txt").open("w", encoding="utf-8")
    monkeypatch.setattr(docker_maint.sys, "stdout", handle)
    monkeypatch.setattr(docker_maint.sys, "stderr", handle)
    try:
        assert docker_maint.inherited_streams() == {"stdout": handle, "stderr": handle}
    finally:
        handle.close()


def test_a_stream_with_no_file_descriptor_is_left_to_inherit(monkeypatch):
    """None under a bare `pythonw.exe`, and a capture object under pytest. Both mean
    there is nothing to hand down, and passing them to `subprocess` would raise."""

    class Captured:
        def fileno(self):
            raise OSError("not a real descriptor")

    monkeypatch.setattr(docker_maint.sys, "stdout", None)
    monkeypatch.setattr(docker_maint.sys, "stderr", Captured())
    assert docker_maint.inherited_streams() == {}


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW exists only on Windows")
def test_a_window_less_child_still_reports_to_the_caller(tmp_path, monkeypatch):
    """The regression itself, run rather than read: a real flagged child, and its output
    has to arrive. Drop `inherited_streams()` from `run` and this goes empty.

    The marker is built by the child and differs in case from anything in the command,
    because `run` echoes the command it is about to run into the same stream -- an
    assertion on a literal the argv also contains passes on the echo alone, which is
    what the first draft of this test did.
    """
    path = tmp_path / "run.txt"
    with path.open("w", encoding="utf-8") as handle:
        monkeypatch.setattr(docker_maint.sys, "stdout", handle)
        monkeypatch.setattr(docker_maint.sys, "stderr", handle)
        code = docker_maint.run([sys.executable, "-c", "print('the child spoke'.upper())"])
    assert code == 0
    assert "THE CHILD SPOKE" in path.read_text(encoding="utf-8")


def script_literals(source: str) -> list[str]:
    """Every `*.py` filename an installer names *in code*, docstrings excluded.

    The exclusion is what makes this usable rather than noisy: an installer's prose
    names sibling scripts freely -- `install-docker-prune.py` explains itself by
    reference to `scripts/devkit_schtasks.py`, which it imports and never launches. A
    plain text scan reads that as a launch and demands console suppression from a module
    that runs at install time, in a terminal someone is looking at.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith(".py")
        and id(node) not in docstrings
    ]


@pytest.mark.parametrize(("name", "module"), JOBS, ids=IDS)
def test_every_script_a_job_launches_is_covered(name, module):
    """The half that closes the loop: a new job cannot be registered against a script
    nobody checked. Read off the installer rather than a list here, for the same reason
    the rest of this file is -- a list is what went stale."""
    source = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
    for literal in script_literals(source):
        rel = f"scripts/{Path(literal).name}"
        if not (REPO_ROOT / rel).is_file():
            continue
        assert rel in UNATTENDED, (
            f"{name} launches {rel}, which is not in UNATTENDED. Every script a "
            f"scheduled job runs has to suppress the console windows of what it spawns."
        )
