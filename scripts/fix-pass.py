#!/usr/bin/env python3
"""The fix pass: ship what sessions finished, send fixers at what is red, file the rest.

One pass, whether a click or the scheduler started it (through `fix-pass-watchdog.py`,
which survives this file crashing). Two rules hold it together:

- **Anything a script can do, no session does.** A session is sent only at the part
  that needs a change to the code, told to fix that and stop; merging the base,
  committing, pushing, opening the PR, reading the gate and updating a branch are each
  one command here.
- **Every observation ends green, in flight, or filed.** There is no "needs a human":
  what the pass cannot turn green becomes a finding on the harness-defect ledger
  (`fix_findings.py`), which the devkit session this same pass sends takes over.

1. **Ship every intent** (`ship_intent.py`); a refused commit is a failure like any other.
2. **Merge green adoptions**, then start any release `main` owes (`fix_release.py`).
3. **Read back** (`fix_loop.py`): blocked reports, friction files, dead sessions,
   transcripts, resolutions that did not hold, waits that have gone stale -- filed now,
   so the backlog read next already carries them.
4. **Collect everything red** (`fix_red.py`), the harness-defect backlog included, and
   what Dependabot cannot do (`fix_dependabot.py`: failing update jobs, alerts no PR
   answers -- under its own daily cap), plan it (`fix_plan.py`) and classify it
   (`fix_cycle.py`).
5. **Harness first**: while anything harness-shaped is red, one devkit session gets
   all of it and project fixers are held, out loud. Only one at a time.
6. **Then projects**, conflicts first, each under `fix_budget.budget`: the ledger, the
   escalation ladder.
7. **Installers current** (`installers.py maintain`), on a dispatching pass, so a
   merged change to what a job registers is live within a pass, not a day.
8. **Upkeep** (`tend`): a default branch's uncommitted lockfile carried or restored
   (`fix_drift.py`), any other drift reported and left; tracker issues whose workflow is
   green at the tip closed (`fix_issues.py`).

The pass keeps inside the watchdog's stop (`send_deadline`): past its deadline the ship
under way is ended and held, no further step starts and no session is launched, and the
record says what was left. Each step prints a line as it starts.

Every pass appends a line to `logs/fix-pass.history.jsonl`, which `fix_stall` reads.
`"devkit.fixPass"` in the workspace file is `off` (the default), `plan` (write it all,
do nothing) or `dispatch`. Scheduled runs use `claude-bg`. The record is
`logs/fix-pass.log`, overwritten per pass.

`tests/test_fix_pass.py` drives the wiring with every subprocess replaced.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import agent_models
import agent_tabs
import broken_pr_menu as menu
import devkit_project
import fix_cycle
import fix_dependabot
import fix_drift
import fix_issues
import fix_ledger
import fix_loop
import fix_plan
import fix_release
import fix_red
import fix_send
import gate_evidence
import git_trust
import installers
import ship_intent
import sweep
import worktree
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The dispatch half lives in `fix_send.py`; these names are what the pass and its tests
# reach it by, and `fix_prs` is the same module object `fix_send` loaded.
fix_prs = fix_send.fix_prs
send_all = fix_send.send_all
dispatch = fix_send.dispatch
update_branch = fix_send.update_branch
# The interpreter resolver the push gate uses: the tree's venv, or the checkout's when
# the tree has none, so `ship.py --fix` runs with the project's own pre-commit.
push_gate = load_by_path("run_push_gate", REPO_ROOT / "scripts" / "precommit" / "run_push_gate.py")

Finding = fix_loop.Finding
Journal = fix_loop.fix_findings.Journal

ARTIFACT = Path("logs") / "fix-pass.log"
HISTORY = Path("logs") / "fix-pass.history.jsonl"
# A week of half-hourly passes.
HISTORY_KEEP = 336
SCHEDULED_AGENT = "claude-bg"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# One dispatching pass at a time, machine-wide: beside the boxes, which every pass shares
# whichever checkout it runs from. On 2026-09-29 a supervised pass run from a worktree
# and the scheduled one started 21 seconds apart; both shipped the same two intents, and
# the loser's push was refused ("cannot lock ref ... reference already exists") and filed
# as a ship failure against a branch that had shipped (babfee68). A plan pass ships and
# sends nothing, so it needs no lock.
RUN_LOCK_NAME = "fix-pass.lock"
# Long enough for a normal pass to finish and the waiter to run after it, current.
RUN_LOCK_WAIT = 300.0
# Past the watchdog's 25-minute stop (`fix-pass-watchdog.TIMEOUT`): a pass it killed
# leaves the lock behind, and the next half-hourly fire breaks it.
RUN_LOCK_STALE = 26 * 60.0

# The ship step's share of the watchdog's 25 minutes: no intent starts shipping once the
# step has run this long, and the next pass ships it. Each ship is a commit stage, a push
# and a PR, none of them bounded, and on 2026-10-07, just after a wake, they ran at a
# fraction of their speed: ibkr_trader #98's took 17 minutes, data-lake's was still in
# `ship.py --fix` when the watchdog stopped the pass -- which had sent no one, written no
# record and left its lock (the 7s that ship takes now is the usual cost). Short, because
# one ship started just inside it still has to finish, and the pass after it.
SHIP_BUDGET_SECONDS = 4 * 60.0

# The seconds this run has before the watchdog stops it, handed over by the watchdog
# (`fix-pass-watchdog.WINDOW_ENV`): a rerun after a mid-pass merge gets only what is left
# of the fire. A pass started any other way assumes the whole of the watchdog's stop.
WINDOW_ENV = "DEVKIT_FIX_PASS_SECONDS"
WINDOW_SECONDS = 25 * 60.0
# What the pass keeps back from that stop: past it, the ship under way is ended
# (`ship_intent.bounded`), no further step starts (`Journal.left`) and no session is
# launched. That evening the watchdog stopped two passes running -- each with no record,
# no history line and its lock left behind. At 01:00 the reading steps took fifteen
# minutes after a five-minute ship, and the send began at minute 20, launching three
# sessions at up to 2.5 minutes each. At 01:30 one roguelike commit's hooks ran past
# fifteen minutes. The step under way at the deadline still has to finish, and the
# record after it.
SEND_RESERVE_SECONDS = 5 * 60.0
LATE = "left to the next pass, past the deadline"
# The send step's result when it raised: nothing sent, nothing held, the task red.
UNSENT: tuple[tuple, tuple, int] = ((), (), EXIT_FAILED)

# Every CLI the pass spawns before it can say anything, probed by running it: found is
# not enough. On Windows `python3` is often only the Store alias, which exits 9009, and
# every git hook runs through it. `gh auth token` rather than `--version`: it fails when
# no credential is stored, and reads nothing but the store.
REQUIRED_TOOLS = {"git": ("--version",), "gh": ("auth", "token"), "python3": ("-c", "")}
# A stored token can still be one GitHub refuses, and a `gh` whose token expired answers
# every read with nothing, which reads as nothing red -- so `gh auth status` is asked
# too. But it asks api.github.com, and failing on its exit code alone refused the whole
# pass as "not usable from this PATH", telling the operator to restart VS Code, when
# GitHub merely did not answer (f455f3fe, `gh` installed and logged in). Only its saying
# the token is invalid counts; GitHub being down is each step's to meet, not the PATH's.
GH_STATUS = ("gh", "auth", "status")
GH_REFUSED_SAYS = "invalid"


def window(env: Mapping[str, str] = os.environ) -> float:
    """The seconds this run has before the watchdog stops it (`WINDOW_ENV`)."""
    try:
        return float(env.get(WINDOW_ENV, ""))
    except ValueError:
        return WINDOW_SECONDS


def send_deadline(started: float, env: Mapping[str, str] = os.environ) -> float:
    """The `time.monotonic()` past which a pass started at `started` launches no one."""
    return started + window(env) - SEND_RESERVE_SECONDS


def runs(argv: list[str]) -> bool:
    """Whether `argv` runs and exits 0, through the pass's own window-less runner."""
    try:
        return ship_intent.run_quiet(argv).returncode == 0
    except OSError:
        return False


