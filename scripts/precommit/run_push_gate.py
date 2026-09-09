#!/usr/bin/env python3
"""Run the PR gate locally, before a push: lint, the test suite, then the hook tests.

Published as the `devkit-push-gate` hook and wired at pre-commit's **pre-push** stage.
The commit stage stays the sub-second fixers; this is the rest of what CI runs, at the
moment CI would run it, so a failure is read from `logs/` a minute after `git push`
instead of from a workflow artifact ten minutes after it. It exists because the gap
between the two was the routine way a green commit became a red PR: nothing between a
commit and the runner ever ran mypy or a test.

Three of the commands CI runs, stopping at the first failure so each wrapper's artifact is
the one still on disk when the push is refused:

  1. `scripts/lint-all.py` -- ruff, mypy, whatever else the project's copy runs
  2. `scripts/run-tests.py` -- the application suite
  3. `pytest scripts/hooks/tests/` -- the vendored harness tier

Lint is first because it auto-fixes: anything that can rewrite a file has to come after
it, or the later step reports clean on what the earlier one just repaired. The two test
tiers' order is free, and is deliberately not CI's -- the gate stops at the first failure,
so running the application suite second keeps `logs/test-failures.log` the artifact the
refusal points at, while CI (which has no such stop) runs the fast vendored tier first so
a broken harness still reports when the application suite is red.

What the gate does *not* reproduce is not a judgement call left to the reader:
`tests/test_gate_parity.py` reads `.github/workflows/pr-gate.yml` and fails on any `run:`
step that neither matches a step here nor carries a written reason for the difference.

Each step is skipped, out loud, when the project does not have the file it needs: the
wrappers are project-owned (`.claude/rules/engineering.md`, "a missing one is a silent
skip by design"), and a project that has not adopted the vendored tier has no hook tests.

Runs with the consuming repo as cwd, the way every hook in this directory does, and
spawns the project's own interpreter through `project_python` when the project ships it,
so a push from a shell that never activated the venv still runs the venv's tools.
`SKIP=devkit-push-gate git push` is pre-commit's own bypass for a deliberate WIP push.

Stdlib only. Tested in `tests/test_run_push_gate.py`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_by_path

HOOK_ID = "devkit-push-gate"

# What a fake runner in the tests has to look like: called with the argv, `cwd` and
# `check`, answering a CompletedProcess whose `returncode` is read.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Step:
    name: str
    # After the interpreter, which is resolved per project rather than written here.
    argv: tuple[str, ...]
    # The repo-relative path without which the step has nothing to run.
    requires: str


STEPS: tuple[Step, ...] = (
    Step("lint", ("scripts/lint-all.py",), "scripts/lint-all.py"),
    Step("tests", ("scripts/run-tests.py",), "scripts/run-tests.py"),
    Step("hook tests", ("-m", "pytest", "scripts/hooks/tests/", "-q"), "scripts/hooks/tests"),
)


def interpreter(root: Path) -> str:
    """The interpreter to run the project's tooling with.

    The project's `scripts/project_python.py` when it has one -- that module knows about
    the venv, and about borrowing a checkout's venv from a worktree that has none -- and
    otherwise the interpreter running this hook. The wrappers re-exec themselves through
    the same helper, so the pytest step is the one that needs the answer here.
    """
    helper = root / "scripts" / "project_python.py"
    if not helper.is_file():
        return sys.executable
    try:
        module = load_by_path("project_python", helper)
    except (ImportError, OSError, SyntaxError):
        # Unloadable, unreadable or unparsable: the interpreter question is then the
        # only thing the helper was going to answer, and this one is the fallback its
        # own callers use. Anything else the helper raises is a defect worth the
        # traceback, and a refused push is the right place to read it.
        return sys.executable
    return str(module.interpreter(root, "pytest"))


def plan(root: Path) -> list[tuple[Step, list[str] | None]]:
    """Each step with the command that runs it, or None when `root` lacks its file."""
    python = interpreter(root)
    return [
        (step, [python, *step.argv] if (root / step.requires).exists() else None) for step in STEPS
    ]


def run_gate(root: Path, runner: Runner = subprocess.run) -> int:
    """Run the steps in order; the first non-zero exit is the hook's, and ends the run."""
    for step, command in plan(root):
        if command is None:
            print(f"push-gate: {step.name}: no {step.requires} in this project -- skipped")
            continue
        print(f"push-gate: {step.name}: {' '.join(command[1:])}", flush=True)
        result = runner(command, cwd=root, check=False)
        if result.returncode:
            print(
                f"push-gate: {step.name} failed (exit {result.returncode}). Fix it from the "
                f"artifact under logs/, or push anyway with SKIP={HOOK_ID}."
            )
            return int(result.returncode)
    print("push-gate: clean")
    return 0


def main() -> int:
    return run_gate(Path.cwd())


if __name__ == "__main__":
    sys.exit(main())
