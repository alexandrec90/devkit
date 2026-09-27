#!/usr/bin/env python3
"""The half of the fix pass that makes it self-correcting: read back, then file.

`fix-pass.py` ships intents and sends fixers at what is red. This is everything that
tells it whether any of that worked, and turns every "no" into a finding on the
harness-defect ledger, where the devkit session picks it up on the next pass:

- **What each worktree says.** A fixer's blocked report is marked on the dispatch
  ledger and filed with its problem as the key, which parks that problem until the
  devkit session resolves it (`fix_budget.budget`). Each line of any session's
  `logs/friction.md` is filed as friction. A dispatched session that never started,
  or went quiet without an intent or a report, frees its dispatch for an immediate
  re-send and is filed, so *why* sessions die gets fixed too.
- **What every transcript says** (`session_friction.py`): the turns the harness cost
  sessions nobody dispatched, which is most of them.
- **Whether resolutions held** (`fix_verify.py`): a group retired against a fix that
  never merged is reopened, and one whose fix is still in flight sends no session; once
  it merges, the rows filed while it waited are retired against it, not re-sent.
- **Whether what was filed still stands** (`session_friction.outdated`): a friction row
  the detectors on the default branch no longer file is retired, with that reason, and
  one an open PR's detector no longer files is resolved against that PR
  (`friction_pending`).
- **What has sat too long** (`fix_stall.py`): a hold, a cap or a skip past a day.

Outside `dispatch` mode nothing is written -- not the ledger, not a tree, not the
harvest cursor -- and what would be filed is only listed.

Tested in `tests/test_fix_loop.py`, and through the pass in `tests/test_fix_pass.py`.
"""

from __future__ import annotations

import datetime as _dt
import sys
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bg_sessions
import fix_cycle
import fix_findings
import fix_ledger
import fix_reports
import fix_stall
import fix_verify
import friction_pending
import ship_intent
import harness_triage as triage
import session_friction
import sweep

Finding = fix_findings.Finding


@dataclass(frozen=True)
class Context:
    root: Path  # the workspace root: every checkout is a child
    projects: list[str]
    devkit_dir: Path  # where the harness-defect ledger lives
    ledger_path: Path  # the dispatch ledger
    history_path: Path
    mode: str
    now: _dt.datetime

    @property
    def writes(self) -> bool:
        return self.mode == fix_cycle.DISPATCH


@dataclass
class Closed:
    """What reading back found that the rest of the pass needs."""

    lines: list[str] = field(default_factory=list)  # for the record, `blocked` rows
    # A devkit session still working the harness, by tree: a second one is held, since
    # two sessions at one backlog was a waste the ledger alone could not see.
    harness_busy: str = ""
    # problem -> the tree its latest fixer worked in, which is where an escalation of
    # that problem sends the devkit session to take it over.
    trees: dict[str, str] = field(default_factory=dict)
    # (project, branch) -> a tree some session other than the pass's is working in. A
    # fixer is never sent in beside it: the first supervised run sent one into the tree
    # an interactive session was editing, which only went well because it sat idle.
    busy: dict[tuple[str, str], str] = field(default_factory=dict)
    # Trees whose stamped session is done or dead, and the idle sessions stopped in them.
    finished: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    # ref -> pr of each resolution whose fix has not merged (`in_flight`): the backlog
    # step sends no session at a group waiting on one.
    in_flight: dict[str, str] = field(default_factory=dict)
    # Directories a session is busy in now (`bg_sessions.working`), stamped one or not.
    working: frozenset[str] = frozenset()


def close(ctx: Context, journal: fix_findings.Journal) -> Closed:
    """Every read-back step, each isolated: one that raises is a finding, not a stop."""
    closed = Closed()
    closed.working = journal.step("sessions", working_dirs, default=frozenset())
    trees = journal.step("trees", fix_reports.read_trees, ctx.root, ctx.projects, default=[])
    for tree in trees:
        journal.step("tree", _one_tree, ctx, tree, journal, closed)
    cursor = ctx.ledger_path.parent / session_friction.CURSOR_NAME
    journal.add(*journal.step("harvest", _harvest, ctx, cursor, default=[]))
    if ctx.writes:
        closed.lines += journal.step("verify", _verify, ctx, default=[])
        closed.lines += journal.step("recheck", _recheck, ctx, default=[])
        runner = ship_intent.run_quiet
        closed.stopped = journal.step(
            "stop", bg_sessions.stop_finished, closed.finished, runner, default=[]
        )
    closed.in_flight = journal.step("in-flight", in_flight, ctx, default={})
    history = fix_stall.read_history(ctx.history_path)
    journal.add(*journal.step("stall", fix_stall.stalled, history, ctx.now, default=[]))
    return closed


