#!/usr/bin/env python3
"""A PC whose elevated worker is missing or stale is a person's step, while it is.

Installing or upgrading `devkit-elevated` takes an administrator, once per PC, and no job
holds one, so no fixer can take the step. The finding names it after
`harness_triage.PERSON_STEP` and awaits them, the shape `fix_visibility.py` set: the morning
toast carries the exact command, and a later pass that finds it true no longer retires it.

**One row per machine, and a machine retires only its own.** `host=` is deliberately not
part of a ledger signature, so the machine's name goes in the detail: without it two PCs
owing this would be one group and one line. The ledger can be pooled, so a row this pass
did not cause is a statement about another PC, which only that PC's pass can check.

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


def detail(host: str, checkout: Path, state: str, why: str) -> str:
    """The finding's line, stable across passes: no count, no date."""
    head = (
        f"the elevated worker is not installed on {host}"
        if state == elevated_request.MISSING
        else f"the elevated worker on {host} is outdated: {why}"
    )
    return f"{head} -- {fix_findings.triage.PERSON_STEP} {step(host, checkout)}"


def check(
    ctx: fix_loop.Context,
    journal: fix_findings.Journal,
    ask: Callable[[Path], tuple[str, str]] = elevated_request.state,
) -> list[str]:
    """File this PC's step while it is owed, retire it once it is not: lines for the
    record. Off Windows there is no worker to be missing, so nothing happens at all."""
    if not elevated_request.WINDOWS:
        return []
    machine = fix_findings.triage.harness_events.host_name()
    state, why = ask(ctx.devkit_dir)
    if state == elevated_request.UNKNOWN:
        return []
    owed = state in (elevated_request.MISSING, elevated_request.OUTDATED)
    current = detail(machine, ctx.devkit_dir, state, why) if owed else ""
    lines = []
    if owed:
        journal.add(fix_findings.Finding(KIND, fix_loop.fix_cycle.DEVKIT, current))
        lines.append(f"{machine} -- elevated worker {state}: filed for its owner")
    return lines + retire(ctx, machine, f"{KIND}: {current}" if owed else "")


def retire(ctx: fix_loop.Context, machine: str, keep: str) -> list[str]:
    """Resolve this machine's open rows other than `keep`, the wording owed now."""
    marker = f"{KIND}: "
    triage = fix_findings.triage
    stale = [
        item
        for item in triage.open_items(triage.load(ctx.devkit_dir), (fix_findings.FINDING,))
        if item.fields.get("detail", "").startswith(marker)
        and item.host == machine
        and item.fields["detail"] != keep
    ]
    note = "the elevated worker on this PC is as this checkout ships it, as of this pass"
    if stale and ctx.writes:
        triage.resolve([item.id for item in stale], note, root=ctx.devkit_dir)
    verb = "retired" if ctx.writes else "would retire"
    return [f"{verb} [{item.id}] -- {note}" for item in stale]
