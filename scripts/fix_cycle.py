#!/usr/bin/env python3
"""The fix pass's decisions: harness first, project fixers held, every dispatch capped.

`fix_plan.py` decides what one red thing gets; this decides the *order* and the
*budget* across all of them, which is what turns a click into something that can run
unattended. Three rules, each pure and tested in `tests/test_fix_cycle.py`:

- **Classify before dispatching.** A failure is `HARNESS` when the gate itself said so
  -- a vendored test, a lint finding in a vendored path, a signature shared by two or
  more projects, a commit refused by the toolchain rather than by the change, or
  anything in devkit -- `PROJECT` when the evidence points at the project's own code,
  a vendored ratchet red on one project's ordinary PR included, and `UNKNOWN` when there is no evidence to point anywhere. Unknown goes to the project
  bucket, where the fixer will say "upstream" and stop; guessing the other way sends a
  devkit session at a project bug.
- **Hold every project fixer while the harness is red.** Nearly every red PR of the last
  month was a devkit fan-out, and a project fixer sent at one fixes a symptom eight
  times. So while any harness failure exists, or devkit's own default branch is red,
  the pass sends **one** devkit session at the whole harness set and holds the rest,
  saying so loudly -- a quiet pass has to read as "everything is blocked", never as
  "nothing to do". devkit's own PRs are not held: one may be the fix. A release still
  being adopted holds only that project's other PRs, behind its adoption.
- **Escalate what makes no progress; never park it.** The ledger stops a second
  dispatch at the same commit. A failure `fix_ledger.ATTEMPTS` fixers left unchanged
  is filed on the harness-defect ledger (`fix_budget.py`), where the devkit session takes it
  over, and gets fresh fixers once that is resolved. The devkit session, with nothing
  above it, backs off instead. No daily fuse caps any of it: the supervisor's spend
  watch does that job, with a person reading it.

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

# The exception to `fix_plan.VENDORED_TESTS` being devkit's: vendored tests that judge
# the checkout they run in, each comparing the project's own code with a baseline the
# project owns. They sit here, beside the classifier they steer. The file is
# byte-identical everywhere and what it measures is not, so one red in one project on
# an ordinary PR is that PR's growth -- carameli #395 grew its own `scripts/lint-all.py`
# past its recorded ceiling. The same one red across consumers is still devkit's: the
# v0.11.21 adoption was red in eight repos because the untested-symbols rule changed.
RATCHETS = frozenset(
    {
        f"{fix_plan.VENDORED_TESTS}test_structure_check.py::test_nothing_is_new_or_worse_than_the_baseline",
        f"{fix_plan.VENDORED_TESTS}test_structure_check.py::test_the_baseline_holds_only_what_the_code_still_earns",
        f"{fix_plan.VENDORED_TESTS}test_untested_symbols.py::test_every_public_symbol_is_named_by_a_test",
    }
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
# A `RERUN` is a `gh workflow run` call, and goes as itself for the same reason.
BRANCH_SHAPED = (fix_plan.UPDATE, fix_plan.RERUN, fix_plan.RESOLVE)


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


def is_own_ratchet(
    failure: fix_plan.Failure, shared: set[tuple[str, ...]], prefixes: Iterable[str]
) -> bool:
    """Red only on a ratchet, in one project, on a change that adopts nothing.

    Then the ratchet measured that change's own code against the project's own baseline
    (carameli #395), and the fix is on its head. Shared -- `shared` is the signatures seen
    in two or more projects -- or on an adoption, the ratchet itself may be what moved,
    as in the v0.11.21 fan-out, and that is devkit's.
    """
    return (
        bool(failure.signature)
        and all(entry in RATCHETS for entry in failure.signature)
        and failure.signature not in shared
        and not fix_plan.adoption_tag(failure.head, tuple(prefixes))
    )


def classify(
    failure: fix_plan.Failure, shared: set[tuple[str, ...]], prefixes: Iterable[str] = ()
) -> str:
    ids = [entry for entry in failure.signature if entry != fix_plan.CONFLICT]
    # A devkit PR is red on its own diff: one against a red main is held by the plan
    # before it gets here. Its vendored tests judge its own code -- the ratchets above
    # all -- so the fix lands on its head branch, never in the upstream session, whose
    # fresh branch off main has nothing to fix and cannot reach the PR (devkit #387, #393, #394).
    # A devkit commit the commit stage refused is the same shape: the fix is in the tree
    # the refusal happened in, which the first supervised run's upstream session spent
    # seven calls repairing from outside after the refusal was folded into it.
    if failure.project == DEVKIT and failure.kind in (fix_plan.PR, fix_plan.COMMIT):
        return PROJECT if ids else UNKNOWN
    # Before `_harness_shaped` and `HARNESS_PATHS`, each of which would claim it (#395).
    if is_own_ratchet(failure, shared, prefixes):
        return PROJECT
    if _harness_shaped(failure, shared):
        return HARNESS
    if ids and all(fix_plan.in_vendored_tier(entry, HARNESS_PATHS) for entry in ids):
        return HARNESS
    if failure.kind == fix_plan.COMMIT and any(
        marker in " ".join(ids).lower() for marker in HARNESS_REFUSALS
    ):
        return HARNESS
    return PROJECT if ids else UNKNOWN


def classify_all(
    failures: Iterable[fix_plan.Failure], prefixes: Iterable[str] = ()
) -> dict[str, str]:
    """Class per failure, keyed the way the ledger keys them. `prefixes` are the
    adoption branch stems (`adoption_prs.adoption_prefixes`)."""
    listed = list(failures)
    shared = shared_signatures(listed)
    named = tuple(prefixes)
    return {fix_ledger.failure_key(f): classify(f, shared, named) for f in listed}


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
    """Every failure under it is one of devkit's own branches: a PR, or a refused commit."""
    return all(
        f.project == DEVKIT and f.kind in (fix_plan.PR, fix_plan.COMMIT) for f in decision.failures
    )


# The order within a phase: updates and re-runs first (free), then conflicts (nothing
# else about the PR is knowable until it is resolved), then the rest.
RANK = {fix_plan.UPDATE: 0, fix_plan.RERUN: 0, fix_plan.RESOLVE: 1}


def _harness_first(harness_ones: list[fix_plan.Decision]) -> list[fix_plan.Decision]:
    """The branch-shaped harness decisions as themselves, then the rest folded into one."""
    branch_shaped = (d for d in harness_ones if d.action in BRANCH_SHAPED)
    go = sorted(branch_shaped, key=lambda d: RANK.get(d.action, 2))
    foldable = [d for d in harness_ones if d.action not in BRANCH_SHAPED]
    if foldable:
        go.append(fold_harness(foldable))
    return go


def only_the_harness(
    backlog: fix_plan.Failure | None,
) -> tuple[Harness, list[fix_plan.Decision], list, list]:
    """What the pass sends when planning itself raised: the backlog, and nothing else.

    The crash is on that backlog by then, so the devkit session is what fixes the plan.
    Everything else waits a pass rather than going out on a plan nobody could make.
    """
    harness = Harness(False, ("the plan step raised; only the devkit session goes",))
    go = (
        [
            fold_harness(
                [fix_plan.Decision(fix_plan.DISPATCH, fix_plan.describe(backlog), (backlog,))]
            )
        ]
        if backlog
        else []
    )
    return harness, go, [], []


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
    # What a dispatched session reported it could not do, one line each.
    blocked: tuple[str, ...] = ()
    # `fix_release.cut_release`'s one line: a release started, would start, or why not.
    # Empty when main carries nothing a tag owes a consumer.
    release: str = ""
    # Default branches with no verdict at the tip, whose gate the pass re-ran.
    regated: tuple[str, ...] = ()
    # What this pass filed on the harness-defect ledger, and how much is open there:
    # the loop's own state, so a record that sends nothing still says what is owed.
    filed: tuple[str, ...] = ()
    backlog: int = 0
    # Finished fixers' idle processes the pass stopped (`bg_sessions.py`).
    stopped: tuple[str, ...] = ()
    # What the pass decided on the ledger itself: retired, reopened, settled, pending.
    verified: tuple[str, ...] = ()


def _names(decision: fix_plan.Decision) -> str:
    return ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)


