#!/usr/bin/env python3
"""The fix pass's decisions: harness first, project fixers held, every dispatch capped.

`fix_plan.py` decides what one red thing gets; this decides the *order* and the
*budget* across all of them, which is what turns a click into something that can run
unattended. Three rules, each pure and tested in `tests/test_fix_cycle.py`:

- **Classify before dispatching.** A failure is `HARNESS` when the gate itself said so
  -- a vendored test, a lint finding in a vendored path, a signature shared by two or
  more projects, a commit refused by the toolchain rather than by the change, or
  anything in devkit -- `PROJECT` when the evidence points at the project's own code,
  and `UNKNOWN` when there is no evidence to point anywhere. Unknown goes to the project
  bucket, where the fixer will say "upstream" and stop; guessing the other way sends a
  devkit session at a project bug.
- **Hold every project fixer while the harness is red.** Nearly every red PR of the last
  month was a devkit fan-out, and a project fixer sent at one fixes a symptom eight
  times. So while any harness failure exists, or devkit's own default branch is red, or a
  release is still being adopted, the pass sends **one** devkit session at the whole
  harness set and holds the rest, saying so loudly -- a quiet pass has to read as
  "everything is blocked", never as "nothing to do".
- **Cap it.** The ledger stops a second dispatch at the same commit; this stops a
  third at a new one. A target (a PR, a branch, devkit) gets at most `PER_TARGET_PER_DAY`
  sessions a day, and the pass as a whole at most `PER_DAY`, the harness phase drawing
  first. Past the cap a target reads "needs a human" and is left alone.

The switch is the workspace file: `"devkit.fixPass"` under `settings`, `off` (the
default), `plan` (write what would happen, do nothing) or `dispatch`. The scheduled job
reads it on every pass; the VS Code task passes `dispatch` explicitly. Wired to be fully
automatic, and off until a week of manual passes has read right.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import fix_plan

HARNESS = "harness"
PROJECT = "project"
UNKNOWN = "unknown"

# The switch, under `"settings"` in the workspace file beside `devkit.onHold`.
SETTING = "devkit.fixPass"
OFF = "off"
PLAN = "plan"
DISPATCH = "dispatch"
MODES = (OFF, PLAN, DISPATCH)

# The checkout the harness is fixed in, by its registry name.
DEVKIT = "devkit"

# Paths a lint finding or a refused commit can name that belong to the vendored tier.
HARNESS_PATHS = (
    "scripts/hooks/",
    "scripts/precommit/",
    "scripts/sync-devkit.py",
    "scripts/ship.py",
    ".pre-commit-config.yaml",
    ".claude/rules/",
    ".claude/skills/",
)

# What a commit refused by the toolchain rather than by the change says. Every one of
# these has happened: the fixer's interpreter missing from a fresh worktree, pre-commit
# itself absent, a third-party hook's environment failing to build.
HARNESS_REFUSALS = (
    "executable",
    "not found",
    "could not run pre-commit",
    "pre-commit was not found",
    "installationerror",
    "failed to build",
    "environment",
)

# The budget. Two a day per target because the second is the retry after a fix that
# did not take; the third is the loop nobody asked for.
PER_TARGET_PER_DAY = 2
PER_DAY = 8


@dataclass(frozen=True)
class Harness:
    clean: bool
    reasons: tuple[str, ...]


# --- the switch -----------------------------------------------------------------------


def mode_from_workspace(text: str) -> str:
    """`devkit.fixPass` off the workspace file; `OFF` for anything absent or misspelt."""
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return OFF
    settings = payload.get("settings") if isinstance(payload, dict) else None
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    return value if value in MODES else OFF


# --- classification -------------------------------------------------------------------


def shared_signatures(failures: Iterable[fix_plan.Failure]) -> set[tuple[str, ...]]:
    """Signatures seen in two or more projects: one cause, several victims."""
    seen: dict[tuple[str, ...], set[str]] = {}
    for failure in failures:
        if failure.signature:
            seen.setdefault(failure.signature, set()).add(failure.project)
    return {sig for sig, projects in seen.items() if len(projects) >= 2}


def _names_harness_path(entry: str) -> bool:
    text = entry.removeprefix("lint ")
    return any(text.startswith(path) for path in HARNESS_PATHS)


def classify(failure: fix_plan.Failure, shared: set[tuple[str, ...]]) -> str:
    if failure.project == DEVKIT:
        return HARNESS
    if failure.signature and failure.signature in shared:
        return HARNESS
    if fix_plan.is_vendored(failure.signature):
        return HARNESS
    ids = [entry for entry in failure.signature if entry != fix_plan.CONFLICT]
    if ids and all(_names_harness_path(entry) for entry in ids):
        return HARNESS
    if failure.kind == fix_plan.COMMIT and any(
        marker in " ".join(ids).lower() for marker in HARNESS_REFUSALS
    ):
        return HARNESS
    if not ids:
        return UNKNOWN
    return PROJECT


def classify_all(failures: Iterable[fix_plan.Failure]) -> dict[str, str]:
    """Class per failure, keyed the way the ledger keys them."""
    listed = list(failures)
    shared = shared_signatures(listed)
    return {fix_plan.failure_key(f): classify(f, shared) for f in listed}


# --- the phase gate -------------------------------------------------------------------


def harness_state(
    classes: dict[str, str], devkit_green: bool | None, pending_adoptions: Iterable[str]
) -> Harness:
    """Clean only when nothing harness-shaped is red and no release is mid-adoption.

    `devkit_green` is None when the gate's verdict could not be read, which counts as
    not clean: an unreadable harness is not one to send project fixers behind.
    """
    reasons = []
    red = sum(1 for cls in classes.values() if cls == HARNESS)
    if red:
        reasons.append(f"{red} harness failure(s) open")
    if devkit_green is None:
        reasons.append("devkit's default-branch gate could not be read")
    elif not devkit_green:
        reasons.append("devkit's default-branch gate is red")
    pending = sorted(set(pending_adoptions))
    if pending:
        reasons.append(f"the newest release is still being adopted in {', '.join(pending)}")
    return Harness(not reasons, tuple(reasons))


def decision_class(decision: fix_plan.Decision, classes: dict[str, str]) -> str:
    """A decision is harness if any failure under it is."""
    found = {classes.get(fix_plan.failure_key(f), UNKNOWN) for f in decision.failures}
    if HARNESS in found or decision.action == fix_plan.UPSTREAM:
        return HARNESS
    return PROJECT if PROJECT in found else UNKNOWN


def phase(
    decisions: Iterable[fix_plan.Decision], classes: dict[str, str], harness: Harness
) -> tuple[list[fix_plan.Decision], list[tuple[fix_plan.Decision, str]]]:
    """`(go, held)`: what this pass sends, and what it holds with the reason.

    Skips are never in either list. While the harness is red, every harness decision is
    folded into one devkit session and every project one is held. Once clean, conflicts
    go first: a conflicted PR's gate cannot run, so nothing else about it is knowable.
    """
    live = [d for d in decisions if d.action != fix_plan.SKIP]
    harness_ones = [d for d in live if decision_class(d, classes) == HARNESS]
    project_ones = [d for d in live if decision_class(d, classes) != HARNESS]
    if not harness.clean:
        why = "held until the harness is clean: " + "; ".join(harness.reasons)
        go = [fold_harness(harness_ones)] if harness_ones else []
        return go, [(d, why) for d in project_ones]
    ordered = sorted(project_ones, key=lambda d: 0 if d.action == fix_plan.RESOLVE else 1)
    return harness_ones + ordered, []


def fold_harness(decisions: list[fix_plan.Decision]) -> fix_plan.Decision:
    """One devkit session for every harness failure this pass found."""
    if len(decisions) == 1 and decisions[0].action == fix_plan.UPSTREAM:
        return decisions[0]
    failures = tuple(f for d in decisions for f in d.failures)
    projects = sorted({f.project for f in failures})
    return fix_plan.Decision(
        fix_plan.UPSTREAM,
        f"the harness is red in {len(projects)} checkout(s) ({', '.join(projects)}); "
        "one devkit session for all of it",
        failures,
    )


# --- the caps -------------------------------------------------------------------------


def target_of(key: str) -> str:
    """The PR, branch or checkout a ledger key names: the first three fields."""
    parts = key.split(":")
    if parts[0] == fix_plan.UPSTREAM:
        return DEVKIT
    return ":".join(parts[:3])


def sent_today(ledger: dict[str, dict], now: _dt.datetime) -> dict[str, int]:
    """Dispatches per target on `now`'s date, from the ledger's own timestamps."""
    counts: dict[str, int] = {}
    day = now.date().isoformat()
    for key, entry in ledger.items():
        if isinstance(entry, dict) and str(entry.get("when", "")).startswith(day):
            target = target_of(key)
            counts[target] = counts.get(target, 0) + 1
    return counts


def within_caps(
    decision: fix_plan.Decision,
    ledger: dict[str, dict],
    now: _dt.datetime,
    per_target: int = PER_TARGET_PER_DAY,
    per_day: int = PER_DAY,
) -> tuple[bool, str]:
    counts = sent_today(ledger, now)
    if sum(counts.values()) >= per_day:
        return False, f"the pass has sent {per_day} sessions today; the rest wait for tomorrow"
    target = target_of(fix_plan.decision_key(decision))
    if counts.get(target, 0) >= per_target:
        return False, f"{target} has had {per_target} sessions today -- needs a human"
    return True, ""


# --- the account ------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    """What one pass did, in the order the record reads: the inputs to `render`."""

    mode: str
    harness: Harness
    shipped: tuple[str, ...] = ()
    go: tuple[fix_plan.Decision, ...] = ()
    held: tuple[tuple[fix_plan.Decision, str], ...] = ()
    capped: tuple[tuple[fix_plan.Decision, str], ...] = ()
    sent: tuple[str, ...] = ()
    merged: tuple[str, ...] = ()
    # What the plan declined out loud: a release PR, a superseded adoption, a release
    # commit's red. In the record because a pass that holds everything behind one of
    # these has to say which one, or "harness RED" reads as a defect nobody can find.
    skipped: tuple[fix_plan.Decision, ...] = ()


def _names(decision: fix_plan.Decision) -> str:
    return ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)


def render(account: Account) -> str:
    """The pass's own record, written whether or not it sent anything."""
    harness = account.harness
    lines = [f"fix-pass: mode={account.mode}"]
    lines += [f"shipped  {line}" for line in account.shipped]
    lines.append(
        "harness  clean" if harness.clean else "harness  RED -- " + "; ".join(harness.reasons)
    )
    lines += [f"{d.action:8} {_names(d)} -- {d.note}" for d in account.go]
    lines += [f"held     {_names(d)} -- {why}" for d, why in account.held]
    lines += [f"capped   {_names(d)} -- {why}" for d, why in account.capped]
    lines += [f"skip     {_names(d)} -- {d.note}" for d in account.skipped]
    lines += [f"sent     {line}" for line in account.sent]
    lines += [f"merged   {line}" for line in account.merged]
    return "\n".join(lines)


# --- green adoptions, the one thing the pass merges ---------------------------------------


GREEN = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})


def _labels(row: dict) -> set[str]:
    return {
        str(entry.get("name", "")) if isinstance(entry, dict) else str(entry)
        for entry in row.get("labels", []) or []
    }


def _gate_green(row: dict) -> bool:
    rollup = row.get("statusCheckRollup") or []
    if not isinstance(rollup, list) or not rollup:
        return False
    verdicts = {str(node.get("conclusion") or node.get("state") or "").upper() for node in rollup}
    return verdicts <= GREEN and str(row.get("mergeable", "")).upper() != "CONFLICTING"


def green_adoptions(rows: Iterable[dict], prefixes: tuple[str, ...], label: str) -> list[dict]:
    """Open adoption PRs whose gate passed and that carry the label: mergeable unattended.

    An adoption is upstream churn whose green gate is the whole review, and letting
    those land is what keeps a release's fan-out from piling up in the queue. Nothing
    else is merged by the pass; every other green PR waits for a person.
    """
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and not row.get("isDraft")
        and str(row.get("headRefName", "")).startswith(prefixes)
        and label in _labels(row)
        and _gate_green(row)
    ]
