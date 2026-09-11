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

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"

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
    "GIT_INDEX_VERSION",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
    "GIT_NAMESPACE",
):
    os.environ.pop(_leaked, None)

# `scripts/` for the importable devkit modules; `scripts/hooks/` so a test can load
# the vendored harness_config that generated manifests must satisfy.
for _path in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "hooks"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Every import below the bootstrap is E402 by construction — that is the whole design of
# this file, stated once here rather than restated on each line. A per-line `# noqa: E402`
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
import ship
import sweep
import task_branch
import task_input
import task_slug
import worktree
import wt_profile

# Reached through `worktree` rather than imported again, and not only to spare this file
# an eleventh suppression: the box teardown is monkeypatched from both test modules, so
# it has to be the same module object the code under test holds.
box_teardown = worktree.box_teardown

# The live multi-root registry: a workstation file that sits *beside* the checkout, so
# it exists on the desktop this harness drives and never in a CI clone. The handful of
# tests that assert against the real file (rather than a fixture) carry the marker
# below; without it they fail on GitHub with a FileNotFoundError naming a path no
# runner could have. Skipping is deliberate — the coverage they add is "the registry on
# *this* machine is intact", which is exactly the thing CI has no opinion about.
LIVE_WORKSPACE = devkit_project.DEFAULT_WORKSPACE

needs_live_workspace = pytest.mark.skipif(
    not LIVE_WORKSPACE.exists(),
    reason=f"{LIVE_WORKSPACE.name} is a workstation-local registry, not part of the checkout",
)


def on_a_task_branch(root: Path) -> bool:
    """Is `root` carrying an edit that has not merged yet?

    The other half of the marker below, and it was missing. That marker's *reason* has
    always said "after a branch merges", but its predicate asked only WHERE the checkout
    is -- so a box was skipped and the static checkout on a task branch was not, though
    both hold exactly the same thing: a canonical `workspace.jsonc` the live file cannot
    match yet, because rendering happens after the merge.

    The failure that closes is the one `in_an_ephemeral_box` closed for boxes, arriving
    by the other route: an agent editing the workspace file in the static checkout gets a
    red suite at the finish line, whose first suggested fix (`--adopt-workspace`) is the
    one move that deletes the edit it just made.

    Reads the branch rather than the diff: `task_branch.MANAGED_BRANCH_PREFIXES` is the
    same namespace `ship.py` accepts, plus the `worktree-<topic>` spelling
    `claude --worktree` cuts. Anything else -- the default branch above all -- is a
    checkout whose copy is supposed to match, and there the check keeps meaning something.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    branch = (done.stdout or "").strip()
    return bool(branch) and (
        branch.startswith(task_branch.MANAGED_BRANCH_PREFIXES)
        or branch.startswith(ship.CLI_WORKTREE_PREFIX)
    )


def in_an_ephemeral_box(root: Path) -> bool:
    """True when `root` is a disposable worktree of either tier, not a static checkout.

    Keyed off `worktree.BOXES_DIR_NAME` and `sweep.cli_worktree_checkout` rather than the
    literals, so none of the three can drift.

    Both tiers, because what the marker below actually asks is "is this checkout the one
    the live workspace file is rendered from", and a `.claude/worktrees/` worktree is no
    more that than a box is. It read as one until a branch edited `workspace.jsonc` from
    inside one and the drift check reported its own un-merged edit as drift -- which is
    the failure the marker exists to prevent, arriving through the tier that did not
    exist when it was written.
    """
    return root.parent.name == worktree.BOXES_DIR_NAME or (
        sweep.cli_worktree_checkout(root) is not None
    )


# The narrower marker, for every assertion that reads the live file expecting *this*
# checkout's canonical copy. The live workspace is rendered from the static checkout once
# a branch merges, so from a box the comparison reads a file describing merged `main`
# against a canonical copy carrying an un-merged edit -- and reports the edit as drift.
# It began as the drift check alone, and the registration/retirement pair in
# `test_devkit_project.py` was added to it after a branch that introduced a picker made
# both of them assert that its own un-merged edit had already landed.
# Every task that touches `workspace.jsonc` therefore failed the Stop gate at the finish
# line, with the failure's own first suggestion (`--adopt-workspace`) being the one move
# that deletes the branch's edit. Rendering early is not the answer either: the live file
# would then point VS Code at a task shape the *static* checkout's scripts cannot serve
# until the same branch merges. So the check stays exactly where it means something.
needs_the_static_checkout = pytest.mark.skipif(
    in_an_ephemeral_box(REPO_ROOT) or on_a_task_branch(REPO_ROOT),
    reason="the live workspace is rendered from the static checkout after a branch merges",
)

__all__ = [
    "LIVE_WORKSPACE",
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
    "load_script",
    "needs_live_workspace",
    "needs_the_static_checkout",
    "on_a_task_branch",
    "sweep",
    "task_input",
    "task_slug",
    "vendor_manifest",
    "worktree",
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
    for rel in load_script("scripts/sync-devkit.py").MANIFEST:
        source = REPO_ROOT / rel
        if not source.is_file():
            raise AssertionError(f"MANIFEST names {rel}, which is not in devkit")
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


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
