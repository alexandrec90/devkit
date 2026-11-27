"""Path setup and helpers for devkit's own tests.

Two things make this file's shape non-obvious, and both are load-bearing.

**There is deliberately no `conftest.py` in this directory.** `scripts/hooks/tests/`
has one, it is vendored (it is in `sync-devkit.py`'s MANIFEST), and every test in
that tree does `from conftest import load_module`. pytest puts both test directories
on `sys.path`, so a second `conftest.py` here would race it for the top-level module
name `conftest` — whichever directory pytest collected first would win, and the other
tree's tests would fail to import. That is exactly what happened: `pytest tests/
scripts/hooks/tests/` passed while `pytest scripts/hooks/tests/ tests/` failed with
seven collection errors. A uniquely-named module cannot collide, so path setup lives
here instead, and this module is imported first by every test in this tree.

**It re-exports the modules under test.** Importing `support` for its side effect and
then importing `devkit_ports` separately would work — until the import sorter put
`devkit_ports` first (alphabetically) and the path was not yet set up. Re-exporting
makes the dependency explicit and immune to reordering.
"""

import argparse
import importlib.util
import itertools
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"

# The one job exempt from "scheduled jobs run unelevated", by installer, with the reason.
# `tests/test_installer_contract.py` and `tests/test_scheduled_jobs.py` read this and
# nothing else: a second name here is a second elevated job, which is a design change.
ELEVATED_EXEMPTION: dict[str, str] = {
    "install-elevated-worker.py": (
        "Ending a stuck WSLService is refused to every unelevated token (its DACL grants no "
        "start or stop), and that recovery otherwise waits on a person. A central admin "
        "dispatcher was refused because it would run merged code as admin. So this one job "
        "has one verb, run from a copy in an admin-only folder, under an interpreter copied "
        "there too, and takes no input but a file name. An administrator registers it once "
        "per PC, so nothing unelevated maintains it; it runs nothing from a checkout, so it "
        "cannot be wrapped by log-wrap.py or write logs/."
    ),
}

# The harness kill switch, cleared for this whole process. `scripts/hooks/tests/` does the
# same thing in its `conftest.py` and for the same reason: the variable lives in a
# `settings.json` `env` block, so it is present in the environment of every agent session
# on a machine where someone has switched the harness off, and absent in CI. Inherited, it
# does not fail this suite -- it hollows it out. `worktree-guard.py` and `task_slug.py`
# consult it now, so forty end-to-end guard tests would drive a `main()` that returned
# before reading stdin and would pass by agreeing that nothing happened.
#
# Done here, at import, rather than in an autouse fixture, because an autouse fixture needs
# a `conftest.py` and this directory cannot have one -- see above. A test that wants the
# variable set still wins: `monkeypatch.setenv` runs long after this line.
os.environ.pop("DEVKIT_HOOKS_OFF", None)

# Git's repo scoping, cleared for the same reason and with far worse consequences if it
# is not. A hook inherits `GIT_DIR` and `GIT_INDEX_FILE`, so a suite run from inside one
# -- which is exactly what `devkit-push-gate` does on every push -- hands every test that
# builds a repo in `tmp_path` and shells out to `git -C <tmp>` the PUSHING repo instead.
# That is not a failed assertion: on the push this was written for, the fixtures committed
# into this worktree, moved its branch through four of their own commits and left HEAD
# detached. `run_push_gate.gate_env` is the fix at the source; this is the suite refusing
# to act on the variables at all, because any other runner can inherit them too.
for _leaked in (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_INDEX_VERSION",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
):
    os.environ.pop(_leaked, None)

# `scripts/` for the importable devkit modules; `scripts/hooks/` so a test can load
# the vendored harness_config that generated manifests must satisfy.
for _path in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "hooks"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Every import below the bootstrap is E402 by construction — that is the whole design of
# this file, stated once here rather than restated on each line. A per-line E402 suppression
# said nothing a reader could act on and grew by one whenever a module was added; the
# claim is a property of the file, so it is written as one. Scoped to E402 alone, so any
# other finding in here still fails.
# ruff: noqa: E402
import devkit_jsonc
import devkit_ports
import devkit_project
import devkit_render
import git_policy
import guard_probes
import harness_config
import harness_state
import project_registry
import sweep
import task_slug
import worktree
import worktree_tiers
import wt_profile

# Reached through `worktree` rather than imported again, and not only to spare this file
# an eleventh suppression: the box teardown is monkeypatched from both test modules, so
# it has to be the same module object the code under test holds.
box_teardown = worktree.box_teardown

# The end-to-end tier `scripts/e2e.py` runs nightly on this workstation; `addopts`
# deselects it from every other run. A test that needs the checkouts beside this one --
# rather than a fixture -- carries it and reads them through `live_checkouts`.
local = pytest.mark.local


