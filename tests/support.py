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
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"

# `scripts/` for the importable devkit modules; `scripts/hooks/` so a test can load
# the vendored harness_config that generated manifests must satisfy.
for _path in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "hooks"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import devkit_jsonc  # noqa: E402
import devkit_ports  # noqa: E402
import devkit_project  # noqa: E402
import devkit_render  # noqa: E402
import git_policy  # noqa: E402
import harness_config  # noqa: E402
import sweep  # noqa: E402
import task_input  # noqa: E402
import task_slug  # noqa: E402
import worktree  # noqa: E402

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


def in_an_ephemeral_box(root: Path) -> bool:
    """True when `root` is one of the workspace's disposable boxes, not a static checkout.

    Keyed off `worktree.BOXES_DIR_NAME` rather than the literal, so the two cannot drift.
    """
    return root.parent.name == worktree.BOXES_DIR_NAME


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
    in_an_ephemeral_box(REPO_ROOT),
    reason="the live workspace is rendered from the static checkout after a branch merges",
)

__all__ = [
    "LIVE_WORKSPACE",
    "REPO_ROOT",
    "TEMPLATES",
    "devkit_jsonc",
    "devkit_ports",
    "devkit_project",
    "devkit_render",
    "gh_steps_without_repo_context",
    "git_policy",
    "harness_config",
    "in_an_ephemeral_box",
    "load_script",
    "needs_live_workspace",
    "needs_the_static_checkout",
    "sweep",
    "task_input",
    "task_slug",
    "vendor_manifest",
    "worktree",
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
