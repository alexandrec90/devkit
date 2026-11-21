#!/usr/bin/env python3
"""The ship task: commit, push and open the PR for what sessions finished, and nothing else.

Runs every two minutes on every PC (`install-ship-schedule.py`, task `devkit-ship`), and
sends no one. Which intents it takes:

- **Every one in a direct-flow project** (`delivery.py`), on any machine. Nothing stands
  between a finished session and `main`, so the half hour the fix pass fires on is too
  long, and the fix pass leaves these alone.
- **A gated one too when `devkit.fixPass` here is exactly `off`**, through the gated path
  with its fixers and its labels. Shipping and sending fixers were one switch, so a second
  PC set to `off` held four intents nobody would ever ship. **So `off` now means "this PC
  sends no fixers", not "nothing runs": `harness-switch.py` is the stop.** `plan` is a
  dry run, and ships nothing gated; `dispatch` leaves gated intents to the fix pass.

It holds exactly where the pass holds (`ship_step.ship_each`), through the same code. A
refused intent is not tried again until its words or its tree move
(`ship_intent.stored_refusal`): on a dispatching machine the fix pass turns the refusal
into a failure and sends a fixer, and on any other nothing can, so it is filed.

**One shipper at a time** (`ship_step.SHIP_LOCK_NAME`): not the fix pass's own lock, which
a pass holds for up to 25 minutes, but one the pass takes only around its ship step. A
fire holds it, and exits at once if it cannot have it: the next fire is two minutes away.

**A fire that finds nothing leaves the record alone**: it costs one `git worktree list`
per project, and one every two minutes would otherwise erase what the last ship said
before anyone read it. `logs/ship-pass.log` is the record, written when something
shipped, held or failed; what cannot be shipped goes on the ledger, and a non-zero exit
is kept for this script itself breaking, since `log-wrap.py` would file those again.

Tested in `tests/test_ship_pass.py`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import fix_cycle
import git_trust
import ship_intent
import ship_step
import worktree

REPO_ROOT = Path(__file__).resolve().parents[1]

ARTIFACT = Path("logs") / "ship-pass.log"
# No ship starts this far into a fire: the rest wait two minutes for the next one, and
# each fire stays near its interval, so the scheduler is rarely told a run is still going.
START_BUDGET_SECONDS = 100.0
# The ship under way is ended here and held: the same bound the pass sets, inside the
# task's own limit.
WINDOW_SECONDS = 10 * 60.0

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

fix_findings = ship_step.fix_loop.fix_findings
triage = ship_step.fix_loop.triage


def takes(intent: ship_intent.Intent, mode: str) -> bool:
    """Whether this task ships `intent` on a machine whose fix pass is in `mode`."""
    return intent.direct or mode == fix_cycle.OFF


def write_artifact(text: str) -> Path:
    path = REPO_ROOT / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def set_refused_aside(
    intents: list[ship_intent.Intent], mode: str, journal: ship_step.Journal
) -> tuple[list[ship_intent.Intent], list[str]]:
    """`(the intents to ship, a line for each stored refusal)`. Filed where the fix pass
    will not read it: with no fixer coming, the ledger is what is left."""
    ship: list[ship_intent.Intent] = []
    lines: list[str] = []
    for intent in intents:
        outcome = ship_intent.stored_refusal(intent)
        if outcome is None:
            ship.append(intent)
            continue
        where = f"{intent.project} {intent.branch}"
        lines.append(ship_step.shipped_line(outcome, where))
        if mode != fix_cycle.DISPATCH:
            file_refusal(intent, journal)
    return ship, lines


def file_refusal(intent: ship_intent.Intent, journal: ship_step.Journal) -> None:
    """The commit stage's refusal of `intent`, as a finding, stable across recurrences."""
    state = ship_intent.read_state(intent.tree)
    why = ship_intent.refusal_reason(str(state.get("output", "")))
    detail = f"{intent.project} {intent.branch}: {state.get('step', 'commit')}: {why}"
    fix_findings.file(journal, "ship-refused", intent.project, detail, str(intent.tree))


def ship(root: Path, intents: list[ship_intent.Intent], mode: str) -> tuple[list[str], bool]:
    """Ship `intents`: `(lines for the record, whether a step of this run crashed)`."""
    devkit_dir = root / fix_cycle.DEVKIT
    deadline = time.monotonic() + WINDOW_SECONDS
    journal = ship_step.Journal(devkit_dir, left=lambda: deadline - time.monotonic())
    todo, lines = set_refused_aside(intents, mode, journal)
    # `DISPATCH` whatever the switch says: `mode` is the fix pass's, and this task's
    # whole job is the shipping that switch does not reach.
    shipped, refused, _failed = journal.step(
        "ship",
        ship_step.ship_each,
        todo,
        root,
        fix_cycle.DISPATCH,
        journal,
        START_BUDGET_SECONDS,
        default=([], [], False),
    )
    if mode != fix_cycle.DISPATCH:
        taken = {failure.tree for failure in refused}
        for intent in todo:
            if str(intent.tree) in taken:
                file_refusal(intent, journal)
    fix_findings.record_all(journal.findings, triage.load(devkit_dir), devkit_dir)
    return lines + shipped, bool(journal.crashed)


def run(workspace: Path) -> int:
    """One fire. Exits quietly when another fire holds the lock or there is nothing to do."""
    root = workspace.parent
    text = workspace.read_text(encoding="utf-8")
    mode = fix_cycle.mode_from_workspace(text)
    projects = devkit_project.known_projects(text)
    lock = ship_step.SHIP_LOCK_NAME
    with worktree.named_lock(root, lock, 0.0, ship_step.SHIP_LOCK_STALE) as held:
        if not held:
            return EXIT_OK
        intents = [i for i in ship_intent.find_intents(root, projects) if takes(i, mode)]
        if not intents:
            # Only where nothing has: a fire with nothing to ship erases no record.
            if not (REPO_ROOT / ARTIFACT).is_file():
                write_artifact("ship-pass: nothing to ship")
            return EXIT_OK
        lines, crashed = ship(root, intents, mode)
    report = "\n".join([f"ship-pass: fixPass={mode}", *lines])
    print(report)
    write_artifact(report)
    return EXIT_FAILED if crashed else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, default=worktree.DEFAULT_WORKSPACE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    workspace = args.workspace.resolve()
    if not workspace.is_file():
        write_artifact(f"ship-pass: FAILED -- no workspace file at {workspace}")
        print(f"ship-pass: no workspace file at {workspace}", file=sys.stderr)
        return EXIT_USAGE
    # In this process only, before any git call: a tree an elevated session made is one
    # git otherwise refuses. Writing git's global config stays the dispatching pass's.
    git_trust.adopt(workspace.parent, write=False)
    try:
        return run(workspace)
    except Exception as exc:
        write_artifact(f"ship-pass: CRASHED -- {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    sys.exit(main())