def gh_token_refused() -> bool:
    """Whether `gh auth status` says the stored token is invalid; a status that failed
    for any other reason -- GitHub not answering -- is not that."""
    try:
        done = ship_intent.run_quiet(list(GH_STATUS))
    except OSError:
        return False
    said = f"{done.stdout or ''}\n{done.stderr or ''}".lower()
    return done.returncode != 0 and GH_REFUSED_SAYS in said


def missing_tools() -> list[str]:
    missing = [tool for tool, args in REQUIRED_TOOLS.items() if not runs([tool, *args])]
    if "gh" not in missing and gh_token_refused():
        missing.insert(list(REQUIRED_TOOLS).index("gh"), "gh")
    return missing


def write_artifact(text: str, root: Path | None = None) -> Path:
    path = (root or REPO_ROOT) / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def append_history(account: fix_cycle.Account, now: _dt.datetime, root: Path | None = None) -> Path:
    """`fix_cycle.history_line` appended, kept to the last `HISTORY_KEEP`. The record
    says what the newest pass did; this says how long anything has been waiting."""
    path = (root or REPO_ROOT) / HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    line = fix_cycle.history_line(account, now)
    try:
        kept = path.read_text(encoding="utf-8").splitlines()[-(HISTORY_KEEP - 1) :]
    except OSError:
        kept = []
    path.write_text("\n".join([*kept, line]) + "\n", encoding="utf-8")
    return path


