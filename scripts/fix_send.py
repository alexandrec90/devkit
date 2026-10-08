#!/usr/bin/env python3
"""The fix pass's dispatch half: what goes, under the budget, and how it is sent.

Split out of `fix-pass.py`, which had reached its `file_lines` ceiling on three
changes in a row -- by the engineering rule a defect report, not a raise. The seam is
the one its imports already showed: everything here is `fix_budget`, `fix_ledger` and
`fix-prs.py`, and nothing in the rest of the pass touches those.

Tested through the pass in `tests/test_fix_pass.py`.
"""

from __future__ import annotations

import io
import json
import re
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import agent_models
import branch_facts
import fix_budget
import fix_findings
import fix_ledger
import fix_loop
import fix_plan
import host_memory
import release
import ship_intent
import sweep
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The dispatch half of the click task, loaded by path because the file is hyphenated.
# Its runner is the pass's window-less one, so a scheduled pass opens nothing visible.
fix_prs = load_by_path("fix_prs", REPO_ROOT / "scripts" / "fix-prs.py")

Journal = fix_findings.Journal

EXIT_OK = 0
EXIT_FAILED = 1
# EX_TEMPFAIL: the pass's own code moved under it, so it sent no one. The watchdog
# fast-forwards and runs it again; any other caller simply runs it again.
EXIT_STALE = 75

# The commit each checkout's code was loaded from, pinned by `pin_loaded` as a pass
# starts. The reconcile job fast-forwards the static checkout every 15 minutes, so a HEAD
# read mid-pass can already be the new code while the modules in memory are the old:
# 0b9c6b88 crashed in `send` 39s after one, on the very bug that fast-forward had brought
# #416's fix for, and `code_moved` read HEAD == origin as "current".
LOADED_FROM: dict[str, str] = {}


def pin_loaded(root: Path) -> None:
    """Record `root`'s HEAD as the commit this process's modules came from."""
    head = sweep.git_for(root)("rev-parse", "HEAD")
    if head.returncode == 0 and head.stdout.strip():
        LOADED_FROM[str(root)] = head.stdout.strip()


def code_moved(root: Path) -> str:
    """`old..new` when `origin/<default>` changed `scripts/` since the code was loaded.

    The watchdog fast-forwards the checkout once, before the pass; anything merged after
    that routes with the code the pass started on. carameli #395 reached a devkit session
    that way, 21s before #407 -- the routing fix that would have sent it to carameli --
    merged. `old` is the pinned commit (`LOADED_FROM`), else HEAD. Only a checkout the
    watchdog would have updated is judged: a linked worktree, a branch with commits of
    its own and any git failure read as not moved.
    """
    if (root / ".git").is_file():
        return ""
    git = sweep.git_for(root)
    head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip()
    base = head.removeprefix("origin/") or "main"
    if git("fetch", "--quiet", "origin", base).returncode != 0:
        return ""
    loaded = LOADED_FROM.get(str(root)) or git("rev-parse", "HEAD").stdout.strip()
    old, new = loaded, git("rev-parse", f"origin/{base}").stdout.strip()
    if not old or not new or old == new:
        return ""
    if git("merge-base", "--is-ancestor", old, new).returncode != 0:
        return ""
    changed = git("diff", "--name-only", old, new, "--", "scripts/")
    return f"{old[:9]}..{new[:9]}" if changed.returncode == 0 and changed.stdout.strip() else ""


def hold_if_moved(
    go: list[fix_plan.Decision], held: list[tuple[fix_plan.Decision, str]], ctx: fix_loop.Context
) -> tuple[list[fix_plan.Decision], list[tuple[fix_plan.Decision, str]], str]:
    """`(go, held, moved)`: every decision held when `code_moved`, none when not.

    Only a dispatching pass asks; a plan fetches nothing and routes nobody.
    """
    moved = code_moved(REPO_ROOT) if ctx.writes else ""
    if not moved:
        return go, held, ""
    return (
        [],
        [*held, *((d, f"devkit's scripts/ moved {moved} mid-pass; rerun") for d in go)],
        moved,
    )


