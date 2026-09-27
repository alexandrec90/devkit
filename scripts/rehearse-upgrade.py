#!/usr/bin/env python3
"""Rehearse an upgrade: a project rendered at the previous release adopts this tree.

The PR gate's job *A project on the previous release survives adopting this one*
(`.github/workflows/pr-gate.yml`) exists for the class of break devkit's own suite
cannot see -- a vendored change assuming something a fresh render has and an upgraded
project does not. That job used to be an inline shell block, so a session could run
every targeted check green and still learn about that class only from a gate cycle:
#424's vendored temp-root contract reddened exactly such a project and cost a full
cycle and a fixer session to find (85c4fb7f). And the block was not safe to copy onto
a workstation: `new-project.py` registers its render in the machine's real
`alex-projects.code-workspace` unless told `--no-register`.

So the rehearsal is this script, and the gate job runs it: one recipe, run the same way
in both places.

1. The previous release is the newest tag, not `git describe` -- which release is
   newest, not which is reachable (`new-project.latest_devkit_tag` has the account).
2. Its generator renders a bare project into a scratch directory, `--no-register`.
3. `sync-devkit.py --pull` runs twice, as `upgrade-project.pull_to_fixpoint` does: the
   first pull replaces `sync-devkit.py` itself, the second runs the pulled copy.
4. The adoption is committed, then the consumer's own gate runs: the vendored hook
   tests, `run-tests.py`, `lint-all.py`, and `sync-devkit.py --check`.

Two things differ from a bare copy of the CI block, both to make a workstation look like
the runner: `$DEVKIT_DIR` is set only for the `--check` it is meant for, and the
adoption commit runs with no git hooks -- a runner has none, and this machine's global
branch policy would refuse a commit on the probe's default branch.

Usage:
    python scripts/rehearse-upgrade.py

Failures go to `logs/rehearse-upgrade.log`, cleared on a pass. Tested in
`tests/test_rehearse_upgrade.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = Path("logs") / "rehearse-upgrade.log"
PROJECT = "probe_upgrade"
DEVKIT_ENV = "DEVKIT_DIR"
NO_REGISTER = "--no-register"

EXIT_OK = 0
EXIT_FAILED = 1

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    devkit_dir: str = ""


def run_command(argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """`subprocess.run`, output captured; a missing program is a failed step."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **kwargs,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def previous_tag(root: Path, runner: Runner = run_command) -> str:
    """The newest release tag, or "" when the repository has none."""
    listed = runner(["git", "tag", "--sort=-v:refname"], cwd=str(root))
    lines = (listed.stdout or "").split() if listed.returncode == 0 else []
    return lines[0] if lines else ""


def knows_no_register(generator: Path) -> bool:
    """Whether that release's `new-project.py` can render without touching the workspace."""
    try:
        return NO_REGISTER in generator.read_text(encoding="utf-8")
    except OSError:
        return False


def plan(python: str, src: Path, previous: Path, parent: Path, no_hooks: Path) -> list[Step]:
    """Every step, in order, as the gate job runs them."""
    project = parent / PROJECT
    # `--allow-untagged`: a PR tree has no tag. `--allow-dirty`: a session's tree is
    # uncommitted, and the probe is thrown away; on the runner's clean checkout it is inert.
    pull = (
        python,
        *("scripts/sync-devkit.py", "--pull", "--src", str(src)),
        *("--allow-untagged", "--allow-dirty"),
    )
    render = (
        python,
        str(previous / "scripts" / "new-project.py"),
        PROJECT,
        *("--preset", "bare", "--parent", str(parent), "--no-remote", "--yes", NO_REGISTER),
    )
    commit = ("git", "-c", f"core.hooksPath={no_hooks}", "commit", "-qm", "Adopt devkit")
    return [
        Step("render at the previous release", render, parent),
        Step("pull this tree", pull, project),
        Step("pull again with the pulled sync-devkit", pull, project),
        Step("stage the adoption", ("git", "add", "-A"), project),
        Step("commit the adoption", commit, project),
        Step(
            "vendored hook tests", (python, "-m", "pytest", "scripts/hooks/tests/", "-q"), project
        ),
        Step("run-tests", (python, "scripts/run-tests.py"), project),
        Step("lint-all", (python, "scripts/lint-all.py"), project),
        Step(
            "drift check",
            (python, "scripts/sync-devkit.py", "--check"),
            project,
            devkit_dir=str(src),
        ),
    ]


