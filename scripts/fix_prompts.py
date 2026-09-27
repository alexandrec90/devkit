#!/usr/bin/env python3
"""What each dispatched session is told, as one function per shape of red.

Split out of `fix_plan.py`, which decides *what* gets a session; this is the words.
Every prompt names the failure the way the record does (`fix_plan.name_of`), the
evidence directory, the branch the worktree is on and the finish line -- which is the
ship skill, that is, `logs/ship-intent.md`, never a commit or a push. A fixer fixes
and stops; everything a script can do -- merging the base in, committing, pushing,
opening or updating the PR, reading the gate -- the pass does, because a session's
turn spent on any of it is the expensive way to run a command. What a fixer cannot do
it says in `logs/fix-blocked.md` (`fix_reports.BLOCKED_FILE`), the one channel back.
The conflict prompt names no failure on purpose: the gate cannot have run against an
unmergeable head, and a resolver told "also fix the tests" fixes the wrong thing.

Stdlib only, no `gh`. Tested in `tests/test_fix_prompts.py`.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_worktrees as aw
from fix_plan import COMMIT, CONFLICT, EVIDENCE_DIR, LEDGER, PR, Failure, describe, name_of
from fix_reports import BLOCKED_FILE, FRICTION_FILE, REFUSED_FILE
from harness_triage import ARTIFACT as _TRIAGE_ARTIFACT
from junit_report import READABLE
from ship_intent import INTENT_FILE, REFUSAL_LINE, REFUSED, STATE_FILE, read_state

# How every prompt's body ends. The ship skill is the finish line and the blocked file
# is the only other way out, for the blockers `STOP` names right after it; both are
# files, so the pass reads the outcome without a session.
FINISH = (
    "When it is done, run the targeted tests with this tree's own .venv interpreter, the "
    "linter and the ratchets the gate "
    "runs -- python scripts/hooks/structure_check.py, python scripts/hooks/untested_symbols.py "
    "and, where the tree has it, python scripts/hot-budget.py for the instruction files "
    "-- in every tree you changed, then ship it with the ship skill and stop: the fix pass "
    "commits, pushes, opens or updates the PR and reads what the gate says. Nobody is "
    "watching this session, so never ask a question and never end on one: a choice "
    "between approaches is yours -- the option you would recommend is the decision, so do "
    "it and give the reason and the alternatives in the intent. Only if one of the "
    f"blockers below stands in the way, write {BLOCKED_FILE.as_posix()} naming it, in a "
    "sentence or two, and stop. "
    "Fix causes, not instances: whatever cost you turns is fixed where it comes from, so "
    "the next session cannot hit it -- a tree with no .venv means the provisioner that "
    "should have run is what gets fixed, with a test -- and a repair to this tree alone "
    "is a workaround. Write and edit files with the Write and Edit tools, never a shell "
    "heredoc or a patch script run through Bash: the Bash tool collapses backslashes in "
    "them, and the damage costs a test run to find. An append is an Edit that replaces "
    "the file's last lines with themselves plus the new text. "
    f"Either way, if the harness cost you turns -- a refusal, a missing tool, evidence "
    f"that was wrong or absent, an instruction that sent you the wrong way -- put one line "
    f"per thing in {FRICTION_FILE.as_posix()}: the pass files each for the devkit session, "
    f"which fixes it at the cause, and a cause that lives outside this repository is "
    f"fixed there, not worked around here. End a line whose cause you fixed yourself "
    f"with fixed on this branch: the pass then files it as settled by this branch, not "
    f"as a new job for a fixer."
)

# What the devkit session is told about the harness-defect ledger, when the backlog is
# among its failures. No quotes or backticks: the sentence crosses a `wt` command line.
LEDGER_STEPS = (
    " Work them as .claude/skills/triage-harness/SKILL.md says -- verify each against current code "
    "before believing it, fix what is real, and retire each group with "
    "python scripts/harness_triage.py --resolve-like ID --note WHAT-FIXED-IT --pr BRANCH "
    "once the fix is in your intent; the pass reopens a group whose branch never merges. "
    "A fixers-exhausted, blind-evidence or fixer-blocked group is a problem fixers could "
    "not move: fix what in the harness failed them, and fix the problem itself in the "
    "tree its evidence names, leaving an intent there too. Cut any tree this work needs "
    "with python scripts/agent-worktree.py new from this tree: it carries over this "
    "tree's logs/fix-origin, so that PR merges once green too. Leave its intent in that "
    "tree's logs/ship-intent.md exactly as here, and pass that tree's branch as --pr -- "
    "any repository's branch is accepted."
)

# Every prompt's one exit short of a fix. It used to read "if it cannot be fixed, stop
# and say what is in the way", and a fresh session treated any obstacle as "cannot":
# devkit #387's stopped with the fix one worktree away. So the exit names the only
# blockers that justify it and the obstacles that do not, and asks for the evidence.
# No quotes or backticks, for the same reason as above.
STOP = (
    "Stopping without a fix is for three blockers only: a refusal by the harness or a "
    "tool (quote the exact command), a credential, admin right or paid service only a "
    "person has -- never a decision, which is yours -- or a fix that must land outside "
    "this repository. An obstacle a session can clear itself "
    "is not one of them: adding a worktree on another branch, fetching or merging a ref, "
    "reproducing the failure, reading code outside the diff, reshaping code to fit a "
    f"limit. If you write {BLOCKED_FILE.as_posix()}, name which of the three it is and "
    "what you tried first."
)

# Every prompt's first sentence: the role, declared by the one thing that starts a
# fixer, so no session has to infer it. The vendored rules are written for project
# sessions; the two that would otherwise steer a fixer wrong -- the harness guardrail in
# engineering.md and session-scope.md -- each point at .claude/fixer.md for this role.
# The override is spelled here as well as in that file, so a consumer that has not
# pulled the file yet still gets it.
ROLE = (
    "You are a fixer session, dispatched by the fix pass. Read .claude/fixer.md first if "
    "this checkout has it: where it or this prompt differs from a rule written for "
    "project sessions -- the harness is not your job, report a dead end and stop -- "
    "this prompt and that file win."
)


def _framed(body: str) -> str:
    """The body between the role that opens every prompt and the exit that closes it."""
    return f"{ROLE} {body} {STOP}"


def _ids(sig: tuple[str, ...]) -> str:
    return ", ".join(entry for entry in sig if entry != CONFLICT) or "see the run"


def _logs(failure: Failure) -> str:
    """Where the evidence is -- and, when none came down, that none did.

    Two sessions were told the logs were under `logs/gate/` and spent turns finding the
    directory absent: the run had aged out, or uploaded nothing. Saying so is cheaper.
    """
    if failure.evidence:
        return (
            f"The gate's own logs are in {EVIDENCE_DIR}/ in this worktree -- read "
            f"{_first_read(Path(failure.evidence))} there first. {LINUX}"
        )
    where = failure.url or "the run"
    return f"No artifact came down from the run; read it at {where} first. {LINUX}"


def _first_read(evidence: Path) -> str:
    """`READABLE` when a junit report named a failure, else the `.log` files that came down.

    8f622cc6: devkit's own suite reports only in `test-failures.log`, so a prompt naming
    `READABLE` unconditionally sent a fixer after a file that was never written.
    """
    if (evidence / READABLE).is_file():
        return f"{READABLE} (each failing test with its message and traceback), then the .log files"
    logs = sorted(p.relative_to(evidence).as_posix() for p in evidence.rglob("*.log"))
    return ", ".join(logs) if logs else "whatever is there"


# The gate runs on Linux and a fixer on this machine: one shipped a fix whose only real
# check it could not run, and said nothing, because local green looked like proof.
LINUX = (
    "The gate ran on Linux: a failure that does not reproduce here is reasoned from the "
    "log, and the intent says what was and was not verified on this machine."
)


def standing_refusal(tree: Path) -> str:
    """The tree's last commit refusal as `step: why`, or "" when its last ship went through.

    carameli #395's fixer was told only the red check, while the tree's
    `ship-state.json` held the real blocker -- an earlier intent the commit stage had
    refused -- and five calls went on finding it (d609d34d).
    """
    state = read_state(tree)
    if state.get("stage") != REFUSED:
        return ""
    lines = [line.strip() for line in str(state.get("output", "")).splitlines() if line.strip()]
    # The line that names the check, then the output's last words, which say why.
    first = [line for line in lines if REFUSAL_LINE.search(line)][:1]
    return f"{state.get('step', 'commit')}: {' | '.join(dict.fromkeys(first + lines[-2:]))[:400]}"


def _refused_too(refusal: str) -> str:
    if not refusal:
        return ""
    return (
        f" This tree also holds an intent ({INTENT_FILE.as_posix()}) the commit stage "
        f"refused, recorded in {STATE_FILE.as_posix()} -- {refusal}. Your work ships "
        "through that same stage, so clear that refusal as part of this fix."
    )


def pr_prompt(failure: Failure, refusal: str = "", left: str = "") -> str:
    """One branch, in its own worktree: a conflict to resolve, a refused commit, or a red PR.

    Three shapes, one function, because the worktree and the finish line are the same
    and only the middle differs. The conflict prompt names no failure on purpose: the
    gate cannot have run, and a resolver told "also fix the tests" fixes the wrong thing.
    `refusal` is the tree's `standing_refusal`, which the commit shape already is, and
    `left` is why the tree was not brought to origin's head (`fix-prs.refresh_head`).
    """
    if CONFLICT in failure.signature:
        return _framed(
            f"PR #{failure.number} in {failure.project} has a merge conflict with "
            f"origin/{failure.base}. This worktree is checked out on its head branch "
            f"{failure.head}.{_left_as_is(left, failure.head)} Merge origin/{failure.base} in and resolve "
            "the conflicts so "
            "that both sides' intent survives -- git diff --check must find no conflict "
            "marker in any file, not only the code -- and leave the merge uncommitted: the fix "
            "pass concludes it with the hooks running, and whatever the gate says after "
            f"that is the next pass's business, not this session's.{_refused_too(refusal)} "
            f"{FINISH}"
        )
    if failure.kind == COMMIT:
        return _framed(
            f"The commit stage refused the change on {failure.head} in {failure.project}: "
            f"{_ids(failure.signature)}. The pre-commit output is in {EVIDENCE_DIR}/ in this "
            "worktree, which is the worktree the change was made in, and the message it "
            f"was being shipped with is in {REFUSED_FILE.as_posix()} -- reuse it when it "
            f"still fits. Fix what the output reports. {FINISH}"
        )
    return _framed(
        f"PR #{failure.number} in {failure.project} against origin/{failure.base} is stuck: "
        f"{failure.reason}. Failing: {_ids(failure.signature)}. {_logs(failure)} "
        f"This worktree is checked out on the PR head branch {failure.head}"
        + (f".{_left_as_is(left, failure.head)}" if left else ", already up to date with its base.")
        + " Fix what the gate is failing on, and nothing else about "
        f"the PR.{_refused_too(refusal)} {FINISH}"
    )


TRIAGE_LOG = _TRIAGE_ARTIFACT.name


def _ledger_log(failure: Failure) -> str:
    """Where the backlog's groups are in the tree: its evidence is placed under the slot
    its directory is named for. "In that directory's" read as `logs/gate/` itself, and
    both ledger sessions of one supervised run opened that first."""
    slot = Path(failure.evidence).name if failure.evidence else ""
    where = f"{EVIDENCE_DIR}/{slot}/" if slot else f"{EVIDENCE_DIR}/*/"
    return f" The ledger groups are in {where}{TRIAGE_LOG} in this worktree."


def _left_as_is(left: str, head: str) -> str:
    """What a reused tree holds that the session did not put there, or "".

    #422's resolver was sent into a tree holding an earlier session's half-done merge --
    21 files unstaged, no MERGE_HEAD -- and was told nothing about it (f7167792).
    """
    if not left:
        return ""
    return (
        f" The pass did not bring it to origin's head -- {left} -- so run git status and "
        f"git log origin/{head}..HEAD first: what origin does not have was left by an "
        "earlier session, not by you. Read it before "
        "anything else, keep what serves this fix, and restore only what you have read "
        "and judged wrong."
    )


def upstream_prompt(failures: tuple[Failure, ...], branch: str) -> str:
    """One devkit session for everything harness-shaped this pass found.

    A vendored failure several consumers share, devkit's own red default branch, a
    refused commit the toolchain caused: one session, every failure named with what
    its gate said, so the session reads the whole set before deciding what one fix
    covers it.
    """
    ordered = sorted(failures, key=lambda f: (f.project, f.kind, f.number))
    projects = sorted({f.project for f in ordered})
    rows = "; ".join(f"{f.project} {name_of(f)} -- {describe(f)}" for f in ordered)
    urls = ", ".join(f"{f.project} {name_of(f)} {f.url}".rstrip() + _held(f) for f in ordered)
    return _framed(
        f"The harness is red in {len(projects)} checkout(s) ({', '.join(projects)}): "
        f"{rows}. The fix belongs here in devkit, once -- in the vendored file, the "
        "test, or the template that generates the project-owned file it names -- not in "
        f"each consumer. Each failure's gate logs are under {EVIDENCE_DIR}/ in this "
        "worktree, one directory per failure that uploaded any; for the rest, read the "
        "run at its URL."
        + "".join(_ledger_log(f) + LEDGER_STEPS for f in ordered if f.kind == LEDGER)
        + f" This worktree is on the fresh branch {branch} off the default branch. Say in "
        f"the intent which of these the fix unblocks: {urls}. {OWN_DIFF} {FINISH}"
    )


def with_trees(failures: tuple[Failure, ...], root: Path, git_for) -> tuple[Failure, ...]:
    """Each PR failure with the worktree holding its head, where one does.

    `Failure.tree` was a refused commit's alone; a consumer PR folded into the upstream
    session was named by URL only, so the session searched the project for the tree its
    fix belonged in. Anything git cannot list leaves the failure as it was.
    """
    found: list[Failure] = []
    for failure in failures:
        project_dir = root / failure.project
        if failure.kind == PR and failure.head and not failure.tree and project_dir.is_dir():
            listed = git_for(project_dir)("worktree", "list", "--porcelain")
            ok = listed.returncode == 0
            held = aw.holder(project_dir, listed.stdout, failure.head)[0] if ok else ""
            if held:
                failure = replace(failure, tree=str(held))
        found.append(failure)
    return tuple(found)


def _held(failure: Failure) -> str:
    """Where a PR's head is checked out, when the pass found it: a session sent at a
    consumer PR by URL alone spent turns searching the project's worktrees for it."""
    if failure.kind != PR or not failure.tree:
        return ""
    return f" (its head is checked out in {failure.tree})"


