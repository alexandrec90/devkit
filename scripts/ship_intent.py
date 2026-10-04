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
dispatchable failure, an intent already shipped at this tree's state means nothing to do,
and so does one over a tree with nothing changed or committed (`commits_ahead`), or
whose every change is already on its base (`lands_nothing`).

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
import shutil
import subprocess
import sys
import tempfile
import time
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
EMPTY = "empty"  # nothing changed and nothing committed: the work was all on the ledger

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
    surprising or silently passed over. A `spent` intent is not returned at all, and
    is judged before the branch: which branch a shipped message sits on says nothing.

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
        subject, body = parse_intent(intent_path.read_text(encoding="utf-8", errors="replace"))
        if not subject or spent(Intent(project, tree, branch, subject, body), read_state(tree)):
            continue
        if project not in defaults:
            defaults[project] = tb.detect_default_branch(git_for(root / project), fallback="main")
        base = defaults[project]
        shippable, why = ship.is_shippable(branch, base)
        if not shippable and branch != base and has_open_pr(gh_for(root / project), branch):
            shippable, why = True, ""
        found.append(Intent(project, tree, branch, subject, body, "" if shippable else why, base))
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


def already_shipped(intent: Intent, state: dict, porcelain: str, head: str = "") -> bool:
    """Shipped, with nothing changed in the tree since: nothing to do.

    The words are deliberately not compared. A message edited after the ship, with no
    file changed, has no commit to carry it; shipping again would push nothing and ask
    for a second PR on a branch whose first may already have merged. The digest is
    still recorded, for the record's own sake -- what the tree *was* shipped as.

    The sha is compared, when both are known: a record of a ship is a record of *that*
    commit. #463's tree said `shipped` at 035c6b7 with two commits on top that never
    reached origin, and a clean tree read as that ship set the intent aside (3ae36740).
    """
    if state.get("stage") != SHIPPED or porcelain.strip():
        return False
    sha = str(state.get("sha", ""))
    return not (head and sha) or sha == head


def spent(intent: Intent, state: dict) -> bool:
    """The state file says these very words already shipped from this tree.

    An intent that outlived its ship -- written before `set_aside` existed, or left
    behind by one that could not move it -- is not work. Read as work it was reported
    `blocked` once someone cut a hand-named branch in the same tree, telling a person
    to move a change that had merged the day before; with edits on top it would be
    committed under the old message. The words are compared here, unlike in
    `already_shipped`: an edited message is a new intent and gets the ordinary path.
    """
    return state.get("stage") == SHIPPED and state.get("intent") == intent.digest


def merging(tree: Path, runner: Runner) -> bool:
    """Whether the tree is in the middle of a merge: `MERGE_HEAD` exists.

    Its commit is work even over a clean `git status`. A merge that changes no file --
    origin's side already in through a criss-cross, or a resolution that kept ours
    everywhere -- has nothing to stage, and was read as nothing to ship: #538's resolver
    set up exactly that merge, and the pass set its intent aside as "already shipped",
    leaving the PR as conflicted as before. The file is looked for rather than the ref
    verified, so git failing to answer reads as no merge, as everywhere else here.
    """
    where = runner(["git", "rev-parse", "--git-path", "MERGE_HEAD"], cwd=tree)
    named = (where.stdout or "").strip() if where.returncode == 0 else ""
    if not named:
        return False
    path = Path(named)
    return (path if path.is_absolute() else tree / path).is_file()


def _settled(
    intent: Intent, porcelain: str, base: str, runner: Runner, when: str, mid_merge: bool = False
) -> Outcome | None:
    """This pass's answer when nothing needs doing: shipped already, nothing to ship,
    or refused again. A merge in progress (`merging`) is never the first two."""
    state = read_state(intent.tree)
    head = _shipped_head(intent.tree, state, porcelain, runner)
    if not mid_merge and already_shipped(intent, state, porcelain, head):
        # Consumed, as a fresh ship's intent is: left in place it was re-read and
        # re-reported by every pass -- ten from before the pass set intents aside.
        set_aside(intent.tree, SHIPPED_FILE)
        return Outcome(intent, SKIPPED, "already shipped at this intent; set aside")
    if not porcelain.strip() and not mid_merge and commits_ahead(intent.tree, base, runner) == 0:
        return _empty(intent, when, "nothing changed or committed: no PR to open; set aside")
    if lands_nothing(intent.tree, base, runner):
        why = f"every change in the tree is already on origin/{base}: no PR to open; set aside"
        return _empty(intent, when, why)
    return still_refused(intent, state, porcelain)


