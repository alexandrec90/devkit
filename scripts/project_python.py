#!/usr/bin/env python3
"""Which interpreter the project's dev tooling actually lives in.

**A tool that is installed and a tool that is reachable are different facts**, and
everything here exists because devkit spent a session unable to tell them apart. The
`.venv` held `pytest`, `ruff` and `mypy`; the interpreter on `PATH` was the bare
workstation CPython, which held none of them. Three separate surfaces read the second
fact and reported it as the first:

| surface | what it did | what it should have said |
| --- | --- | --- |
| `run-tests.py` | `No module named pytest`, artifact carrying only that | run them under the venv |
| `lint-all.py` | `ruff: not installed - skipped`, then **`lint-all: clean`** | nothing ran, so nothing is clean |
| `.pre-commit-config.yaml` | ``Executable `ruff` not found``, commit refused | same |

The middle row is the dangerous one and the reason this module is not just a
convenience: a green from a linter that ran nothing is a **false negative on every rule
at once**, and it is indistinguishable at a glance from the real thing. The lint policy
in `.claude/rules/engineering.md` is built on findings being actionable; a check that
reports clean without looking teaches everyone to believe a word it has not earned.

Why the interpreter and not `PATH`: a venv's `Scripts/`(`bin/`) directory is only on
`PATH` after activation, and **an agent's shell is never activated** -- it is a fresh
non-interactive process per tool call, so an `activate` in one call is gone by the
next. The interpreter, by contrast, is a path on disk that needs no ambient state, and
`<venv python> -m ruff` reaches the same executable that `ruff` would have. So callers
resolve an interpreter here and spawn `-m <tool>`, rather than hoping for a `PATH` that
agent sessions structurally do not have.

**Never installs anything.** A missing venv is reported, not provisioned: this runs
inside a pre-commit hook and a PostToolUse lint pass, neither of which may take the
minutes an install costs, and both of which run in repos this module does not own.

Stdlib only -- `scripts/hooks/` imports it, and those run before any venv exists.
Tested in `tests/test_project_python.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Where a venv keeps its interpreter, per platform. Windows puts it in `Scripts/`, every
# POSIX platform in `bin/`; both spellings are tried on every platform rather than
# branching on `os.name`, because a repo checked out on one and run under WSL or Git Bash
# on the other is an ordinary day in this workspace and costs one `is_file()` to survive.
VENV_SUBPATHS: tuple[tuple[str, ...], ...] = (
    ("Scripts", "python.exe"),
    ("bin", "python"),
)

# The directory name a project's virtualenv uses. Single-valued on purpose: devkit is
# uv-native and `uv sync` writes `.venv`, so a second spelling here would be a guess
# about a layout nothing in this repo produces.
VENV_DIR = ".venv"

# Set by `re_exec` on the child it spawns, and read by it on entry. Without it a venv
# whose interpreter somehow still cannot import the tool would re-exec itself forever,
# and a hook that spins is worse than one that fails.
REEXEC_GUARD = "DEVKIT_PROJECT_PYTHON_REEXEC"


def venv_python(root: Path) -> Path | None:
    """The interpreter inside `root`'s virtualenv, or None when there is not one.

    None is a first-class answer rather than a failure: a fresh clone, a CI runner that
    installs tools globally, and a consumer project that never adopted a venv are all
    ordinary, and every caller here has a correct behaviour for "no venv" that is not
    "crash".
    """
    for parts in VENV_SUBPATHS:
        candidate = root / VENV_DIR / Path(*parts)
        if candidate.is_file():
            return candidate
    return None


def has_module(executable: str | Path, module: str) -> bool:
    """Whether `executable` can import `module`, asked by running it.

    `importlib.util.find_spec` would answer for *this* interpreter, which is the one
    question no caller has: the whole point is to ask about a different one. So this
    spawns it, and a spawn that fails for any reason -- missing file, no permission, a
    timeout -- answers False, because "cannot be asked" and "does not have it" lead to
    the same fallback and distinguishing them would only give the caller a second error
    path that does nothing different.
    """
    try:
        result = subprocess.run(
            [str(executable), "-c", f"import {module}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def interpreter(root: Path, module: str = "", current: str | None = None) -> str:
    """The interpreter to run `module` with: the current one when it can, else the venv's.

    **The current interpreter wins when it already has the module.** That ordering is
    what keeps CI and an activated shell untouched -- both arrive with the tool
    importable, so neither pays a subprocess to be told what it already knows, and
    neither is silently switched onto a venv that may hold different versions.

    With no `module` named, the venv is preferred outright. That is the "run the project's
    tooling" case, where the caller wants the environment rather than a specific import.
    """
    here = current or sys.executable
    if module and has_module(here, module):
        return here
    venv = venv_python(root)
    if venv is None:
        return here
    if module and not has_module(venv, module):
        return here
    return str(venv)


def re_exec(
    root: Path, module: str, argv: list[str], env: dict[str, str] | None = None
) -> int | None:
    """Re-run this process under the venv interpreter. None when it should carry on here.

    The caller's own entry point uses it as a first statement:

        code = project_python.re_exec(REPO_ROOT, "pytest", sys.argv)
        if code is not None:
            return code

    None means "you are already the right interpreter, continue" -- which is the answer
    on CI, in an activated shell, and on any machine with no venv. An int means a child
    ran and this process's only remaining job is to hand its exit code up unchanged.

    Guarded by `REEXEC_GUARD` so the child never re-execs again. That is not a
    theoretical loop: `has_module` can answer False for a venv interpreter that is
    present but broken -- a half-finished `uv sync`, a `.venv` copied between machines --
    and without the guard each generation would spawn another.
    """
    environment = dict(os.environ if env is None else env)
    if environment.get(REEXEC_GUARD):
        return None
    chosen = interpreter(root, module, current=sys.executable)
    if Path(chosen) == Path(sys.executable):
        return None
    environment[REEXEC_GUARD] = "1"
    try:
        return subprocess.run([chosen, *argv], env=environment, check=False).returncode
    except (OSError, subprocess.SubprocessError):
        # The venv looked right and would not start. Carrying on under the current
        # interpreter reproduces the original diagnosis rather than replacing it with a
        # spawn error the reader cannot act on.
        return None


def tool_command(root: Path, module: str, args: list[str]) -> list[str]:
    """`[<interpreter>, "-m", <module>, *args]` -- the spawn every caller here wants.

    One place so that the `-m` form is not re-derived at three call sites, and so the
    fallback when there is no venv is identical in all of them.
    """
    return [interpreter(root, module), "-m", module, *args]