# --- the steps ----------------------------------------------------------------------------


def provision_for_ship(tree: Path) -> str:
    """Install the tree's toolchain when neither it nor its checkout has the project's own
    pre-commit: a line for the record, "" when nothing needed doing.

    `ship.py --fix` refuses without one ("provision first"), and that refusal went to a
    devkit session: sports_betting's worktree-harmonic-humming-kay, cut with no `.venv`
    from a checkout with none, was refused on every pass of the 2026-10-02 supervision.
    Provisioning is one command, and anything a script can do, no session does. A
    pre-commit on PATH is not the project's: the fixers it runs are the wrong versions.
    """
    own = ship_intent.ship.pre_commit_command(
        tree, sweep.source_checkout(tree), which=lambda _name: None, find_spec=lambda _name: None
    )
    if own is not None:
        return ""
    steps = worktree.plan_provision(tree, quiet=True)
    if not steps:
        return ""
    ok, notes = worktree.run_provision(tree, steps)
    if not ok:
        return "FAILED to provision: " + "; ".join(n for n in notes if n.startswith("FAILED"))
    return "provisioned its toolchain: " + ", ".join(step.label for step in steps)


def not_shipping(
    intent: ship_intent.Intent,
    busy: frozenset[str],
    now: _dt.datetime,
    mode: str,
    journal: Journal | None = None,
) -> str:
    """The record line for an intent this pass does not ship, or "" for one it does:
    held (`fix_loop.why_held`), unshippable (filed), or any intent of a dry run."""
    where = f"{intent.project} {intent.branch}"
    if held := fix_loop.why_held(busy, intent.tree, now):
        return f"{where} -- held: {held}"
    if intent.blocked:
        fix_loop.fix_findings.file(
            journal,
            "intent-unshippable",
            intent.project,
            f"{where}: {intent.blocked}",
            str(intent.tree),
        )
        return f"{where} -- NOT shipped: {intent.blocked}"
    if mode != fix_cycle.DISPATCH:
        spent = ship_intent.is_spent(intent)
        return f"{where} -- {'would set aside, already shipped' if spent else 'would ship'}: {intent.subject}"
    return ""


def shipped_line(outcome: ship_intent.Outcome, where: str) -> str:
    """One line: a refusal's detail is hook output, and its newlines broke the record
    into rows no reader of it could attribute. A refusal says why on the line
    `refusal_reason` picks -- its tail is the hook's boilerplate -- and the rest is
    evidence; for anything else the tail says why."""
    detail = outcome.detail
    if outcome.stage == ship_intent.REFUSED:
        state = ship_intent.read_state(outcome.intent.tree)
        if output := str(state.get("output", "")):
            detail = f"{state.get('step', 'commit')}: {ship_intent.refusal_reason(output)}"
    return f"{where} -- {outcome.stage}: {' '.join(detail.split())[-240:]}"


def carried_where(intent: ship_intent.Intent, outcome: ship_intent.Outcome, root: Path) -> str:
    """The record's name for a ship carried off a retired branch, re-pointing the
    resolutions that named it.

    The record names what went out, or it reads as a merged branch shipping again. The
    session's resolutions named the retired name too, whose PR predates them, so they
    follow the fix to its new one. A hand-named branch never headed a PR, so every
    resolution naming it does.
    """
    if intent.adopt:
        since = ship_intent.EVER
    else:
        since = ship_intent.retired_at(intent.tree, intent.branch)
    moved = fix_loop.triage.repoint(
        intent.branch, outcome.intent.branch, since, root / fix_cycle.DEVKIT
    )
    return (
        f"{intent.project} {outcome.intent.branch} (carried off {intent.branch}; "
        f"{len(moved)} resolution(s) re-pointed)"
    )