# A background fixer measured ~450 MB (the session and its pty host) once it loads no
# MCP server; the floor leaves the person at the machine room for their own work.
SESSION_MB = 500
MEMORY_FLOOR_MB = 2048
HELD_FOR_MEMORY = "held for memory"
# Why a session is not launched by a pass past its deadline (`fix-pass.SEND_RESERVE_SECONDS`
# before the watchdog's stop). A launch provisions a tree and starts a session, 1-2.5
# minutes each on 2026-10-08, and the pass stopped after its third had no record written.
HELD_FOR_TIME = "held for time"


def update_branch(failure: fix_plan.Failure, root: Path) -> int:
    """An `UPDATE`: merge the base into the PR on GitHub, so its gate re-runs as-is now.

    No session and no worktree. A PR that comes back green is done; one still red at
    the new sha is a new ledger key and gets its session next pass; one GitHub cannot
    update (a conflict) reads `CONFLICTING` next pass and goes to the resolver. One that
    only GitHub calls conflicting (`merges_clean`) is merged here instead: GitHub would
    refuse to update it on that same verdict.
    """
    if failure.merges_clean:
        return push_clean_merge(failure, root)
    gh = sweep.gh_for(root / failure.project)
    done = gh("pr", "update-branch", str(failure.number))
    if done.returncode != 0 and (state := _state(gh, failure.number)) not in ("", "OPEN"):
        # Closed or merged since the pass read it: "update-failed #390" was filed 27s
        # after #390 closed, and a sweep spent two calls finding that out.
        print(f"  {failure.project} #{failure.number}: {state.lower()} meanwhile; nothing to do")
        return EXIT_OK
    if done.returncode != 0:
        why = (done.stderr or done.stdout or "").strip().splitlines()
        print(
            f"  {failure.project} #{failure.number}: update-branch failed: {why[-1] if why else '?'}"
        )
        return EXIT_FAILED
    print(f"  {failure.project} #{failure.number}: branch updated; the gate re-runs")
    return EXIT_OK


def push_clean_merge(failure: fix_plan.Failure, root: Path) -> int:
    """Merge `origin/<base>` into the PR's head in the checkout's object store and push the
    merge commit to the head branch: what a resolver sent at #538 did by hand.

    No worktree: `merge-tree` writes the tree and `commit-tree` the commit, both against
    the refs `gate_evidence` judged clean. The push is a fast-forward of the head sha the
    pass read, so a head pushed to meanwhile is refused rather than overwritten.

    The push skips the pre-push gate, as every other push the pass makes does
    (`release.push_env`). Pushed from the project's own checkout, that gate ran the whole
    suite over whatever that working copy held, not over the merge: #538's push spent six
    minutes in it and came back an `update-failed` naming no cause. CI gates the merge.
    """
    git = sweep.git_for(root / failure.project)
    where = f"  {failure.project} #{failure.number}"
    tree = branch_facts.clean_merge(git, failure.base, failure.sha)
    if not tree:
        print(f"{where}: no longer merges cleanly with origin/{failure.base}")
        return EXIT_FAILED
    message = f"Merge origin/{failure.base} into {failure.head}"
    base = f"refs/remotes/origin/{failure.base}"
    made = git("commit-tree", tree, "-p", failure.sha, "-p", base, "-m", message)
    commit = (made.stdout or "").strip() if made.returncode == 0 else ""
    pushed = (
        git(
            "push",
            "--quiet",
            "origin",
            f"{commit}:refs/heads/{failure.head}",
            env=release.push_env(),
        )
        if commit
        else made
    )
    if not commit or pushed.returncode != 0:
        why = (pushed.stderr or pushed.stdout or "").strip().splitlines()
        print(f"{where}: pushing the merge failed: {why[-1] if why else '?'}")
        return EXIT_FAILED
    print(f"{where}: GitHub said conflicting; pushed git's clean merge, the gate re-runs")
    return EXIT_OK


def _state(gh, number: int) -> str:
    """The PR's state (`OPEN`, `CLOSED`, `MERGED`), or "" when it cannot be read."""
    done = gh("pr", "view", str(number), "--json", "state")
    try:
        return str(json.loads(done.stdout or "{}").get("state", "")) if done.returncode == 0 else ""
    except (ValueError, AttributeError):
        return ""


