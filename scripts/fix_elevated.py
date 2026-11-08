#!/usr/bin/env python3
"""What the fix pass does about the elevated worker: says what is true, and prompts for nothing.

**The harness does not ask for an install or an upgrade.** The installer and the
interpreter it copies are read from folders the user can write, so running it trusts the
machine at that moment. An unreviewed merge plus a prompted admin paste is the dispatcher
this design refused. So a copy behind the checkout files nothing at all, and `--check` is
where that is read: a worker that is installed keeps working.

**One person's step, and only while it is true:** a real request waiting on a machine with
no worker (`elevated_request.read_waiting`), which is a need rather than a state. It names
the machine and the exact command, and goes when the request ages out or a worker appears.

Two more are filed for a fixer rather than a person, since neither wants an admin paste:
- a run the worker reports `failed` or `refused`: it cannot write the ledger, and its
  exit code does not outlast the next fire, so `elevated_request.results` is how those
  reach anyone;
- a task or folder the user can reach (`EXPOSED`), which is a defect however it happened.

**One row per machine, and a machine retires only its own.** `host=` is deliberately not
part of a ledger signature, so the machine's name goes in the detail. The ledger can be
pooled, so a row this pass did not cause is about another PC, which only that PC can check.

Tested in `tests/test_fix_elevated.py`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import elevated_request
import fix_findings
import fix_loop

KIND = "elevated-worker"


def step(host: str, checkout: Path) -> str:
    """The person's step, naming the machine: it is all the tray row shows, and the
    operator has more than one PC."""
    return (
        f"on {host}, from an elevated terminal, run "
        f"`{elevated_request.install_command(checkout)}`: one administrator action per PC, "
        "which no job can take for you"
    )


def waiting_detail(host: str, checkout: Path, verb: str) -> str:
    """The finding's line, stable across passes: no count, no date."""
    return (
        f"{verb} was requested on {host}, which has no elevated worker "
        f"-- {fix_findings.triage.PERSON_STEP} {step(host, checkout)}"
    )


def owed(
    host: str,
    checkout: Path,
    answer: tuple[str, str],
    waiting: list[str],
    outcomes: dict[str, tuple[str, str]],
) -> list[str]:
    """Every detail true of this machine now."""
    state, why = answer
    details = [waiting_detail(host, checkout, verb) for verb in waiting]
    if state == elevated_request.EXPOSED:
        details.append(f"the elevated worker on {host} is exposed: {why}")
    details += [
        f"{subject} on {host} {result}: {detail}"
        for subject, (result, detail) in sorted(outcomes.items())
        if result in (elevated_request.FAILED, elevated_request.REFUSED)
    ]
    return details


def check(
    ctx: fix_loop.Context,
    journal: fix_findings.Journal,
    ask: Callable[[Path], tuple[str, str]] = elevated_request.state,
    outcomes: Callable[[], dict[str, tuple[str, str]]] = elevated_request.results,
) -> list[str]:
    """File what is true of this PC, retire what no longer is: lines for the record. Off
    Windows there is no worker, so nothing happens at all."""
    if not elevated_request.WINDOWS:
        return []
    machine = fix_findings.triage.harness_events.host_name()
    answer = ask(ctx.devkit_dir)
    if answer[0] == elevated_request.UNKNOWN:
        return []
    missing = answer[0] == elevated_request.MISSING
    waiting = elevated_request.read_waiting(ctx.devkit_dir, ctx.now.timestamp()) if missing else []
    if not missing and ctx.writes:
        elevated_request.clear_waiting(ctx.devkit_dir)
    details = owed(machine, ctx.devkit_dir, answer, waiting, outcomes())
    journal.add(*(fix_findings.Finding(KIND, fix_loop.fix_cycle.DEVKIT, line) for line in details))
    filed = [f"{machine} -- filed: {line.split(' -- ')[0]}" for line in details]
    return filed + retire(ctx, machine, {f"{KIND}: {line}" for line in details})


def retire(ctx: fix_loop.Context, machine: str, keep: set[str]) -> list[str]:
    """Resolve this machine's open rows other than `keep`, the ones true now."""
    marker = f"{KIND}: "
    triage = fix_findings.triage
    stale = [
        item
        for item in triage.open_items(triage.load(ctx.devkit_dir), (fix_findings.FINDING,))
        if item.fields.get("detail", "").startswith(marker)
        and item.host == machine
        and item.fields["detail"] not in keep
    ]
    note = "no longer true of this PC, as of this pass"
    if stale and ctx.writes:
        triage.resolve([item.id for item in stale], note, root=ctx.devkit_dir)
    verb = "retired" if ctx.writes else "would retire"
    return [f"{verb} [{item.id}] -- {note}" for item in stale]