def live_checkouts() -> list[tuple[str, Path]]:
    """Every registered project with a checkout on this machine, as (name, path).

    For a `local` test, which asserts "the projects on *this* machine are intact" --
    exactly what CI has no opinion about. None found is a failure, never an empty pass:
    the tier runs on the workstation that holds them, so nothing has a reason to skip.
    """
    root = project_registry.projects_root()
    names = project_registry.projects(root)
    if not names:
        pytest.fail(f"no registered checkout under {root}")
    return [(name, root / name) for name in names]


def in_an_ephemeral_box(root: Path) -> bool:
    """True when `root` is a disposable worktree of either tier, not a static checkout.

    Keyed off `worktree_tiers` for both halves rather than the literals, so none of them
    can drift -- and the *shape* test rather than
    `sweep.cli_worktree_checkout`, because "is this disposable" is answerable from the
    path alone while "whose is it" is not: Codex's tier needs a `.git` read that a
    predicate should not depend on.
    """
    return worktree_tiers.is_box(root) or worktree_tiers.is_worktree(root)


__all__ = [
    "REPO_ROOT",
    "TEMPLATES",
    "box_teardown",
    "devkit_jsonc",
    "devkit_ports",
    "devkit_project",
    "devkit_render",
    "gh_steps_without_repo_context",
    "git_policy",
    "guard_probes",
    "harness_config",
    "harness_state",
    "in_an_ephemeral_box",
    "live_checkouts",
    "load_script",
    "local",
    "project_registry",
    "sweep",
    "task_slug",
    "vendor_manifest",
    "worktree",
    "worktree_tiers",
    "wt_profile",
]


def _logical_commands(script: str) -> list[str]:
    """`script` split on newlines, with backslash-continued lines rejoined."""
    joined = script.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines()]


def gh_steps_without_repo_context(workflow: dict) -> list[str]:
    """Names of steps that call `gh` in a job that has no checkout and no repo to use.

    `gh` resolves the repository from `git remote` whenever it is not told one, so in a
    job that skipped `actions/checkout` it exits on "fatal: not a git repository"
    before doing anything — a failure that names git and says nothing about the missing
    `--repo`. Either `GH_REPO` is in scope or every invocation passes `--repo`.
    """
    offenders = []
    workflow_env = workflow.get("env") or {}
    for job_name, job in (workflow.get("jobs") or {}).items():
        steps = job.get("steps") or []
        if any("actions/checkout" in str(step.get("uses", "")) for step in steps):
            continue
        job_env = job.get("env") or {}
        for step in steps:
            script = step.get("run")
            if not script:
                continue
            env = {**workflow_env, **job_env, **(step.get("env") or {})}
            if "GH_REPO" in env:
                continue
            for command in _logical_commands(script):
                if command.split(" ", 1)[:1] == ["gh"] and "--repo" not in command:
                    offenders.append(f"{job_name} / {step.get('name', '<unnamed>')}")
                    break
    return offenders


TEST_TIER_MARKERS = ("scripts/run-tests.py", "scripts/hooks/tests/")
LINT_MARKERS = ("scripts/lint-all.py", "ruff format", "ruff check")
# An `if:` holding one of these runs the step after any red step before it.
RUNS_AFTER_ANY_RED = ("!cancelled()", "always()")


def checks_a_red_check_skips(workflow: dict) -> list[str]:
    """Names of check steps -- a lint or a test tier -- that a red check before them in
    the job would skip.

    A step with no `if:` runs only on `success()`, so with two checks in one job a red
    first one leaves the second unrun, and the failure artifact names half the reds:
    a red vendored tier hid the devkit suite (ledger f07541c7), and a red mypy hid both
    test tiers (f872ef7c). Each later check must also run when the one before it failed:
    `!cancelled()` in its `if:`, or `success() || steps.<that check's id>.outcome ==
    'failure'`, which is enough where nothing earlier in the job can fail.
    """
    offenders = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        checks = [
            step
            for step in job.get("steps") or []
            if any(m in str(step.get("run", "")) for m in TEST_TIER_MARKERS + LINT_MARKERS)
        ]
        for before, step in itertools.pairwise(checks):
            condition = str(step.get("if", ""))
            wanted = f"steps.{before.get('id')}.outcome == 'failure'"
            if any(clause in condition for clause in RUNS_AFTER_ANY_RED):
                continue
            if "id" not in before or wanted not in condition:
                offenders.append(f"{job_name} / {step.get('name', '<unnamed>')}")
    return offenders


