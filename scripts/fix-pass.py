#!/usr/bin/env python3
"""The fix pass: ship what sessions finished, then send fixers at what is red, in order.

One pass, whether a click or the scheduler started it:

1. **Ship every intent.** A session that is done leaves `logs/ship-intent.md` in its
   worktree and nothing else (`ship_intent.py`). The pass runs the fixers, commits with
   that message, pushes with the push gate skipped, opens the PR with the label and
   records the outcome. A refused commit becomes a failure like any other.
2. **Collect everything red.** Refused commits, red PRs and open scheduled-failure
   issues, each with the gate's own artifact (`gate_evidence.py`), planned by
   `fix_plan.py` and classified by `fix_cycle.py`.
3. **Harness first.** While anything harness-shaped is red -- a vendored test, a shared
   signature, devkit's own gate, a release mid-adoption -- one devkit session gets the
   whole set and every project fixer is held, out loud.
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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import adoption_prs
import broken_pr_menu as menu
import devkit_project
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


def dispatch(decision: fix_plan.Decision, root: Path, agent: str) -> int:
    first = decision.failures[0]
    on_branch = first.kind in (fix_plan.PR, fix_plan.COMMIT)
    if decision.action in (fix_plan.DISPATCH, fix_plan.RESOLVE) and on_branch:
        return fix_prs.dispatch_pr(first, root, agent, ship_intent.run_quiet)
    return fix_prs.dispatch_fresh(decision, root, agent, ship_intent.run_quiet)


# --- the pass -----------------------------------------------------------------------------


def run(workspace: Path, mode: str, agent: str, now: _dt.datetime | None = None) -> int:
    now = now or _dt.datetime.now(_dt.UTC)
    if mode == fix_cycle.OFF:
        write_artifact(f"fix-pass: mode=off -- set {fix_cycle.SETTING} to plan or dispatch")
        return EXIT_OK
    root = workspace.parent
    projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))

    shipped, refused = ship_intents(root, projects, mode)
    found = menu.scan(workspace)
    failures = refused + gate_evidence.collect(workspace, found)
    newest = gate_evidence.newest_release(root / fix_cycle.DEVKIT)
    decisions = fix_plan.plan(
        failures, tb.slugify(newest) if newest else "", adoption_prs.adoption_prefixes()
    )
    classes = fix_cycle.classify_all(failures)
    devkit_dir = root / fix_cycle.DEVKIT
    green = (
        gate_evidence.default_branch_green(
            sweep.gh_for(devkit_dir), tb.detect_default_branch(sweep.git_for(devkit_dir))
        )
        if devkit_dir.is_dir()
        else None
    )
    harness = fix_cycle.harness_state(classes, green, pending_adoptions(root, projects, newest))
    go, held = fix_cycle.phase(decisions, classes, harness)

    ledger_path = worktree.boxes_root(root) / fix_plan.LEDGER_NAME
    ledger = fix_plan.read_ledger(ledger_path)
    sent: list[str] = []
    capped: list[tuple[fix_plan.Decision, str]] = []
    worst = EXIT_OK
    for decision in go:
        names = ", ".join(f"{f.project} #{f.number}" for f in decision.failures)
        if when := fix_plan.already_sent(decision, ledger):
            capped.append((decision, f"already dispatched at {when}"))
            continue
        ok, why = fix_cycle.within_caps(decision, ledger, now)
        if not ok:
            capped.append((decision, why))
            continue
        if mode != fix_cycle.DISPATCH:
            sent.append(f"{names} -- would send ({decision.action})")
            continue
        code = dispatch(decision, root, agent)
        if code == EXIT_OK:
            fix_plan.record(ledger_path, fix_plan.decision_key(decision), decision.note, now)
            ledger = fix_plan.read_ledger(ledger_path)
            sent.append(f"{names} -- {decision.action}")
        else:
            sent.append(f"{names} -- FAILED to open a session")
            worst = EXIT_FAILED

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
    try:
        return run(workspace, mode, agent)
    except (menu.FixError, worktree.WorktreeError, devkit_project.ProjectError) as exc:
        print(f"fix-pass: {exc}", file=sys.stderr)
        write_artifact(f"fix-pass: FAILED -- {exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