def deadline_of(journal: Journal | None) -> float | None:
    """The `time.monotonic()` the journal's deadline falls at (`Journal.left`), or None
    for a journal with none: what `ship_intent.bounded` ends a ship by."""
    if journal is None or journal.left is None:
        return None
    return time.monotonic() + journal.left()


def ship_intents(
    root: Path,
    projects: list[str],
    mode: str,
    journal: Journal | None = None,
    budget: float = SHIP_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[str], list[fix_plan.Failure], bool]:
    """Step 1. `(lines for the record, refused commits as failures, any ship failed)`.

    A push or a PR that failed is retried next pass and filed now (GitHub's own failure
    only once it lasts: `ship_intent.DEFERRED`); the third element turns the task red
    over a branch that did not go out. One no PR can be opened from is filed for the
    devkit session to move. One whose fixer is still busy in its tree waits, and so does
    one a live session has edited past (`fix_loop.why_held`), and so does every one left
    once the step has spent `budget` seconds: the steps after it are what send anyone
    anywhere. The budget bounds when a ship may start; the journal's deadline
    (`Journal.left`) bounds the one under way, which is ended there and held for the
    next pass.
    """
    lines: list[str] = []
    refused: list[fix_plan.Failure] = []
    failed = False
    started = clock()
    busy = fix_loop.fixers_working() if mode == fix_cycle.DISPATCH else frozenset()
    now = _dt.datetime.now(_dt.UTC)
    for intent in ship_intent.find_intents(root, projects):
        where = f"{intent.project} {intent.branch}"
        if line := not_shipping(intent, busy, now, mode, journal):
            lines.append(line)
            continue
        if (shipping := clock() - started) >= budget:
            lines.append(
                f"{where} -- held: this pass spent {int(shipping // 60)} min shipping; "
                f"the next pass ships it"
            )
            continue
        # Flushed as it starts: the record is written only at the end, so a pass stopped
        # mid-ship otherwise leaves the watchdog no line saying where it was.
        print(f"fix-pass: shipping {where}", flush=True)
        base = intent.base or "main"
        if provisioned := provision_for_ship(intent.tree):
            lines.append(f"{where} -- {provisioned}")
        # Read after provisioning, so the commit stage runs with the `.venv` just made.
        python = push_gate.interpreter(intent.tree)
        try:
            with ship_intent.bounded(deadline_of(journal)):
                outcome = ship_intent.ship_one(intent, python, base)
        except ship_intent.OutOfTime as stopped:
            # Ended, not refused: nothing about the change said no, so no fixer is sent.
            cleared = stopped.ended and ship_intent.clear_index_lock(intent.tree)
            lines.append(
                f"{where} -- held: {stopped}{' (its index.lock removed)' if cleared else ''}"
                f"; the next pass ships it"
            )
            continue
        if outcome.intent.branch != intent.branch:
            where = carried_where(intent, outcome, root)
        lines.append(shipped_line(outcome, where))
        if outcome.stage == ship_intent.REFUSED:
            refused.append(ship_intent.refusal_failure(outcome, base))
        if outcome.stage == ship_intent.FAILED:
            failed = True
            fix_loop.fix_findings.file(
                journal,
                "ship-failed",
                intent.project,
                f"{where}: {outcome.detail[:200]}",
                str(intent.tree),
            )
    return lines, refused, failed


def refresh_installers(workspace: Path, journal: Journal | None = None) -> int:
    """`installers.py maintain`, in-process: every installer's `--check`, `--yes` where
    stale. Its exit code; a failed installer is filed, with that job's artifact.

    That job fires once a day, and a change to what an installer registers is live only
    once it has: #404 moved the scheduled pass behind its watchdog, and the task kept
    running the pass bare, with nothing to catch its crashes, until the next morning's
    fire (990856e5). The pass is what merges such a change and fires every half hour,
    so a dispatching one applies it. After the send, so a re-registration of the pass's
    own task cannot come between a decision and its dispatch.
    """
    code = installers.main(["maintain", "--workspace", str(workspace)])
    if code == 2 and journal is not None:
        artifact = installers.sweep.source_checkout(REPO_ROOT) / installers.ARTIFACT
        fix_loop.fix_findings.file(
            journal,
            "installer-failed",
            fix_cycle.DEVKIT,
            "installer-failed: an installer's --check or --yes failed under the fix pass",
            fix_loop.fix_findings.kept(artifact, journal.devkit_dir, "installers"),
        )
    return code


