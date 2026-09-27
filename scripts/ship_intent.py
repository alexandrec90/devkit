#!/usr/bin/env python3
"""Ship what a session said it was done with: the intent file, and what the pass does with it.

A session's whole shipping act is now one file. It writes `logs/ship-intent.md` into
its worktree -- the commit subject on the first line, the body after a blank one -- and
stops. No commit, no gate, no push. The only thing the session knows that nothing else
can recover is *why* the change was made, so that is the only thing it is asked for;
a diff can be read by anyone, and the message is the one changelog consumers get.

The fix pass (`scripts/fix-pass.py`) does the rest, here: run the commit-stage fixers
through the tree's own `ship.py --fix`, commit with the message, push with the push gate
skipped -- CI judges, and the pass reads its artifact -- open the PR, labelled
`automerge` only when the fix pass cut the tree (`labels_for`): a fixer's PR merges
once green, a person's waits for them. The outcome is recorded in `logs/ship-state.json`
beside the intent. A refused commit is
recorded too, with the pre-commit output as evidence, so the pass can tell it from a
session still working: no intent file means hands off, an intent with a refusal means a
dispatchable failure, an intent already shipped at this tree's state means nothing to do.

**The intent is consumed.** Once shipped it becomes `logs/ship-intent.shipped.md`; once
a fixer is sent at a refusal it becomes `logs/ship-intent.refused.md` (the pass does
that at dispatch, `set_aside`). So the file exists only between a session writing it
and the pass acting on it, and shipping again is always a fresh intent -- the ship
skill writes one.

**A dirty tree with no intent file is never touched.** That is the whole line between
"still working" and "done", and it is drawn by the session, not guessed from the tree.
It is also what makes a fixer safe in a reused worktree: its edits are a dirty tree
with no intent until it ships.

Both files live under `logs/`, which every project ignores, so neither can be committed
by the `git add -A` this runs. Every spawn is window-less: the pass is a scheduled job.

Tested in `tests/test_ship_intent.py`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import fix_plan
import fix_reports
import sweep
import task_branch as tb
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# `ship.py` is vendored and hyphen-free, but loaded by path anyway: importing it would
# run its module-level git calls against whatever the cwd is. Only `is_shippable` is
# used -- the one pure answer to "is this branch one a PR can be opened from".
ship = load_by_path("ship", REPO_ROOT / "scripts" / "ship.py")

INTENT_FILE = Path("logs") / "ship-intent.md"
STATE_FILE = Path("logs") / "ship-state.json"
# Where the intent goes once the pass has acted on it. Shipped: the words that went
# out, kept for the record. Refused: the message a fixer was sent to earn, kept where
# that fixer can reuse it. Either way `INTENT_FILE` is gone, so a tree with edits and
# no intent is a session still working -- including the fixer's own -- and the pass
# keeps its hands off. Before this, a fixer reusing the worktree of a shipped PR had
# its first uncommitted edit committed under the old message and pushed to the PR.
SHIPPED_FILE = Path("logs") / "ship-intent.shipped.md"
REFUSED_FILE = fix_reports.REFUSED_FILE

# The pre-push hook the pass skips. CI runs the same gate on the PR and the pass reads
# its artifact, so running it here would only make the push take minutes for a verdict
# the next pass reads anyway.
SKIP_PUSH_GATE = "devkit-push-gate"

# What `Outcome.stage` can be.
SKIPPED = "skipped"  # shipped already at this intent, or not a shippable branch
REFUSED = "refused"  # the commit stage said no; a failure the pass can dispatch
FAILED = "failed"  # the push or the PR failed; try again next pass
SHIPPED = "shipped"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Intent:
    project: str
    tree: Path
    branch: str
    subject: str
    body: str
    # Why this intent cannot be shipped from where it sits -- on the default branch,
    # say -- or "". The pass reports a blocked intent rather than passing it over: an
    # intent nothing mentions is work that sits unstaged on `master` until a person
    # happens to look, which is how the first ledger sweep's carameli fix was found.
    blocked: str = ""
    # The checkout's default branch: what the PR opens against, and what decided
    # `blocked`. Read once per checkout by `find_intents`.
    base: str = ""

    @property
    def digest(self) -> str:
        """What the state file remembers an intent as: its words, not its file."""
        return hashlib.sha256(f"{self.subject}\n{self.body}".encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Outcome:
    intent: Intent
    stage: str
    detail: str = ""
    url: str = ""


def run_quiet(
    argv: list[str], cwd: Path | str | None = None, env: dict[str, str] | None = None, **kwargs
):
    """The one spawn this module makes, window-less because the pass is scheduled.

    Takes `subprocess.run`'s keywords, because the pass hands this to `fix-prs.py` as
    its runner and that tier -- `cut_tree`, `agent-box.open_agent`, `launch_background`
    -- calls its runner exactly as it would call `subprocess.run`: `check=False`, no
    `cwd`, its own `capture_output`. The first dispatch the pass ever made died on
    `TypeError` here, with the record unwritten, because every test on either side
    had replaced the other. The window flag and a non-raising call are forced; the
    rest is the caller's.
    """
    options: dict = {"capture_output": True, "text": True}
    options.update(kwargs)
    options["check"] = False
    return subprocess.run(
        argv,
        cwd=None if cwd is None else str(cwd),
        env=env,
        creationflags=sweep.NO_WINDOW,
        **options,
    )


# --- the intent file ----------------------------------------------------------------


def parse_intent(text: str) -> tuple[str, str]:
    """`(subject, body)` from the file's text: first non-empty line, then the rest."""
    lines = str(text).strip().splitlines()
    if not lines:
        return "", ""
    subject = lines[0].strip().lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()
    return subject, body


