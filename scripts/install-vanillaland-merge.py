#!/usr/bin/env python3
"""Install the daily `git-merge-default.py --checkout VanillaLand` run as a Windows task.

VanillaLand is the reference checkout: no harness, no `.devkit.toml`, an Azure DevOps
remote that `gh` cannot speak to, and a human's own long-lived branch checked out with
months of uncommitted work on top of it. Every other unattended job in this workspace
therefore steps over it -- `reconcile` does not sweep it, `upgrade-project.py` does not
see it -- and the one thing it *does* need, keeping the trunk merged in, was the one
thing nobody automated. `develop` moves daily; the branch here does not, and the cost
of that is paid all at once, weeks later, as a merge nobody can review.

`scripts/git-merge-default.py` is already the operation, already resolves against the
raw registry so a reference checkout is addressable, and is already wired as a VS Code
task. This adds the runner that does not need someone to click it.

Three things make the unattended run different from the clicked one, and each is a
constant below rather than a default inherited from the picker:

- **`--base develop` is pinned, not `auto`.** `auto` reads `refs/remotes/origin/HEAD`
  and repairs it when unset, which is right for a picker that must work in seven
  checkouts. Here the answer is known, and the failure modes are not symmetric: a pin
  that goes wrong merges nothing and exits 2 every morning, in the log and on the
  session-start line, while a detection that goes wrong merges `main` into someone's
  work. Pass `--base auto` to override.
- **It runs at 05:00**, after the 03:00 upgrade and the 04:00 prune. Three `pythonw`
  jobs starting together on a laptop that has just woken is how one of them times out,
  and this one is last because it is the only one that touches a working tree a human
  is going to open.
- **`log-wrap.py --always`**, because the interesting outcome here is the quiet one.
  A pass writes `Already up to date` and a conflict writes the file list, and an
  artifact that appears only on failure cannot be told apart from a job that stopped
  running.

**What a reader of that artifact must know, and the reason this docstring says it
rather than leaving it to the log.** The merge script sets uncommitted work aside in a
named stash when git refuses over it, and on a *conflict* it deliberately leaves it
there -- popping into half-merged files would bury the conflicts. So the outcome this
job can produce unattended is a checkout with a merge in progress and a working tree
that looks emptied. Nothing is lost, `git stash list` has it, and the artifact named
below spells out the recovery; but somebody sitting down to that at 09:00 without
having read this is the failure worth pre-empting. The next morning's run is harmless
in that state -- git refuses a second merge, the same unmerged paths are re-reported,
and nothing new is attempted.

Nothing is pushed, ever, by any path through this. `git-merge-default.py` has no push
in it, and VanillaLand's `pre-push` hook would refuse the ref anyway.

Everything else follows `install-docker-prune.py` deliberately: same argv builders, same
`--status` / `--uninstall` / dry-run-unless-`--yes` shape, same refusal to install from
an ephemeral box whose path will not exist next week -- with `install-upgrade-schedule`'s
`--devkit` as the way out of that last one, since the command this registers is entirely
scripts the static checkout already has.

Windows-only by nature. On any other platform it says so and exits 0.

The builders are pure and tested in `tests/test_install_vanillaland_merge.py`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_schtasks
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

TASK_NAME = "devkit-vanillaland-merge"

# The checkout name as the workspace registry spells it -- `git-merge-default.py`
# resolves `--checkout` against that file, so this has to match it exactly rather than
# being a path.
CHECKOUT = "VanillaLand"

# See the module docstring: pinned rather than detected, because the two ways this can be
# wrong cost very different amounts.
DEFAULT_BASE = "develop"

# After the 03:00 upgrade and the 04:00 prune.
DEFAULT_AT = "05:00"

# The wrapper's title, and the artifact path it therefore writes. Kept as a pair here
# because the second is derived from the first by `log_wrap.slug` -- a test asserts they
# still agree rather than trusting this comment.
#
# Deliberately distinct from the label the VS Code task uses (`Git: Merge Origin Default
# into Current Branch`). Sharing it would have a click on any checkout overwrite the
# scheduled job's only record of last night, and a reader comparing the two would have no
# way to tell which run wrote the file.
LABEL = "Scheduled: VanillaLand Merge Develop"
ARTIFACT = "logs/scheduled-vanillaland-merge-develop.log"

# Same guard as the sibling installers, for the same reason: `pathlib` reads `os.name` at
# call time, so a test that patches it breaks every later `Path(...)`.
WINDOWS = os.name == "nt"


def merge_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "git-merge-default.py"


def wrapper_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "log-wrap.py"


def interpreter(root: Path = REPO_ROOT) -> str:
    """The interpreter the scheduled task should run, belonging to `root`.

    `sys.executable` is the wrong answer for the one caller that matters. `--devkit`
    exists so an agent working in a box can register the job against the static
    checkout, and it moved every *script* path across while leaving the interpreter
    pointing at the box's own `.venv` -- which `reconcile` deletes the moment the PR
    merges. The escape hatch registered exactly the failure it was the escape from, and
    silently: the task runs fine until the box is reaped.

    Falls back to `sys.executable` when `root` has no virtualenv -- the ordinary case for
    a checkout whose tools are on PATH -- except when the running one is itself a box's.
    There, `sys._base_executable` is the interpreter that *created* this virtualenv, and
    it is the only one in reach that outlives every box.
    """
    venv = (
        root / ".venv" / ("Scripts" if WINDOWS else "bin") / ("python.exe" if WINDOWS else "python")
    )
    if venv.is_file():
        return str(venv)
    if sweep.BOXES_DIR_NAME in Path(sys.executable).parts:
        return getattr(sys, "_base_executable", "") or sys.executable
    return sys.executable


# The interpreter for the task's own `<Command>`, so the nightly run opens no console --
# and therefore has no stdout, which is why this job must write an artifact.
# `devkit_schtasks.windowless` owns the resolution. This file used to carry a private copy
# of it, on the stated reasoning that importing one installer from another to borrow six
# lines couples their lifecycles; six identical copies were then wrong in the same way for
# as long as it took one job's interpreter to come from a uv `.venv`.
windowless = devkit_schtasks.windowless


def console(python: str) -> str:
    """`python.exe` beside `pythonw.exe`, for the command the wrapper actually runs.

    The inverse of `windowless`, and the two are not interchangeable halves of a
    preference: the task's own `<Command>` must be windowless, and the interpreter
    *inside* the wrapped argv must not be. `log-wrap.py` spawns it with
    `CREATE_NO_WINDOW`, which Windows **ignores for a GUI-subsystem child** -- so a
    `pythonw.exe` there is left with no console at all, and every process it goes on to
    spawn is handed a fresh visible one. A console child with the flag gets a hidden
    console instead, and passes it down.

    Falls back to the given interpreter when there is no `python.exe` beside it, and is
    the identity for the console interpreter a human installs from.
    """
    if os.path.basename(python).lower() != "pythonw.exe":
        return python
    candidate = os.path.join(os.path.dirname(python), "python.exe")
    return candidate if os.path.isfile(candidate) else python


def merge_arguments(
    python: str,
    root: Path = REPO_ROOT,
    base: str = DEFAULT_BASE,
    checkout: str = CHECKOUT,
) -> str:
    """The arguments the scheduled task runs, as one string -- interpreter excluded.

    Nested `log-wrap.py --always <label> -- <python> git-merge-default.py ...`, the same
    nesting a dispatched VS Code task gets from `devkit_project.plan_command`. The inner
    interpreter is the **console** one (`console`, not `windowless`): the wrapper spawns
    it with `CREATE_NO_WINDOW`, which Windows ignores for a GUI-subsystem child, so a
    `pythonw.exe` here would be console-less and give each `git` below it a window.

    `--workspace` is passed explicitly even though the script resolves the same path from
    its own location. A scheduled task's registered command line is the only record of
    what it operates on, and a reader of `schtasks /Query` should not have to know a
    default to know the blast radius -- both sibling jobs pass theirs for that reason.

    Every path is quoted: this workspace lives under a user profile, and profile names
    contain spaces on most machines that are not this one.
    """
    return " ".join(
        [
            f'"{wrapper_script(root)}"',
            "--always",
            f'"{LABEL}"',
            "--",
            f'"{console(python)}"',
            f'"{merge_script(root)}"',
            "--checkout",
            checkout,
            "--base",
            base,
            "--workspace",
            f'"{sweep.default_workspace(root)}"',
        ]
    )


def task_document(python: str, arguments: str, at: str, root: Path = REPO_ROOT) -> str:
    """The task XML registering (or replacing) the daily merge.

    `working_dir` is the whole reason this is not a one-liner: `log-wrap.py` resolves
    `logs/` from the cwd, and a scheduled task's cwd is `system32`. The cwd is *devkit*,
    not VanillaLand -- the merge script takes its target by name, and the artifact belongs
    beside every other devkit job's, in the checkout `schedule_health.ARTIFACTS` resolves
    against.
    """
    return devkit_schtasks.task_xml(
        python,
        arguments,
        devkit_schtasks.daily_trigger(at),
        working_dir=str(root),
    )


def uninstall_argv(name: str) -> list[str]:
    return ["schtasks", "/delete", "/tn", name, "/f"]


def query_argv(name: str) -> list[str]:
    return ["schtasks", "/query", "/tn", name]


def _run_argv(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`devkit_schtasks.Runner` shape: a spawn failure is a returncode, not a traceback."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def _run(argv: list[str]) -> tuple[int, str]:
    done = _run_argv(argv)
    return done.returncode, (done.stdout or done.stderr or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", default=True)
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--name", default=TASK_NAME)
    parser.add_argument("--at", default=DEFAULT_AT, help="daily start time, HH:MM (24-hour)")
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"branch to merge FROM (default {DEFAULT_BASE!r}); 'auto' reads it off the remote",
    )
    parser.add_argument(
        "--checkout",
        default=CHECKOUT,
        help=f"registry name of the checkout to merge into (default {CHECKOUT!r})",
    )
    parser.add_argument(
        "--devkit",
        type=Path,
        default=REPO_ROOT,
        help=(
            "the devkit checkout the task should run from (default: this one). Name the "
            "*static* checkout when installing from an ephemeral box -- a task pointing "
            "into .worktrees/ dies the moment reconcile reaps it"
        ),
    )
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually call schtasks")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = args.devkit.expanduser().resolve()

    if not WINDOWS:
        print("install-vanillaland-merge: Windows-only; nothing to do here.")
        return 0

    if args.status:
        code, out = _run(query_argv(args.name))
        print(out or f"no scheduled task called {args.name}")
        return 0 if code == 0 else 1

    if args.uninstall:
        target = uninstall_argv(args.name)
        if not args.apply:
            print(f"Would run: {' '.join(target)}\n\nDry run -- re-run with --yes.")
            return 0
        code, out = _run(target)
        print(out or f"removed {args.name}")
        return code

    # Both refusals are scoped to `--yes`, and both are ordered ahead of the register
    # call rather than folded into it. The dry run is a *read* of what would be
    # registered and has to keep working from wherever an agent is standing -- a box,
    # usually -- so only the act of writing a task is refused.
    if args.apply and sweep.BOXES_DIR_NAME in root.parts:
        # The registered command carries the checkout's path verbatim, and a box is
        # destroyed by `reconcile` -- so this would install a task that works until the
        # next reconcile pass and then fails nightly, forever, in silence.
        #
        # `--devkit` is the way out rather than a warning: an agent working in a box can
        # register the job against the *static* checkout, whose `git-merge-default.py`
        # and `log-wrap.py` are the ones the task actually runs. Checked before the file
        # test below because a real box *has* the script -- naming the box is the more
        # useful of the two diagnoses whenever both apply.
        print(
            f"install-vanillaland-merge: {root} is an ephemeral box, which reconcile "
            f"destroys. Point --devkit at the static checkout, which outlives the boxes.",
            file=sys.stderr,
        )
        return 2

    if args.apply and not merge_script(root).is_file():
        print(
            f"install-vanillaland-merge: no merge script at {merge_script(root)}", file=sys.stderr
        )
        return 2

    python = windowless(interpreter(root))
    arguments = merge_arguments(python, root=root, base=args.base, checkout=args.checkout)
    if not args.apply:
        print(
            f'Would run: "{python}" {arguments}\n\n'
            f"  daily at   {args.at}\n"
            f"  merges     origin/{args.base} into whatever branch {args.checkout} is on\n"
            f"  in         {root} (the cwd; the merge happens in {args.checkout})\n"
            f"  records    {ARTIFACT} (every run, pass or fail)\n"
            f"  commits    locally, and NEVER pushes\n"
            f"  on conflict leaves the merge IN PROGRESS with the files named in the log,\n"
            f"             and any uncommitted work it set aside still in the stash\n"
            f"  on battery runs anyway, and catches up a fire it slept through\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    ok, out = devkit_schtasks.register(
        args.name, task_document(python, arguments, args.at, root=root), _run_argv
    )
    print(out or f"installed {args.name} (daily at {args.at})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
