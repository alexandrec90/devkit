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


def test_the_release_pass_writes_the_file_its_installer_advertises():
    """Same shape as the prune: `release-pipeline.py` writes no artifact of its own --
    most of its runs are clicks, and `devkit_project.plan_command` wraps those. The
    wrapper is what gives the scheduled caller one, so the claim here is about the label.

    The label differs from the clicked task's deliberately, and that is asserted rather
    than left to the comment: the two slugging to one file would let the next morning's
    click erase the only record of what fired at 2am.
    """
    installer = load_script("scripts/install-release-schedule.py")
    devkit_project = load_script("scripts/devkit_project.py")
    log_wrap = load_script("scripts/log-wrap.py")
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"
    assert "--always" in installer.schedule_for(root=REPO_ROOT).arguments
    clicked = devkit_project.ACTIONS["release"].label
    assert log_wrap.slug(installer.LABEL) != log_wrap.slug(clicked)


def test_the_scheduled_release_never_loses_the_predicate_that_makes_it_affordable():
    """`--if-needed` is the difference between a nightly release and a nightly tag.

    Without it this job would cut a release for a doc fix and open an adoption PR in
    every consumer to deliver nothing -- and `--yes` without `--if-needed` is the exact
    shape that edit would take, since both live in one tuple that is easy to trim.
    """
    installer = load_script("scripts/install-release-schedule.py")
    arguments = installer.schedule_for(root=REPO_ROOT).arguments
    assert "--if-needed" in arguments
    assert "--yes" in arguments


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


def test_the_global_tools_pass_writes_the_file_its_installer_advertises():
    """This one writes its own artifact rather than being wrapped: the content that
    matters is not the captured stdout of an npm command but the rollback line for
    every version it moved, and that is a thing only the runner can render."""
    installer = load_script("scripts/install-global-tools.py")
    runner = load_script("scripts/global-tools.py")
    assert installer.ARTIFACT == runner.ARTIFACT.as_posix()


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
    "scripts/release-pipeline.py": "devkit-release runs it nightly, behind --if-needed",
    "scripts/upgrade-project.py": "devkit-upgrade-projects runs it nightly",
    "scripts/docker-maint.py": "devkit-docker-prune and devkit-docker-stop-idle run it nightly",
    "scripts/git-merge-default.py": "devkit-vanillaland-merge runs it nightly",
    "scripts/global-tools.py": "devkit-global-tools runs it nightly",
    "scripts/log-wrap.py": "the wrapper three of those jobs are launched through",
    # reached from an entry point
    "scripts/sweep.py": "the git and gh IO for reconcile, upgrade and the merge",
    "scripts/sync-devkit.py": "upgrade-project.py spawns it per project, once per pass",
    "scripts/release.py": "release-pipeline.py imports it for the version and bump helpers",
    "scripts/git_policy.py": "the single spawn point git-merge-default.py runs git through",
    "scripts/agent_clis.py": "global-tools.py runs the agent-CLI stage of every nightly pass",
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