def dispatch(
    decision: fix_plan.Decision, root: Path, launch: agent_models.Launch, problem: str = ""
) -> int:
    first = decision.failures[0]
    if decision.action == fix_plan.UPDATE:
        return update_branch(first, root)
    if decision.action == fix_plan.RERUN:
        return fix_prs.rerun_workflow(first, root)
    key = fix_ledger.decision_key(decision)
    on_branch = first.kind in (fix_plan.PR, fix_plan.COMMIT)
    if decision.action in (fix_plan.DISPATCH, fix_plan.RESOLVE) and on_branch:
        code = fix_prs.dispatch_pr(first, root, launch, ship_intent.run_quiet, key, problem)
        if code == EXIT_OK and first.kind == fix_plan.COMMIT and first.tree:
            # The refused intent is the fixer's to earn again: with it gone, the tree
            # is a session still working until the fixer ships, and the next pass does
            # not re-run the commit stage over its half-made edits.
            ship_intent.set_aside(Path(first.tree), ship_intent.REFUSED_FILE)
        return code
    return fix_prs.dispatch_fresh(decision, root, launch, ship_intent.run_quiet, key, problem)


def send_all(
    go: list[fix_plan.Decision],
    ctx: fix_loop.Context,
    launch: agent_models.Launch,
    journal: Journal | None = None,
    closed: fix_loop.Closed | None = None,
    items: list | None = None,
    left: Callable[[], float] | None = None,
) -> tuple[list[str], list[tuple[fix_plan.Decision, str]], int]:
    """Steps 5 and 6: what the phase let through, each under `fix_budget.budget`.

    `(sent lines, waiting decisions with why, worst exit code)`. The ledger is written
    only for a dispatch that opened. `items` is the harness-defect ledger, for what
    became of each problem's escalation; `closed` says which devkit session is still
    working, which holds another, and which tree each problem's fixer worked in, which
    is what an escalation names. `left` is the seconds before the pass's send deadline:
    at or past it, no session is launched (`HELD_FOR_TIME`).
    """
    closed = closed or fix_loop.Closed()
    ledger = fix_ledger.read_ledger(ctx.ledger_path)
    sent: list[str] = []
    capped: list[tuple[fix_plan.Decision, str]] = []
    worst = EXIT_OK
    room = host_memory.available_mb()
    for decision in go:
        names = ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)
        if why := _occupied(decision, closed):
            capped.append((decision, why))
            continue
        problem = fix_ledger.problem_key(decision)
        escalated = fix_loop.fix_findings.escalation(problem, items or [])
        verdict = fix_budget.budget(decision, ledger, ctx.now, escalated)
        if verdict.finding and journal is not None:
            journal.add(verdict.finding.at(closed.trees.get(problem, "")))
        why = verdict.why if not verdict.go else _no_memory(decision, room)
        if why := why or _no_time(decision, left):
            capped.append((decision, why))
            continue
        if room is not None and decision.action not in fix_plan.NO_SESSION:
            room -= SESSION_MB
        line, code = _send_one(decision, ctx, launch, verdict.effort, journal)
        sent.append(f"{names} -- {line}")
        worst = max(worst, code)
        ledger = fix_ledger.read_ledger(ctx.ledger_path)
    return sent, capped, worst


def _no_memory(decision: fix_plan.Decision, room: int | None) -> str:
    """Why the machine cannot take this session now, or "". Not a cap on sessions --
    the pass has none -- but the one limit the machine sets regardless: round four sent
    nine at once and Claude Code killed the supervisor for low memory. A held decision
    is not recorded, so the next pass sends it; one held too long is a stale wait."""
    if room is None or decision.action in fix_plan.NO_SESSION:
        return ""
    if room - SESSION_MB >= MEMORY_FLOOR_MB:
        return ""
    return (
        f"{HELD_FOR_MEMORY}: {room} MB free, a session needs {SESSION_MB} above {MEMORY_FLOOR_MB}"
    )


def _no_time(decision: fix_plan.Decision, left: Callable[[], float] | None) -> str:
    """Why this pass launches no more sessions, or "": its send deadline has passed. An
    update or a rerun opens no session and is one call, so it is never held for time."""
    if left is None or decision.action in fix_plan.NO_SESSION or left() > 0:
        return ""
    return f"{HELD_FOR_TIME}: this pass is past its send deadline; the next pass sends it"