def _empty(intent: Intent, when: str, why: str) -> Outcome:
    """Nothing to ship, recorded as such, with the session's outcome kept beside it."""
    write_state(intent.tree, {"stage": EMPTY, "when": when, "intent": intent.digest})
    set_aside(intent.tree, SHIPPED_FILE)
    return Outcome(intent, EMPTY, why)


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
    if status.returncode != 0:
        return False  # unreadable is not clean: say it would ship, as a dispatch tries to
    porcelain, state = status.stdout or "", read_state(intent.tree)
    head = _shipped_head(intent.tree, state, porcelain, runner)
    if not already_shipped(intent, state, porcelain, head):
        return False
    return not merging(intent.tree, runner)


def _shipped_head(tree: Path, state: dict, porcelain: str, runner: Runner) -> str:
    """HEAD, read only where `already_shipped` would otherwise say yes; "" unknown."""
    if state.get("stage") != SHIPPED or porcelain.strip() or not state.get("sha"):
        return ""
    head = runner(["git", "rev-parse", "HEAD"], cwd=tree)
    return (head.stdout or "").strip() if head.returncode == 0 else ""


def _first(done: subprocess.CompletedProcess[str]) -> str:
    """Git's first line of complaint."""
    text = (done.stderr or done.stdout or "").strip()
    return text.splitlines()[0] if text else f"exit {done.returncode}"


# What git prints when another process holds one of its lock files: `index.lock` for an
# add, `HEAD.lock` or a ref's lock for a commit.
LOCK_HELD = re.compile(r"Unable to create '[^']*\.lock': File exists")
# Seconds between tries of a git step that met a held lock. Anything that looks at the
# tree takes `index.lock` for a moment -- an editor's git view, a session's own `git
# status` -- so the pass's `git add` lost that race once and filed "add refused" over a
# lock that was gone before the fixer sent at it opened (devkit, 0927-17).
LOCK_WAITS = (1, 2, 4, 8)


def _wait(seconds: float) -> None:
    time.sleep(seconds)


def run_git(argv: list[str], tree: Path, runner: Runner) -> subprocess.CompletedProcess[str]:
    """`runner(argv)`, tried again after each of `LOCK_WAITS` while git says a lock is held.

    A lock still held after the last wait is left to refuse: a stale one needs a person or
    a fixer to decide it is stale, and fifteen seconds is well past any git call's hold.
    """
    done = runner(argv, cwd=tree)
    for seconds in LOCK_WAITS:
        if done.returncode == 0 or not LOCK_HELD.search((done.stdout or "") + (done.stderr or "")):
            break
        _wait(seconds)
        done = runner(argv, cwd=tree)
    return done


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
        added = run_git(["git", "add", "-A"], intent.tree, runner)
        if added.returncode != 0:
            return "add", (added.stdout or "") + (added.stderr or "")
        fixed = runner([python, "scripts/ship.py", "--fix"], cwd=intent.tree)
        output = (fixed.stdout or "") + (fixed.stderr or "")
        if fixed.returncode == 0:
            break
    else:
        return "fixers", output
    added = run_git(["git", "add", "-A"], intent.tree, runner)
    if added.returncode != 0:
        return "add", (added.stdout or "") + (added.stderr or "")
    committed = run_git(["git", "commit", "-F", str(INTENT_FILE)], intent.tree, runner)
    if committed.returncode != 0:
        return "commit", (committed.stdout or "") + (committed.stderr or "")
    return "", ""


# What `git_policy.branch` says when a commit lands on a name whose PR merged; its remedy
# is one new branch at HEAD, which a session was otherwise sent to make
# (`_carry_to_free_branch`).
RETIRED_MARK = "is permanently retired because its PR merged"
# How many fresh names one refused commit is carried across. The first nearly always
# takes; each further refusal is a name whose PR merged and whose refs were deleted since
# (GitHub's delete-on-merge, a sweep's prune), which no ref listing can show.
CARRIES = 8
# `tb.branch_name`'s `-N` collision suffix after its `-<mmdd>` stamp. `N` is short so a
# slug ending in a number (`agent/pr-1234-0919`) is not read as a stamp plus a suffix.
_COLLISION_SUFFIX = re.compile(r"(-\d{4})-\d{1,3}$")