def names_the_no_window_flag(value: ast.expr) -> bool:
    """`NO_WINDOW`, `sweep.NO_WINDOW`, `worktree.sweep.NO_WINDOW` -- and not `0`.

    That the keyword was *present* used to be the whole assertion, which passes for
    `creationflags=0` and for any other flag someone reaches for. The keyword is not
    the property being claimed; the value is.
    """
    if isinstance(value, ast.Name):
        return value.id == "NO_WINDOW"
    if isinstance(value, ast.Attribute):
        return value.attr in {"NO_WINDOW", "CREATE_NO_WINDOW"}
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
        return names_the_no_window_flag(value.left) or names_the_no_window_flag(value.right)
    return False


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
        flag = next((kw.value for kw in site.keywords if kw.arg == "creationflags"), None)
        assert flag is not None, (
            f"{rel}:{site.lineno} spawns a process without creationflags. Under "
            f"pythonw.exe this opens a console window; pass creationflags=NO_WINDOW."
        )
        assert names_the_no_window_flag(flag), (
            f"{rel}:{site.lineno} passes creationflags, but the value is not a "
            f"NO_WINDOW constant. The keyword was the whole of this check once, which "
            f"makes creationflags=0 -- or any other flag -- pass it."
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


# --- the flag is half of it: the interpreter is the other half -------------------
#
# `CREATE_NO_WINDOW` was on every spawn in the reachable set above, and a window still
# appeared nightly for about sixteen seconds. The reason is a Windows rule the flag's
# own name hides: **it is ignored for a GUI-subsystem child.** `pythonw.exe` has no
# console to suppress, so passing the flag alongside it is a no-op, and the child is
# left console-*less* -- which is the exact condition that makes Windows allocate a
# fresh visible console for each of *its* children.
#
# So a job that spawns `sys.executable` propagates console-lessness instead of stopping
# it, and the window opens one hop further down, where nothing is looking. Measured on
# this workstation, under `pythonw.exe`, with the flag set on both spawns:
#
#     sys.executable -m venv X   -> rc 0 in 16.5s, and a console window for the
#                                   `ensurepip` that `venv` re-spawns
#     python.exe     -m venv Y   -> rc 0 in 11.7s, no window
#
# `console_python()` is that second spelling, and pairing it with the flag is what
# actually suppresses the subtree: a console child spawned with `CREATE_NO_WINDOW` gets
# a real console that is merely hidden, and every descendant inherits it.
#
# Hence a blanket ban rather than a spawn-argv scan. `git_policy` builds its argv in a
# helper and spawns it three functions away through an injected `runner`; a check that
# looked only at spawn sites would have read that as clean. The interpreter is not
# allowed into these modules at all, except inside the function whose job is to convert
# it.

CONSOLE_HELPERS = frozenset({"console_python", "console"})


def interpreter_sites(source: str) -> list[int]:
    """Lines naming `sys.executable`, outside the helper that exists to replace it.

    Attribute nodes only, so the prose above and every docstring that explains the rule
    are invisible to it -- `script_literals` had to learn the same lesson.
    """
    tree = ast.parse(source)
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in CONSOLE_HELPERS:
            exempt.update(id(child) for child in ast.walk(node))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"executable", "_base_executable"}
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and id(node) not in exempt
    ]


@pytest.mark.parametrize("rel", sorted(UNATTENDED), ids=sorted(UNATTENDED))
def test_no_scheduled_job_spawns_the_interpreter_that_is_running_it(rel):
    lines = interpreter_sites((REPO_ROOT / rel).read_text(encoding="utf-8"))
    assert not lines, (
        f"{rel}:{lines} names sys.executable. Under a scheduled job that is "
        f"pythonw.exe, and CREATE_NO_WINDOW is ignored for a GUI-subsystem child -- so "
        f"the child is left with no console and Windows gives each of its own children "
        f"a visible one. Spawn console_python() with creationflags=NO_WINDOW instead; "
        f"both, since neither alone suppresses the window."
    )


def modules_defining_a_console_helper() -> list[str]:
    """Found rather than listed, for the reason the rest of this file is."""
    return [
        rel
        for rel in sorted(UNATTENDED)
        if "def console_python(" in (REPO_ROOT / rel).read_text(encoding="utf-8")
    ]


HELPER_MODULES = modules_defining_a_console_helper()


def test_the_helper_exists_somewhere_to_be_checked():
    """The ban above is satisfiable by deleting every spawn, which would pass this file
    and break the jobs. Something has to still own the conversion."""
    assert "scripts/sweep.py" in HELPER_MODULES, (
        f"no module in UNATTENDED defines console_python(); found {HELPER_MODULES}"
    )


@pytest.fixture
def two_interpreters(tmp_path):
    """A directory holding both spellings, as a real Python install does."""
    console = tmp_path / "python.exe"
    gui = tmp_path / "pythonw.exe"
    console.write_text("", encoding="utf-8")
    gui.write_text("", encoding="utf-8")
    return console, gui


@pytest.mark.parametrize("rel", HELPER_MODULES, ids=HELPER_MODULES)
def test_the_helper_returns_the_console_twin_under_a_scheduled_job(
    rel, two_interpreters, monkeypatch
):
    console, gui = two_interpreters
    module = load_script(rel)
    monkeypatch.setattr(module.sys, "executable", str(gui))
    assert module.console_python() == str(console)


