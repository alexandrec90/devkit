#!/usr/bin/env python3
"""Take a finished after-merge task's file out of the repo: one PR, at most daily.

A done task's file is litter the next reader has to rule out, and its done ref outlives
it, so nothing is lost by removing it. Split out of `after-merge-pass.py` along the seam
its imports show: this half writes to the repository, and that one to GitHub's refs.

No tree is cut: the commit is built in a throwaway index against `origin/<base>`, as
`fix_send.push_clean_merge` builds its merge, so nothing touches a checkout a person may
be in. The push skips the pre-push gate for that same reason: CI gates the PR. The label
is the project's flow's (`after_merge.LABELS`), so the PR needs nobody. A claim per day
keeps a second PC from opening a second PR, and an open one from an earlier day is left
to finish rather than joined by another.

Claims past use are deleted here too (`job_claims.prune`): it is the one daily step that
already holds a project's refs.

Tested in `tests/test_after_merge_pass.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import after_merge
import job_claims
import release
import sweep
import task_branch as tb

BRANCH = "after-merge-tidy"
TITLE = "Remove finished after-merge tasks"
PUSH_SECONDS = 120


def daily(board, cycle, tidied: dict[str, str], force: bool = False) -> None:
    """`board`'s tidy, unless this machine already ran it today; `tidied` keeps the date."""
    today = _dt.datetime.fromtimestamp(cycle.seams.now(), _dt.UTC).date().isoformat()
    if tidied.get(board.project) == today and not force:
        return
    tidied[board.project] = today
    git = cycle.seams.git_for(board.path)
    job_claims.prune(git, job_claims.prunable(board.refs, cycle.seams.now()))
    finished = after_merge.removable(board.tasks, board.done, cycle.hosts)
    if finished:
        cycle.say(open_pr(board, finished, today, cycle))


def open_pr(board, finished: list[after_merge.Task], today: str, cycle) -> str:
    """The PR removing `finished`; the line for the record."""
    project, seams = board.project, cycle.seams
    git, gh = seams.git_for(board.path), seams.gh_for(board.path)
    listed = gh("pr", "list", "--state", "open", "--limit", "200", "--json", "headRefName")
    if listed.returncode != 0 or open_tidy(listed.stdout):
        return f"{project}: tidy waits on its open PR"
    attempt = job_claims.claim(git, f"tidy-{today}", job_claims.TIDY, seams.host, seams.now())
    if not attempt.won:
        return f"{project}: tidy not claimed ({attempt.outcome})"
    commit = removal_commit(git, board.base, finished)
    if not commit:
        return f"{project}: tidy could not build its commit"
    # A name per day: the branch policy refuses a push to a branch whose PR has merged.
    branch = tb.branch_name(BRANCH, set(), _dt.date.fromisoformat(today))
    with sweep.until(time.monotonic() + PUSH_SECONDS):
        pushed = git("push", "origin", f"{commit}:refs/heads/{branch}", env=release.push_env())
    if pushed.returncode != 0:
        return f"{project}: tidy could not push: {(pushed.stderr or '').strip()[:160]}"
    label = after_merge.LABELS[board.flow]
    gh(*sweep._label_args(label))
    made = gh(
        "pr",
        "create",
        "--base",
        board.base,
        "--head",
        branch,
        "--title",
        TITLE,
        "--body",
        pr_body(finished),
        "--label",
        label,
    )
    if made.returncode != 0:
        return f"{project}: tidy could not open its PR: {(made.stderr or '').strip()[:160]}"
    return f"{project}: tidy opened {(made.stdout or '').strip()} for {len(finished)} tasks"


def open_tidy(listing: str | None) -> bool:
    """A `gh pr list` of head branches holds an earlier day's tidy."""
    prefix = f"{tb.BRANCH_PREFIX}{BRANCH}"
    heads = (str(pr.get("headRefName", "")) for pr in json.loads(listing or "[]"))
    return any(head.startswith(prefix) for head in heads)


def pr_body(finished: list[after_merge.Task]) -> str:
    listed = "\n".join(f"- `{task.path}` (`{task.id}`)" for task in finished)
    return (
        "These after-merge tasks are done, so their files have nothing left to say. Each "
        f"task's done ref stays on GitHub.\n\n{listed}\n"
    )


def removal_commit(git: sweep.Git, base: str, finished: list[after_merge.Task]) -> str:
    """A commit on `origin/<base>` without `finished`'s files; "" if a step failed."""
    with tempfile.TemporaryDirectory(prefix="devkit-tidy-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        tip = f"origin/{base}"
        steps = [
            ("read-tree", tip),
            ("update-index", "--force-remove", "--", *(task.path for task in finished)),
        ]
        if any(git(*step, env=env).returncode != 0 for step in steps):
            return ""
        tree = git("write-tree", env=env)
        made = git("commit-tree", (tree.stdout or "").strip(), "-p", tip, "-m", TITLE)
        ok = tree.returncode == 0 and made.returncode == 0
        return (made.stdout or "").strip() if ok else ""