def _one_tree(
    ctx: Context, tree: fix_reports.Tree, journal: fix_findings.Journal, closed: Closed
) -> None:
    where = f"{tree.project} {tree.branch or tree.path.name}"
    problem = str(tree.stamp.get("problem", ""))
    key = str(tree.stamp.get("key", ""))
    if problem:
        closed.trees[problem] = str(tree.path)
    # Judged before anything is filed away: a filed report is still the session's outcome.
    _judge_session(ctx, tree, where, journal, closed)
    if reason := fix_reports.blocked_reason(tree.path):
        journal.add(
            Finding(
                "fixer-blocked",
                tree.project,
                f"{where}: {reason}",
                key=problem,
                evidence=str(tree.path),
            )
        )
        closed.lines.append(f"{where} -- {reason}")
        if ctx.writes:
            fix_ledger.mark_blocked(ctx.ledger_path, key, reason)
            fix_reports.file_away(tree.path, fix_reports.BLOCKED_FILE)
    # Where the lines will be once filed away below: naming the file about to be renamed
    # gave every friction row a dead path.
    kept = fix_reports.filed(fix_reports.FRICTION_FILE) if ctx.writes else fix_reports.FRICTION_FILE
    for line in tree.friction:
        evidence = str(tree.path / kept)
        # A line its session fixed here is settled by this branch, not a new job for a fixer.
        settles = tree.branch if tree.branch and fix_reports.fixed_here(line) else ""
        journal.add(
            Finding(
                "reported",
                tree.project,
                line,
                evidence=evidence,
                event=fix_findings.FRICTION,
                settles_with=settles,
            )
        )
    if tree.friction and ctx.writes:
        fix_reports.file_away(tree.path, fix_reports.FRICTION_FILE)


def _judge_session(
    ctx: Context, tree: fix_reports.Tree, where: str, journal: fix_findings.Journal, closed: Closed
) -> None:
    key = str(tree.stamp.get("key", ""))
    state, transcript = fix_reports.session_state(tree.path, ctx.now)
    if state in (fix_reports.DONE, *fix_reports.DEAD):
        closed.finished.append(str(tree.path))
    live = fix_reports.active_transcript(tree.path, ctx.now)
    # By its transcript, the stamped session is never "another" one: a finished fixer's
    # own transcript held six branches while this very pass shipped their intents. A
    # session listed busy in the tree is at work whoever it is, intent or no intent.
    running = bg_sessions.busy_in(closed.working, tree.path)
    if tree.branch and (running or (live and str(live) != transcript)):
        closed.busy[(tree.project, tree.branch)] = str(tree.path)
    if state in fix_reports.DEAD:
        # No key: a dead session is re-sent at once, not parked behind the finding. One
        # that never started is cited by what its launcher said, when it left a record.
        # A launcher's answer is the signature, and the branch -- new every dispatch --
        # stays in the evidence: filed under it, one unreachable service was eight open
        # groups in eight hours, one per hourly re-send (1e5e57f4 and seven more).
        launched = fix_reports.launch_line(tree.path) if not transcript else ""
        detail = (
            f"{tree.project}: the dispatched session {state} -- {launched}"
            if launched
            else f"{where}: the dispatched session {state}"
        )
        cited = str(tree.path / fix_reports.LAUNCH_FILE) if launched else str(tree.path)
        journal.add(Finding("fixer-no-outcome", tree.project, detail, evidence=transcript or cited))
        if ctx.writes:
            fix_ledger.mark_dead(ctx.ledger_path, key, state)
            fix_reports.note_on_stamp(tree.path, "dead", state)
    elif (state == fix_reports.WORKING or running) and fix_ledger.is_upstream(key):
        closed.harness_busy = str(tree.path)


def working_dirs() -> frozenset[str]:
    """`bg_sessions.working` over what `claude agents` lists; empty where it cannot."""
    return bg_sessions.working(bg_sessions.listed(ship_intent.run_quiet))