@pytest.mark.parametrize("rel", HELPER_MODULES, ids=HELPER_MODULES)
def test_the_helper_is_the_identity_when_there_is_already_a_console(
    rel, two_interpreters, monkeypatch
):
    """Every interactive caller, every POSIX machine, and this test run itself."""
    console, _gui = two_interpreters
    module = load_script(rel)
    monkeypatch.setattr(module.sys, "executable", str(console))
    assert module.console_python() == str(console)


@pytest.mark.parametrize("rel", HELPER_MODULES, ids=HELPER_MODULES)
def test_the_helper_falls_back_rather_than_raising_when_there_is_no_twin(
    rel, tmp_path, monkeypatch
):
    """An embedded install can ship `pythonw.exe` alone. A hook that raised here would
    fail the edit it was gating, which is a worse outcome than a window."""
    gui = tmp_path / "pythonw.exe"
    gui.write_text("", encoding="utf-8")
    module = load_script(rel)
    monkeypatch.setattr(module.sys, "executable", str(gui))
    assert module.console_python() == str(gui)


# --- and the task's own <Command> is an interpreter, not a stub for one ---------
#
# Everything above this line checks a *name*: that the file chosen is called
# `pythonw.exe`, that no spawn names `sys.executable`, that the wrapped half is the
# console twin. All of it passed while `devkit-global-tools` opened a window on every
# fire, because inside a virtualenv `pythonw.exe` is not an interpreter -- it is a stub
# deferring to the base install named in `pyvenv.cfg`, and uv builds that stub as a
# trampoline which *spawns* the base as a child. A console child of a console-less
# scheduled task is exactly what Windows hands a brand new visible console to.
#
# So this is the one check in this file with a filesystem under it. A source scan cannot
# tell a trampoline from an interpreter; only a layout on disk can.

WINDOWLESS_HELPERS = ("windowless", "windowless_python")


def windowless_resolvers() -> list[tuple[str, object]]:
    """Each job installer's resolver for the task's own `<Command>`, found not listed.

    A seventh installer that grows a private copy of the two-line version joins this
    check by existing, which is the whole point: six of them did, and all six were wrong
    in the same way.
    """
    found = []
    for name, module in JOBS:
        for attr in WINDOWLESS_HELPERS:
            resolver = getattr(module, attr, None)
            if callable(resolver):
                found.append((name, resolver))
                break
    return found


RESOLVERS = windowless_resolvers()
RESOLVER_IDS = [name for name, _resolver in RESOLVERS]


def test_every_job_has_a_resolver_for_its_own_command():
    assert len(RESOLVERS) == len(JOBS), (
        f"{sorted(set(IDS) - set(RESOLVER_IDS))} define none of {WINDOWLESS_HELPERS}, so "
        f"nothing here checks what interpreter their task is registered against"
    )


