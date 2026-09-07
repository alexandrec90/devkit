#!/usr/bin/env python3
"""Keep the installed global git policy on the release the upgrade run adopts.

`install-git-policy.py` copies the branch-policy hooks into `~/.devkit/git-hooks`, and
the hooks run *that copy*. It is pinned to a release tag on purpose, so it goes stale
on exactly one event -- a new tag -- and the fix is a command somebody has to remember
to run. The session-start status line reports the drift, and a banner is the kind of
reminder that is read the third time.

The nightly `upgrade-project.py --all --yes` already reacts to a new tag by adopting it
in every consumer, so it is the natural owner: this is the rider it runs, once per
pass, after the release is known. Two guards keep it safe unattended:

- **Only a stale install is touched.** The installer's `--check` answers 0 (current),
  1 (drifted or behind) or 2 (nothing installed here, or a hooks path this does not
  own). Only 1 leads to an install; 2 is left alone, said out loud, because claiming a
  machine that never opted in is worse than a stale one that did.
- **Only from the tag, never from the working tree.** `--ref <tag>` names the release
  the run adopts, so consumers and the runtime move together, and the escape hatch
  that once put uncommitted policy on a machine for two days is never spelled here.

A failed install is carried out as a run-level outcome, so it reaches
`logs/upgrade.log` and the exit code; a scheduled job under `pythonw` has no other way
to say it. Stdlib only. Tested in `tests/test_policy_runtime.py`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_policy
import sweep

INSTALLER = Path("scripts") / "install-git-policy.py"

# `install-git-policy.py --check`'s contract, spelled here so a reader of `refresh`
# does not have to hold the other file's docstring in mind.
CHECK_CURRENT = 0
CHECK_STALE = 1
CHECK_ABSENT = 2

# The pseudo-name a failed reinstall carries in `logs/upgrade.log`. Parenthesised like
# `upgrade-project.py`'s `(run)` and `(release)`, which is how its artifact tells a
# run-level outcome from a checkout it could offer to retry by name.
POLICY_SCOPED = "(policy)"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
T = TypeVar("T")


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Spawn without a console window: this runs under the scheduled `pythonw` job."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        creationflags=sweep.NO_WINDOW,
    )


def check_argv(devkit: Path) -> list[str]:
    """`--check`, through the installer in the devkit checkout the run pulls from.

    Spawned with `console_python()`, not `sys.executable`: under the scheduled job that
    is `pythonw.exe`, and a console-less child gets a fresh visible console for every
    `git` the installer runs. A console interpreter spawned with `NO_WINDOW` gets a
    hidden one its descendants inherit -- `git_policy.console_python` has the account.
    """
    return [git_policy.console_python(), str(devkit / INSTALLER), "--check"]


def install_argv(devkit: Path, tag: str) -> list[str]:
    """`--yes --ref <tag>`: the release, by name, and never `--from-worktree`."""
    return [git_policy.console_python(), str(devkit / INSTALLER), "--yes", "--ref", tag]


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    """The installer's own last word, for a message that names the cause."""
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text.splitlines()[-1] if text else f"exit {result.returncode}"


def refresh(
    devkit: Path,
    tag: str,
    dry_run: bool,
    every: bool,
    outcome: Callable[[str, int, str], T],
    runner: Runner = run_command,
) -> list[T]:
    """Reinstall the policy runtime from `tag` when, and only when, it is stale.

    Returns the run-level outcomes to record: empty on every path but a failed install.
    `outcome` builds one in the caller's own type, because that type lives in a script
    this module cannot import by name. `every` is the unattended `--all` pass; a run
    naming one project is about that project, not about this machine.
    """
    if not every:
        return []
    check = runner(check_argv(devkit))
    if check.returncode == CHECK_CURRENT:
        print("upgrade: the global git policy runtime is current.")
        return []
    if check.returncode != CHECK_STALE:
        print(f"upgrade: global git policy runtime left alone -- {last_line(check)}")
        return []
    if dry_run:
        print(f"upgrade: would reinstall the global git policy runtime from {tag}.")
        return []
    result = runner(install_argv(devkit, tag))
    if result.returncode:
        return [
            outcome(
                POLICY_SCOPED,
                2,
                f"upgrade: reinstalling the global git policy runtime from {tag} failed: "
                f"{last_line(result)}. By hand: python scripts/install-git-policy.py --yes",
            )
        ]
    print(f"upgrade: global git policy runtime reinstalled from {tag}.")
    return []