def branch_stem(branch: str) -> str:
    """`branch` without its `-N` collision suffix: `agent/x-0927-14` -> `agent/x-0927`, so
    a carry from a suffixed name counts on from the family, not `agent/x-0927-14-2`."""
    return _COLLISION_SUFFIX.sub(r"\1", branch)


def next_free_name(stem: str, taken: set[str]) -> str:
    """`<stem>-<n>`, one above the highest `n` in `taken` -- never a gap below it.

    A fixed range of candidates (it was `-2`..`-9`) ran out on a topic the pass had cut
    sixteen branches for in one day, and the carry gave up with the tree stranded on its
    retired name (`agent/fix-harness-ledger-0927`). A gap is no better: it is usually a
    name whose PR merged and whose refs were then deleted, which the policy still refuses,
    and a reused name is what `fix_verify.relevant` has to guard against.
    """
    suffixed = re.compile(re.escape(stem) + r"-(\d+)")
    used = [1] + [int(m.group(1)) for name in taken if (m := suffixed.fullmatch(name))]
    return f"{stem}-{max(used) + 1}"


def _taken_names(stem: str, runner: Runner, tree: Path) -> set[str]:
    """Every local and origin branch in `stem`'s family, in two calls however many exist."""
    local = runner(
        ["git", "for-each-ref", "--format=%(refname:short)", f"refs/heads/{stem}*"], cwd=tree
    )
    remote = runner(["git", "ls-remote", "--heads", "origin", f"{stem}*"], cwd=tree)
    names = set((local.stdout or "").split())
    for line in (remote.stdout or "").splitlines():
        names.add(line.split("\t")[-1].strip().removeprefix("refs/heads/"))
    return names


def _carry_to_free_branch(
    intent: Intent, stem: str, runner: Runner, tried: set[str]
) -> Intent | str:
    """The intent moved onto the next `<stem>-<n>` no ref and no refusal has used, or
    git's complaint when the move failed.

    A new name at HEAD, and HEAD pointed at it, touching neither the index nor the tree:
    what `git switch -c` does, minus its refusal of a tree mid-merge ("cannot switch
    branch while merging"), which is the tree a fixer resolving a conflict leaves --
    exactly when its PR merged under it and its name retired (carameli, e21febb8 and
    f95dffbc). `git checkout -b` would carry it but drop `MERGE_HEAD`, committing the
    merge with one parent.
    """
    tried.add(intent.branch)
    name = next_free_name(stem, _taken_names(stem, runner, intent.tree) | tried)
    for argv in (["git", "branch", name], ["git", "symbolic-ref", "HEAD", f"refs/heads/{name}"]):
        done = runner(argv, cwd=intent.tree)
        if done.returncode != 0:
            return f"`{' '.join(argv)}`: {_first(done)}"
    return replace(intent, branch=name)


def _commit_carrying(intent: Intent, python: str, runner: Runner) -> tuple[Intent, str, str]:
    """`commit_intent`, moving to a free branch each time the policy refuses a retired
    name, at most `CARRIES` times: `(the intent as committed, step, output)`."""
    step, output = commit_intent(intent, python, runner)
    stem, tried = branch_stem(intent.branch), set[str]()
    for _ in range(CARRIES):
        if step != "commit" or RETIRED_MARK not in output:
            break
        moved = _carry_to_free_branch(intent, stem, runner, tried)
        if isinstance(moved, str):
            # Said, or the stored refusal reads as a carry never tried (f95dffbc).
            output = f"{output.rstrip()}\n[fix pass] the carry to a fresh name failed: {moved}\n"
            break
        intent = moved
        step, output = commit_intent(intent, python, runner)
    return intent, step, output


def retired_at(tree: Path, branch: str, gh_for=sweep.gh_for) -> str:
    """When `branch`'s newest merged PR merged, as `gh` prints it; "" when unknown.

    What `harness_triage.carried` measures a resolution against once the intent here was
    carried off that retired name: one written since cannot mean that PR, which is the
    line `fix_verify.relevant` draws too. It was the merge-base's commit time, which moves
    on each time the branch merges its base in -- and then re-pointed nothing.
    """
    done = gh_for(tree)("pr", "list", "--head", branch, "--state", "merged", "--json", "mergedAt")
    if getattr(done, "returncode", 1) != 0:
        return ""
    try:
        rows = json.loads(getattr(done, "stdout", "") or "[]")
    except ValueError:
        return ""
    times = [str(r.get("mergedAt")) for r in rows if isinstance(r, dict) and r.get("mergedAt")]
    return max(times) if times else ""


