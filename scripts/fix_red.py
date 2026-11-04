#!/usr/bin/env python3
"""Step 3 of the fix pass: everything red, and a gate re-run where nothing could be read.

`collect_red` gathers every PR's failure, every default branch's own, and the
harness-defect backlog. A default branch with no verdict at its tip gets its gate
re-run (`regate`). The usual cause is a merge by the auto-merge workflow: a push made
with `GITHUB_TOKEN` raises no `push` event, so nothing gates the new tip, and devkit's
unreadable main held every project PR on every pass after. A `workflow_dispatch` is the
one event that token may raise, and the run it starts is on the branch, so the next pass
reads it like any other.

Cut out of `fix-pass.py`, which was at its `file_lines` and `imports` ceilings, and
`gate_evidence.py`, at its `definitions` one: that module reads what a gate said, and
the re-run is the one thing the pass does *to* a gate.

Tested in `tests/test_fix_red.py`, and through the pass in `tests/test_fix_pass.py`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import broken_pr_menu as menu
import e2e_results
import fix_backlog
import fix_cycle
import fix_findings
import fix_loop
import fix_plan
import gate_evidence
import sweep
import task_branch as tb

# A gate GitHub refused to start over the account's billing (`fix_plan.is_billing_refused`):
# one group for the account, whichever checkouts it hit, naming the step only its owner
# can take, so it awaits that person (`triage.awaiting_person`) and the morning toast says
# so, rather than the plan's skip line being the only place it is written.
BILLING_KIND = "actions-billing-refused"
BILLING_DETAIL = (
    "GitHub refuses to start the account's Actions jobs -- "
    f"{fix_findings.triage.PERSON_STEP} {fix_plan.BILLING_STEP}"
)


def collect_red(
    workspace: Path, projects: list[str], refused: list[fix_plan.Failure]
) -> tuple[list[fix_plan.Failure], bool | str | None, list[str]]:
    """Everything red, devkit's default-branch verdict (None: unreadable), and the
    checkouts whose default-branch verdict could not be read.

    devkit's verdict is its gate's alone: a red local run is a failure to fix, not a
    reason to hold every project behind devkit's main the way a red gate is.

    Each default branch's own gate is read beside the PRs: a red one is a failure to
    send a session at (devkit's is the harness itself), not only a reason to hold. The
    harness-defect backlog is `backlog_failure`'s, read separately so that a crash here
    is still something the devkit session is sent at.
    """
    found = menu.scan(workspace, projects)
    branches = gate_evidence.collect_default_branches(workspace, projects)
    failures = refused + gate_evidence.collect(workspace, found)
    failures += [failure for _, failure in branches.values() if failure]
    # After the branches: their read fetched origin, which the local runs' tips are read
    # against. The tests no hosted runner can run, red on this machine (`e2e_results`).
    failures += e2e_results.collect(workspace, projects, sweep.git_for)
    green, _ = branches.get(fix_cycle.DEVKIT, (None, None))
    unread = [name for name, (verdict, _) in branches.items() if verdict is None]
    return failures, green, unread


def billing(
    ctx: fix_loop.Context, journal: fix_findings.Journal, skipped: list[fix_plan.Decision]
) -> list[str]:
    """File `BILLING_KIND` when the plan skipped a gate over billing; else retire an open
    one, as of this pass: lines for the record.

    Nothing else retires it -- the person who settled the bill rarely writes the
    resolution, as with a login (`fix_loop.retire_login`). Not after a collect step that
    raised, which read no gate and so proves nothing about the account.
    """
    refused = [f for d in skipped for f in d.failures if fix_plan.is_billing_refused(f.signature)]
    if refused:
        journal.add(
            fix_findings.Finding(BILLING_KIND, "devkit", BILLING_DETAIL, evidence=refused[0].url)
        )
        return []
    if "collect" in journal.crashed:
        return []
    marker = f"{BILLING_KIND}: "
    stale = [
        item
        for item in fix_findings.triage.open_items(
            fix_findings.triage.load(ctx.devkit_dir), (fix_findings.FINDING,)
        )
        if item.fields.get("detail", "").startswith(marker)
    ]
    if not stale:
        return []
    note = "no gate was refused over billing on this pass"
    if ctx.writes:
        fix_findings.triage.resolve([item.id for item in stale], note, root=ctx.devkit_dir)
    verb = "retired" if ctx.writes else "would retire"
    return [f"{verb} [{item.id}] -- {note}" for item in stale]


def backlog_failure(
    workspace: Path, in_flight: dict[str, str] | None = None
) -> fix_plan.Failure | None:
    """The harness-defect ledger's open backlog as one failure; None when it is empty.
    `in_flight` is `fix_loop.in_flight`'s: groups waiting on a merge send nothing."""
    devkit_dir = workspace.parent / fix_cycle.DEVKIT
    if not devkit_dir.is_dir():
        return None
    return fix_backlog.ledger_failure(devkit_dir, gate_evidence.evidence_root(workspace), in_flight)


# bc1ec6d1: one `HTTP 504` from the dispatch API was filed as a harness defect and sent a
# fixer at devkit over a GitHub hiccup. A server error or a dropped connection is retried
# after each of these pauses; a refusal (a 4xx) is not, since asking again changes nothing.
REGATE_RETRY_SECONDS = (10, 30)


def transient(said: str) -> bool:
    """Whether `gh` failed on GitHub's side or the network's, not on the request."""
    return sweep.transient(said)


def _last_line(done: object) -> str:
    why = (getattr(done, "stderr", "") or getattr(done, "stdout", "") or "").strip()
    return why.splitlines()[-1].strip() if why else "?"


# `ship_intent.SKIP_PUSH_GATE`, spelled here because this module imports nothing that
# ships; `tests/test_fix_red.py` holds the two equal.
SKIP_PUSH_GATE = "devkit-push-gate"


def seed_env() -> dict[str, str]:
    """The environment a seed push runs in: `new-project.scripted_setup_env`'s branch
    policy waiver -- this is the push that generator was making -- and the push gate
    skipped the way the pass skips it for every push, added to any `SKIP` already set:
    the push to the default branch raises the gate in CI, which is where it is read."""
    env = dict(os.environ)
    skipped = [s for s in env.get("SKIP", "").split(",") if s.strip()]
    env["SKIP"] = ",".join([*skipped, SKIP_PUSH_GATE])
    env["DEVKIT_SKIP_BRANCH_POLICY"] = "1"
    return env


def unseeded(gh) -> bool:
    """Whether the checkout's repository on GitHub holds no branch at all."""
    done = gh("repo", "view", "--json", "isEmpty", "--jq", ".isEmpty")
    return getattr(done, "returncode", 1) == 0 and (done.stdout or "").strip() == "true"


def seed(git, base: str) -> tuple[bool, str]:
    """Push the local `base` to an empty GitHub repo: `(pushed, what for the record)`.

    3120f1ce: `new-project.py`'s `gh repo create --push` made skinamp's repo and the run
    was ended before its push finished -- a second click on the task terminated it -- so
    GitHub held no branch, the dispatch 404ed, and the pass filed it on every run. Nothing
    is overwritten: an empty repo has no branch to lose, and the local one is what the
    generator was pushing.
    """
    if getattr(git("rev-parse", "--verify", "--quiet", f"refs/heads/{base}"), "returncode", 1):
        return False, f"{base} -- FAILED to seed the empty GitHub repo: no local {base}"
    done = git("push", "-u", "origin", base, env=seed_env())
    if getattr(done, "returncode", 1) == 0:
        return True, f"{base} -- GitHub held no branch; pushed {base}, which raises its gate"
    return False, f"{base} -- FAILED to seed the empty GitHub repo: {_last_line(done)}"


def regate(project_dir: Path) -> tuple[bool, str]:
    """Run the gate on the checkout's default branch: `(dispatched, what for the record)`.

    A failure still transient after every retry is said as *deferred*, without `FAILED`:
    the tip is still unread, so the next pass re-runs it, and `fix-pass.py` files only a
    `FAILED` line -- a GitHub outage is nothing a devkit session can fix. A 404 from a
    repo GitHub holds no branch of is a generator that stopped before its push, finished
    here (`seed`); asked only after the 404, so a healthy re-run costs no extra call.
    """
    git = sweep.git_for(project_dir)
    base = tb.detect_default_branch(git, fallback="main")
    gh = sweep.gh_for(project_dir)
    for pause in (*REGATE_RETRY_SECONDS, None):
        done = gh("workflow", "run", gate_evidence.GATE_WORKFLOW, "--ref", base)
        if getattr(done, "returncode", 1) == 0:
            return True, f"{base} -- no verdict at the tip; gate re-run"
        said = _last_line(done)
        if said.startswith("HTTP 404") and unseeded(gh):
            return seed(git, base)
        if not transient(said):
            return False, f"{base} -- FAILED to re-run the gate: {said}"
        if pause is not None:
            time.sleep(pause)
    tries = len(REGATE_RETRY_SECONDS) + 1
    return (
        False,
        f"{base} -- gate re-run deferred: GitHub answered {said} {tries} times; the next pass retries",
    )


def regate_unread(root: Path, unread: list[str], mode: str) -> tuple[list[str], set[str]]:
    """Re-run the gate wherever the default branch had no verdict: `(lines, re-run)`.

    An unreadable devkit main holds every project fixer, and nothing else ever makes it
    readable. A checkout re-run here is `RUNNING` for this pass, as after any merge.
    Outside `dispatch` mode it only says what it would do, and counts the checkout as
    re-run all the same: a rehearsal is read for what dispatch would hold, and it held
    every project fixer behind an unreadable devkit main that dispatch re-gates first.
    """
    lines: list[str] = []
    rerun: set[str] = set()
    for name in unread:
        if mode != fix_cycle.DISPATCH:
            lines.append(f"{name} -- would re-run the gate: no verdict at the tip")
            rerun.add(name)
            continue
        ok, line = regate(root / name)
        lines.append(f"{name} {line}")
        if ok:
            rerun.add(name)
    return lines, rerun
