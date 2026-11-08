#!/usr/bin/env python3
"""Fix or revert: a direct-flow default branch that stayed red gets its bad commits taken off.

A project in the direct flow (`delivery.py`) merges first, so a bad commit reaches its
default branch and a fixer goes after it there. If that fixer has not turned it green
within `RED_GRACE` of the first pass that saw the red (`red_seen.py`, never the commit's own
time: a PC that slept through the hours must not spend the fixer's chance in its first
pass), the branch stops waiting: every project session is cutting from a red tip. So the
commits that made it red are reverted, each through the same road as any other change,
and each revert carries a queue task to land the work again with the failure fixed, so
nothing is lost with it.

**The red range** (`red_range`) is the commits of the default branch after `origin/green`
up to the oldest one that failed the way the tip fails: its failing jobs, read per commit,
overlap the tip's (`failed_jobs`). A gate that ran on that commit saw every commit since
the last green one, and any of them may be the cause. Only a run whose conclusion is
`failure` and whose job really ran counts as red; a commit that failed on other jobs is
not this red; a commit the gate passed ends the range below it, in case `green` is lagging.
When nothing in the range matches, nothing is reverted, and it stays the fixer's.

**What is never named** (`candidates`): tidy's own commit, known by its exact subject and
author, so a session's `Tidy: drop the helper` is not exempt; a release, whose red is a
missing tag and whose revert would leave the real cause on the branch; and a `Revert "`
commit, which is this module's, and reverting it would land the very work it took off.

**Each revert is an intent in a disposable tree**, as `fix_drift.py` carries a relock:
`agent/revert-<sha7>` off `origin/<base>`, `git revert` staged, the queue file beside it,
and the ship task commits, pushes and labels it `merge-now` like anything else in the flow.
A tidy commit after the target that touches a file the target touched goes in the same
revert, newest first (`tidied_after`): it reformatted those very lines, so without it the
revert conflicts in the ordinary case. The next post-merge tidy formats again.

**One revert per sha, ever.** A branch already there says so, on origin or here, where
the intent may still be waiting for the ship task. A merged PR says so too: merging can
delete the branch, and a second revert of a commit already gone would only conflict.

**The re-land cycle ends at one.** The queue task asks for a `Reland-of: <sha>` trailer. A
commit carrying one that goes red is reverted once more, with nothing queued, and filed
for the devkit session: work that has failed twice needs more than a third try.

**Never in a gated project**, and never on evidence that does not implicate a commit
(`revertible`): a run no runner took, a bill, a release commit's red by construction, or
what tidy still owns. A commit the revert conflicts on is left alone and filed: forcing one
takes later work down with it, and the branch's fixer is still on the failure.

Tested in `tests/test_fix_revert.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_findings
import fix_loop
import fix_plan
import fix_reports
import fix_trees
import gate_evidence
import red_seen
import ship_intent
import sweep

# How long a default branch may stay red, from when a pass first saw it, before the commits
# are reverted. Long enough for two passes' fixers to land something.
RED_GRACE = _dt.timedelta(hours=2)

# The branch the post-merge job fast-forwards to each commit whose gate passed. Missing means
# no commit has been judged green yet: with no floor there is no range, so nothing is reverted.
GREEN_REF = "refs/remotes/origin/green"
BRANCH_PREFIX = "agent/revert-"
QUEUE_DIR = Path(".devkit-queue")
# The post-merge job's commit, exactly: `tidy-main.py`'s subject and author.
TIDY_SUBJECT = "Tidy: formatters, baselines and mirrors"
TIDY_AUTHOR = "github-actions[bot]"
# Subjects that are never the cause: this module's own, and a release's.
REVERT_SUBJECT = 'Revert "'
RELEASE_SUBJECT = re.compile(r"Release v\d+\.\d+\.\d+\b")
# The line a re-land's commit carries (`reland_task`), which ends the cycle at one.
RELAND_TRAILER = re.compile(r"^Reland-of:\s*(\S+)\s*$", re.M)
# Runs read to place the first red commit: past `RED_GRACE` at a busy project's rate.
RUN_WINDOW = 100
# More commits than this in one range is not a bad commit: something is wrong with `green`,
# and a mass revert would be the larger incident. Filed instead.
MOST_REVERTS = 10
# One commit per `%x1e`, its fields split by `%x1f`: the body runs over many lines, and a
# subject may hold anything but a control character.
LOG_FORMAT = "%H%x1f%P%x1f%ct%x1f%an%x1f%s%x1f%B%x1e"


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    when: float  # the commit time, which for a squash is when its PR merged
    merge: bool = False  # two parents: reverted against the first
    author: str = ""
    reland_of: str = ""  # what its `Reland-of:` trailer names

    @property
    def short(self) -> str:
        return self.sha[:7]

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}{self.short}"

    @property
    def task(self) -> Path:
        return QUEUE_DIR / f"reland-{self.short}.toml"

    @property
    def is_tidy(self) -> bool:
        return self.subject == TIDY_SUBJECT and self.author == TIDY_AUTHOR

    @property
    def is_release(self) -> bool:
        return RELEASE_SUBJECT.match(self.subject) is not None


def parse_log(text: str) -> list[Commit]:
    """`LOG_FORMAT` records as commits; one that is not a commit is left out."""
    commits = []
    for record in str(text).split("\x1e"):
        fields = record.strip("\r\n").split("\x1f", 5)  # a bare `strip` eats `\x1f` too
        if len(fields) != 6 or not fields[0] or not fields[2].isdigit():
            continue
        sha, parents, when, author, subject, body = fields
        trailer = RELAND_TRAILER.search(body)
        relands = trailer.group(1) if trailer else ""
        merge = len(parents.split()) > 1
        commits.append(Commit(sha, subject, float(when), merge, author, relands))
    return commits


def judged_runs(runs: list[dict]) -> dict[str, tuple[bool, str]]:
    """Commit -> `(passed, run id)`, newest judging run first as `gh` lists them. Only
    `success` and `failure` judge: a run that was cancelled, timed out or never started
    said nothing about the commit."""
    judged: dict[str, tuple[bool, str]] = {}
    for run in runs:
        sha = str(run.get("headSha", ""))
        conclusion = str(run.get("conclusion", "")).lower()
        finished = str(run.get("status", "")) == "completed"
        if sha and finished and conclusion in ("success", "failure"):
            judged.setdefault(sha, (conclusion == "success", str(run.get("databaseId", ""))))
    return judged


def failed_jobs(jobs: list[dict]) -> frozenset[str]:
    """The names of the jobs that failed on something the repo did: a job GitHub refused
    or never gave a runner ran no step, and one red in `fix_plan.RUNNER_STEPS` failed
    to be stood up. Empty for a run with no such job, which says nothing about its commit."""
    names = set()
    for job in jobs:
        if str(job.get("conclusion", "")).lower() != "failure":
            continue
        steps = [step for step in job.get("steps", []) or [] if isinstance(step, dict)]
        red = [s for s in steps if str(s.get("conclusion", "")).lower() == "failure"]
        if steps and not (red and all(s.get("name") in fix_plan.RUNNER_STEPS for s in red)):
            names.add(str(job.get("name", "?")))
    return frozenset(names)


def red_range(
    commits: list[Commit], verdicts: dict[str, frozenset[str]], tip_jobs: frozenset[str]
) -> list[Commit]:
    """The commits that may have turned the branch red the way the tip is red, oldest first.

    `commits` start after `green`. `verdicts` maps a commit to its failing jobs, empty for
    one the gate passed; a commit it has no entry for was not judged. A judged commit whose
    failing jobs miss `tip_jobs` ran those jobs clean, so it and everything before it are
    cleared of this red. The range ends at the oldest commit since then that overlaps them,
    and a release never ends it: `[Release vX, Y]` red on one job is Y's. Empty when none does.
    """
    start, first_red = 0, None
    for index, commit in enumerate(commits):
        jobs = verdicts.get(commit.sha)
        if jobs is None:
            continue
        if not jobs & tip_jobs:
            start, first_red = index + 1, None
        elif first_red is None and not commit.is_release:
            first_red = index
    return [] if first_red is None else commits[start : first_red + 1]


def overdue(seen: _dt.datetime, now: _dt.datetime) -> bool:
    """Whether `RED_GRACE` has passed since a pass first saw this red (`red_seen`)."""
    return now - seen >= RED_GRACE


def candidates(commits: list[Commit]) -> list[Commit]:
    """The commits in a range a revert may name: none of tidy's, a release or a revert."""
    return [
        commit
        for commit in commits
        if not (commit.is_tidy or commit.is_release or commit.subject.startswith(REVERT_SUBJECT))
    ]


