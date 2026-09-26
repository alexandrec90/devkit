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
  times. So while any harness failure exists, or devkit's own default branch is red,
  the pass sends **one** devkit session at the whole harness set and holds the rest,
  saying so loudly -- a quiet pass has to read as "everything is blocked", never as
  "nothing to do". devkit's own PRs are not held: one may be the fix. A release still
  being adopted holds only that project's other PRs, behind its adoption.
- **Stop what makes no progress.** The ledger stops a second dispatch at the same
  commit; `fix_ledger.ATTEMPTS` stops a third at an unchanged failure, whatever commit
  it is at. A failure that changed is progress and goes. `PER_TARGET_PER_DAY` and
  `PER_DAY` are fuses behind that, for a pass whose reading has gone wrong.

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
import fix_ledger
import fix_plan

HARNESS = "harness"
PROJECT = "project"
UNKNOWN = "unknown"

# The switch, under `"settings"` in the workspace file.
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

# Actions the fold cannot take, because each *is* an operation on one named PR rather
# than a piece of work. An `UPDATE` is a `gh pr update-branch` call and no session at
# all; a `RESOLVE` needs a worktree on the PR's own head branch, which is the one thing
# an upstream session -- a fresh branch off the default -- does not have. devkit #381
# was a conflict folded this way, and the session it opened was told to fix the harness
# "in the vendored file, the test, or the template" on a branch that could never land
# on the PR. A harness PR in either shape goes as itself, before the folded session.
BRANCH_SHAPED = (fix_plan.UPDATE, fix_plan.RESOLVE)

# The fuses. The retry policy is `fix_ledger.ATTEMPTS` -- per problem, spent only by a
# fixer that left the same failure behind -- and these are only what stops a pass
# whose reading has gone wrong: a signature that changes on every run reads as
# progress forever, and nothing else would notice. Set above what a working day
# needs, so a fuse that trips is a defect to read about in the record, not a budget.
PER_TARGET_PER_DAY = 4
PER_DAY = 24


@dataclass(frozen=True)
class Harness:
    clean: bool
    reasons: tuple[str, ...]
    # Projects whose adoption of the newest release is still open. Not a harness
    # reason: it holds that project's other PRs, which the adoption may fix, and nobody
    # else's. As a fleet-wide hold, one red carameli adoption (#389) held every PR in
    # every consumer and devkit's own for a day, behind a fixer rationed to one a day.
    adopting: tuple[str, ...] = ()


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


def _harness_shaped(failure: fix_plan.Failure, shared: set[tuple[str, ...]]) -> bool:
    """The failure is the harness's by where it is, what it shares, or what it names.

    An adoption PR is classified like any other PR, by what is failing: a vendored test
    is the harness, the project's own lint under a new rule is the project's. Which
    of the two it is decides where the fixer goes -- the one devkit session, or the
    adoption branch itself.

    A devkit PR is not the harness by any of these: see `classify`.
    """
    return (
        failure.project == DEVKIT
        or bool(failure.signature and failure.signature in shared)
        or fix_plan.is_vendored(failure.signature)
    )


def classify(failure: fix_plan.Failure, shared: set[tuple[str, ...]]) -> str:
    ids = [entry for entry in failure.signature if entry != fix_plan.CONFLICT]
    # A devkit PR is red on its own diff: one against a red main is held by the plan
    # before it gets here. Its vendored tests judge its own code -- the ratchets above
    # all -- so the fix lands on its head branch, never in the upstream session, whose
    # fresh branch off main has nothing to fix and cannot reach the PR (devkit #393, #394).
    if failure.project == DEVKIT and failure.kind == fix_plan.PR:
        return PROJECT if ids else UNKNOWN
    if _harness_shaped(failure, shared):
        return HARNESS
    if ids and all(fix_plan.in_vendored_tier(entry, HARNESS_PATHS) for entry in ids):
        return HARNESS
    if failure.kind == fix_plan.COMMIT and any(
        marker in " ".join(ids).lower() for marker in HARNESS_REFUSALS
    ):
        return HARNESS
    return PROJECT if ids else UNKNOWN


def classify_all(failures: Iterable[fix_plan.Failure]) -> dict[str, str]:
    """Class per failure, keyed the way the ledger keys them."""
    listed = list(failures)
    shared = shared_signatures(listed)
    return {fix_ledger.failure_key(f): classify(f, shared) for f in listed}


# --- the phase gate -------------------------------------------------------------------


def harness_state(
    classes: dict[str, str], devkit_green: bool | str | None, pending_adoptions: Iterable[str]
) -> Harness:
    """Clean only when nothing harness-shaped is red; the adoptions still open ride along.

    `devkit_green` is None when the gate's verdict could not be read, which counts as
    not clean: an unreadable harness is not one to send project fixers behind. It is
    `fix_plan.RUNNING` when the run at the tip has not finished, which is not a reason:
    the verdict before it stands, and holding on it held every project fixer for the
    pass after every merge to devkit main.

    The harness-defect ledger's backlog is harness-shaped and rides in the devkit
    session when one goes, but does not by itself make one go: one unresolved hook
    event anywhere was holding every project fixer.
    """
    reasons = []
    red = sum(
        1
        for key, cls in classes.items()
        if cls == HARNESS and fix_ledger.key_kind(key) != fix_plan.LEDGER
    )
    if red:
        reasons.append(f"{red} harness failure(s) open")
    if devkit_green is None:
        reasons.append("devkit's default-branch gate could not be read")
    elif devkit_green is False:
        reasons.append("devkit's default-branch gate is red")
    return Harness(not reasons, tuple(reasons), tuple(sorted(set(pending_adoptions))))