def _ship_and_merge(
    root: Path, projects: list[str], mode: str, journal: Journal
) -> tuple[list[str], list[fix_plan.Failure], bool, list[str]]:
    """Steps 1 and 2, each isolated: `(shipped lines, refusals, ship failed, merged)`."""
    # Not failed by default: a crash is already `journal.crashed`, and one left for time
    # is late.
    shipped, refused, failed = journal.step(
        "ship", ship_intents, root, projects, mode, journal, default=([], [], False)
    )
    if mode != fix_cycle.DISPATCH:
        return shipped, refused, failed, []
    merged = journal.step("merge", fix_release.merge_green_adoptions, root, projects, default=[])
    return shipped, refused, failed, merged


def _collect(
    workspace: Path,
    projects: list[str],
    refused: list[fix_plan.Failure],
    now: _dt.datetime,
    journal: Journal,
) -> tuple[list[fix_plan.Failure], bool | str | None, list[str], list[str]]:
    """Step 4's reading, each source isolated: `(failures, devkit's default-branch
    verdict, the default branches with none, what Dependabot fails on that no session
    can change)`. A Dependabot read that raised costs its own failures, not the gate's."""
    failures, green, unread = journal.step(
        "collect", fix_red.collect_red, workspace, projects, refused, default=([], None, [])
    )
    found, notes = journal.step(
        "dependabot", fix_dependabot.collect, workspace, projects, now, default=([], [])
    )
    return failures + found, green, unread, notes


def _file_failures(lines: list[str], kind: str, journal: Journal) -> None:
    """A failed regate or merge in the record is a finding against that checkout."""
    for line in lines:
        if "FAILED" in line:
            fix_loop.fix_findings.file(journal, kind, line.split(" ", 1)[0], line)


def tend(workspace: Path, ctx: fix_loop.Context, journal: Journal) -> tuple[list[str], list[str]]:
    """The checkouts' own upkeep, after the send: `(drift lines, issue lines)`.

    Uncommitted drift on a default branch (`fix_drift.tend`) and the tracker issues a
    green tip has answered (`fix_issues.sweep_green`). Each step isolated, and a line
    either says `FAILED` on is filed against its checkout, as a failed merge is.
    """
    drift = journal.step("drift", fix_drift.tend, workspace, ctx.projects, ctx.mode, default=[])
    issues = journal.step(
        "issues", fix_issues.sweep_green, workspace, ctx.projects, ctx.mode, default=[]
    )
    _file_failures(drift, "drift-failed", journal)
    _file_failures(issues, "issue-close-failed", journal)
    return drift, issues


def decide(
    failures: list[fix_plan.Failure],
    green: bool | str | None,
    newest: str,
    adopting: list[str],
    prefixes: tuple[str, ...],
) -> tuple[
    fix_cycle.Harness,
    list[fix_plan.Decision],
    list[tuple[fix_plan.Decision, str]],
    list[fix_plan.Decision],
]:
    """Steps 4-6's decisions: `(harness, go, held, skipped)`, pure over what was read."""
    decisions = fix_plan.plan(failures, newest, prefixes)
    classes = fix_cycle.classify_all(failures, prefixes)
    harness = fix_cycle.harness_state(classes, green, adopting)
    go, held = fix_cycle.phase(decisions, classes, harness, prefixes)
    return harness, go, held, [d for d in decisions if d.action == fix_plan.SKIP]


# --- the pass -----------------------------------------------------------------------------


def context(workspace: Path, mode: str, now: _dt.datetime) -> fix_loop.Context:
    """What every step of a pass reads: the checkouts, the two ledgers, the clock, and
    when the machine last started, which tells a fixer a restart killed from a dead one."""
    root = workspace.parent
    # Every registered checkout, `devkit.onHold` or not: a PR that exists is work in
    # flight whatever the setting says, and the pass is the last thing that would move it.
    projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    ledger_path = worktree.boxes_root(root) / fix_ledger.LEDGER_NAME
    devkit_dir = root / fix_cycle.DEVKIT
    booted = fix_loop.fix_reports.booted_at(now)
    history = REPO_ROOT / HISTORY
    return fix_loop.Context(root, projects, devkit_dir, ledger_path, history, mode, now, booted)


def pass_journal(devkit_dir: Path, deadline: float) -> Journal:
    """The pass's journal: its step errors, `deadline` (a `time.monotonic()`) as
    `Journal.left`, and a flushed line as each step starts (`Journal.trace`), so the
    watchdog's log of a pass it stopped names the step its minutes went to."""
    begun = time.monotonic()
    errors = (menu.FixError, worktree.WorktreeError, devkit_project.ProjectError)
    return Journal(
        devkit_dir,
        errors=fix_loop.fix_findings.STEP_ERRORS + errors,
        left=lambda: deadline - time.monotonic(),
        trace=lambda name: print(
            f"fix-pass: step {name} at {int(time.monotonic() - begun)}s", flush=True
        ),
    )


