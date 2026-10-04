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

Every pass appends a line to `logs/fix-pass.history.jsonl`, which `fix_stall` reads.
`"devkit.fixPass"` in the workspace file is `off` (the default), `plan` (write it all,
do nothing) or `dispatch`. Scheduled runs use `claude-bg`. The record is
`logs/fix-pass.log`, overwritten per pass.

`tests/test_fix_pass.py` drives the wiring with every subprocess replaced.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
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

# Every CLI the pass spawns before it can say anything, probed by running it: found is
# not enough. On Windows `python3` is often only the Store alias, which exits 9009, and
# every git hook runs through it. `gh auth status` rather than `--version`: a `gh`
# whose token expired answers every read with nothing, which reads as nothing red.
REQUIRED_TOOLS = {"git": ("--version",), "gh": ("auth", "status"), "python3": ("-c", "")}


def runs(argv: list[str]) -> bool:
    """Whether `argv` runs and exits 0, through the pass's own window-less runner."""
    try:
        return ship_intent.run_quiet(argv).returncode == 0
    except OSError:
        return False


def missing_tools() -> list[str]:
    return [tool for tool, args in REQUIRED_TOOLS.items() if not runs([tool, *args])]


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


def ship_intents(
    root: Path, projects: list[str], mode: str, journal: Journal | None = None
) -> tuple[list[str], list[fix_plan.Failure], bool]:
    """Step 1. `(lines for the record, refused commits as failures, any ship failed)`.

    A push or a PR that failed is retried next pass and filed now; the third element
    turns the task red rather than green over a branch that did not go out. An intent
    where no PR can be opened from is filed for the devkit session to move. An intent
    whose fixer is still busy in its tree waits, and so does one a live session has
    edited past (`fix_loop.why_held`).
    """
    lines: list[str] = []
    refused: list[fix_plan.Failure] = []
    failed = False
    busy = fix_loop.fixers_working() if mode == fix_cycle.DISPATCH else frozenset()
    now = _dt.datetime.now(_dt.UTC)
    for intent in ship_intent.find_intents(root, projects):
        where = f"{intent.project} {intent.branch}"
        if held := fix_loop.why_held(busy, intent.tree, now):
            lines.append(f"{where} -- held: {held}")
            continue
        if intent.blocked:
            lines.append(f"{where} -- NOT shipped: {intent.blocked}")
            fix_loop.fix_findings.file(
                journal,
                "intent-unshippable",
                intent.project,
                f"{where}: {intent.blocked}",
                str(intent.tree),
            )
            continue
        if mode != fix_cycle.DISPATCH:
            spent = ship_intent.is_spent(intent)
            lines.append(
                f"{where} -- {'would set aside, already shipped' if spent else 'would ship'}: {intent.subject}"
            )
            continue
        base = intent.base or "main"
        if provisioned := provision_for_ship(intent.tree):
            lines.append(f"{where} -- {provisioned}")
        # Read after provisioning, so the commit stage runs with the `.venv` just made.
        outcome = ship_intent.ship_one(intent, push_gate.interpreter(intent.tree), base)
        if outcome.intent.branch != intent.branch:
            # Carried off a retired name: the record names what went out, or it reads as
            # a merged branch shipping again. The session's resolutions named the retired
            # name too, whose PR predates them, so they follow the fix to its new one.
            since = ship_intent.retired_at(intent.tree, intent.branch)
            moved = fix_loop.triage.repoint(
                intent.branch, outcome.intent.branch, since, root / fix_cycle.DEVKIT
            )
            where = (
                f"{intent.project} {outcome.intent.branch} (carried off {intent.branch}; "
                f"{len(moved)} resolution(s) re-pointed)"
            )
        # One line: a refusal's detail is hook output, and its newlines broke the record
        # into rows no reader of it could attribute. A refusal says why on the line
        # `refusal_reason` picks -- its tail is the hook's boilerplate -- and the rest is
        # evidence; for anything else the tail says why.
        detail = outcome.detail
        if outcome.stage == ship_intent.REFUSED:
            state = ship_intent.read_state(outcome.intent.tree)
            if output := str(state.get("output", "")):
                detail = f"{state.get('step', 'commit')}: {ship_intent.refusal_reason(output)}"
        lines.append(f"{where} -- {outcome.stage}: {' '.join(detail.split())[-240:]}")
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
    shipped, refused, failed = journal.step(
        "ship", ship_intents, root, projects, mode, journal, default=([], [], True)
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


def run(
    workspace: Path,
    mode: str,
    launch: agent_models.Launch,
    now: _dt.datetime | None = None,
) -> int:
    now = now or _dt.datetime.now(_dt.UTC)
    if mode == fix_cycle.OFF:
        # A switched-off fire did nothing, so it says so only where nothing else has:
        # every half hour it would otherwise erase the record a manual pass just wrote.
        if not (REPO_ROOT / ARTIFACT).is_file():
            write_artifact(f"fix-pass: mode=off -- set {fix_cycle.SETTING} to plan or dispatch")
        return EXIT_OK
    ctx = context(workspace, mode, now)
    root, projects, devkit_dir = ctx.root, ctx.projects, ctx.devkit_dir
    errors = (menu.FixError, worktree.WorktreeError, devkit_project.ProjectError)
    journal = Journal(devkit_dir, errors=fix_loop.fix_findings.STEP_ERRORS + errors)
    prefixes = fix_release.adoption_prefixes()
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
    newest = step("newest-release", gate_evidence.newest_release, devkit_dir, default="")
    dispatching = mode == fix_cycle.DISPATCH
    release = step(
        "release", fix_release.cut_release, workspace, newest, green, dispatching, now, default=""
    )
    adopting = step("adoptions", fix_release.pending_adoptions, root, projects, newest, default=[])
    fallback = fix_cycle.only_the_harness(backlog)
    harness, go, held, skipped = step(
        "plan", decide, failures, green, newest, adopting, prefixes, default=fallback
    )

    # Nothing routes with code a merge replaced mid-pass; the watchdog reruns it current.
    go, held, moved = step("current", fix_send.hold_if_moved, go, held, ctx, default=(go, held, ""))
    items = fix_loop.triage.load(devkit_dir)
    sent, capped, worst = step(
        "send", send_all, go, ctx, launch, journal, closed, items, default=([], [], EXIT_FAILED)
    )
    if dispatching:
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
        *(tuple(rows) for rows in (unsent, drift, issues)),
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
) -> int:
    """`run`, with no other dispatching pass on the machine running beside it.

    One still holding the lock after `wait` owns this fire: this pass exits clean, and
    says so in this checkout's record -- the running pass is usually the scheduled one,
    in another checkout, so left alone the record here was the previous pass's, which
    the supervisor read back as this one's. A lock that could
    not be made at all -- no lock directory standing -- is no evidence of another pass,
    so the pass runs, as it did before there was a lock.
    """
    if mode != fix_cycle.DISPATCH:
        return run(workspace, mode, launch)
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
        return run(workspace, mode, launch)


def main(argv: list[str] | None = None) -> int:
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
        return run_alone(workspace, mode, launch)
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