def vendor_manifest(root: Path) -> None:
    """Copy devkit's MANIFEST into a rendered tree, the way the generator does.

    `render_tree()` writes only what is under `templates/`. The real generator then
    runs `vendor_harness()`, which shells out to the project's freshly-copied
    `sync-devkit.py --pull` — so a project on disk is always *both* tiers, and a test
    that renders one of them is asserting about half a project.

    That matters more than it sounds: moving a file from `templates/` into the
    MANIFEST would otherwise take every test covering it out of scope in the same
    commit, and the suite would go green because the file had stopped existing rather
    than because it was still correct. `.github/workflows/dependabot-automerge.yml`
    made exactly that move.

    The bytes are copied here rather than by invoking `--pull` because the suite
    renders dozens of trees and a subprocess apiece would dominate it. The file list
    is read from `sync-devkit.py` itself, so this and the generator can disagree about
    *how* a file arrives but never about which files do.
    """
    # `manifest_for`, not `MANIFEST`: the gated tier is part of what a real pull delivers
    # to a tree whose `.devkit.toml` switches it on, and a helper that skipped it would
    # take every test of a gated file out of scope the way the docstring above warns.
    for rel in load_script("scripts/sync-devkit.py").manifest_for(root):
        source = REPO_ROOT / rel
        if not source.is_file():
            raise AssertionError(f"MANIFEST names {rel}, which is not in devkit")
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    # The pull's settings pass too, which is how a tree gets what its shape implies but no
    # template can spell: a project that installs no package of its own (the desktop
    # shape) has Claude Code link its `.venv` into every worktree, and the template is
    # strict JSON before rendering, with no room for a block that only one shape carries.
    load_script("scripts/project_settings.py").settings_pass(root)


def windows_layout(tmp_path, *names: str):
    """Real files named `names` under `tmp_path`, returned in the order given.

    The convention for testing a Windows-shaped interpreter or install layout, and the
    alternative to the thing that keeps failing CI: a hardcoded literal like
    `r"C:\\py\\pythonw.exe"`. `Path` splits that into components on Windows and leaves it
    as one filename on POSIX, so a test built on one asserts a real branch here and a
    branch that never runs on the `ubuntu-latest` runner -- silently, since the assertion
    is usually about the *result* rather than the split.

    `scripts/posix-rehearsal.py` deliberately does not gate this class: catching it would
    mean answering "does not exist" for backslash paths, which is right for a literal and
    wrong for every real interpreter path on this machine. So it is a convention, and this
    helper is what makes the convention cheaper than the literal.

        console, _ = windows_layout(tmp_path, "python.exe", "pythonw.exe")
    """
    made = []
    for name in names:
        target = tmp_path / name
        target.write_text("", encoding="utf-8")
        made.append(target)
    return tuple(made)


def load_script(relpath: str):
    """Load a hyphen-named script (path relative to the repo root) as a module.

    `new-project.py` cannot be imported normally. The subtlety is the registration
    order: the module must be in `sys.modules` **before** `exec_module` runs,
    because `@dataclass` resolves its string annotations by looking the defining
    module up by name — exec'ing first raises `AttributeError: 'NoneType' object has
    no attribute '__dict__'` from inside dataclasses, which points nowhere useful.
    """
    path = REPO_ROOT / relpath
    name = path.stem.replace("-", "_")
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {relpath} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def make_args(**overrides):
    """The argparse namespace `new-project.py`'s `plan()` expects, with the CLI's defaults."""
    new_project = load_script("scripts/new-project.py")
    base = {
        "name": "demo_project",
        "description": "A demo.",
        "display_name": "",
        "parent": "",
        "github_owner": "alexandrec90",
        "python_version": "3.12",
        "default_branch": "main",
        "devkit_ref": "v0.1.0",
        "db_url_scheme": "postgresql+psycopg",
        "src_layout": False,
        "preset": None,
        "remote": True,
        "register": True,
        "dry_run": True,
        **{f: False for f in new_project.FEATURES},
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def registry():
    return devkit_ports.load(REPO_ROOT)


def generate(tmp_path: Path, features: dict) -> Path:
    """Render the tree for one feature set into tmp_path and return the root.

    Here rather than in `test_new_project.py` so a test file that checks the generated
    tree as a whole can be run alone: `run-tests.py` names `test_generated_tree.py` for
    any template change, and the generator's own tests take minutes.
    """
    new_project = load_script("scripts/new-project.py")
    args = make_args(parent=str(tmp_path), **features)
    # The generator's own resolution, so a test cannot render a shape `main` would refuse.
    new_project.resolve_features(args)
    the_plan = new_project.plan(args, registry())
    the_plan.root.mkdir(parents=True, exist_ok=True)
    new_project.render_tree(the_plan, dry_run=False)
    new_project.write_package(the_plan, dry_run=False)
    # Both tiers, because a real project is both. See `vendor_manifest`.
    vendor_manifest(the_plan.root)
    return the_plan.root