def release_and_plan(
    workspace: Path,
    ctx: fix_loop.Context,
    journal: Journal,
    failures: list[fix_plan.Failure],
    green: bool | str | None,
    backlog: fix_plan.Failure | None,
) -> tuple[str, tuple]:
    """Step 4's release and plan, each isolated: `(release line, decide's result)`, the
    plan falling back to the harness alone when it raises."""
    step = journal.step
    newest = step("newest-release", gate_evidence.newest_release, ctx.devkit_dir, default="")
    dispatching = ctx.mode == fix_cycle.DISPATCH
    release = step(
        "release",
        fix_release.cut_release,
        workspace,
        newest,
        green,
        dispatching,
        ctx.now,
        default="",
    )
    adopting = step(
        "adoptions", fix_release.pending_adoptions, ctx.root, ctx.projects, newest, default=[]
    )
    prefixes = fix_release.adoption_prefixes()
    fallback = fix_cycle.only_the_harness(backlog)
    return release, step(
        "plan", decide, failures, green, newest, adopting, prefixes, default=fallback
    )


def late_lines(journal: Journal) -> tuple[str, ...]:
    """The record's line naming every step left for the deadline (`Journal.late`). Past
    it the record is what is left to protect: the watchdog's stop would take it, the
    history line and the lock, and every skipped step keeps for a pass."""
    return (f"{LATE}: {', '.join(journal.late)}",) if journal.late else ()


def run(
    workspace: Path,
    mode: str,
    launch: agent_models.Launch,
    now: _dt.datetime | None = None,
    deadline: float | None = None,
) -> int:
    """One pass. `deadline` is the `time.monotonic()` past which it ends the ship under
    way, starts no further step and launches no session (`send_deadline`); by default,
    measured from this call."""
    now = now or _dt.datetime.now(_dt.UTC)
    deadline = send_deadline(time.monotonic()) if deadline is None else deadline
    if mode == fix_cycle.OFF:
        # A switched-off fire did nothing, so it says so only where nothing else has:
        # every half hour it would otherwise erase the record a manual pass just wrote.
        if not (REPO_ROOT / ARTIFACT).is_file():
            write_artifact(f"fix-pass: mode=off -- set {fix_cycle.SETTING} to plan or dispatch")
        return EXIT_OK
    ctx = context(workspace, mode, now)
    root, projects, devkit_dir = ctx.root, ctx.projects, ctx.devkit_dir
    journal = pass_journal(devkit_dir, deadline)
    step = journal.step

    shipped, refused, ship_failed, merged = _ship_and_merge(root, projects, mode, journal)
    closed = step("read-back", fix_loop.close, ctx, journal, default=fix_loop.Closed())
    failures, green, unread, unsent = _collect(workspace, projects, refused, now, journal)
    regated, rerun = step("regate", fix_red.regate_unread, root, unread, mode, default=([], set()))
    _file_failures(regated, "regate-failed", journal)
    _file_failures(merged, "merge-failed", journal)
    green = fix_plan.RUNNING if fix_cycle.DEVKIT in rerun else green
    # Filed before the backlog is read, and the backlog read on its own: whatever broke
    # above -- the collect step included -- reaches the devkit session this same pass.
    filed = fix_loop.record(ctx, journal)
    closed.verified += step("pending", fix_loop.recheck_open, ctx, default=[])
    backlog = step("backlog", fix_red.backlog_failure, workspace, closed.in_flight, default=None)
    failures += [backlog] if backlog else []
    release, (harness, go, held, skipped) = release_and_plan(
        workspace, ctx, journal, failures, green, backlog
    )

    # Nothing routes with code a merge replaced mid-pass; the watchdog reruns it current.
    go, held, moved = step("current", fix_send.hold_if_moved, go, held, ctx, default=(go, held, ""))
    items = fix_loop.triage.load(devkit_dir)
    # Run past the deadline too, holding each session it would launch out loud: a pass
    # that skipped it would leave its decisions out of the record altogether.
    sent, capped, worst = step(
        "send", send_all, go, ctx, launch, journal, closed, items, default=UNSENT, always=True
    )
    if mode == fix_cycle.DISPATCH:
        step("installers", refresh_installers, workspace, journal, default=2)
    drift, issues = tend(workspace, ctx, journal)
    filed += fix_loop.record(ctx, journal)
    failed_steps = ship_failed or bool(journal.crashed)
    account = fix_cycle.Account(
        mode,
        harness,
        tuple(shipped),
        tuple(go),
        tuple(held),
        tuple(capped),
        tuple(sent),
        tuple(merged),
        tuple(skipped),
        tuple(closed.lines),
        release,
        tuple(regated),
        tuple(filed),
        fix_loop.backlog(ctx),
        tuple(closed.stopped),
        tuple(closed.verified),
        dependabot=tuple(unsent),
        drift=tuple(drift),
        issues=tuple(issues),
        late=late_lines(journal),
    )
    publish(account, now)
    return fix_send.EXIT_STALE if moved else max(worst, EXIT_FAILED if failed_steps else EXIT_OK)