def step_env(step: Step, base: Mapping[str, str]) -> dict[str, str]:
    """`base` without `$DEVKIT_DIR`, except for the one step that names it."""
    env = {key: value for key, value in base.items() if key != DEVKIT_ENV}
    if step.devkit_dir:
        env[DEVKIT_ENV] = step.devkit_dir
    return env


def run_steps(
    steps: Sequence[Step], runner: Runner, base: Mapping[str, str]
) -> tuple[Step, subprocess.CompletedProcess[str]] | None:
    """The first step that failed, with what it said; None when every step passed."""
    for step in steps:
        print(f"rehearse-upgrade: {step.name}", flush=True)
        done = runner(list(step.argv), cwd=str(step.cwd), env=step_env(step, base))
        if done.returncode != 0:
            return step, done
    return None


def report(failed: tuple[Step, subprocess.CompletedProcess[str]] | None, tag: str) -> str:
    """The artifact: empty on a pass, the failing step and its output otherwise."""
    if failed is None:
        return ""
    step, done = failed
    return (
        f"# rehearse-upgrade: FAILED at '{step.name}' (adopting this tree from {tag})\n"
        f"# cwd: {step.cwd}\n# argv: {' '.join(step.argv)}\n# exit: {done.returncode}\n\n"
        f"{done.stdout or ''}{done.stderr or ''}"
    )


def rehearse(tag: str, scratch: Path, runner: Runner, base: Mapping[str, str]) -> str:
    """Check the previous release out under `scratch`, run the plan, remove it again."""
    previous, parent, no_hooks = scratch / "previous", scratch / "upgrade", scratch / "no-hooks"
    parent.mkdir()
    no_hooks.mkdir()
    added = runner(["git", "worktree", "add", "--detach", str(previous), tag], cwd=str(REPO_ROOT))
    if added.returncode != 0:
        return report((Step("check out the previous release", (), REPO_ROOT), added), tag)
    try:
        if not knows_no_register(previous / "scripts" / "new-project.py"):
            return (
                f"# rehearse-upgrade: {tag}'s new-project.py has no {NO_REGISTER}, and "
                "rendering with it would register a probe in this machine's workspace.\n"
            )
        steps = plan(sys.executable, REPO_ROOT, previous, parent, no_hooks)
        return report(run_steps(steps, runner, base), tag)
    finally:
        runner(["git", "worktree", "remove", "--force", str(previous)], cwd=str(REPO_ROOT))


def main(argv: Sequence[str] | None = None, runner: Runner = run_command) -> int:
    del argv  # no options; accepted so the entry point has the house signature
    tag = previous_tag(REPO_ROOT, runner)
    artifact = REPO_ROOT / ARTIFACT
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if not tag:
        text = "# rehearse-upgrade: no release tag to rehearse from (fetch tags first)\n"
    else:
        with tempfile.TemporaryDirectory(
            prefix="devkit-rehearse-", ignore_cleanup_errors=True
        ) as scratch:
            text = rehearse(tag, Path(scratch), runner, os.environ)
    artifact.write_text(text, encoding="utf-8")
    if text:
        print(text, end="")
        print(f"rehearse-upgrade: FAILED -- details in {ARTIFACT}")
        return EXIT_FAILED
    print(f"rehearse-upgrade: a project at {tag} adopts this tree cleanly")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
