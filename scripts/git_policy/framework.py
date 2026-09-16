"""Running the repository's own `pre-commit` framework after the policy passes.

Split from the policy because it answers a different question -- not "may this ref move"
but "where is this project's pre-commit, and what environment does it need" -- and the
two have never shared anything but the runner.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ._core import Runner, _git, _stdout, console_python, emit
from .branch import parse_push_updates


def _venv_roots(root: Path, runner: Runner) -> tuple[Path, ...]:
    """`root`, then the checkout it is a worktree of when that is a different directory.

    A worktree checks out **tracked files only**, and `.venv` is gitignored in every
    project here — so a worktree nobody provisioned has no virtualenv of its own, and
    looking in one place made "commit from a worktree" impossible: the framework was
    reported as not installed and the commit was refused, in a repo whose checkout has
    `pre-commit` sitting in `.venv` two directories up.

    Read off `--git-common-dir` rather than off a path convention, because the two
    worktree tiers on this machine sit at different depths — `<workspace>/.worktrees/`
    for a provisioned box, `<checkout>/.claude/worktrees/` and `~/.codex/worktrees/` for
    the plain kind a `--worktree` flag and `scripts/agent-worktree.py` cut — and git already knows the
    answer for both, and for whatever the third tier turns out to be. A box has its own
    `.venv`, so this changes nothing for one; the ordering keeps it that way.

    Through the injected `runner` rather than a bare `subprocess.run`, for the reason
    `NO_WINDOW` is declared in `_core`: this file is in the reachable set of the
    unattended jobs, and a spawn without `creationflags` opens a console window under
    `pythonw.exe`. `tests/test_scheduled_jobs.py` is the gate, and it caught this one on
    its first commit -- it now names `_core.py` as the package's single spawn point,
    which is what keeps this tier honest about reaching git only through the runner.
    """
    main = _stdout(
        _git(runner, "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    if not main:
        return (root,)
    checkout = Path(main).parent
    return (root,) if checkout == root else (root, checkout)


def _pre_commit_command(root: Path, runner: Runner) -> list[str] | None:
    candidates = tuple(
        base / ".venv" / tail
        for base in _venv_roots(root, runner)
        for tail in (Path("Scripts") / "pre-commit.exe", Path("bin") / "pre-commit")
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    executable = shutil.which("pre-commit")
    if executable:
        return [executable]
    if importlib.util.find_spec("pre_commit") is not None:
        return [console_python(), "-m", "pre_commit"]
    return None


# Two agents reported this message in one week, both saying it names a refusal and no
# remedy -- so it reads as policy declining the commit rather than a tool being missing,
# and the first guess is `--no-verify`. The stale-copy line is not padding: this file is
# COPIED to `~/.devkit/git-hooks` and the hooks run the copy, so `_venv_roots` can be
# fixed here and still be missing where it fires -- which is how both reports were
# produced. Nothing about a stale copy looks wrong; only `--check` answers it.
NO_PRE_COMMIT = (
    "[devkit branch policy] project has .pre-commit-config.yaml but pre-commit is not installed",
    "  looked in: .venv of this tree and of the checkout it was cut from, PATH, "
    "and this interpreter",
    "  install it:      uv pip install pre-commit   (or: pip install pre-commit)",
    "  provision a box: python scripts/worktree.py provision <box>",
    "  if it IS installed, the hooks may be running a stale copy of this policy:",
    "                   python scripts/install-git-policy.py --check",
)


def framework_env(
    command: Sequence[str], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment the framework runs in: its own directory first on `PATH`.

    `_pre_commit_command` deliberately looks past `PATH` -- into this tree's `.venv`
    and then the checkout's -- because a plain worktree has no virtualenv of its own.
    That finding is only half an answer. pre-commit resolves a `language: system`
    hook's executable from the **subprocess `PATH`**, not from wherever it was itself
    launched, so committing from such a worktree found `pre-commit.exe` two directories
    up and then failed with `Executable detect-secrets-hook not found` -- the framework
    and its hooks disagreeing about which environment they are in, reported as a
    missing tool. Putting the chosen executable's own directory first closes that: the
    hooks installed beside pre-commit are the ones it was resolved from.

    First rather than appended, so the venv wins over a different copy already on
    `PATH` -- the whole point of having looked there. A no-op when the command was
    found on `PATH` to begin with, and harmless for the `-m pre_commit` form, whose
    interpreter sits in that same directory.
    """
    env = dict(os.environ if environ is None else environ)
    if not command:
        return env
    home = str(Path(command[0]).parent)
    existing = env.get("PATH", "")
    env["PATH"] = f"{home}{os.pathsep}{existing}" if existing else home
    return env


def _run_pre_commit_framework(
    root: Path, runner: Runner, stage: str = "pre-commit", raw_updates: str = ""
) -> int:
    if not (root / ".pre-commit-config.yaml").is_file():
        return 0
    # The push stage runs for a push that publishes a branch, and not for a deletion or
    # a tag-only push: the stage is the PR gate (minutes of tests), and nothing a
    # deletion could break is in it. `pre-commit install` would wire the same stage
    # itself; this dispatcher owns `core.hooksPath`, so it has to run it in its place.
    if stage == "pre-push" and all(u.deletion for u in parse_push_updates(raw_updates)):
        return 0
    command = _pre_commit_command(root, runner)
    if command is None:
        for line in NO_PRE_COMMIT:
            emit(line, stream=sys.stderr)
        return 1
    # `--all-files` for the push stage: its hooks are `always_run` gates over the whole
    # tree, and without it pre-commit scopes to the *staged* diff -- stashing unstaged
    # work for the duration -- to run hooks that ignore the file list anyway.
    args = ["run", "--hook-stage", stage] + (["--all-files"] if stage == "pre-push" else [])
    # `stream=True`: relay the framework's output as it is produced rather than after it
    # finishes. Nothing here parses it -- only the exit code is read -- and the push stage
    # is minutes of lint and tests, so captured it made `git push` print nothing at all
    # until the gate was over. That silence was read as a hang and answered with a second
    # push, which started a second full gate on the same machine; three concurrent gates
    # starving each other is how a branch stopped landing at all. The relay below still
    # runs, because `run_command` falls back to capturing whenever this process has no
    # stdout of its own to lend -- a scheduled job under `pythonw.exe`, or a test harness.
    result = runner([*command, *args], cwd=root, env=framework_env(command), stream=True)
    if result.stdout:
        emit(result.stdout, end="")
    if result.stderr:
        emit(result.stderr, end="", stream=sys.stderr)
    return result.returncode