def publish(account: fix_cycle.Account, now: _dt.datetime, root: Path | None = None) -> Path:
    """The pass's account rendered, printed, written as the record and added to the
    history; the record's path."""
    text = fix_cycle.render(account)
    print(text)
    path = write_artifact(text, root)
    print(f"fix-pass: record at {path}")
    append_history(account, now, root)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mode",
        choices=fix_cycle.MODES,
        default=None,
        help=f"off, plan or dispatch (default: the workspace file's {fix_cycle.SETTING}, else off)",
    )
    parser.add_argument(
        "--agent",
        default=SCHEDULED_AGENT,
        choices=sorted(fix_prs.AGENT_MODES),
        help="which CLI a dispatched session opens in; a scheduled pass always uses claude-bg",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="the scheduled job: mode from the workspace file, agent forced to claude-bg",
    )
    parser.add_argument("--workspace", type=Path, default=worktree.DEFAULT_WORKSPACE)
    # Carried, never interpreted: the pair reaches `fix-prs.open_session` unchanged. A
    # scheduled pass passes neither and so opens at whatever the CLI is configured with,
    # except where `fix_budget.budget` climbs the ladder.
    agent_models.add_arguments(parser)
    return parser


# The scheduled job that runs this pass: `install-fix-pass-task.py`'s `TASK_NAME`.
SCHEDULED_TASK = "devkit-fix-pass"


def hand_to_scheduled_task() -> int:
    """Start the scheduled pass now instead of running this one elevated.

    An elevated process may not launch a background session (`agent_tabs.ELEVATED`: its
    service would lock the scheduled pass out), so a pass run from an elevated VS Code
    refused every launch and left each to a scheduled pass up to 30 minutes away. The
    task runs with the user's ordinary token, so starting it is that same pass, now.

    Either way this checkout's record is rewritten to say so: left alone it still held
    the previous pass, which a supervisor read back three times as clean dispatches.
    """
    done = subprocess.run(
        ["schtasks", "/Run", "/TN", SCHEDULED_TASK],
        capture_output=True,
        text=True,
        check=False,
        creationflags=sweep.NO_WINDOW,
    )
    if done.returncode != 0:
        why = f"elevated, and could not start {SCHEDULED_TASK}: " + " ".join(
            (done.stderr or done.stdout or "").split()
        )
        print(f"fix-pass: {why}", file=sys.stderr)
        write_artifact(f"fix-pass: FAILED -- {why}")
        return EXIT_USAGE
    write_artifact(
        f"fix-pass: handed to {SCHEDULED_TASK} -- this shell is elevated; the task runs "
        f"the pass unelevated from its own checkout, and records it there"
    )
    print(
        f"fix-pass: this shell is elevated, so the pass was handed to the scheduled task "
        f"{SCHEDULED_TASK}, which runs unelevated; its record lands in {ARTIFACT}"
    )
    return EXIT_OK