def revertible(failure: fix_plan.Failure) -> bool:
    """Whether this red says anything about a commit: a default branch's, read, naming a
    failure, and neither the runner's nor the bill's nor a release's nor, while its grace
    lasts, tidy's."""
    sig = failure.signature
    if failure.kind != fix_plan.BRANCH or failure.unread or failure.lost_runs:
        return False
    if fix_plan.names_nothing(sig) or fix_plan.is_runner_side(sig):
        return False
    if fix_plan.is_billing_refused(sig) or fix_plan.is_release_red(sig):
        return False
    return failure.tidy_lapsed or not fix_plan.is_tidy_owned(sig, failure.hooks)


def _shown(signature: tuple[str, ...]) -> str:
    more = f" (+{len(signature) - 6} more)" if len(signature) > 6 else ""
    return ", ".join(signature[:6]) + more


def reland_task(commit: Commit, failure: fix_plan.Failure) -> str:
    """The queue file a revert commits, which is what keeps the work from being lost.

    `json.dumps` writes a TOML basic string: the same quotes and the same escapes, and a
    subject can hold either."""
    hours = int(RED_GRACE.total_seconds() // 3600)
    prompt = (
        f"The fix pass reverted {commit.sha} ({commit.subject}) from {failure.base}: "
        f"{failure.workflow} stayed red for {hours} hours after the fix pass first saw it, and "
        f"that commit was in the range that could have caused it. The failure: "
        f"{_shown(failure.signature)}. Read the reverted work with `git show {commit.sha}`, "
        f"land it again on a new branch with that failure fixed, and ship it, with "
        f"`Reland-of: {commit.sha}` on a line of its own in the intent's body."
    )
    return f'when = "merged"\nagent = {json.dumps(prompt, ensure_ascii=False)}\n'


def revert_intent(
    commit: Commit, failure: fix_plan.Failure, tidied: tuple[Commit, ...] = ()
) -> str:
    """The commit message the ship task commits the revert under."""
    hours = int(RED_GRACE.total_seconds() // 3600)
    also = (
        f"Tidy's {', '.join(f'`{t.short}`' for t in tidied)} rewrote these lines, so "
        "it goes too; the next tidy formats again.\n\n"
        if tidied
        else ""
    )
    if commit.reland_of:
        after = (
            f"This commit re-landed `{commit.reland_of}` and went red again, so nothing "
            "is queued: it is filed for the devkit session.\n"
        )
    else:
        after = (
            f"`{commit.task.as_posix()}` queues landing this work again with that failure "
            "fixed, once this merges.\n"
        )
    return (
        f'{REVERT_SUBJECT}{commit.subject}"\n\n'
        f"`{commit.short}` is in the range that turned {failure.workflow} red on "
        f"`{failure.base}`, which stayed red for {hours} hours. The failure: "
        f"{_shown(failure.signature)}.\n\n{also}{after}"
    )


def revert_argv(commit: Commit) -> list[str]:
    """Staged, not committed: the ship task commits it, with the queue file."""
    mainline = ["-m", "1"] if commit.merge else []
    return ["git", "revert", "--no-commit", *mainline, commit.sha]


def green_sha(checkout: Path) -> str:
    """Where `green` stands; "" when it does not exist yet."""
    done = sweep.git_for(checkout)("rev-parse", "--verify", "--quiet", GREEN_REF)
    return (done.stdout or "").strip() if getattr(done, "returncode", 1) == 0 else ""


def read_range(checkout: Path, failure: fix_plan.Failure) -> list[Commit]:
    """`red_range` as git and GitHub have it, read per commit; empty when there is no
    `green` yet, or anything it needs cannot be read: a wrong culprit costs more than a
    pass's wait."""
    if not green_sha(checkout):
        return []
    since = f"{GREEN_REF}..refs/remotes/origin/{failure.base}"
    log = sweep.git_for(checkout)("log", "--first-parent", f"--format={LOG_FORMAT}", since)
    if getattr(log, "returncode", 1) != 0:
        return []
    commits = list(reversed(parse_log(log.stdout)))
    gh = sweep.gh_for(checkout)
    judged = judged_runs(gate_evidence.default_branch_runs(gh, failure.base, limit=RUN_WINDOW))
    verdicts: dict[str, frozenset[str]] = {}
    for commit in commits:
        passed, run_id = judged.get(commit.sha, (None, ""))
        if passed:
            verdicts[commit.sha] = frozenset()
        elif passed is False:
            jobs = gate_evidence.read_jobs(gh, run_id)
            if jobs is None:
                return []
            if failed := failed_jobs(jobs):
                verdicts[commit.sha] = failed
    return red_range(commits, verdicts, verdicts.get(failure.sha, frozenset()))


def touched(checkout: Path, commit: Commit) -> frozenset[str]:
    """The paths `commit` changed against its first parent; empty when git cannot say."""
    done = sweep.git_for(checkout)("diff", "--name-only", f"{commit.sha}^1", commit.sha)
    return (
        frozenset((done.stdout or "").split())
        if getattr(done, "returncode", 1) == 0
        else (frozenset())
    )


def tidied_after(checkout: Path, commit: Commit, base: str) -> tuple[Commit, ...]:
    """Tidy's commits after `commit` on `base` that touch a file it touched, newest first."""
    span = f"{commit.sha}..refs/remotes/origin/{base}"
    log = sweep.git_for(checkout)("log", "--first-parent", f"--format={LOG_FORMAT}", span)
    if getattr(log, "returncode", 1) != 0:
        return ()
    mine = touched(checkout, commit)
    return tuple(
        later
        for later in parse_log(log.stdout)
        if later.is_tidy and mine & touched(checkout, later)
    )


def reverted_before(checkout: Path, commit: Commit) -> bool:
    """Whether this sha has had its one revert: its branch is here or on origin, or a PR
    ever headed it. A failed read counts as yes: this is the one question not worth
    guessing at, and the next pass asks again."""
    git, gh = sweep.git_for(checkout), sweep.gh_for(checkout)
    local = git("rev-parse", "--verify", "--quiet", f"refs/heads/{commit.branch}")
    if getattr(local, "returncode", 1) == 0:
        return True
    remote = git("ls-remote", "--heads", "origin", commit.branch)
    if getattr(remote, "returncode", 1) != 0 or (remote.stdout or "").strip():
        return True
    ever = gh("pr", "list", "--head", commit.branch, "--state", "all", "--json", "number")
    if getattr(ever, "returncode", 1) != 0:
        return True
    try:
        return bool(json.loads(ever.stdout or "[]"))
    except ValueError:
        return True


def discard(checkout: Path, tree: Path, branch: str) -> str:
    """The disposable tree gone, a half-done revert with it, and its branch too, so a later
    pass can try again: "", or the tail of a record line saying why not."""
    git = sweep.git_for(checkout)
    removed = git("worktree", "remove", "--force", str(tree))
    deleted = git("branch", "-D", branch)
    failed = removed if getattr(removed, "returncode", 1) != 0 else deleted
    if getattr(failed, "returncode", 1) == 0:
        return ""
    return f"; FAILED to discard {tree}: {(getattr(failed, 'stderr', '') or '').strip()}"


def _write(tree: Path, relative: Path, text: str) -> None:
    (tree / relative).parent.mkdir(parents=True, exist_ok=True)
    (tree / relative).write_text(text, encoding="utf-8")


def _file_second_red(
    project: str, commit: Commit, failure: fix_plan.Failure, journal: fix_findings.Journal
) -> None:
    """A re-land that went red too: nothing is queued, so the devkit session has it."""
    said = f"{commit.sha} {commit.subject}\nRe-lands {commit.reland_of}\n{failure.signature}\n"
    fix_findings.file(
        journal,
        "reland-red-again",
        project,
        f"{project}: re-landed work went red again; reverted, and no further re-land queued",
        fix_findings.evidence_file(said, journal.devkit_dir, "reland-red-again"),
    )


def revert_one(
    project: str,
    checkout: Path,
    commit: Commit,
    failure: fix_plan.Failure,
    journal: fix_findings.Journal,
) -> str:
    """Revert `commit`, and the tidy commits that rewrote its lines, in a fresh tree left as
    an intent for the ship task; the record's line."""
    where = f"{project} {commit.short}"
    tree, branch = fix_trees.cut_fresh_tree(checkout, commit.branch, failure.base)
    if tree is None:
        return f"{where} -- FAILED to cut {branch} off origin/{failure.base}"
    tidied = tidied_after(checkout, commit, failure.base)
    for step in (*tidied, commit):
        undone = ship_intent.run_quiet(revert_argv(step), cwd=tree)
        if undone.returncode != 0:
            said = f"{step.sha} {step.subject}\n\n{undone.stdout or ''}{undone.stderr or ''}"
            left = discard(checkout, tree, branch)
            fix_findings.file(
                journal,
                "revert-conflict",
                project,
                f"{project}: reverting a commit of a red range conflicts with {failure.base}",
                fix_findings.evidence_file(said, journal.devkit_dir, "revert-conflict"),
            )
            return f"{where} -- conflicts with {failure.base}: aborted and filed{left}"
    nothing = ship_intent.run_quiet(["git", "diff", "--cached", "--quiet"], cwd=tree)
    if nothing.returncode == 0:
        return f"{where} -- already undone on {failure.base}{discard(checkout, tree, branch)}"
    if commit.reland_of:
        _file_second_red(project, commit, failure, journal)
    else:
        _write(tree, commit.task, reland_task(commit, failure))
    _write(tree, fix_reports.ORIGIN_FILE, "fix-pass\n")
    _write(tree, ship_intent.INTENT_FILE, revert_intent(commit, failure, tidied))
    return f"{where} -- reverting {commit.subject}: {branch} left to the ship task"


def revert_range(
    ctx: fix_loop.Context, failure: fix_plan.Failure, journal: fix_findings.Journal
) -> list[str]:
    """Every commit of `failure`'s red range, newest first, once it is `overdue`."""
    project, checkout = failure.project, ctx.root / failure.project
    commits = read_range(checkout, failure)
    if not commits:
        return []
    seen = red_seen.first_seen(
        ctx.devkit_dir, project, "revert", commits[-1].sha, ctx.now, ctx.writes
    )
    if not overdue(seen, ctx.now):
        return []
    named = candidates(commits)
    if len(named) > MOST_REVERTS:
        detail = f"{project}: a red range too long to revert; `green` may have stopped moving"
        fix_findings.file(journal, "revert-range-long", project, detail)
        return [f"{project} -- {len(named)} commits in the red range: filed, none reverted"]
    lines: list[str] = []
    for commit in reversed(named):
        if reverted_before(checkout, commit):
            continue
        if ctx.writes:
            lines.append(revert_one(project, checkout, commit, failure, journal))
        else:
            lines.append(f"{project} {commit.short} -- would revert: {commit.subject}")
    return lines


def tend(
    ctx: fix_loop.Context, failures: list[fix_plan.Failure], journal: fix_findings.Journal
) -> list[str]:
    """The fix pass's step: `revert_range` for each direct-flow default branch that is red
    on evidence that implicates a commit; lines for the record."""
    lines: list[str] = []
    for failure in failures:
        if failure.project in ctx.direct and revertible(failure):
            lines += revert_range(ctx, failure, journal)
    return lines


def mark_tidy_lapsed(
    ctx: fix_loop.Context, failures: list[fix_plan.Failure]
) -> list[fix_plan.Failure]:
    """`failures`, each red `fix_plan.owned_by_tidy` would skip marked `tidy_lapsed` once
    `fix_plan.TIDY_GRACE` has passed since a pass first saw it. The plan is pure, so what
    the state file says reaches it on the failure. `green` standing still is what makes
    it the same red: the signature alone comes back on a later, unrelated day."""

    def lapsed(failure: fix_plan.Failure) -> bool:
        if not fix_plan.owned_by_tidy(failure, ctx.direct):
            return False
        key = f"{green_sha(ctx.root / failure.project)} {' '.join(failure.signature)}"
        seen = red_seen.first_seen(
            ctx.devkit_dir, failure.project, "tidy", key, ctx.now, ctx.writes
        )
        return ctx.now - seen >= fix_plan.TIDY_GRACE

    return [replace(f, tidy_lapsed=True) if lapsed(f) else f for f in failures]