def fixers_working() -> frozenset[str]:
    """Where a background session is busy: a fixer that wrote its intent and kept going.

    Shipping under it committed a tree mid-edit twice -- #422's sweep, whose later edits
    were left unstaged for the resolver to find, and 0926-19, whose second fix needed a
    second commit. Background only: the supervisor that runs the pass by hand is an
    interactive session, busy by definition, and ships its own tree through it.
    """
    return bg_sessions.working(bg_sessions.listed(ship_intent.run_quiet), ("background",))


def _harvest(ctx: Context, cursor: Path) -> list[Finding]:
    if ctx.writes:
        return session_friction.harvest(ctx.root, cursor, ctx.now)
    with tempfile.TemporaryDirectory() as scratch:
        # A plan pass must not move the cursor, so it reads from a copy of it: from an
        # empty one, every rehearsal listed three days already filed as "would file".
        copy = Path(scratch) / cursor.name
        if cursor.is_file():
            shutil.copyfile(cursor, copy)
        return session_friction.harvest(ctx.root, copy, ctx.now)


def _verify(ctx: Context) -> list[str]:
    items = triage.load(ctx.devkit_dir)
    lookup = fix_verify.gh_lookup(ctx.root, ctx.projects, sweep.gh_for)
    cache = ctx.ledger_path.parent / fix_verify.CACHE_NAME
    outcome = fix_verify.verify(items, lookup, cache, ctx.now)
    lines = []
    for ref, why in outcome.reopen:
        triage.reopen([ref], why, root=ctx.devkit_dir)
        lines.append(f"reopened [{ref}] -- {why}")
    for ref, note, pr in outcome.covered:
        triage.resolve([ref], note, pr=pr, root=ctx.devkit_dir)
        lines.append(f"retired [{ref}] -- {note}")
    return lines


def _recheck(ctx: Context) -> list[str]:
    """Retire every open friction row today's detectors no longer file (`outdated`)."""
    items = triage.open_items(triage.load(ctx.devkit_dir))
    lines = []
    for ref, why in session_friction.outdated(items):
        triage.resolve([ref], why, root=ctx.devkit_dir)
        lines.append(f"retired [{ref}] -- {why}")
    return lines


def recheck_open(ctx: Context) -> list[str]:
    """Retire each open friction row an open PR's detector no longer files, against it.

    Called after `record`, not inside `close`: the rows this pass's harvest filed are the
    likeliest to be ones a detector fix in review already stops (`friction_pending`).
    """
    items = triage.open_items(triage.load(ctx.devkit_dir))
    fixes = friction_pending.detector_fixes(
        sweep.gh_for(ctx.devkit_dir), sweep.git_for(ctx.devkit_dir)
    )
    lines = []
    for ref, note, pr in friction_pending.pending(items, fixes):
        if ctx.writes:
            triage.resolve([ref], note, pr=pr, root=ctx.devkit_dir)
        lines.append(f"{'pending' if ctx.writes else 'would hold'} [{ref}] on #{pr}")
    return lines


def in_flight(ctx: Context) -> dict[str, str]:
    """`fix_verify.in_flight` over the ledger and the cache `_verify` just refreshed."""
    cache = ctx.ledger_path.parent / fix_verify.CACHE_NAME
    return fix_verify.in_flight(triage.load(ctx.devkit_dir), cache, ctx.now)


def record(ctx: Context, journal: fix_findings.Journal) -> list[str]:
    """File what the journal holds, once; lines for the record either way.

    Emptied as it goes, so the pass can call this after reading back -- putting this
    pass's findings in front of the devkit session this same pass -- and again at the
    end for what dispatching found.
    """
    found, journal.findings = journal.findings, []
    items = triage.load(ctx.devkit_dir)
    if ctx.writes:
        written = fix_findings.record_all(found, items, ctx.devkit_dir)
        return [f"{f.project} {f.headline[:160]}" for f in written]
    return [f"would file: {f.project} {f.headline[:160]}" for f in fix_findings.fresh(found, items)]


def backlog(ctx: Context) -> int:
    """How much is open on the harness-defect ledger, for the record's last line."""
    return len(triage.open_items(triage.load(ctx.devkit_dir)))