def commits_ahead(tree: Path, base: str, runner: Runner) -> int | None:
    """Commits on the tree's HEAD that `origin/<base>` lacks; None when git cannot say.

    Zero over a clean tree is a session whose fix was all ledger -- a group resolved
    against another branch's PR -- with nothing to push. Shipped anyway, it pushed an
    empty branch and `gh pr create` refused it ("No commits between"): a `ship-failed`
    on every pass, and a fixer left to pick between that and a false `fix-blocked.md`.
    """
    counted = runner(["git", "rev-list", "--count", f"origin/{base}..HEAD"], cwd=tree)
    if counted.returncode != 0:
        return None
    try:
        return int((counted.stdout or "").strip())
    except ValueError:
        return None


def lands_nothing(tree: Path, base: str, runner: Runner) -> bool:
    """Whether everything the tree holds -- committed, staged, edited, untracked -- is on
    `origin/<base>` already: merged there, it changes nothing. False when git cannot say.

    Dirty or ahead is not the same as new. carameli's minor-and-patch tree was both: its
    PR squash-merged while a fixer was mid-merge in it, so the staged files matched
    `origin/master` byte for byte and the one commit ahead was the pre-squash original.
    Its intent said "Nothing to ship", the commit stage ran anyway, was refused on the
    retired name, and the refusal sent a second fixer to say it again (e21febb8).

    A merge in progress counts `MERGE_HEAD` as a parent of the fork, as the commit will:
    a resolution that took the trunk's side as of the merged commit is no change of the
    tree's own, though the trunk has moved that file on since (f95dffbc).
    """
    upstream = f"origin/{base}"
    merging = runner(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=tree)
    heads = ["HEAD", "MERGE_HEAD"] if merging.returncode == 0 else ["HEAD"]
    # With three commits `merge-base` answers for the first against a merge of the rest.
    forked = runner(["git", "merge-base", upstream, *heads], cwd=tree)
    fork = (forked.stdout or "").strip() if forked.returncode == 0 else ""
    held = _held_tree(tree, runner) if fork else ""
    if not held:
        return False
    target = runner(["git", "rev-parse", f"{upstream}^{{tree}}"], cwd=tree)
    merged = runner(
        ["git", "merge-tree", "--write-tree", f"--merge-base={fork}", upstream, held], cwd=tree
    )
    if target.returncode != 0 or merged.returncode != 0:
        return False  # 1 is a conflict: the tree has something origin does not
    lines = (merged.stdout or "").split()
    return bool(lines) and lines[0] == (target.stdout or "").strip()


def _held_tree(tree: Path, runner: Runner) -> str:
    """The tree `git add -A` would commit, written through a copy of the index so the real
    one is never touched -- and, being a copy, its stat cache spares rehashing every file.
    "" when git cannot say.

    The copy keeps the index's mtime (`copy2`): git trusts a cached stat only for a file
    older than the index, so a copy stamped now vouched for an edit made in the tick the
    index was written, and read it as unchanged.
    """
    where = runner(["git", "rev-parse", "--git-path", "index"], cwd=tree)
    if where.returncode != 0 or not (where.stdout or "").strip():
        return ""
    index = Path((where.stdout or "").strip())
    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / "index"
        try:
            shutil.copy2(index if index.is_absolute() else tree / index, probe)
        except OSError:
            return ""
        env = {**os.environ, "GIT_INDEX_FILE": str(probe)}
        if runner(["git", "add", "-A"], cwd=tree, env=env).returncode != 0:
            return ""
        written = runner(["git", "write-tree"], cwd=tree, env=env)
    return (written.stdout or "").strip() if written.returncode == 0 else ""