# The upstream session's way out when a red PR's cause is its own diff: the default
# branch is green, so nothing on a fresh branch off it can land on the PR. devkit #387
# stopped here with the fix in plain view. `fix_cycle.classify` routes a devkit PR to
# its own branch first; this is what a session does when one arrives anyway. The pass
# ships an intent from any worktree (`ship_intent.find_intents`), so it pushes this too.
OWN_DIFF = (
    "A PR whose failure does not reproduce here is red on its own diff: fetch its head, "
    "add a worktree on it, fix it there and ship it with the ship skill from that "
    "worktree, and name it in the intent here."
)


def branch_prompt(failure: Failure, branch: str) -> str:
    """A default branch whose own gate is red: fix on a fresh branch, off that red base."""
    return _framed(
        f"The {failure.workflow} workflow in {failure.project} is red on "
        f"origin/{failure.base} itself, at {failure.sha[:12] or 'its head'} ({failure.url}). "
        f"Failing: {_ids(failure.signature)}. {_logs(failure)} "
        f"This worktree is on the fresh branch {branch} off origin/{failure.base}, "
        "so the failure reproduces here. Fix it; every PR against this base is red until "
        f"the fix lands. {FINISH}"
    )


def nightly_prompt(failure: Failure, branch: str) -> str:
    """A scheduled workflow that failed on the default branch: fix on a fresh branch."""
    return _framed(
        f"The {failure.workflow} workflow in {failure.project} is failing on "
        f"origin/{failure.base}; issue #{failure.number} ({failure.url}) tracks it. "
        f"Failing: {_ids(failure.signature)}. {_logs(failure)} "
        f"This worktree is on the fresh branch {branch} off "
        f"origin/{failure.base}. Fix it; the issue closes itself when the workflow next "
        f"passes. {FINISH}"
    )