def render(account: Account) -> str:
    """The pass's own record, written whether or not it sent anything."""
    harness = account.harness
    lines = [f"fix-pass: mode={account.mode}"]
    lines += labelled("shipped", account.shipped) + labelled("regate", account.regated)
    lines.append(
        "harness  clean" if harness.clean else "harness  RED -- " + "; ".join(harness.reasons)
    )
    if harness.adopting:
        lines.append(
            f"adopting {', '.join(harness.adopting)} -- the newest release; "
            "each one's other PRs wait for its adoption"
        )
    lines += labelled("blocked", account.blocked) + labelled("verified", account.verified)
    lines += [f"{d.action:8} {_names(d)} -- {d.note}" for d in account.go]
    lines += labelled("held", [f"{_names(d)} -- {why}" for d, why in account.held])
    lines += labelled("capped", [f"{_names(d)} -- {why}" for d, why in account.capped])
    lines += labelled("skip", [f"{_names(d)} -- {d.note}" for d in account.skipped])
    for label, rows in (
        ("sent", account.sent),
        ("merged", account.merged),
        ("filed", account.filed),
        ("stopped", account.stopped),
    ):
        lines += labelled(label, rows)
    lines.append(f"ledger   {account.backlog} open on the harness-defect ledger")
    lines += labelled("release", [account.release] if account.release else [])
    return "\n".join(lines)


def labelled(label: str, rows: Iterable[str]) -> list[str]:
    """Each row under its record label, the labels padded to one column."""
    return [f"{label:8} {row}" for row in rows]


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
            # By name, so `fix_stall` can tell how long each has sat, and why.
            "waiting": {_names(d): why for d, why in (*account.held, *account.capped)},
            "skipped": {_names(d): d.note for d in account.skipped},
            "filed": len(account.filed),
            "backlog": account.backlog,
            "release": account.release,
        }
    )