def _occupied(decision: fix_plan.Decision, closed: fix_loop.Closed) -> str:
    """Why a session is already where this one would go, or "": the one devkit session
    at the harness, or any live session in the branch's own tree.

    The devkit session opens a fresh devkit tree (`dispatch_fresh`), so no branch tree
    is in its way: a consumer's refused intent folded into it was capped by the idle
    session in that intent's tree on every pass, and the backlog grew with none sent.
    """
    if decision.action == fix_plan.UPSTREAM:
        if closed.harness_busy:
            return f"held until the devkit session in {closed.harness_busy} finishes"
        named = named_by(decision, closed.devkit_fixes)
        return f"pending {named}, which names every failure it is for" if named else ""
    first = decision.failures[0]
    tree = (
        closed.busy.get((first.project, first.head))
        if first.kind in (fix_plan.PR, fix_plan.COMMIT)
        else None
    )
    return f"a session is working in {tree}" if tree else ""


def named_by(decision: fix_plan.Decision, fixes: tuple[tuple[int, str], ...]) -> str:
    """The devkit PRs (`fix_loop.devkit_fixes`) whose bodies name every failure of
    `decision` by URL, as `devkit #N, #M`; "" when any one is named by none.

    2026-10-01: a devkit session was sent at roguelike #52 and social-scraper #23 while
    #483, whose body named both as what it unblocks, was open, and again once it had
    merged and was waiting on adoption. The ledger's backlog rides in the session without
    deciding whether it goes (`fix_cycle.harness_state`), so it is not asked.
    """
    urls = [f.url for f in decision.failures if f.kind != fix_plan.LEDGER]
    if not urls or not all(urls):
        return ""
    found: list[int] = []
    for url in urls:
        whole = re.compile(re.escape(url.rstrip("/")) + r"(?![\w/])")  # not #5 inside #52
        naming = [number for number, body in fixes if whole.search(body)]
        if not naming:
            return ""
        found += [n for n in naming if n not in found]
    return "devkit " + ", ".join(f"#{n}" for n in sorted(found))


def _send_one(
    decision: fix_plan.Decision,
    ctx: fix_loop.Context,
    launch: agent_models.Launch,
    effort: str,
    journal: Journal | None,
) -> tuple[str, int]:
    """Dispatch one decision the budget let through; `(record line, exit code)`.

    The ledger is written only for a dispatch that opened; one that did not is filed.
    """
    if not ctx.writes:
        would = {fix_plan.UPDATE: "update the branch", fix_plan.RERUN: "re-run the workflow"}
        return f"would {would.get(decision.action, f'send ({decision.action})')}", EXIT_OK
    how = agent_models.Launch(launch.agent, launch.model, effort) if effort else launch
    problem = fix_ledger.problem_key(decision)
    code, said = _saying(dispatch, decision, ctx.root, how, problem)
    if code != EXIT_OK:
        first = decision.failures[0]
        names = ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)
        detail, kind = f"{names}: {decision.note[:160]}", f"{decision.action}-failed"
        # Why it failed is only ever printed, and a scheduled pass's streams go nowhere:
        # the finding for #538 named no step until its push was reproduced by hand.
        where = ""
        if journal is not None and said.strip():
            where = fix_findings.evidence_file(f"{detail}\n\n{said}", journal.devkit_dir, kind)
        fix_findings.file(journal, kind, first.project, detail, evidence=where)
        return f"FAILED to {decision.action}", EXIT_FAILED
    key = fix_ledger.decision_key(decision)
    fix_ledger.record(
        ctx.ledger_path,
        key,
        decision.note,
        ctx.now,
        problem=problem,
        source=fix_budget.source(decision),
    )
    return decision.action + (f" at effort {effort}" if effort else ""), EXIT_OK


class _Tee(io.TextIOBase):
    """A stream that keeps what is written to it and passes it on, when there is an on."""

    def __init__(self, on, kept: io.StringIO) -> None:
        self.on, self.kept = on, kept

    def write(self, text: str) -> int:
        self.kept.write(text)
        if self.on is not None:  # None under `pythonw.exe`, which the scheduled pass is
            self.on.write(text)
        return len(text)

    def flush(self) -> None:
        if self.on is not None:
            self.on.flush()


def _saying(fn, *args) -> tuple[int, str]:
    """`(fn(*args), what it printed to either stream)`; the printing still happens."""
    kept = io.StringIO()
    with redirect_stdout(_Tee(sys.stdout, kept)), redirect_stderr(_Tee(sys.stderr, kept)):
        code = fn(*args)
    return code, kept.getvalue()
