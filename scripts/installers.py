#!/usr/bin/env python3
"""Keep every devkit installer's work current on this machine, with nobody remembering to.

Each `scripts/install-*.py` registers one thing -- a scheduled job, or the global git
policy -- and until this pass existed the only thing that ran any of them was a person
reading a README. That is the failure the jobs were written to prevent, one tier up: a
job an installer never registered is invisible to `schedule_health.py`, which reads the
scheduler and so cannot report what was never there, and a job whose installer gained a
flag keeps running the old command line until somebody re-runs it by hand. On this
workstation two jobs had never been registered, and nothing anywhere could say so.

So this asks every installer the one question they all answer -- `--check`, exit 0
current, 1 needs (re)installing, 2 left alone -- and in `maintain` mode runs `--yes` on
each one that answered 1. It is itself a scheduled job (`install-installers-schedule.py`
registers it, daily and at logon), which makes that installer the one a machine ever
needs run by hand, and only once.

**Which checkout.** Always the static one (`sweep.source_checkout`), whatever checkout
this file is run from: the registered command lines name the static checkout's scripts,
so that is the copy whose installers have to agree with the machine. Run from a
worktree, this spawns the static checkout's installers, not the worktree's.

**What a re-install can change, and the option it would lose.** `--yes` registers what
the installer builds *today*, which is how a gained flag is applied and a moved checkout
is followed. It is also why an option chosen at install time -- `--merge` on the
reconcile pass -- is remembered by nothing but the registered task, and a re-register
from defaults would silently drop it. `devkit.installers` in the workspace file's
`settings` is where such options live: `{"install-reconcile-task.py": ["--merge"]}` is
passed to that installer's `--check` and its `--yes` alike, so what is compared is what
is registered.

**Disabling is the installers' business, never this pass's.** A job the operator stood
down (`harness-switch.py --off --job <name>`) is registered *disabled* by its own
installer, which is what its `--check` expects to find; this pass skips nothing and
enables nothing. `install-git-policy.py` answers 2 for "nothing installed here" and is
left there on purpose: rewriting global git config on a machine that never opted in is a
decision, not maintenance.

Read-only by default: `status` runs every `--check` and writes the report; `maintain`
is what the scheduler runs. The artifact `logs/installers.log` is rewritten every pass,
on a clean one too, so its mtime says the job is alive. Stdlib only, and every decision
is an importable function tested in `tests/test_installers.py`.
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import devkit_schtasks
import harness_state
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Rewritten every pass; `install-installers-schedule.py` advertises the same path to
# `schedule_health`, and `tests/test_scheduled_jobs.py` checks the two agree.
ARTIFACT = Path("logs/installers.log")

# The workspace-file setting holding per-installer options, keyed by installer file name.
SETTING = "devkit.installers"

# Found, never listed: a new installer is in scope the moment the file exists, which is
# the property `tests/test_installer_contract.py` holds every installer to.
INSTALLER_GLOB = "scripts/install-*.py"

# Per installer. Generous because `install-git-policy.py --yes` reads a tag out of git
# and the docker installers may wait on `schtasks` behind a busy scheduler; still finite,
# so one wedged installer cannot hold the whole pass past its own time limit.
TIMEOUT_SECONDS = 300

CURRENT = "current"
STALE = "stale"
LEFT_ALONE = "left alone"
REINSTALLED = "reinstalled"
FAILED = "failed"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Spawn an installer windowless: this runs under the scheduled `pythonw` job.

    A failure to spawn at all is a returncode rather than a traceback, so `check` has
    one thing to inspect and the pass goes on to the next installer.
    """
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
            creationflags=sweep.NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 3, "", str(exc))


@dataclass(frozen=True)
class Outcome:
    """One installer's verdict, as the artifact and the exit code consume it."""

    installer: str
    verdict: str
    detail: str


def discover(root: Path) -> list[Path]:
    """Every installer under `root`, in name order so the report is stable."""
    return sorted(root.glob(INSTALLER_GLOB))


def task_name(script: Path) -> str:
    """The `TASK_NAME` an installer declares, read off its source without importing it.

    "" for an installer that registers no job (`install-git-policy.py`), and for a file
    that cannot be read: this feeds a report line about the ledger, not a decision.
    """
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ""
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        value = node.value
        if isinstance(target, ast.Name) and target.id == "TASK_NAME":
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return ""


def parse_options(text: str) -> dict[str, list[str]]:
    """`SETTING` out of a workspace file: installer file name -> the extra arguments.

    Lenient the way `reap-stale.parse_settings` is, for its reason: a scheduled task
    whose stdout goes nowhere must not crash on a hand-edited file. An entry that is not
    a list of strings is dropped rather than passed to an installer as something else.
    """
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    settings = payload.get("settings") if isinstance(payload, dict) else None
    raw = settings.get(SETTING) if isinstance(settings, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): list(arguments)
        for name, arguments in raw.items()
        if isinstance(arguments, list) and all(isinstance(item, str) for item in arguments)
    }


def read_options(workspace: Path | None) -> dict[str, list[str]]:
    """The options file on disk, or nothing: a machine with no workspace file has no
    options, which is the state every installer's defaults are for."""
    if workspace is None or not workspace.is_file():
        return {}
    return parse_options(workspace.read_text(encoding="utf-8"))