def read_state(tree: Path) -> dict:
    try:
        loaded = json.loads((tree / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(tree: Path, state: dict) -> None:
    path = tree / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def has_open_pr(gh, branch: str) -> bool:
    """Whether `branch` is already the head of an open PR; False when `gh` cannot say."""
    listed = gh("pr", "list", "--head", branch, "--state", "open", "--json", "number")
    if getattr(listed, "returncode", 1) != 0:
        return False
    try:
        rows = json.loads(listed.stdout or "[]")
    except ValueError:
        return False
    return isinstance(rows, list) and bool(rows)


def find_intents(
    root: Path, projects: list[str], git_for=sweep.git_for, gh_for=sweep.gh_for
) -> list[Intent]:
    """Every worktree of every registered checkout that carries an intent file.

    Through `git worktree list` (`fix_reports.agent_trees`), so a box, a `--worktree`
    checkout and the static checkout on a task branch are all found the same way. A
    tree on a branch a PR cannot be opened from (`ship.is_shippable`) is returned
    `blocked` with the reason, for the record, rather than shipped somewhere
    surprising or silently passed over.

    A branch that already heads an open PR is shippable whatever it is called: the rule
    is about where a *new* PR opens, and that PR is open. devkit #390 was opened by hand
    from `flag-wired-agent-hooks`; a resolver sent at it works on that branch, and its
    intent was refused on every pass, so the PR could only stay conflicted.
    """
    found: list[Intent] = []
    defaults: dict[str, str] = {}
    for project, tree, branch in fix_reports.agent_trees(root, projects, git_for):
        intent_path = tree / INTENT_FILE
        if not branch or not intent_path.is_file():
            continue
        if project not in defaults:
            defaults[project] = tb.detect_default_branch(git_for(root / project), fallback="main")
        base = defaults[project]
        shippable, why = ship.is_shippable(branch, base)
        if not shippable and branch != base and has_open_pr(gh_for(root / project), branch):
            shippable, why = True, ""
        subject, body = parse_intent(intent_path.read_text(encoding="utf-8", errors="replace"))
        if subject:
            found.append(
                Intent(project, tree, branch, subject, body, "" if shippable else why, base)
            )
    return found


def set_aside(tree: Path, target: Path) -> Path | None:
    """Move the tree's intent to `target` (`SHIPPED_FILE` or `REFUSED_FILE`), replacing
    an earlier one there. None when the tree carries no intent."""
    source = tree / INTENT_FILE
    if not source.is_file():
        return None
    destination = tree / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    return destination


# --- shipping one --------------------------------------------------------------------


def already_shipped(intent: Intent, state: dict, porcelain: str) -> bool:
    """Shipped, with nothing changed in the tree since: nothing to do.

    The words are deliberately not compared. A message edited after the ship, with no
    file changed, has no commit to carry it; shipping again would push nothing and ask
    for a second PR on a branch whose first may already have merged. The digest is
    still recorded, for the record's own sake -- what the tree *was* shipped as.
    """
    return state.get("stage") == SHIPPED and not porcelain.strip()


def _settled(intent: Intent, porcelain: str) -> Outcome | None:
    """This pass's answer when nothing needs doing: shipped already, or refused again."""
    state = read_state(intent.tree)
    if already_shipped(intent, state, porcelain):
        # Consumed, as a fresh ship's intent is: left in place it was re-read and
        # re-reported by every pass -- ten from before the pass set intents aside.
        set_aside(intent.tree, SHIPPED_FILE)
        return Outcome(intent, SKIPPED, "already shipped at this intent; set aside")
    return still_refused(intent, state, porcelain)


def labels_for(tree: Path) -> tuple[str, ...]:
    """`automerge` for work the fix pass started, nothing for a person's.

    `automerge` is an authorization the vendored `dependabot-automerge.yml` honours on
    any PR once its gate passes, so it goes only where the pass itself is the author: a
    tree it cut for a fixer (`fix_reports.ORIGIN_FILE`), or one a fixer cut and marked
    the same way. A fixer committing to a person's PR works in that person's tree, which
    carries no mark, so their PR still waits for them -- and `ensure_pr` labelling a
    reused PR too is why the mark is the tree's origin, not the dispatch stamp.
    """
    return (sweep.AUTOMERGE_LABEL,) if (tree / fix_reports.ORIGIN_FILE).is_file() else ()


def still_refused(intent: Intent, state: dict, porcelain: str) -> Outcome | None:
    """The last refusal again, when neither the words nor the tree have moved since it.

    A refusal held behind a red harness was re-run through the whole commit stage on
    every pass of the third supervised run, to the same answer each time. Nothing that
    could change the answer has changed, so the stored one stands, and it is still a
    failure the plan can place (`refusal_failure` reads the same state).
    """
    if state.get("stage") != REFUSED or state.get("intent") != intent.digest:
        return None
    if state.get("tree") != _digest(porcelain):
        return None
    if RETIRED_MARK in str(state.get("output", "")):
        return None  # the carry to a free branch answers it now; try again
    detail = f"{state.get('step', 'commit')}: {str(state.get('output', '')).strip()[-400:]}"
    return Outcome(intent, REFUSED, detail)


def _digest(porcelain: str) -> str:
    return hashlib.sha256(porcelain.encode("utf-8", "replace")).hexdigest()[:12]


def is_spent(intent: Intent, runner: Runner = run_quiet) -> bool:
    """`already_shipped`, asked of the tree itself: what a `plan` pass reads, so it says
    what a `dispatch` would do rather than "would ship" over work that merged."""
    status = runner(["git", "status", "--porcelain"], cwd=intent.tree)
    return already_shipped(intent, read_state(intent.tree), status.stdout or "")


def commit_intent(intent: Intent, python: str, runner: Runner) -> tuple[str, str]:
    """Add, fixers, add, commit: `("", "")` when it went through, else `(step, output)`.

    The tree is staged before the fixers run, and restaged for one retry when they fail.
    detect-secrets' commit hook refuses to scan at all while its baseline has unstaged
    changes, and it is also what rewrites the baseline when a change moves a flagged line
    -- so over an unstaged tree both of `ship.py --fix`'s passes failed on "baseline is
    unstaged", a refusal no code change could answer (carameli #395). Staging between
    runs is what a person committing by hand does; the commit takes all of it anyway.
    """
    output = ""
    for _attempt in (1, 2):
        added = runner(["git", "add", "-A"], cwd=intent.tree)
        if added.returncode != 0:
            return "add", (added.stdout or "") + (added.stderr or "")
        fixed = runner([python, "scripts/ship.py", "--fix"], cwd=intent.tree)
        output = (fixed.stdout or "") + (fixed.stderr or "")
        if fixed.returncode == 0:
            break
    else:
        return "fixers", output
    added = runner(["git", "add", "-A"], cwd=intent.tree)
    if added.returncode != 0:
        return "add", (added.stdout or "") + (added.stderr or "")
    committed = runner(["git", "commit", "-F", str(INTENT_FILE)], cwd=intent.tree)
    if committed.returncode != 0:
        return "commit", (committed.stdout or "") + (committed.stderr or "")
    return "", ""


# What `git_policy.branch` says when a commit lands on a name whose PR merged; its remedy
# is one `git switch -c`, which a session was otherwise sent to make.
RETIRED_MARK = "is permanently retired because its PR merged"
FREE_NAMES = range(2, 10)


def _carry_to_free_branch(
    intent: Intent, stem: str, runner: Runner, tried: set[str]
) -> Intent | None:
    """The intent moved onto `<stem>-<n>`, the first name with no local or remote ref and
    not refused already; None when none is left, or the switch failed."""
    tried.add(intent.branch)
    for n in FREE_NAMES:
        name = f"{stem}-{n}"
        if name in tried:
            continue
        local = runner(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd=intent.tree
        )
        remote = runner(["git", "ls-remote", "--heads", "origin", name], cwd=intent.tree)
        if local.returncode == 0 or (remote.stdout or "").strip():
            tried.add(name)
            continue
        if runner(["git", "switch", "-c", name], cwd=intent.tree).returncode != 0:
            return None
        return replace(intent, branch=name)
    return None


def _commit_carrying(intent: Intent, python: str, runner: Runner) -> tuple[Intent, str, str]:
    """`commit_intent`, moving to a free branch each time the policy refuses a retired
    name: `(the intent as committed, step, output)`."""
    step, output = commit_intent(intent, python, runner)
    stem, tried = intent.branch, set[str]()
    while step == "commit" and RETIRED_MARK in output:
        moved = _carry_to_free_branch(intent, stem, runner, tried)
        if moved is None:
            break
        intent = moved
        step, output = commit_intent(intent, python, runner)
    return intent, step, output


def ship_one(
    intent: Intent,
    python: str,
    base: str,
    runner: Runner = run_quiet,
    gh_for=sweep.gh_for,
    now: _dt.datetime | None = None,
) -> Outcome:
    """Fixers, commit, push, PR -- stopping at the first refusal and recording it."""
    tree = intent.tree
    when = (now or _dt.datetime.now(_dt.UTC)).isoformat(timespec="seconds")
    status = runner(["git", "status", "--porcelain"], cwd=tree)
    if settled := _settled(intent, status.stdout or ""):
        return settled
    if (status.stdout or "").strip():
        intent, step, output = _commit_carrying(intent, python, runner)
        if step:
            after = runner(["git", "status", "--porcelain"], cwd=tree).stdout or ""
            record = {"stage": REFUSED, "step": step, "output": output, "when": when}
            write_state(tree, {**record, "intent": intent.digest, "tree": _digest(after)})
            return Outcome(intent, REFUSED, f"{step}: {output.strip()[-400:]}")

    env = dict(os.environ)
    env["SKIP"] = SKIP_PUSH_GATE
    pushed = runner(["git", "push", "-u", "origin", intent.branch], cwd=tree, env=env)
    if pushed.returncode != 0:
        detail = (pushed.stderr or pushed.stdout or "").strip()[-400:]
        return Outcome(intent, FAILED, f"push: {detail}")

    url, _created, error = sweep.ensure_pr(
        gh_for(tree),
        sweep.Plan(
            pr_title=intent.subject,
            pr_body=intent.body or intent.subject,
            pr_head=intent.branch,
            pr_base=base,
            pr_labels=labels_for(tree),
        ),
    )
    if error:
        return Outcome(intent, FAILED, f"pr: {error}")
    head = runner(["git", "rev-parse", "HEAD"], cwd=tree)
    sha = (head.stdout or "").strip()
    write_state(
        tree, {"stage": SHIPPED, "sha": sha, "url": url, "when": when, "intent": intent.digest}
    )
    set_aside(tree, SHIPPED_FILE)
    return Outcome(intent, SHIPPED, url, url)


# --- a refusal as a failure the plan can place ---------------------------------------


# `blocked` is `git_policy`'s word; without it the line kept was its trailing `details:`
# pointer, and the ledger never said why (b4b33191).
REFUSAL_LINE = re.compile(
    r"Failed\b|refus|\berror\b|\bblocked\b|not a namespaced|conflict string", re.I
)


def refusal_line(output: str) -> str:
    """The line of a refused commit's output that says why, cut to one record line.

    "fixers refused" was the whole signature when no test id or lint line matched, and a
    session had to open `ship-state.json` to learn the branch name was the objection.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    why = next((line for line in lines if REFUSAL_LINE.search(line)), lines[-1] if lines else "")
    # It becomes part of a signature, which must not change between two refusals of the
    # same kind -- or every retry reads as progress and `fix_ledger.ATTEMPTS` never trips.
    why = re.sub(r"\d+", "N", re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", why))
    return " ".join(why.split())[:160]


def refusal_failure(outcome: Outcome, base: str) -> fix_plan.Failure:
    """The refused commit in the plan's own terms, its output placed as evidence.

    The evidence goes straight under the tree's `logs/gate/`, because the fixer will open
    in this very worktree: the branch is held here, so `fix-prs.existing_tree` reuses it.
    """
    intent = outcome.intent
    state = read_state(intent.tree)
    output = str(state.get("output", ""))
    where = intent.tree / fix_plan.EVIDENCE_DIR
    where.mkdir(parents=True, exist_ok=True)
    (where / "pre-commit.log").write_text(output, encoding="utf-8")
    step = str(state.get("step", "commit"))
    sig = fix_plan.signature_from_logs([output]) or (f"{step} refused: {refusal_line(output)}",)
    return fix_plan.Failure(
        kind=fix_plan.COMMIT,
        project=intent.project,
        number=0,
        title=intent.subject,
        url="",
        head=intent.branch,
        base=base,
        sha=intent.digest,
        reason=f"commit stage refused at {step}",
        signature=sig,
        evidence=str(where),
        tree=str(intent.tree),
    )
