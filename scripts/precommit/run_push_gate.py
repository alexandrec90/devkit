#!/usr/bin/env python3
"""Run the PR gate locally, before a push: lint, the test suite, then the hook tests.

Published as the `devkit-push-gate` hook and wired at pre-commit's **pre-push** stage.
The commit stage stays the sub-second fixers; this is the rest of what CI runs, at the
moment CI would run it, so a failure is read from `logs/` a minute after `git push`
instead of from a workflow artifact ten minutes after it. It exists because the gap
between the two was the routine way a green commit became a red PR: nothing between a
commit and the runner ever ran mypy or a test.

Three of the commands CI runs, stopping at the first failure so each wrapper's artifact is
the one still on disk when the push is refused, plus one CI has no use for:

  1. `scripts/lint-all.py` -- ruff, mypy, whatever else the project's copy runs
  2. `scripts/run-tests.py` -- the application suite
  3. `pytest scripts/hooks/tests/` -- the vendored harness tier
  4. `scripts/posix-rehearsal.py` -- the suite again, with the host's platform faked

Lint is first because it auto-fixes: anything that can rewrite a file has to come after
it, or the later step reports clean on what the earlier one just repaired. The two test
tiers' order is free, and is deliberately not CI's -- the gate stops at the first failure,
so running the application suite second keeps `logs/test-failures.log` the artifact the
refusal points at, while CI (which has no such stop) runs the fast vendored tier first so
a broken harness still reports when the application suite is red.

The rehearsal is last because it is the only step that can pass and fail for the same
reason twice: running it before the suite would report a platform assumption in a test
that is simply broken, and "fix the real failure first" is the cheaper order. It is also
the one step with no CI counterpart, which `tests/test_gate_parity.py` requires a written
reason for -- see `LOCAL_ONLY` there. Running it in CI would rehearse POSIX on a POSIX
runner, which is the suite a second time and nothing else.

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

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_by_path

HOOK_ID = "devkit-push-gate"

# What a fake runner in the tests has to look like: called with the argv, `cwd`, `check`
# and `env`, answering a CompletedProcess whose `returncode` is read.
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
    Step("posix rehearsal", ("scripts/posix-rehearsal.py",), "scripts/posix-rehearsal.py"),
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


# Git exports these into every hook's environment, naming the repository the push is
# happening in. They are not hints: each one **overrides a subprocess's `cwd=`**, so a
# test fixture that builds a throwaway repo and runs `git commit` in it with an inherited
# environment commits to the real repository instead.
#
# The first three are the ones observed to bite. The rest redirect the same resolution by
# another route -- the object store, the index format, the ceiling git stops searching at
# -- and cost nothing to strip: a step that wants the pushed repository has `cwd`.
LEAKED_GIT_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
    "GIT_INDEX_VERSION",
)


def gate_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the gate's steps run in: this hook's, minus git's own variables.

    Every step here is spawned from inside a git hook, and the suites they run build
    throwaway repositories by the dozen. Without this scrub those fixtures write to the
    repository being pushed: observed as `init`, `seed`, `c0`, `c1` and a `checkout` of a
    fixture's tag landing in a real worktree's reflog, leaving its branch ref pointing at
    a fixture commit and its index unusable. Nothing reported it -- the push was refused
    for an unrelated failing test, and the corruption was found afterwards in `git
    reflog`.

    Scrubbed here rather than in each fixture because this is the seam where the
    variables enter: one place covers all four steps, every suite under them, and every
    consumer -- and a fixture that forgets the scrub is not a defect anyone would notice
    until it has already rewritten a branch. `scripts/hooks/tests/test_session_start.py`
    scrubs the first four names for the same reason, and is the precedent for the list.

    A step that genuinely wants the pushed repository has `cwd` -- which is the repo root
    -- and `git rev-parse`, both of which say the same thing without steering an
    unrelated subprocess.
    """
    env = dict(os.environ if environ is None else environ)
    for leaked in LEAKED_GIT_VARS:
        env.pop(leaked, None)
    return env


def plan(root: Path) -> list[tuple[Step, list[str] | None]]:
    """Each step with the command that runs it, or None when `root` lacks its file."""
    python = interpreter(root)
    return [
        (step, [python, *step.argv] if (root / step.requires).exists() else None) for step in STEPS
    ]


def run_gate(root: Path, runner: Runner = subprocess.run) -> int:
    """Run the steps in order; the first non-zero exit is the hook's, and ends the run."""
    env = gate_env()
    for step, command in plan(root):
        if command is None:
            print(f"push-gate: {step.name}: no {step.requires} in this project -- skipped")
            continue
        print(f"push-gate: {step.name}: {' '.join(command[1:])}", flush=True)
        result = runner(command, cwd=root, check=False, env=env)
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
