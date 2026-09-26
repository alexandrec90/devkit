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
2. **Merge green adoptions** (only those: every other green PR waits for a person).
3. **Read back** (`fix_loop.py`): blocked reports, friction files, dead sessions,
   transcripts, resolutions that did not hold, waits that have gone stale -- filed now,
   so the backlog read next already carries them.
4. **Collect everything red** (`fix_red.py`), the harness-defect backlog included,
   plan it (`fix_plan.py`) and classify it (`fix_cycle.py`).
5. **Harness first**: while anything harness-shaped is red, one devkit session gets
   all of it and project fixers are held, out loud. Only one at a time.
6. **Then projects**, conflicts first, each under `fix_budget.budget`: the ledger, the
   escalation ladder and the fuses.

Every pass appends a line to `logs/fix-pass.history.jsonl`, which `fix_stall` reads.
`"devkit.fixPass"` in the workspace file is `off` (the default), `plan` (write it all,
do nothing) or `dispatch`. Scheduled runs use `claude-bg`. The record is
`logs/fix-pass.log`, overwritten per pass.

`tests/test_fix_pass.py` drives the wiring with every subprocess replaced.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import adoption_prs
import agent_models
import broken_pr_menu as menu
import devkit_project
import fix_cycle
import fix_ledger
import fix_loop
import fix_plan
import fix_red
import fix_send
import gate_evidence
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
# What `merge_green_adoptions` reads of each open PR to judge it green.
ADOPTION_FIELDS = "number,headRefName,isDraft,labels,mergeable,statusCheckRollup"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

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


def ship_intents(
    root: Path, projects: list[str], mode: str, journal: Journal | None = None
) -> tuple[list[str], list[fix_plan.Failure], bool]:
    """Step 1. `(lines for the record, refused commits as failures, any ship failed)`.

    A push or a PR that failed is retried next pass and filed now; the third element
    turns the task red rather than green over a branch that did not go out. An intent
    where no PR can be opened from is filed for the devkit session to move.
    """
    lines: list[str] = []
    refused: list[fix_plan.Failure] = []
    failed = False
    for intent in ship_intent.find_intents(root, projects):
        where = f"{intent.project} {intent.branch}"
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
        outcome = ship_intent.ship_one(intent, push_gate.interpreter(intent.tree), base)
        # One line: a refusal's detail is hook output, and its newlines broke the record
        # into rows no reader of it could attribute. The tail says why; the rest is evidence.
        lines.append(f"{where} -- {outcome.stage}: {' '.join(outcome.detail.split())[-240:]}")
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


def pending_adoptions(root: Path, projects: list[str], tag: str) -> list[str]:
    """Projects with the newest release still up for adoption -- the harness mid-flight."""
    if not tag:
        return []
    return [
        name
        for name in projects
        if name != fix_cycle.DEVKIT
        and (root / name).is_dir()
        and adoption_prs.open_adoption_pr(root / name, tag)
    ]


def merge_green_adoptions(
    root: Path, projects: list[str], journal: Journal | None = None
) -> list[str]:
    """Step 2. The one merge the pass makes; `(lines for the record)`."""
    merged: list[str] = []
    prefixes = adoption_prs.adoption_prefixes()
    for name in projects:
        project_dir = root / name
        if name == fix_cycle.DEVKIT or not project_dir.is_dir():
            continue
        gh = sweep.gh_for(project_dir)
        listed = gh("pr", "list", "--state", "open", "--limit", "50", "--json", ADOPTION_FIELDS)
        rows = gate_evidence.gh_json(listed)
        if not isinstance(rows, list):
            rows = []
        for row in adoption_prs.green_adoptions(rows, prefixes, sweep.AUTOMERGE_LABEL):
            ok, message = worktree.merge_pr(gh, int(row.get("number", 0)))
            merged.append(
                f"{name} #{row.get('number')} -- {message if ok else 'FAILED: ' + message}"
            )
            if not ok:
                fix_loop.fix_findings.file(
                    journal, "merge-failed", name, f"#{row.get('number')}: {message[:200]}"
                )
    return merged


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
    classes = fix_cycle.classify_all(failures)
    harness = fix_cycle.harness_state(classes, green, adopting)
    go, held = fix_cycle.phase(decisions, classes, harness, prefixes)
    return harness, go, held, [d for d in decisions if d.action == fix_plan.SKIP]


# --- the pass -----------------------------------------------------------------------------


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
    root = workspace.parent
    # Every registered checkout, `devkit.onHold` or not: a PR that exists is work in
    # flight whatever the setting says, and the pass is the last thing that would move it.
    projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    ledger_path = worktree.boxes_root(root) / fix_ledger.LEDGER_NAME
    devkit_dir = root / fix_cycle.DEVKIT
    ctx = fix_loop.Context(root, projects, devkit_dir, ledger_path, REPO_ROOT / HISTORY, mode, now)
    errors = (menu.FixError, worktree.WorktreeError, devkit_project.ProjectError)
    journal = Journal(devkit_dir, errors=fix_loop.fix_findings.STEP_ERRORS + errors)
    prefixes = adoption_prs.adoption_prefixes()
    step = journal.step

    shipped, refused, ship_failed = step(
        "ship", ship_intents, root, projects, mode, journal, default=([], [], True)
    )
    merged = (
        step("merge", merge_green_adoptions, root, projects, journal, default=[])
        if mode == fix_cycle.DISPATCH
        else []
    )
    closed = step("read-back", fix_loop.close, ctx, journal, default=fix_loop.Closed())
    failures, green, unread = step(
        "collect", fix_red.collect_red, workspace, projects, refused, default=([], None, [])
    )
    regated, rerun = step("regate", fix_red.regate_unread, root, unread, mode, default=([], set()))
    for line in regated:
        if "FAILED" in line:
            fix_loop.fix_findings.file(journal, "regate-failed", line.split(" ", 1)[0], line)
    green = fix_plan.RUNNING if fix_cycle.DEVKIT in rerun else green
    # Filed before the backlog is read, and the backlog read on its own: whatever broke
    # above -- the collect step included -- reaches the devkit session this same pass.
    filed = fix_loop.record(ctx, journal)
    backlog = step("backlog", fix_red.backlog_failure, workspace, default=None)
    failures += [backlog] if backlog else []
    newest = step("newest-release", gate_evidence.newest_release, devkit_dir, default="")
    adopting = step("adoptions", pending_adoptions, root, projects, newest, default=[])
    fallback = fix_cycle.only_the_harness(backlog)
    harness, go, held, skipped = step(
        "plan", decide, failures, green, newest, adopting, prefixes, default=fallback
    )

    items = fix_loop.triage.load(devkit_dir)
    sent, capped, worst = step(
        "send", send_all, go, ctx, launch, journal, closed, items, default=([], [], EXIT_FAILED)
    )
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
        tuple(regated),
        tuple(filed),
        fix_loop.backlog(ctx),
    )
    text = fix_cycle.render(account)
    print(text)
    print(f"fix-pass: record at {write_artifact(text)}")
    append_history(account, now)
    return max(worst, EXIT_FAILED if failed_steps else EXIT_OK)


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


def main(argv: list[str] | None = None) -> int:
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
    try:
        return run(workspace, mode, launch)
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


if __name__ == "__main__":
    sys.exit(main())
