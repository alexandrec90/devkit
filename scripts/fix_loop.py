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
  it merges, the rows filed while it waited are retired against it, not re-sent -- and
  so is a wrapped job's failed run of code the checkout had not yet moved onto
  (`_predates_fix`).
- **Whether what was filed still stands** (`session_friction.outdated`): a friction row
  the detectors on the default branch no longer file is retired, with that reason, and
  one an open PR's detector no longer files is resolved against that PR
  (`friction_pending`).
- **What has sat too long** (`fix_stall.py`): a hold, a cap or a skip past a day.
- **What the machine's own jobs say**: a scheduled job the scheduler reports failing
  (`job_findings`), and an ingestion collector the tray shows as anything but OK
  (`collector_findings`). The tray is the only other reader of either, and a row only a
  person sees is the ending the pass exists to rule out.

Outside `dispatch` mode nothing is written -- not the ledger, not a tree, not the
harvest cursor -- and what would be filed is only listed.

Tested in `tests/test_fix_loop.py`, and through the pass in `tests/test_fix_pass.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bg_sessions
import collector_tasks
import collectors
import fix_cycle
import fix_findings
import fix_ledger
import fix_reports
import fix_stall
import fix_verify
import friction_pending
import ship_intent
import harness_triage as triage
import schedule_health
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
    # When the machine last started (`fix_reports.booted_at`); None judges a session by
    # its silence alone.
    booted: _dt.datetime | None = None

    @property
    def writes(self) -> bool:
        return self.mode == fix_cycle.DISPATCH


@dataclass
class Closed:
    """What reading back found that the rest of the pass needs."""

    lines: list[str] = field(default_factory=list)  # for the record, `blocked` rows
    # What `fix_verify` and the rechecks decided on the ledger: `verified` rows, never
    # `blocked` ones -- a retirement read as a stuck session in a record read for those.
    verified: list[str] = field(default_factory=list)
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
    # Each devkit PR still carrying a fix to consumers, as `(number, body)` (`devkit_fixes`):
    # a consumer failure one names waits for it rather than for a second devkit session.
    devkit_fixes: tuple[tuple[int, str], ...] = ()


def close(ctx: Context, journal: fix_findings.Journal) -> Closed:
    """Every read-back step, each isolated: one that raises is a finding, not a stop."""
    closed = Closed()
    closed.working = journal.step("sessions", working_dirs, default=frozenset())
    trees = journal.step("trees", fix_reports.read_trees, ctx.root, ctx.projects, default=[])
    for tree in trees:
        journal.step("tree", _one_tree, ctx, tree, journal, closed)
    cursor = ctx.ledger_path.parent / session_friction.CURSOR_NAME
    journal.add(*journal.step("harvest", _harvest, ctx, cursor, default=[]))
    journal.add(*journal.step("jobs", job_findings, ctx, default=[]))
    journal.add(*journal.step("collectors", collector_findings, ctx, default=[]))
    if ctx.writes:
        closed.verified += journal.step("verify", _verify, ctx, default=[])
        closed.verified += journal.step("recheck", _recheck, ctx, default=[])
        closed.verified += journal.step("predates-fix", _predates_fix, ctx, default=[])
        runner = ship_intent.run_quiet
        closed.stopped = journal.step(
            "stop", bg_sessions.stop_finished, closed.finished, runner, default=[]
        )
    closed.in_flight = journal.step("in-flight", in_flight, ctx, default={})
    closed.devkit_fixes = journal.step("devkit-fixes", devkit_fixes, ctx, default=())
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
    at_work = _judge_session(ctx, tree, where, journal, closed)
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
    _file_friction(ctx, tree, where, journal, closed, at_work)


def _file_friction(
    ctx: Context,
    tree: fix_reports.Tree,
    where: str,
    journal: fix_findings.Journal,
    closed: Closed,
    at_work: bool,
) -> None:
    """File the tree's friction lines, a "fixed on this branch" one settled by the branch
    once its work went out -- and none of them while its session is still at work on
    one that has not.

    Filed then, such a line is open, and a fixer is sent at a fix still being written:
    c8d4f66b and 5d8e9962 were harvested from peaceful-jumping-cocke a minute after its
    interactive session wrote them, both fixes uncommitted in its tree, and a second
    session was dispatched at the pair. Held whole, because `file_away` takes the whole
    file: the next pass after the session ships or goes quiet files every line.
    """
    claims = any(fix_reports.fixed_here(line) for line in tree.friction)
    out = bool(tree.branch) and claims and went_out(tree.path)
    if claims and tree.branch and not out and at_work:
        closed.lines.append(f"{where} -- friction held: its session is still at work on a fix")
        return
    # Where the lines will be once filed away below: naming the file about to be renamed
    # gave every friction row a dead path.
    kept = fix_reports.filed(fix_reports.FRICTION_FILE) if ctx.writes else fix_reports.FRICTION_FILE
    settled = _settlement(tree) if out else {}
    for line in tree.friction:
        reported = Finding(
            "reported",
            tree.project,
            line,
            evidence=str(tree.path / kept),
            event=fix_findings.FRICTION,
        )
        # A line its session fixed here is settled by this branch, not a new job for a
        # fixer -- once the fix is on the branch, which the words alone do not show.
        journal.add(replace(reported, **settled) if fix_reports.fixed_here(line) else reported)
    if tree.friction and ctx.writes:
        fix_reports.file_away(tree.path, fix_reports.FRICTION_FILE)


def _settlement(tree: fix_reports.Tree) -> dict[str, str]:
    """What settles a "fixed on this branch" line of `tree`'s: its branch, as of when the
    session wrote the file -- this filing can follow the PR's merge, which a resolution
    dated by the filing would discount as a reused name's (f0308756)."""
    try:
        written = fix_findings.stamp_of((tree.path / fix_reports.FRICTION_FILE).stat().st_mtime)
    except OSError:
        written = ""
    return {"settles_with": tree.branch, "since": written}


def went_out(tree: Path, runner=ship_intent.run_quiet) -> bool:
    """Whether the tree's work is committed on its branch: its last ship `shipped`, and
    nothing left uncommitted since.

    A friction line saying "fixed on this branch" settles on that branch, and
    `fix_verify` then holds it only to the branch merging -- not to the fix being in
    it. 5d9806b0 was settled on worktree-rippling-juggling-oasis, whose intent the pass
    had set aside over 19 uncommitted files, so the fix it named never reached the PR
    that was to retire it (7c16fabc). Such a line is filed open instead.
    """
    if ship_intent.read_state(tree).get("stage") != ship_intent.SHIPPED:
        return False
    status = runner(["git", "status", "--porcelain"], cwd=tree)
    return status.returncode == 0 and not (status.stdout or "").strip()


def _judge_session(
    ctx: Context, tree: fix_reports.Tree, where: str, journal: fix_findings.Journal, closed: Closed
) -> bool:
    """Judge the tree's stamped session; whether anyone is at work in the tree now."""
    key = str(tree.stamp.get("key", ""))
    state, transcript = fix_reports.session_state(tree.path, ctx.now, booted=ctx.booted)
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
        closed.lines.extend(judge_dead(ctx, tree, where, state, transcript, journal))
    elif (state == fix_reports.WORKING or running) and fix_ledger.is_upstream(key):
        closed.harness_busy = str(tree.path)
    return running or live is not None


def judge_dead(
    ctx: Context,
    tree: fix_reports.Tree,
    where: str,
    state: str,
    transcript: str,
    journal: fix_findings.Journal,
) -> list[str]:
    """Free a dead session's dispatch, and file it unless a restart killed it; the lines
    for the record."""
    key = str(tree.stamp.get("key", ""))
    if ctx.writes:
        fix_reports.note_on_stamp(tree.path, "dead", state)
    if state == fix_reports.INTERRUPTED:
        # The machine went down under it: no defect to file and no attempt spent, only
        # a re-send, which finds a PR's tree as the session left it.
        if ctx.writes:
            fix_ledger.mark_interrupted(ctx.ledger_path, key, state)
        return [f"{where} -- {state}; sent again"]
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
    return []


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


def still_working(busy: frozenset[str], tree: Path, now: _dt.datetime) -> bool:
    """Whether the fixer in `tree` is at work: listed busy (`fixers_working`), or its
    stamped session is waiting on a background task it started. A turn that ends on
    "the tests will notify me" lists as idle, and 0929-7 was shipped mid-test (8d2f56f5)."""
    return bg_sessions.busy_in(busy, tree) or fix_reports.awaiting_task(tree, now)


def why_held(busy: frozenset[str], tree: Path, now: _dt.datetime) -> str:
    """Why the pass must not ship `tree`'s intent yet, or "": `still_working`, then
    `outran_intent`."""
    if still_working(busy, tree, now):
        return "its session is still working in the tree"
    if late := outran_intent(tree, now):
        return f"{late} changed after the intent was written, and a session is still at work in the tree"
    return ""


def outran_intent(tree: Path, now: _dt.datetime, runner=ship_intent.run_quiet) -> str:
    """A changed path in `tree` written after its untried intent, while a session is
    still live there; else "".

    The commit takes the whole tree, so an intent describes the tree as it was when
    written. A devkit fixer left one in roguelike #81's tree at 15:37; the person's
    interactive session there began its next task at 15:56, and the pass committed its
    half-written files under the fixer's message at 16:00 (e89a0224). `still_working`
    sees only a background session, and an interactive one is busy by definition.
    Once the session goes quiet the tree ships whole, as before. An intent the pass has
    already tried is left alone: its own fixers rewrote files after it.
    """
    intent = tree / ship_intent.INTENT_FILE
    try:
        written = intent.stat().st_mtime
        words = ship_intent.parse_intent(intent.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    tried = ship_intent.read_state(tree).get("intent")
    if tried == ship_intent.Intent("", tree, "", *words).digest:
        return ""
    if fix_reports.active_transcript(tree, now) is None:
        return ""
    status = runner(["git", "status", "--porcelain", "--untracked-files=all"], cwd=tree)
    if status.returncode != 0:
        return ""
    for path in ship_intent.ship.changed_paths(status.stdout or ""):
        try:
            if (tree / path).stat().st_mtime > written:
                return path
        except OSError:
            continue
    return ""


# A scheduled job's own failure, read off the scheduler. Every job but the resident tray
# now runs under `log-wrap.py --always`, which files each failed run itself, so what is
# left here is what only the scheduler can say -- disabled, stale, never ran -- plus a
# failed run of a job that runs bare (a collector's task, an installer not yet re-run).
JOB_KIND = "scheduled-job"
# What `schedule_health.artifact_hint` says when a later run finished clean.
JOB_HISTORY = "the scheduler is reporting history"
# `schedule_health.problems`' line for a run that exited non-zero.
JOB_FAILED_RUN = ": last run failed ("
# The run's time and count, kept out of the detail: a group must survive its recurrences.
_JOB_WHEN = re.compile(r" (?:at|since) \d{4}-\d\d-\d\d \d\d:\d\d| \(\d+ intervals ago\)")


def job_findings(
    ctx: Context,
    jobs: list[schedule_health.Job] | None = None,
    git=ship_intent.run_quiet,
    tasks: dict[str, str] | None = None,
) -> list[Finding]:
    """A finding per devkit job the scheduler says needs attention, bar one it is only
    remembering: a line whose group was resolved after the run it reports. The scheduler
    repeats a daily job's last result for a day, so that run would reopen its own fix.
    Nor one that ran code older than its group's fix (`_ran_before_fix`).

    The scheduled collectors (`collectors.scheduled_tasks`, `{task: log}`) are asked about
    in the same query, as the tray asks: their tasks carry no `devkit-` prefix, so a
    failing host collector was red in the tray and absent here. Each is filed against
    its own project, which is its task's name -- and so is one this machine runs whose
    task the scheduler does not have, which the query can only omit
    (`tray_state.collector_task_states` paints the same row red)."""
    if tasks is None:
        tasks = collectors.scheduled_tasks(ctx.devkit_dir) if jobs is None else {}
    jobs = schedule_health.query(also=frozenset(tasks)) if jobs is None else jobs
    artifacts = {**schedule_health.ARTIFACTS, **tasks}
    by_name = {job.name: job for job in jobs}
    items = triage.load(ctx.devkit_dir)
    found: list[Finding] = []
    local_now = ctx.now.astimezone().replace(tzinfo=None)  # the scheduler speaks local time
    deliberate = schedule_health.stood_down()
    lines = schedule_health.problems(
        jobs, local_now, deliberate, root=ctx.devkit_dir, artifacts=artifacts
    )
    for line in lines:
        name, head = line.split(":", 1)[0], line.split(" -- ", 1)[0]
        job = by_name.get(name)
        finding = Finding(
            JOB_KIND,
            name if name in tasks else fix_cycle.DEVKIT,
            _JOB_WHEN.sub("", head),
            command=line[:300],
        )
        if not _filed_elsewhere(ctx, line, job, finding, items, git):
            found.append(finding.at(_job_evidence(ctx, name, artifacts, job)))
    return found + unscheduled(ctx, tasks, frozenset(by_name))


def _job_evidence(
    ctx: Context, name: str, artifacts: dict[str, str], job: schedule_health.Job | None
) -> str:
    """A copy of the reported run's own artifact, kept beside the ledger; "" when the
    file on disk is not that run's account (`schedule_health.run_artifact`).

    Never the artifact's path: 631737a8 cited `logs/collector-social-scraper.log`, which
    every half-hourly run rewrites, so its triager read a later, passing run there and
    nothing of the one that overlapped (`fix_findings.kept`)."""
    since = job.last_run if job else None
    artifact = schedule_health.run_artifact(name, artifacts, root=ctx.devkit_dir, since=since)
    if not artifact:
        return ""
    return fix_findings.kept(ctx.devkit_dir / artifact, ctx.devkit_dir, Path(artifact).stem)


def _filed_elsewhere(
    ctx: Context,
    line: str,
    job: schedule_health.Job | None,
    finding: Finding,
    items: list[triage.Item],
    git,
) -> bool:
    """Whether `line` is not this pass's to file: history the scheduler is only
    repeating, a failed run its `log-wrap.py --always` wrapper filed already, or a run
    whose group a fix has since retired (`_resolved_since`, `_ran_before_fix`)."""
    if JOB_HISTORY in line:
        return True
    if job is None:
        return False
    if JOB_FAILED_RUN in line and "log-wrap.py" in job.command and "--always" in job.command:
        return True
    ran = _run_began(ctx, finding, job)
    if ran is None:
        return False
    # A collector's task runs its own project's code, not devkit's.
    code = ctx.devkit_dir if finding.project == fix_cycle.DEVKIT else ctx.root / finding.project
    return _resolved_since(finding, items, ran) or _ran_before_fix(
        code, fix_findings.signature(finding), items, ran, git
    )


def _run_began(ctx: Context, finding: Finding, job: schedule_health.Job) -> _dt.datetime | None:
    """When the run a scheduler line reports began, in the scheduler's local time.

    `last_run`, but for a collector's fire skipped behind a run still going: that line
    reports the earlier run, whose start `collectors.py fire` recorded. 4933b284 judged
    the 16:30 scrape by the 17:00 skip, though it began four minutes before its fix
    merged. A recorded start no earlier than the skip is not that run's."""
    ran = job.last_run
    if ran is None or finding.project == fix_cycle.DEVKIT:
        return ran
    if job.last_result not in schedule_health.OVERLAPPING:
        return ran
    began = collector_tasks.started_at(ctx.devkit_dir / collector_tasks.start_path(job.name))
    return began if began is not None and began < ran else ran


# A collector this machine runs whose task the scheduler has never heard of.
UNSCHEDULED = "not scheduled on this machine"


def unscheduled(ctx: Context, tasks: dict[str, str], listed: frozenset[str]) -> list[Finding]:
    """A finding per scheduled collector in `tasks` that the scheduler did not list."""
    return [
        Finding(
            COLLECTOR_KIND,
            name,
            f"{name}: {UNSCHEDULED}",
            evidence=str(ctx.devkit_dir / collectors.ARTIFACT),
        )
        for name in sorted(tasks)
        if name not in listed
    ]


# A container collector the tray shows amber or red. Its health check failing is "the
# project's verdict, not a failure" to `collectors.py`, so `devkit-collectors` exits 0 and
# the scheduler reports nothing: ibkr_trader's check failed for a day -- its reddit job
# 39 runs in, on credentials never set -- with the tray the only thing that knew.
COLLECTOR_KIND = "collector"
# Where a row's state ends and its particulars begin: a container's uptime, a health
# summary stamped with when it was written. Kept out of the detail, as `_JOB_WHEN` is.
_COLLECTOR_PARTICULARS = re.compile(r" -- | \(")


def collector_findings(
    ctx: Context,
    rows: list[tuple[str, str, str]] | None = None,
    health: dict[str, dict] | None = None,
) -> list[Finding]:
    """A finding per collector row (`collectors.tray_rows`) that is not OK, against the
    collector's own project, citing `collectors.py`'s log, which holds the health output.
    Bar a failing health verdict taken from code older than its group's fix
    (`_verdict_predates_fix`), and bar a silent engine (`collectors.NOT_ANSWERING`).
    `health` is the verdicts the rows were drawn from.

    A silent engine is the machine's, not a project's, and `collectors.py maintain` --
    which revives a wedged one (`revive_engine`) -- fails on it under `log-wrap.py
    --always`, filing it once with its own account. Filed here as well, it was a group per
    project beside that one: 5cfe1e8a and df8dd4c0 were read off the tray at 20:11 and
    20:19 on 2026-10-08, the first before the checkout moved onto the revive, the second
    during the very restart that brought the engine back at 20:15."""
    if rows is None:
        rows = collectors.tray_rows(ctx.devkit_dir)
        if health is None:
            home = collectors.config.home(ctx.devkit_dir)
            health = collectors.load_health(home / collectors.HEALTH)
    items = triage.load(ctx.devkit_dir) if health else []
    found: list[Finding] = []
    for name, level, detail in rows:
        if level == collectors.OK or detail == collectors.NOT_ANSWERING:
            continue
        project = name.removeprefix(collectors.ROW_PREFIX)
        state = _COLLECTOR_PARTICULARS.split(detail, maxsplit=1)[0]
        finding = Finding(
            COLLECTOR_KIND,
            project,
            f"{project}: {state}",
            evidence=str(ctx.devkit_dir / collectors.ARTIFACT),
            command=f"{name}: {detail}"[:300],
        )
        verdict = (health or {}).get(project, {})
        stale = _verdict_predates_fix(finding, items, verdict, ctx.now)
        if not (state.startswith(collectors.HEALTH_FAILING) and stale):
            found.append(finding)
    return found


# The row a wrapped job's own `log-wrap.py --always` files for a failed run
# (`log-wrap.FAILED_EVENT`; a hyphenated script, so spelled here rather than imported).
FAILED_RUN_EVENT = "scheduled-job-failed"

# How long a fix goes on excusing a failing verdict from code older than it: its PR's
# merge, the checkout's fast-forward and `collectors.py`'s next pass, which redeploys.
# Past it, a verdict still from older code is one the redeploy never reached.
REDEPLOY_GRACE = _dt.timedelta(days=1)


def _verdict_predates_fix(
    finding: Finding, items: list[triage.Item], verdict: dict, now: _dt.datetime
) -> bool:
    """Whether a failing health verdict ran code older than `finding`'s group's latest
    standing fix, made within `REDEPLOY_GRACE`, in a container `collectors.py` is free
    to redeploy.

    51cca249: ibkr_trader's fix merged at 22:32 and the pass refiled the group at 22:33
    off a verdict from a day-old image, as `_ran_before_fix` saw for a scheduled job. A
    commit holding a fix is made after its resolution, so code no newer than that cannot
    hold it. `collectors.redeploy` moves the container onto the fix within a pass, and a
    redeploy that fails fails `devkit-collectors` itself, so this defers a verdict and
    hides none. A `held` redeploy can last, so it is filed at once: the evidence log
    says why it was held."""
    when = verdict.get(collectors.CODE_AT)
    if verdict.get(collectors.HELD) or not isinstance(when, (int, float)):
        return False
    made = _last_fix(fix_findings.signature(finding), items)
    return (
        made is not None and when < made and now.timestamp() - made < REDEPLOY_GRACE.total_seconds()
    )


def _ran_before_fix(
    checkout: Path, group: tuple, items: list[triage.Item], when: _dt.datetime, git
) -> bool:
    """Whether the run at `when` (naive: the scheduler's local time) executed code older
    than `group`'s latest standing fix, in a checkout that has moved past that fix since.

    cda106d0: `devkit-reap-stale` fired at 19:00:00 and the checkout fast-forwarded onto
    the merged fix at 19:00:02, so the run reported the very failure that fix retired and
    the group read "RECURRED ... that fix did not hold". A commit holding a fix is made
    after its resolution, so code committed before it cannot hold it. This defers a
    verdict by one run and never hides one: the next run executes the newer code.
    """
    made = _last_fix(group, items)
    if made is None:
        return False
    since = when.astimezone(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S +0000")
    ran = _commit_time(checkout, f"HEAD@{{{since}}}", git)
    now = _commit_time(checkout, "HEAD", git)
    return ran is not None and now is not None and ran < made <= now


def _standing_fix(group: tuple, items: list[triage.Item]) -> triage.Item | None:
    """The latest standing resolution of `group`, by when it was made; None if none is."""
    ids = {item.id for item in items if item.signature == group}
    verdict = triage.verdicts(items)
    made = []
    for item in items:
        if (
            item.event == triage.RESOLVED_EVENT
            and item.fields.get("ref") in ids
            and verdict.get(item.fields["ref"]) == (item.event, item.stamp)
        ):
            try:
                made.append((_dt.datetime.fromisoformat(triage.resolved_at(item)), item))
            except ValueError:  # a stamp no ledger writer produces
                continue
    return max(made, key=lambda pair: pair[0])[1] if made else None


def _last_fix(group: tuple, items: list[triage.Item]) -> float | None:
    """When the latest standing resolution of `group` was made, as a POSIX time."""
    fix = _standing_fix(group, items)
    return None if fix is None else _dt.datetime.fromisoformat(triage.resolved_at(fix)).timestamp()


def _predates_fix(
    ctx: Context, git=ship_intent.run_quiet, lookup: fix_verify.Lookup | None = None
) -> list[str]:
    """Retire each open failed-run row this machine's `log-wrap.py --always` filed from a
    run of checkout code older than the merge of its group's standing fix, against that
    fix's own PR -- so the row lands, or reopens, with it.

    6a65dbe7: reap-stale failed at 19:17, before the checkout fast-forwarded onto #567 at
    19:18 -- the fix it reported against had merged at 19:04 and was not yet running. A
    wrapped job files its own row, so `job_findings`' `_ran_before_fix` never saw it, and
    it read "RECURRED ... that fix did not hold". The bound is the fix's *merge*, not its
    resolution: #565 merged between the two, so the code that ran was newer than the
    resolution and still lacked the fix. The row's stamp is the run's end, so a
    fast-forward during the run reads as the newer code: this can only under-retire.
    """
    items = triage.load(ctx.devkit_dir)
    if lookup is None:
        lookup = fix_verify.gh_lookup(ctx.root, ctx.projects, sweep.gh_for)
    host = triage.harness_events.host_name()
    lines = []
    for item in triage.open_items(items, (FAILED_RUN_EVENT,)):
        if item.host != host or item.project != fix_cycle.DEVKIT:
            continue  # another machine's checkout, or a project's own, ran it
        fix = _standing_fix(item.signature, items)
        if fix is None or not (merged := _fix_merged_at(fix, lookup)):
            continue  # no fix, or one in flight, which `pending_groups` already holds
        if not _ran_before(ctx.devkit_dir, item.stamp, merged, git):
            continue
        ref, pr = fix.fields.get("ref", "-"), fix.fields.get("pr", "-")
        note = (
            f"ran code older than the fix for [{ref}] ({pr}), which merged at "
            f"{merged.isoformat()}; the checkout moved onto it after this run"
        )
        made = triage.resolved_at(fix)
        triage.resolve([item.id], note, pr=pr, root=ctx.devkit_dir, resolved=made)
        lines.append(f"retired [{item.id}] -- {note}")
    return lines


def _fix_merged_at(fix: triage.Item, lookup: fix_verify.Lookup) -> _dt.datetime | None:
    """When the PR the resolution `fix` names merged, as `fix_verify` judges it; None
    while it has not, or when GitHub cannot say."""
    pr = fix.fields.get("pr", "")
    resolution = fix_verify.Resolution(fix.fields.get("ref", ""), triage.resolved_at(fix), pr, "")
    project, what = fix_verify.target(pr)
    if not what:
        return None
    merge = fix_verify.landed(resolution, lookup(project, what))
    # `landed` returns only a merge whose `merged_at` it could read, so this parse holds.
    return None if merge is None else _dt.datetime.fromisoformat(merge.merged_at)


def _ran_before(checkout: Path, stamp: str, merged: _dt.datetime, git) -> bool:
    """Whether the checkout's HEAD at ledger time `stamp` was committed before `merged`,
    and its HEAD now was not. A merge commit is made at its PR's `mergedAt`, so a HEAD
    older than that cannot hold it."""
    try:
        when = _dt.datetime.fromisoformat(stamp)
    except ValueError:  # a stamp no ledger writer produces
        return False
    since = when.astimezone(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S +0000")
    ran = _commit_time(checkout, f"HEAD@{{{since}}}", git)
    now = _commit_time(checkout, "HEAD", git)
    return ran is not None and now is not None and ran < merged.timestamp() <= now


def _commit_time(checkout: Path, rev: str, git) -> float | None:
    """`rev`'s commit time in `checkout`; None when git cannot say. A reflog that does not
    reach back to a date answers with its oldest entry, which is not the code that ran."""
    done = git(["git", "-C", str(checkout), "log", "-1", "--format=%ct", rev])
    if done.returncode != 0 or "only goes back" in (done.stderr or ""):
        return None
    try:
        return float(done.stdout.strip())
    except ValueError:
        return None


def _resolved_since(finding: Finding, items: list[triage.Item], when: _dt.datetime) -> bool:
    """Whether `finding`'s group was resolved after `when` (the scheduler's local time)."""
    since = when.astimezone(_dt.UTC).isoformat(timespec="seconds")
    group = fix_findings.signature(finding)
    ids = {item.id for item in items if item.signature == group}
    verdict = triage.verdicts(items)
    return any(
        verdict.get(ref, ("", ""))[0] == triage.RESOLVED_EVENT and verdict[ref][1] > since
        for ref in ids
    )


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


# How long a merged devkit PR goes on holding the consumer failures it names: the release
# and the adoption that carry its fix there, which the pass and the daily upgrade job each
# drive within a day. Past it, a failure still red is one the fix did not reach.
MERGED_FIX_HOLDS = _dt.timedelta(days=2)


def devkit_fixes(ctx: Context, gh_for=sweep.gh_for) -> tuple[tuple[int, str], ...]:
    """Every devkit PR open, or merged within `MERGED_FIX_HOLDS`, as `(number, body)`;
    none when `gh` cannot say, which sends the devkit session as before.

    Merged as well as open (f9ebcfd4): a consumer failure is red until the adoption that
    carries devkit's fix merges, so a pass that held only for open PRs sent a devkit
    session at social-scraper #23 while #483, naming it, sat merged in v0.11.37.
    """
    done = gh_for(ctx.devkit_dir)(
        "pr", "list", "--state", "all", "--limit", "100", "--json", "number,body,state,mergedAt"
    )
    try:
        rows = json.loads(done.stdout or "[]") if done.returncode == 0 else []
    except ValueError:
        return ()
    since = ctx.now - MERGED_FIX_HOLDS
    return tuple(
        (row["number"], str(row.get("body") or ""))
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and isinstance(row.get("number"), int) and _carries(row, since)
    )


def _carries(row: dict, since: _dt.datetime) -> bool:
    """`row` is open, or merged at or after `since`."""
    if row.get("state") == "OPEN":
        return True
    try:
        merged = _dt.datetime.fromisoformat(str(row.get("mergedAt") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return row.get("state") == "MERGED" and merged >= since


def _verify(ctx: Context) -> list[str]:
    items = triage.load(ctx.devkit_dir)
    lookup = fix_verify.gh_lookup(ctx.root, ctx.projects, sweep.gh_for)
    mentions = fix_verify.gh_mentions(ctx.root, ctx.projects, sweep.gh_for)
    cache = ctx.ledger_path.parent / fix_verify.CACHE_NAME
    outcome = fix_verify.verify(items, lookup, cache, ctx.now, mentions)
    lines = []
    for was, fix in outcome.found:
        # The ledger names the PR that holds the fix, not the branch that never did.
        note = f"{was.note} -- merged as {fix.url}, which names [{was.ref}]; pr= said {was.pr}"
        triage.resolve([was.ref], note, pr=fix.url, root=ctx.devkit_dir, resolved=was.stamp)
        lines.append(f"settled [{was.ref}] on {fix.url} -- {was.pr} never landed it")
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