def catch_up(tree: Path, branch: str, runner: Runner) -> str:
    """Merge origin's `branch` into HEAD when it holds commits HEAD lacks; git's output
    when that merge conflicts (and is aborted), "" otherwise.

    The pass brings a behind PR up to its base through GitHub (`gh pr update-branch`),
    and `fix-prs.refresh_head` leaves a tree with edits where it is, because a session is
    working in it. So origin's head moves while the tree holds a commit of its own:
    #463's did 24 times, and every push from the tree was refused non-fast-forward
    (3ae36740, 436fc0c8). No remote branch yet -- a first push -- or a fetch that fails
    is nothing to merge; the push says whatever else is wrong.
    """
    remote = f"refs/remotes/origin/{branch}"
    fetched = runner(
        ["git", "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{remote}"], cwd=tree
    )
    if fetched.returncode != 0:
        return ""
    if runner(["git", "merge-base", "--is-ancestor", remote, "HEAD"], cwd=tree).returncode != 1:
        return ""  # 0: already in HEAD; anything else: git cannot say, so push as before
    merged = run_git(["git", "merge", "--no-edit", remote], tree, runner)
    if merged.returncode == 0:
        return ""
    runner(["git", "merge", "--abort"], cwd=tree)
    output = f"{merged.stdout or ''}\n{merged.stderr or ''}".strip()
    return f"origin/{branch} moved and does not merge into this tree cleanly:\n{output}"


def _record_failure(intent: Intent, step: str, detail: str, when: str) -> None:
    """A push or PR that failed, in the state file: left unwritten, an older `shipped`
    stood and the next pass read the unpushed commit as shipped (3ae36740)."""
    record = {"stage": FAILED, "step": step, "output": detail, "when": when}
    write_state(intent.tree, {**record, "intent": intent.digest})


def _push(intent: Intent, runner: Runner, when: str) -> Outcome | None:
    """Origin's head merged in, then the push past the gate; what stopped it, recorded,
    or None. A merge that conflicts is a refusal a fixer is sent at; a push that fails
    is a failure the next pass retries."""
    tree = intent.tree
    conflict = catch_up(tree, intent.branch, runner)
    if conflict:
        after = runner(["git", "status", "--porcelain"], cwd=tree).stdout or ""
        record = {"stage": REFUSED, "step": "merge", "output": conflict, "when": when}
        write_state(tree, {**record, "intent": intent.digest, "tree": _digest(after)})
        return Outcome(intent, REFUSED, f"merge: {conflict.strip()[-400:]}")
    env = dict(os.environ)
    env["SKIP"] = SKIP_PUSH_GATE
    pushed = runner(["git", "push", "-u", "origin", intent.branch], cwd=tree, env=env)
    if pushed.returncode == 0:
        return None
    detail = (pushed.stderr or pushed.stdout or "").strip()[-400:] or f"exit {pushed.returncode}"
    _record_failure(intent, "push", detail, when)
    return Outcome(intent, FAILED, f"push: {detail}")


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
    if status.returncode != 0:
        # An unreadable tree is not a clean one: read as clean, a supervisor's second
        # intent over 19 modified files was set aside as already shipped.
        return Outcome(intent, FAILED, f"status: git could not read the tree: {_first(status)}")
    porcelain = status.stdout or ""
    mid_merge = not porcelain.strip() and merging(tree, runner)
    if settled := _settled(intent, porcelain, base, runner, when, mid_merge):
        return settled
    if porcelain.strip() or mid_merge:
        intent, step, output = _commit_carrying(intent, python, runner)
        if step:
            after = runner(["git", "status", "--porcelain"], cwd=tree).stdout or ""
            record = {"stage": REFUSED, "step": step, "output": output, "when": when}
            write_state(tree, {**record, "intent": intent.digest, "tree": _digest(after)})
            return Outcome(intent, REFUSED, f"{step}: {output.strip()[-400:]}")

    if stopped := _push(intent, runner, when):
        return stopped

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
        _record_failure(intent, "pr", error, when)
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


def _why(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return next((line for line in lines if REFUSAL_LINE.search(line)), lines[-1] if lines else "")


def refusal_line(output: str) -> str:
    """The line of a refused commit's output that says why, cut to one record line.

    "fixers refused" was the whole signature when no test id or lint line matched, and a
    session had to open `ship-state.json` to learn the branch name was the objection.
    """
    # It becomes part of a signature, which must not change between two refusals of the
    # same kind -- or every retry reads as progress and `fix_ledger.ATTEMPTS` never trips.
    why = re.sub(r"\d+", "N", re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", _why(output)))
    return " ".join(why.split())[:160]


def refusal_reason(output: str) -> str:
    """`refusal_line` as the hook wrote it, for a reader rather than a signature.

    The pass's record kept the *tail* of a refusal's output, which is a hook's closing
    boilerplate: sports_betting's detect-secrets refusal read "refused: mment If a secret
    has already been committed, visit https://help.github.com/...", while the line naming
    the failed hook sat further up, out of reach of the cut.
    """
    return " ".join(_why(output).split())[:160]


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