@pytest.fixture
def uv_style_venv(tmp_path):
    """A virtualenv's `Scripts/`, beside the base install its `pyvenv.cfg` names.

    Both spellings exist in both directories, which is the shape that made this
    invisible: resolving `pythonw.exe` beside the interpreter finds a real file with
    exactly the right name, and every guard that checked the name was satisfied.
    """
    base = tmp_path / "base"
    scripts = tmp_path / "venv" / "Scripts"
    base.mkdir()
    scripts.mkdir(parents=True)
    for directory in (base, scripts):
        for stem in ("python.exe", "pythonw.exe"):
            (directory / stem).write_text("", encoding="utf-8")
    (tmp_path / "venv" / "pyvenv.cfg").write_text(
        f"home = {base}\nuv = 0.11.29\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    return scripts / "python.exe", base / "pythonw.exe"


@pytest.mark.parametrize(("name", "resolve"), RESOLVERS, ids=RESOLVER_IDS)
def test_a_job_registered_from_a_virtualenv_runs_the_base_interpreter(name, resolve, uv_style_venv):
    venv_python, base_gui = uv_style_venv
    assert resolve(str(venv_python)) == str(base_gui), (
        f"{name} would register the venv's own pythonw.exe. Under uv that is a "
        f"trampoline that spawns the base interpreter as a child, and Windows gives the "
        f"child of a console-less task a visible console -- so the task is GUI-subsystem, "
        f"the file is named right, and a window opens anyway. Resolve through "
        f"devkit_schtasks.windowless, which reads pyvenv.cfg."
    )


@pytest.mark.parametrize(("name", "resolve"), RESOLVERS, ids=RESOLVER_IDS)
def test_a_job_registered_from_a_real_install_takes_the_twin_beside_it(
    name, resolve, two_interpreters
):
    """The reversion check for the paragraph above: escaping a venv must not cost the
    ordinary case, which is every job installed from a system Python."""
    console, gui = two_interpreters
    assert resolve(str(console)) == str(gui)


@pytest.mark.parametrize(("name", "resolve"), RESOLVERS, ids=RESOLVER_IDS)
def test_a_job_falls_back_rather_than_raising_when_there_is_no_windowless_twin(
    name, resolve, tmp_path
):
    """POSIX, and any layout shipping `python.exe` alone. An installer that raised here
    would leave the machine with no scheduled job at all, which is worse than a window."""
    console = tmp_path / "python.exe"
    console.write_text("", encoding="utf-8")
    assert resolve(str(console)) == str(console)


# `os.system` and friends take no `creationflags` at all, so a job that reaches for one
# has no way to satisfy the check above -- and the failure would be the same window.

CANNOT_BE_FLAGGED = {
    ("os", "system"),
    ("os", "popen"),
    ("subprocess", "getoutput"),
    ("subprocess", "getstatusoutput"),
}


@pytest.mark.parametrize("rel", sorted(UNATTENDED), ids=sorted(UNATTENDED))
def test_no_scheduled_job_uses_a_spawn_that_cannot_be_flagged(rel):
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        value = node.func.value
        module = value.id if isinstance(value, ast.Name) else getattr(value, "attr", "")
        assert (module, node.func.attr) not in CANNOT_BE_FLAGGED, (
            f"{rel}:{node.lineno} calls {module}.{node.func.attr}, which accepts no "
            f"creationflags. Use subprocess.run(..., creationflags=NO_WINDOW)."
        )


# --- and the interpreter the wrapper is handed is a console one -----------------
#
# Three jobs are launched as `pythonw.exe log-wrap.py --always <label> -- <python>
# <script> ...`, so there are two interpreters in one command line and they are not the
# same one. The outer must be windowless -- it is the job, and its console would be the
# window. The inner must not be: `log-wrap.py` spawns it with `CREATE_NO_WINDOW`, which
# the paragraph above explains is ignored for a GUI child, so a `pythonw.exe` there
# hands every `docker` and every `git` below it a visible console.
#
# This is the pair the instruction tier had backwards -- `scripts/CLAUDE.md` said to
# keep the inner interpreter windowless too -- so it is asserted rather than written
# down.


def wrapped_jobs() -> list[tuple[str, object]]:
    """Job installers whose argv nests `log-wrap.py`, found from the argv itself."""
    found = []
    for name, module in JOBS:
        source = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        if "log-wrap.py" in script_literals(source):
            found.append((name, module))
    return found


WRAPPED = wrapped_jobs()
WRAPPED_IDS = [name for name, _module in WRAPPED]


def arguments_builder(name, module):
    builders = sorted(attr for attr in dir(module) if attr.endswith("_arguments"))
    assert len(builders) == 1, f"{name} has {builders}; expected exactly one argv builder"
    return getattr(module, builders[0])


def test_the_jobs_that_go_through_the_wrapper_are_still_found():
    assert WRAPPED_IDS, "no wrapped job installers found; this suite passes vacuously"


@pytest.mark.parametrize(("name", "module"), WRAPPED, ids=WRAPPED_IDS)
def test_the_wrapped_command_names_a_console_interpreter(name, module, two_interpreters):
    """Hand the builder the *task's own* windowless interpreter -- what `main` has in
    hand -- and the command it wraps must still come out console-subsystem."""
    console, gui = two_interpreters
    arguments = arguments_builder(name, module)(str(gui), root=REPO_ROOT)
    assert "pythonw.exe" not in arguments, (
        f"{name} wraps a command that runs under pythonw.exe. CREATE_NO_WINDOW is "
        f"ignored for it, so everything the wrapped script spawns opens a window."
    )
    assert str(console) in arguments


@pytest.mark.parametrize(("name", "module"), WRAPPED, ids=WRAPPED_IDS)
def test_the_task_itself_still_runs_windowless(name, module, two_interpreters):
    """The other half of the pair, and the reason it is asserted beside its opposite:
    `console` and `windowless` are inverses, and swapping them is silent."""
    console, gui = two_interpreters
    assert module.windowless(str(console)) == str(gui)
    assert module.console(str(gui)) == str(console)


# --- a module reached by an import is reached by the job ------------------------
#
# `UNATTENDED` is a hand-written list of what a job can reach, and the failure it was
# written for is a hand-written list going stale. An import edge is the one part of the
# reachable set that *can* be followed statically, so it is followed here: a helper
# module that gains a spawn joins the check by being imported, not by being remembered.

IMPORTED_NOT_ENTERED: dict[str, str] = {
    "scripts/devkit_project.py": (
        "imported for known_projects and the action table; its only spawn is the VS "
        "Code task dispatcher inside main(), which no job calls"
    ),
}


def local_imports(source: str, available: set[str]) -> set[str]:
    """Sibling `scripts/*.py` modules imported by `source`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module.split(".")[0])
    return found & available


def import_closure() -> set[str]:
    """Every `scripts/*.py` reachable from `UNATTENDED` by import, transitively."""
    available = {path.stem: path for path in (REPO_ROOT / "scripts").glob("*.py")}
    seen: set[str] = set()
    queue = [Path(rel).stem for rel in UNATTENDED]
    while queue:
        stem = queue.pop()
        if stem in seen or stem not in available:
            continue
        seen.add(stem)
        source = available[stem].read_text(encoding="utf-8")
        queue.extend(local_imports(source, set(available)))
    return {f"scripts/{stem}.py" for stem in seen}


def spawns_outside_main(rel: str) -> list[int]:
    """Spawn sites in `rel` that are not lexically inside its `main()`.

    The exemption an `IMPORTED_NOT_ENTERED` entry claims is precisely this: the module
    is imported for a helper, and the code that spawns is the command-line entry point
    nothing imports its way into. A spawn anywhere else is reachable and has to carry
    the flag like any other.
    """
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    entry: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            entry.update(id(child) for child in ast.walk(node))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SPAWN_ATTRS
        and _is_subprocess(node.func.value)
        and id(node) not in entry
    ]


def test_every_module_a_job_imports_is_checked_or_spawns_nothing():
    for rel in sorted(import_closure() - set(UNATTENDED)):
        outside = spawns_outside_main(rel)
        if not outside and rel not in IMPORTED_NOT_ENTERED:
            continue  # nothing to suppress, so nothing to declare
        assert rel in IMPORTED_NOT_ENTERED, (
            f"{rel} is imported by a scheduled job and spawns at {outside}. Add it to "
            f"UNATTENDED so its spawns are checked, or -- if the spawns are only in "
            f"its own main() -- to IMPORTED_NOT_ENTERED with the reason."
        )
        assert not outside, (
            f"{rel} is exempt as main()-only, but spawns at {outside} outside main(). "
            f"Those are reachable from a job; move it into UNATTENDED."
        )


def test_the_import_exemptions_are_not_stale():
    """A module that stopped being imported, or stopped spawning, keeps an exemption
    that reads as a decision someone made about today's code."""
    closure = import_closure()
    for rel in IMPORTED_NOT_ENTERED:
        assert rel in closure, f"{rel} is exempt but no job imports it any more"
        assert rel not in UNATTENDED, f"{rel} is both exempt and checked"
        assert spawn_sites((REPO_ROOT / rel).read_text(encoding="utf-8")), (
            f"{rel} is exempt as main()-only, but no longer spawns anything at all"
        )
