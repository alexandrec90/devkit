#!/usr/bin/env python3
"""The command-line verbs every `scripts/install-*.py` shares, in one implementation.

`devkit_schtasks.py` builds and registers task *documents*; this is the CLI tier above it
-- what `--status` and `--uninstall` mean, and what an installer's `main` prints for them.
The two are separate modules because they answer different questions, and because putting
these here kept `devkit_schtasks` inside the structural limits it was already at.

**Why this exists at all.** `--uninstall` was implemented five times and missing eight
times, and the five copies had drifted: an already-absent task read as a *failure* in four
of them, so one tick of the workspace's uninstall verb would report failures for jobs that
were simply already gone. `install-global-tools.py` had drifted further still -- its
`--uninstall` shared the mutually-exclusive group with `--yes`, so the dry run could not be
spelled and the bare verb deleted a live scheduled task with no confirmation. All thirteen
installers now route through `answer` here, and `tests/test_installer_contract.py` holds
them to it.

Two decisions worth keeping:

- **Absence is success.** An uninstall's goal is a state, not a change, so a task that was
  never registered reads as removed. That is what makes the verb safe to tick twice, and
  it is why `remove` establishes absence by *querying* rather than by matching the delete's
  error text -- `schtasks` localises its messages, and a string match would report every
  removal as failed on a non-English machine.
- **`answer` prints and returns**, so an installer's `main` spends four lines on the whole
  branch. It used to be fourteen inline, which pushed eight `main` functions past
  `structure_check`'s `function_lines` limit at once -- the gate was right, and this is the
  extraction it asked for rather than a raised ceiling.

Stdlib only, like everything an installer imports. Tested in `tests/test_installer_cli.py`.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_schtasks

Runner = devkit_schtasks.Runner


def uninstall_argv(name: str) -> list[str]:
    """The delete `remove` makes.

    `/F` because there is nobody to answer the prompt: every caller is a scheduled pass
    or a workspace task whose console closes on exit, and a `schtasks` waiting on a
    keystroke is indistinguishable from one that hung.
    """
    return ["schtasks", "/Delete", "/TN", name, "/F"]


def query_argv(name: str) -> list[str]:
    """The human-readable query `--status` prints, as distinct from
    `devkit_schtasks.query_xml_argv`'s machine-readable one: this one is for a person
    reading a terminal, so the locale's own wording is a feature here and a hazard there.
    """
    return ["schtasks", "/Query", "/TN", name]


def remove(name: str, run: Runner) -> tuple[bool, str]:
    """Delete the task. `(ok, message)`, and **a task that is not there counts as
    removed** -- see the module docstring for why that is the whole point.
    """
    if run(query_argv(name)).returncode != 0:
        return True, f"{name} was not registered"
    result = run(uninstall_argv(name))
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "schtasks failed").strip()
    return True, (result.stdout or f"removed {name}").strip()


def query_or_remove(
    name: str, *, status: bool, uninstall: bool, apply: bool, run: Runner
) -> tuple[int, str] | None:
    """`(exit code, what to print)`, or None when neither verb was asked for -- which is
    the caller's signal to carry on into `--check`/`--yes`.

    Dry by default like every installer here: without `apply` an `--uninstall` prints the
    command it would run and changes nothing.
    """
    if status:
        result = run(query_argv(name))
        text = (result.stdout or result.stderr or "").strip()
        return (0 if result.returncode == 0 else 1), (text or f"no scheduled task called {name}")
    if uninstall:
        if not apply:
            command = " ".join(uninstall_argv(name))
            return 0, f"Would run: {command}\n\nDry run -- re-run with --yes."
        ok, message = remove(name, run)
        return (0 if ok else 1), message
    return None


def answer(
    name: str,
    *,
    status: bool,
    uninstall: bool,
    apply: bool,
    run: Runner,
    windows: bool,
) -> int | None:
    """The whole `--status`/`--uninstall` branch of an installer's `main`.

    Prints and returns the exit code, or None when neither verb was asked for. Called
    *before* an installer's other checks on purpose: removing a task must not require the
    runner it points at to still exist, which is exactly the state a half-uninstalled or
    moved checkout is in.
    """
    if not (status or uninstall):
        return None
    if not windows:
        print(f"{name}: Windows-only; nothing to do here.")
        return 0
    handled = query_or_remove(name, status=status, uninstall=uninstall, apply=apply, run=run)
    if handled is None:
        return None
    code, message = handled
    print(message, file=sys.stderr if code else sys.stdout)
    return code


# --- which installers a `--only` names ----------------------------------------
#
# The CLI tier too: `--only` is a command-line concept, and `installers.py` is the pass
# that acts on the answer rather than the place that decides what a tick means.

# `install-installers-schedule.py` comes off the machine **first**, and the ordering is
# load-bearing rather than tidy. It registers `devkit-installers`, whose `maintain` pass
# re-registers anything it finds missing -- so an uninstall that took it last, or left it
# for a run that died halfway, would be quietly undone at the next logon and the operator
# would have no way to tell that from an uninstall that never worked. Removing the
# maintainer first makes a partial uninstall *stay* partial, which is the recoverable
# failure: everything still there, and re-runnable.
MAINTAINER = "install-installers-schedule.py"


def short_name(script: Path) -> str:
    """`install-reap-schedule.py` -> `reap-schedule`: what a person ticks in a checklist.

    The file name is the stable identifier -- `TASK_NAME` is not, because two installers
    register no job at all -- but nobody wants `install-` and `.py` in a picker.
    """
    return script.name.removeprefix("install-").removesuffix(".py")


def parse_only(value: str) -> list[str]:
    """A comma-separated `--only` into names, tolerating spaces and empty entries.

    The workspace's checklist joins its ticks with a comma, and a `multiPick` with
    nothing ticked sends the empty string -- which has to mean *all*, never *none*: a
    picker the operator dismissed must not silently become a no-op run they read as
    "there was nothing to do".
    """
    return [part.strip() for part in value.split(",") if part.strip()]


def select(scripts: Sequence[Path], only: Sequence[str]) -> tuple[list[Path], list[str]]:
    """`(the chosen installers, the names that matched nothing)`. Empty `only` is all.

    An unmatched name is *returned* rather than ignored, because a checklist entry that
    silently matches nothing is the failure mode this whole verb exists to avoid: the
    operator ticks five boxes, four run, and the report reads as a complete success.
    """
    if not only:
        return list(scripts), []
    wanted = {name.removeprefix("install-").removesuffix(".py") for name in only}
    chosen = [script for script in scripts if short_name(script) in wanted]
    missing = sorted(wanted - {short_name(script) for script in chosen})
    return chosen, missing


def uninstall_order(scripts: Sequence[Path]) -> list[Path]:
    """`scripts` with the maintainer first -- see `MAINTAINER` for why that matters."""
    return sorted(scripts, key=lambda script: (script.name != MAINTAINER, script.name))