def decision_class(decision: fix_plan.Decision, classes: dict[str, str]) -> str:
    """A decision is harness if any failure under it is."""
    found = {classes.get(fix_ledger.failure_key(f), UNKNOWN) for f in decision.failures}
    if HARNESS in found or decision.action == fix_plan.UPSTREAM:
        return HARNESS
    return PROJECT if PROJECT in found else UNKNOWN


def phase(
    decisions: Iterable[fix_plan.Decision],
    classes: dict[str, str],
    harness: Harness,
    prefixes: Iterable[str] = (),
) -> tuple[list[fix_plan.Decision], list[tuple[fix_plan.Decision, str]]]:
    """`(go, held)`: what this pass sends, and what it holds with the reason.

    Skips are never in either list; a `HOLD` the plan made -- a PR behind a red base --
    is held with the plan's own note, clean or not. While the harness is red, every
    *foldable* harness decision becomes one devkit session and every project one is
    held -- except a devkit PR, which is red on its own diff and may be the harness
    fix itself: held behind the harness, the fix is what never lands. A project with
    its adoption still open holds its other PRs behind that adoption, which goes.
    Updates go first (free, and they may turn the PR green by themselves), then
    conflicts: a conflicted PR's gate cannot run, so nothing else about it is knowable.
    That same order holds for the branch-shaped decisions the fold cannot take.
    """
    planned_holds = [(d, d.note) for d in decisions if d.action == fix_plan.HOLD]
    live = [d for d in decisions if d.action not in (fix_plan.SKIP, fix_plan.HOLD)]
    harness_ones = [d for d in live if decision_class(d, classes) == HARNESS]
    project_ones = [d for d in live if decision_class(d, classes) != HARNESS]
    go = _harness_first(harness_ones)
    held: list[tuple[fix_plan.Decision, str]] = []
    sent: list[fix_plan.Decision] = []
    red = "held until the harness is clean: " + "; ".join(harness.reasons)
    for d in project_ones:
        adopting = sorted({f.project for f in d.failures} & set(harness.adopting))
        if is_devkit_pr(d):
            sent.append(d)
        elif not harness.clean:
            held.append((d, red))
        elif adopting and not fix_plan.is_adoption(d, prefixes):
            held.append((d, f"held until the newest release is adopted in {', '.join(adopting)}"))
        else:
            sent.append(d)
    return go + sorted(sent, key=lambda d: RANK.get(d.action, 2)), held + planned_holds


def is_devkit_pr(decision: fix_plan.Decision) -> bool:
    """Every failure under it is one of devkit's own PRs."""
    return all(f.project == DEVKIT and f.kind == fix_plan.PR for f in decision.failures)


# The order within a phase: updates first (free), then conflicts (nothing else about
# the PR is knowable until it is resolved), then the rest.
RANK = {fix_plan.UPDATE: 0, fix_plan.RESOLVE: 1}


def _harness_first(harness_ones: list[fix_plan.Decision]) -> list[fix_plan.Decision]:
    """The branch-shaped harness decisions as themselves, then the rest folded into one."""
    branch_shaped = (d for d in harness_ones if d.action in BRANCH_SHAPED)
    go = sorted(branch_shaped, key=lambda d: RANK.get(d.action, 2))
    foldable = [d for d in harness_ones if d.action not in BRANCH_SHAPED]
    if foldable:
        go.append(fold_harness(foldable))
    return go