def run_alone(
    workspace: Path,
    mode: str,
    launch: agent_models.Launch,
    wait: float = RUN_LOCK_WAIT,
    stale: float = RUN_LOCK_STALE,
    deadline: float | None = None,
) -> int:
    """`run`, with no other dispatching pass on the machine running beside it.

    One still holding the lock after `wait` owns this fire: this pass exits clean, and
    says so in this checkout's record -- the running pass is usually the scheduled one,
    in another checkout, so left alone the record here was the previous pass's, which
    the supervisor read back as this one's. A lock that could
    not be made at all -- no lock directory standing -- is no evidence of another pass,
    so the pass runs, as it did before there was a lock. `deadline` is `run`'s, and the
    wait for the lock counts against it.
    """
    if mode != fix_cycle.DISPATCH:
        return run(workspace, mode, launch, deadline=deadline)
    root = workspace.parent
    with worktree.named_lock(root, RUN_LOCK_NAME, wait, stale) as held:
        lock = worktree.boxes_root(root) / RUN_LOCK_NAME
        if not held and lock.is_dir():
            why = (
                f"another dispatching pass holds {lock} -- this one ships and sends "
                f"nothing; the running pass's record, in its own checkout, is the one to read"
            )
            print(f"fix-pass: {why}")
            write_artifact(f"fix-pass: yielded -- {why}")
            return EXIT_OK
        return run(workspace, mode, launch, deadline=deadline)


def main(argv: list[str] | None = None) -> int:
    # From the start: the watchdog's stop counts the preflight and the lock wait too.
    deadline = send_deadline(time.monotonic())
    fix_send.pin_loaded(REPO_ROOT)  # before anything can fast-forward the checkout
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    workspace = args.workspace.resolve()
    if not workspace.is_file():
        print(f"fix-pass: no workspace file at {workspace}", file=sys.stderr)
        return EXIT_USAGE
    text = workspace.read_text(encoding="utf-8")
    mode = args.mode or fix_cycle.mode_from_workspace(text)
    agent = SCHEDULED_AGENT if args.scheduled else args.agent
    if args.scheduled:
        mode = fix_cycle.mode_from_workspace(text)
    if mode == fix_cycle.DISPATCH and not args.scheduled and agent_tabs.is_elevated():
        return hand_to_scheduled_task()
    launch = agent_models.Launch.parse(agent, args.model, args.effort)
    if mode != fix_cycle.OFF and (missing := missing_tools()):
        why = (
            f"not usable from this PATH: {', '.join(missing)} -- install it, or `gh auth "
            f"login` (scripts/bootstrap-machine.ps1 -Yes does both), then fully restart VS "
            f"Code: a task inherits the PATH VS Code started with"
        )
        print(f"fix-pass: {why}", file=sys.stderr)
        write_artifact(f"fix-pass: FAILED -- {why}")
        return EXIT_USAGE
    # Before any git call: a tree an elevated session made is one git refuses this
    # unelevated pass, and every push, read and re-gate in it failed (5025d284, e1463857).
    writes = mode == fix_cycle.DISPATCH
    if mode != fix_cycle.OFF and (trusted := git_trust.adopt(workspace.parent, write=writes)):
        print(trusted)
    try:
        return run_alone(workspace, mode, launch, deadline=deadline)
    except (menu.FixError, worktree.WorktreeError, devkit_project.ProjectError) as exc:
        print(f"fix-pass: {exc}", file=sys.stderr)
        write_artifact(f"fix-pass: FAILED -- {exc}")
        return EXIT_USAGE
    except Exception as exc:
        # A crash is the one outcome the record must not miss: the first real dispatch
        # died on a TypeError, and the artifact still described the previous pass. The
        # watchdog reads the exit and the traceback and files both.
        write_artifact(f"fix-pass: CRASHED -- {type(exc).__name__}: {exc}")
        raise


def in_utf8_mode(argv: list[str], utf8_mode: int = sys.flags.utf8_mode) -> int | None:
    """Run this pass again under `-X utf8` when it was not started in UTF-8 mode, and
    return that run's exit code. `None` means this process already is the pass to run.

    Dozens of the pass's runners read a child's output with `text=True` and no encoding,
    so outside UTF-8 mode a child's `”` (0x9d in UTF-8) kills a reader thread on a cp1252
    console. The watchdog and the task dispatcher each start the pass in UTF-8 mode, and
    any other launcher (a terminal, a script calling it by path) used to get the
    traceback. The guard lives in the pass because the pass is the one place every
    launcher reaches.

    The rerun is handed `stdin` so that Windows hands it this process's stdout and stderr
    too: given no std handle at all, a `NO_WINDOW` child writes to a hidden console of its
    own, and a pass started from a terminal printed nothing.
    """
    if utf8_mode:
        return None
    return subprocess.run(
        [sweep.console_python(), "-X", "utf8", str(Path(__file__).resolve()), *argv],
        check=False,
        creationflags=sweep.NO_WINDOW,
        stdin=subprocess.DEVNULL,
    ).returncode


if __name__ == "__main__":
    relaunched = in_utf8_mode(sys.argv[1:])
    sys.exit(main() if relaunched is None else relaunched)