def installer_argv(python: str, script: Path, mode: str, options: Sequence[str]) -> list[str]:
    """`<python> <installer> --check|--yes <options>`.

    The options go on both spellings so the check compares the command line the repair
    would register -- an option on `--yes` alone would be re-applied every pass by a
    check that never expected it.
    """
    return [python, str(script), mode, *options]


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    """The installer's own last word, for a report line that names the cause."""
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text.splitlines()[-1] if text else f"exit {result.returncode}"


def usage_error(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether the installer rejected its own command line.

    `argparse` exits 2 -- the contract's "left alone" -- and the pass first ran against
    installers that did not know `--check` yet, reading every one of them as a verdict.
    The `usage:` it prints first is the tell, and an option in `SETTING` the installer
    does not take must read as a failure rather than as a job deliberately left alone.
    """
    return result.returncode == 2 and "usage:" in (result.stderr or "")


def check(script: Path, python: str, options: Sequence[str], runner: Runner) -> Outcome:
    """One installer's `--check`, classified by the contract in `devkit_schtasks`.

    Any other exit code -- a traceback -- is a failure of the installer itself, and so
    is a rejected command line (`usage_error`); both are reported as one rather than read
    as "stale", which would have `maintain` run `--yes` on a broken installer.
    """
    result = runner(installer_argv(python, script, "--check", options))
    verdicts = {
        devkit_schtasks.CHECK_CURRENT: CURRENT,
        devkit_schtasks.CHECK_STALE: STALE,
        devkit_schtasks.CHECK_LEFT_ALONE: LEFT_ALONE,
    }
    verdict = verdicts.get(result.returncode)
    if verdict is None or usage_error(result):
        return Outcome(
            script.name, FAILED, f"--check exited {result.returncode}: {last_line(result)}"
        )
    return Outcome(script.name, verdict, last_line(result))


def repair(script: Path, python: str, options: Sequence[str], runner: Runner) -> Outcome:
    """One installer's `--yes`. Idempotent by every installer's own contract (`/F`)."""
    result = runner(installer_argv(python, script, "--yes", options))
    if result.returncode:
        return Outcome(
            script.name, FAILED, f"--yes exited {result.returncode}: {last_line(result)}"
        )
    return Outcome(script.name, REINSTALLED, last_line(result))


def reconcile(
    root: Path,
    apply: bool,
    options: dict[str, list[str]],
    runner: Runner | None = None,
    python: str = "",
) -> list[Outcome]:
    """Every installer's verdict, repaired where `apply` says to and the verdict allows.

    `python` defaults to the console interpreter beside this one, not `sys.executable`:
    under the scheduled job that is `pythonw.exe`, and a console-less child gets a fresh
    visible console for every `schtasks` and `git` an installer runs. `sweep.console_python`
    has the account.

    `runner` is resolved here rather than as a default argument, so a test that replaces
    `run_command` on the module replaces what `main` actually spawns with.
    """
    interpreter = python or sweep.console_python()
    spawn = runner or run_command
    outcomes = []
    for script in discover(root):
        extra = options.get(script.name, [])
        outcome = check(script, interpreter, extra, spawn)
        if apply and outcome.verdict == STALE:
            outcome = repair(script, interpreter, extra, spawn)
        outcomes.append(outcome)
    return outcomes


def stood_down_without_installer(names: frozenset[str], root: Path) -> list[str]:
    """Ledger entries no installer here registers -- a job renamed or retired after it
    was stood down, which nothing else would ever mention again."""
    known = {task_name(script) for script in discover(root)}
    return sorted(name for name in names if name not in known)


def render(
    outcomes: Sequence[Outcome], orphans: Sequence[str], when: _dt.datetime, mode: str
) -> str:
    attention = sum(outcome.verdict in {STALE, FAILED} for outcome in outcomes)
    head = (
        f"# installers {when.isoformat(timespec='seconds')} [{mode}] -- {attention} need attention"
    )
    lines = [f"{o.installer}: {o.verdict} -- {o.detail}" for o in outcomes]
    lines += [
        f"stood down: {name} -- no installer here registers it; "
        f"`harness-switch.py --on --job {name}` forgets it"
        for name in orphans
    ]
    return "\n".join([head, *lines, ""])


def exit_code(outcomes: Sequence[Outcome]) -> int:
    """2 when an installer failed, 1 when one is still stale, else 0.

    `status` therefore exits 1 on a machine with a pending install, which makes it a
    check in its own right; `maintain` exits 1 only when a repair was refused.
    """
    if any(outcome.verdict == FAILED for outcome in outcomes):
        return 2
    if any(outcome.verdict == STALE for outcome in outcomes):
        return 1
    return 0


def write_artifact(text: str, root: Path) -> None:
    path = root / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=("status", "maintain"),
        help=(
            "status: run every installer's --check and report (default). maintain: what "
            "the scheduler runs -- also --yes on each one that needs it, named so a "
            "changed default cannot silently make the job a no-op."
        ),
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument(
        "--devkit",
        type=Path,
        default=REPO_ROOT,
        help=(
            "a devkit checkout; the static checkout it belongs to is what is reconciled "
            "and where the artifact is written (default: this one)"
        ),
    )
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = sweep.source_checkout(args.devkit.expanduser().resolve())
    workspace = args.workspace or sweep.default_workspace(root)
    options = read_options(Path(workspace) if workspace else None)
    outcomes = reconcile(root, args.mode == "maintain", options)
    orphans = stood_down_without_installer(harness_state.stood_down(), root)
    text = render(outcomes, orphans, _dt.datetime.now(), args.mode)
    write_artifact(text, root)
    print(text, end="")
    return exit_code(outcomes)


if __name__ == "__main__":
    sys.exit(main())