def fold_harness(decisions: list[fix_plan.Decision]) -> fix_plan.Decision:
    """One devkit session for every foldable harness failure this pass found.

    Never called with a `BRANCH_SHAPED` decision: see the constant.
    """
    if len(decisions) == 1 and decisions[0].action == fix_plan.UPSTREAM:
        return decisions[0]
    failures = tuple(f for d in decisions for f in d.failures)
    projects = sorted({f.project for f in failures})
    return fix_plan.Decision(
        fix_plan.UPSTREAM,
        f"harness-shaped in {len(projects)} checkout(s) ({', '.join(projects)}); "
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
    """Sessions per target on `now`'s date, from the ledger's own timestamps.

    An `UPDATE` is recorded for idempotence but is one `gh` call and no session, so it
    is not counted: after a release merge, a handful of behind PRs once spent the whole
    day's budget on free branch updates and the real fixers waited for tomorrow.
    """
    counts: dict[str, int] = {}
    day = now.date().isoformat()
    for key, entry in ledger.items():
        if key.endswith(f":{fix_plan.UPDATE}"):
            continue
        if isinstance(entry, dict) and str(entry.get("when", "")).startswith(day):
            target = target_of(key)
            counts[target] = counts.get(target, 0) + 1
    return counts


def is_blind(decision: fix_plan.Decision) -> bool:
    """No failure under it has any evidence: no test id, no lint line, no failed step."""
    return all(
        not [entry for entry in f.signature if entry != fix_plan.CONFLICT]
        for f in decision.failures
    )


def moved_on(decision: fix_plan.Decision, ledger: dict[str, dict]) -> bool:
    """Every session this problem has had was sent at a commit other than its head now.

    The head moved under each of them, so each did something. Counted over the life of
    the ledger, like `fix_ledger.attempts`, not over a day: a conflict resolved
    yesterday and back today is the base moving again. A folded upstream key names no
    one commit and never counts as moved.
    """
    parts = fix_ledger.decision_key(decision).split(":")
    if parts[0] == fix_plan.UPSTREAM or len(parts) < 4:
        return False
    problem = fix_ledger.problem_key(decision)
    shas = [
        other.split(":")[3]
        for other, entry in ledger.items()
        if fix_ledger.problem_of(other, entry) == problem and len(other.split(":")) >= 4
    ]
    return bool(shas) and parts[3] not in shas


def within_caps(
    decision: fix_plan.Decision,
    ledger: dict[str, dict],
    now: _dt.datetime,
    per_target: int = PER_TARGET_PER_DAY,
    per_day: int = PER_DAY,
) -> tuple[bool, str]:
    """Whether this dispatch may go, and why not when it may not.

    An update is free and always goes. Otherwise the question is whether sessions have
    already failed at this same problem: `fix_ledger.ATTEMPTS` of them (one, for a
    blind problem, which cannot show progress) and it needs a person. Except a conflict
    whose head has moved since (`moved_on`): a resolver pushes only a merge that
    resolved, so a new conflict at a new commit is the base moving again, not a fix
    that did not take. devkit #390's resolver pushed its merge, main moved within the
    hour, and the fresh conflict read "needs a human" when it needed the resolver.
    Past that, the fuses -- which a working pass never reaches, and which still bound
    a conflict that keeps coming back.
    """
    if decision.action == fix_plan.UPDATE:
        return True, ""
    made = fix_ledger.attempts(decision, ledger)
    limit = fix_ledger.BLIND_ATTEMPTS if is_blind(decision) else fix_ledger.ATTEMPTS
    rebased = decision.action == fix_plan.RESOLVE and moved_on(decision, ledger)
    if made >= limit and not rebased:
        unchanged = "with no evidence to tell progress by" if is_blind(decision) else "unchanged"
        return False, f"{made} session(s) sent and it is still red {unchanged} -- needs a human"
    counts = sent_today(ledger, now)
    if sum(counts.values()) >= per_day:
        return False, f"fuse: the pass has sent {per_day} sessions today -- read the record"
    target = target_of(fix_ledger.decision_key(decision))
    if counts.get(target, 0) >= per_target:
        return False, f"fuse: {target} has had {per_target} sessions today -- read the record"
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
    # What a dispatched session reported it could not do, one line each: the one
    # channel back from a fixer, and what "needs a human" is about.
    blocked: tuple[str, ...] = ()
    # Default branches with no verdict at the tip, whose gate the pass re-ran.
    regated: tuple[str, ...] = ()


def _names(decision: fix_plan.Decision) -> str:
    return ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)


def render(account: Account) -> str:
    """The pass's own record, written whether or not it sent anything."""
    harness = account.harness
    lines = [f"fix-pass: mode={account.mode}"]
    lines += [f"shipped  {line}" for line in account.shipped]
    lines += [f"regate   {line}" for line in account.regated]
    lines.append(
        "harness  clean" if harness.clean else "harness  RED -- " + "; ".join(harness.reasons)
    )
    if harness.adopting:
        lines.append(
            f"adopting {', '.join(harness.adopting)} -- the newest release; "
            "each one's other PRs wait for its adoption"
        )
    lines += [f"blocked  {line}" for line in account.blocked]
    lines += [f"{d.action:8} {_names(d)} -- {d.note}" for d in account.go]
    lines += [f"held     {_names(d)} -- {why}" for d, why in account.held]
    lines += [f"capped   {_names(d)} -- {why}" for d, why in account.capped]
    lines += [f"skip     {_names(d)} -- {d.note}" for d in account.skipped]
    lines += [f"sent     {line}" for line in account.sent]
    lines += [f"merged   {line}" for line in account.merged]
    return "\n".join(lines)


def history_line(account: Account, now: _dt.datetime) -> str:
    """One pass as one JSON line: what went, and why nothing did when nothing did."""
    harness = account.harness
    return json.dumps(
        {
            "when": now.isoformat(timespec="seconds"),
            "mode": account.mode,
            "harness": "clean" if harness.clean else list(harness.reasons),
            "adopting": list(harness.adopting),
            "sent": list(account.sent),
            "held": len(account.held),
            "capped": [f"{_names(d)} -- {why}" for d, why in account.capped],
        }
    )
