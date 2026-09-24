#!/usr/bin/env python3
"""The fix pass: ship what sessions finished, then send fixers at what is red, in order.

One pass, whether a click or the scheduler started it:

1. **Ship every intent.** A session that is done leaves `logs/ship-intent.md` in its
   worktree and nothing else (`ship_intent.py`). The pass runs the fixers, commits with
   that message, pushes with the push gate skipped, opens the PR with the label and
   records the outcome. A refused commit becomes a failure like any other.
2. **Collect everything red.** Refused commits, red PRs, open scheduled-failure issues
   and every default branch whose own gate is red, each with the gate's own artifact
   (`gate_evidence.py`), plus the harness-defect ledger's open backlog
   (`harness_triage.py`) as one failure with its groups as evidence; planned by
   `fix_plan.py` and classified by `fix_cycle.py`. A release commit's red -- the
   newest-tag test, until the tag exists -- is skipped out loud, and reads as green once
   the tag points at it.
3. **Harness first.** While anything harness-shaped is red -- a vendored test, a shared
   signature, devkit's own gate, a release mid-adoption -- one devkit session gets the
   whole set and every project fixer is held, out loud. A harness PR that is behind or
   conflicted goes as itself first: neither is work a fresh branch can do
   (`fix_cycle.BRANCH_SHAPED`).
4. **Then projects**, conflicts first, each under the ledger and the daily caps.
5. **Merge green adoptions**, and nothing else. Every other green PR waits for a person.

Every dispatch is `fix-prs.py`'s: the worktree on the PR's branch, the evidence under
`logs/gate/`, the prompt naming the failing ids. This file only decides what to hand it.

**The switch.** `"devkit.fixPass"` in the workspace file's `settings` is `off` (the
default), `plan` or `dispatch`; the scheduled job (`install-fix-pass-task.py`) reads it
every half hour and the VS Code task passes `--mode dispatch` by hand. `off` writes one
line to the artifact and exits; `plan` writes the whole plan and sends nothing; the
wiring is complete either way, so turning it on is one setting rather than a change.

Scheduled runs use `claude-bg`: a tab is a window, and the scheduler has no desktop to
put one on. The artifact is `logs/fix-pass.log` in the devkit checkout, overwritten per
pass, per the failure-artifact rule in `.claude/rules/engineering.md`.

Every decision is pure and lives in the modules above; what is here is the wiring, and
`tests/test_fix_pass.py` drives it with every subprocess replaced.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import adoption_prs
import agent_models
import broken_pr_menu as menu
import devkit_project
import fix_backlog
import fix_cycle
import fix_plan
import gate_evidence
import ship_intent
import sweep
import task_branch as tb
import worktree
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The dispatch half, loaded by path because the file is hyphenated. Its runner is
# replaced with the window-less one below, so a scheduled pass opens nothing visible.
fix_prs = load_by_path("fix_prs", REPO_ROOT / "scripts" / "fix-prs.py")
# The interpreter resolver the push gate uses: the tree's venv, or the checkout's when
# the tree has none, so `ship.py --fix` runs with the project's own pre-commit.
push_gate = load_by_path("run_push_gate", REPO_ROOT / "scripts" / "precommit" / "run_push_gate.py")

ARTIFACT = Path("logs") / "fix-pass.log"
SCHEDULED_AGENT = "claude-bg"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# Every CLI the pass spawns before it can say anything. A missing one surfaced as a bare
# `FileNotFoundError: [WinError 2]` from deep inside `gate_evidence`, naming no program:
# on a fresh machine `gh` had been installed after VS Code started, and a task inherits
# the PATH VS Code launched with. Each is probed by running it, because found is not
# enough: on Windows `python3` is often only the Store alias, which exits 9009 -- and
# every git hook and pre-commit script entry runs through it, so a pass on such a machine
# dies twice, as a refused commit and as a worktree it cannot cut.
REQUIRED_TOOLS = {"git": ("--version",), "gh": ("--version",), "python3": ("-c", "")}


def runs(argv: list[str]) -> bool:
    try:
        probe = subprocess.run(
            argv, capture_output=True, check=False, creationflags=sweep.NO_WINDOW
        )
    except OSError:
        return False
    return probe.returncode == 0


def missing_tools() -> list[str]:
    return [tool for tool, args in REQUIRED_TOOLS.items() if not runs([tool, *args])]


def write_artifact(text: str, root: Path | None = None) -> Path:
    path = (root or REPO_ROOT) / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


# --- the steps ----------------------------------------------------------------------------


def ship_intents(
    root: Path, projects: list[str], mode: str
) -> tuple[list[str], list[fix_plan.Failure]]:
    """Step 1. `(lines for the record, refused commits as failures)`."""
    lines: list[str] = []
    refused: list[fix_plan.Failure] = []
    for intent in ship_intent.find_intents(root, projects):
        where = f"{intent.project} {intent.branch}"
        if intent.blocked:
            # Work a session left where no PR can be opened from: said, never shipped.
            lines.append(
                f"{where} -- NOT shipped: {intent.blocked}; move the work to a task branch "
                f"(agent-worktree.py new) and leave the intent there"
            )
            continue
        if mode != fix_cycle.DISPATCH:
            lines.append(f"{where} -- would ship: {intent.subject}")
            continue
        base = tb.detect_default_branch(sweep.git_for(intent.tree), fallback="main")
        python = push_gate.interpreter(intent.tree)
        outcome = ship_intent.ship_one(intent, python, base)
        lines.append(f"{where} -- {outcome.stage}: {outcome.detail}")
        if outcome.stage == ship_intent.REFUSED:
            refused.append(ship_intent.refusal_failure(outcome, base))
    return lines, refused


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


def collect_red(
    workspace: Path, projects: list[str], refused: list[fix_plan.Failure]
) -> tuple[list[fix_plan.Failure], bool | None]:
    """Step 2. Everything red, and devkit's default-branch verdict (None: unreadable).

    Each default branch's own gate is read beside the PRs: a red one is a failure to
    send a session at (devkit's is the harness itself), not only a reason to hold. The
    harness-defect ledger's open backlog rides along as one failure of its own.
    """
    found = menu.scan(workspace)
    branches = gate_evidence.collect_default_branches(workspace, projects)
    failures = refused + gate_evidence.collect(workspace, found)
    failures += [failure for _, failure in branches.values() if failure]
    devkit_dir = workspace.parent / fix_cycle.DEVKIT
    if devkit_dir.is_dir():
        backlog = fix_backlog.ledger_failure(devkit_dir, gate_evidence.evidence_root(workspace))
        failures += [backlog] if backlog else []
    green, _ = branches.get(fix_cycle.DEVKIT, (None, None))
    return failures, green


def merge_green_adoptions(root: Path, projects: list[str]) -> list[str]:
    """Step 5. The one merge the pass makes; `(lines for the record)`."""
    merged: list[str] = []
    prefixes = adoption_prs.adoption_prefixes()
    for name in projects:
        project_dir = root / name
        if name == fix_cycle.DEVKIT or not project_dir.is_dir():
            continue
        gh = sweep.gh_for(project_dir)
        listed = gh(
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,headRefName,isDraft,labels,mergeable,statusCheckRollup",
        )
        try:
            rows = json.loads(listed.stdout or "[]") if listed.returncode == 0 else []
        except ValueError:
            rows = []
        for row in fix_cycle.green_adoptions(rows, prefixes, sweep.AUTOMERGE_LABEL):
            ok, message = worktree.merge_pr(gh, int(row.get("number", 0)))
            merged.append(
                f"{name} #{row.get('number')} -- {message if ok else 'FAILED: ' + message}"
            )
    return merged


def update_branch(failure: fix_plan.Failure, root: Path) -> int:
    """An `UPDATE`: merge the base into the PR on GitHub, so its gate re-runs as-is now.

    No session and no worktree. A PR that comes back green is done; one still red at
    the new sha is a new ledger key and gets its session next pass; one GitHub cannot
    update (a conflict) reads `CONFLICTING` next pass and goes to the resolver.
    """
    done = sweep.gh_for(root / failure.project)("pr", "update-branch", str(failure.number))
    if done.returncode != 0:
        why = (done.stderr or done.stdout or "").strip().splitlines()
        print(
            f"  {failure.project} #{failure.number}: update-branch failed: {why[-1] if why else '?'}"
        )
        return EXIT_FAILED
    print(f"  {failure.project} #{failure.number}: branch updated; the gate re-runs")
    return EXIT_OK


def dispatch(decision: fix_plan.Decision, root: Path, launch: agent_models.Launch) -> int:
    first = decision.failures[0]
    if decision.action == fix_plan.UPDATE:
        return update_branch(first, root)
    on_branch = first.kind in (fix_plan.PR, fix_plan.COMMIT)
    if decision.action in (fix_plan.DISPATCH, fix_plan.RESOLVE) and on_branch:
        return fix_prs.dispatch_pr(first, root, launch, ship_intent.run_quiet)
    return fix_prs.dispatch_fresh(decision, root, launch, ship_intent.run_quiet)


def send_all(
    go: list[fix_plan.Decision],
    ledger_path: Path,
    root: Path,
    mode: str,
    launch: agent_models.Launch,
    now: _dt.datetime,
) -> tuple[list[str], list[tuple[fix_plan.Decision, str]], int]:
    """Steps 3 and 4: what the phase let through, each under the ledger and the caps.

    `(sent lines, capped decisions with why, worst exit code)`. The ledger is written
    only for a dispatch that opened; one that failed to is not something the next pass
    should be told already happened.
    """
    ledger = fix_plan.read_ledger(ledger_path)
    sent: list[str] = []
    capped: list[tuple[fix_plan.Decision, str]] = []
    worst = EXIT_OK
    for decision in go:
        names = ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)
        if when := fix_plan.already_sent(decision, ledger):
            capped.append((decision, f"already dispatched at {when}"))
            continue
        ok, why = fix_cycle.within_caps(decision, ledger, now)
        if not ok:
            capped.append((decision, why))
            continue
        if mode != fix_cycle.DISPATCH:
            would = "would update the branch" if decision.action == fix_plan.UPDATE else None
            sent.append(f"{names} -- {would or f'would send ({decision.action})'}")
            continue
        if dispatch(decision, root, launch) == EXIT_OK:
            fix_plan.record(ledger_path, fix_plan.decision_key(decision), decision.note, now)
            ledger = fix_plan.read_ledger(ledger_path)
            sent.append(f"{names} -- {decision.action}")
        else:
            sent.append(f"{names} -- FAILED to open a session")
            worst = EXIT_FAILED
    return sent, capped, worst


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
        # every half hour it would otherwise erase the record a manual pass just wrote,
        # which is the one thing worth reading during the manual week.
        if not (REPO_ROOT / ARTIFACT).is_file():
            write_artifact(f"fix-pass: mode=off -- set {fix_cycle.SETTING} to plan or dispatch")
        return EXIT_OK
    root = workspace.parent
    projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))

    shipped, refused = ship_intents(root, projects, mode)
    failures, green = collect_red(workspace, projects, refused)
    newest = gate_evidence.newest_release(root / fix_cycle.DEVKIT)
    decisions = fix_plan.plan(
        failures, tb.slugify(newest) if newest else "", adoption_prs.adoption_prefixes()
    )
    classes = fix_cycle.classify_all(failures)
    harness = fix_cycle.harness_state(classes, green, pending_adoptions(root, projects, newest))
    go, held = fix_cycle.phase(decisions, classes, harness)
    skipped = [d for d in decisions if d.action == fix_plan.SKIP]

    ledger_path = worktree.boxes_root(root) / fix_plan.LEDGER_NAME
    sent, capped, worst = send_all(go, ledger_path, root, mode, launch, now)

    merged = merge_green_adoptions(root, projects) if mode == fix_cycle.DISPATCH else []
    text = fix_cycle.render(
        fix_cycle.Account(
            mode,
            harness,
            tuple(shipped),
            tuple(go),
            tuple(held),
            tuple(capped),
            tuple(sent),
            tuple(merged),
            tuple(skipped),
        )
    )
    print(text)
    print(f"fix-pass: record at {write_artifact(text)}")
    return worst


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
    # which is the only defensible default for a run nobody is at the keyboard for.
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
            f"not usable from this PATH: {', '.join(missing)} -- install it (scripts/bootstrap-machine.ps1 "
            f"-Yes does), then fully restart VS Code: a task inherits the PATH VS Code "
            f"started with"
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
        # died on a TypeError, and the artifact still described the previous pass.
        write_artifact(f"fix-pass: CRASHED -- {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    sys.exit(main())
