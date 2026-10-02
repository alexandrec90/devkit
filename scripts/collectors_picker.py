#!/usr/bin/env python3
"""The quick-pick the *Machine: Ingestion Collectors* task draws.

One live list, one pick: every verb `collectors.py` takes, once for all collectors and
once per collector, each row saying what this PC is set to now. Live because the set is
`devkit.collectors` in the workspace file and the state is this machine's assignment --
a templated list would be a second copy of the first and could not show the second.

A row's value is the verb, or `<verb>:<project>` for one collector: the dispatcher
passes the pick as one argument, and `collectors.split_pick` is what takes it apart.

Nothing here asks docker. The picker is a person watching an empty box, and whether a
container is up is what the task's own `status` run says once it has been picked.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collectors_config as config
import picker_rows

REPO_ROOT = Path(__file__).resolve().parents[1]

# Joins a verb to the one collector it is for, in a row's value.
PICK_SEP = ":"

# `collectors.ONCE`, spelled here because this module imports only the config, and
# `test_collectors_picker.py` pins the two together.
ONCE = "run-once"

# What each verb is called in the list, for all of them and for one. `status` first: a
# mis-click on the first row must change nothing.
VERBS = (
    ("run-here", "Run {} on this PC", "starts it now, and keeps it up from then on"),
    ("stop-here", "Stop {} on this PC", "stops it now, and keeps it stopped from then on"),
    ("release", "Forget this PC's choice for {}", "neither started nor stopped here any more"),
)


def now(assignment: dict[str, str], project: str) -> str:
    mode = assignment.get(project, "")
    return {config.RUN: "this PC: runs it", config.STOP: "this PC: stops it"}.get(
        mode, "this PC: hands off"
    )


def rows(collectors: list[config.Collector], assignment: dict[str, str]) -> list[str]:
    """Every pick, grouped by verb: all collectors first, then each one."""
    if not collectors:
        return [
            picker_rows.nothing_row(
                "No collectors declared",
                f"add `{config.SETTING}` to the workspace file",
            )
        ]
    summary = ", ".join(f"{c.project} ({now(assignment, c.project)})" for c in collectors)
    found = [
        picker_rows.row(
            "status",
            "Show status -- change nothing",
            "status",
            f"Which collectors exist, what this PC is set to, and whether each is running. {summary}",
        )
    ]
    for verb, label, effect in VERBS:
        found.append(picker_rows.row(verb, label.format("all collectors"), verb, effect))
        found += [
            picker_rows.row(
                f"{verb}{PICK_SEP}{c.project}",
                label.format(c.project),
                now(assignment, c.project),
                f"{effect}. {what(c)}",
            )
            for c in collectors
        ]
    # Last, and per collector only: a run by hand assigns nothing, so it has no "all".
    found += [
        picker_rows.row(
            f"{ONCE}{PICK_SEP}{c.project}",
            f"Run {c.project} once now",
            now(assignment, c.project),
            f"In this terminal, whatever this PC is set to -- e.g. its first run before "
            f"scheduling it. Refused while its scheduled run is going. {what(c)}",
        )
        for c in collectors
        if c.scheduled
    ]
    return found


def what(collector: config.Collector) -> str:
    """One sentence on what the collector is, in either kind."""
    if collector.scheduled:
        return (
            f"Runs `{' '.join(collector.command)}` in {collector.project} every "
            f"{collector.minutes} minutes, as its own scheduled task."
        )
    return f"Compose service `{collector.service}` in {collector.project}."


def main(root: Path = REPO_ROOT) -> int:
    """Nothing but rows on stdout: it *is* the quick-pick."""
    base = config.home(root)
    collectors, _notes = config.declared(base)
    picker_rows.emit(rows(collectors, config.load_assignment(base / config.ASSIGNMENT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
