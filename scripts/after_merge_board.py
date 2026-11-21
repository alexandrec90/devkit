#!/usr/bin/env python3
"""One project's after-merge queue, read off `origin` with nothing but git and `gh`.

The reading half of `after-merge-pass.py`, split along the seam its imports show: this
only asks, and that one acts. **The queue comes from `origin/<default>` after a fetch**,
through `git ls-tree` and `git show`, never from a working tree: there a task from an
unmerged branch would run, and one merged since the checkout last moved would not.

The one fetch of a cycle brings the `refs/devkit/*` refs too, so what is claimed and what
is done is read locally, and a claim another PC holds costs this one no call to GitHub.
Tags come with it by git's own following, which `released` relies on.

Tested in `tests/test_after_merge_board.py`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import after_merge
import job_claims
import sweep
import task_branch as tb

Task = after_merge.Task

GREEN_REF = "refs/remotes/origin/green"
FETCH = (
    "fetch",
    "--quiet",
    "--prune",
    "origin",
    "+refs/heads/*:refs/remotes/origin/*",
    f"+{job_claims.REFS}*:{job_claims.REFS}*",
)


@dataclass(frozen=True)
class Board:
    """One project's queue as `origin` has it now."""

    project: str
    path: Path
    base: str = ""
    flow: str = after_merge.GATED
    tasks: tuple[Task, ...] = ()
    refs: tuple[job_claims.Ref, ...] = ()
    error: str = ""

    @property
    def done(self) -> frozenset[str]:
        return job_claims.done_ids(self.refs)

    @cached_property
    def marks(self) -> after_merge.Marks:
        """The state of each task, as the refs fetched with this board say it."""
        handed = {
            key: found
            for key, ref in job_claims.keys_under(self.refs, job_claims.HANDED).items()
            if (found := after_merge.parse_handed(ref.subject, ref.at))
        }
        return after_merge.Marks(
            self.done,
            handed,
            frozenset(job_claims.keys_under(self.refs, job_claims.FAILED_REFS)),
            {k: r.at for k, r in job_claims.keys_under(self.refs, job_claims.REROUTED).items()},
        )

    @property
    def successes(self) -> frozenset[str]:
        """The done keys whose subject carries a success, which is what a `task:` waits on."""
        finished = job_claims.keys_under(self.refs, job_claims.DONE)
        return frozenset(
            key
            for key, ref in finished.items()
            if after_merge.is_success(after_merge.status_of(ref.subject))
        )

    @property
    def done_names(self) -> dict[str, frozenset[str]]:
        """Each name a successful done ref's subject carries, with the keys it finished as:
        `done <project>/<name> by <host>: <how>`. All a tidied file leaves behind."""
        names: dict[str, set[str]] = {}
        for key, _at, name in self.done_rows:
            if key in self.successes:
                names.setdefault(name, set()).add(key)
        return {name: frozenset(keys) for name, keys in names.items()}

    @property
    def done_rows(self) -> list[tuple[str, float, str]]:
        """`(key, when, name)` of every done ref."""
        rows = []
        for key, ref in job_claims.keys_under(self.refs, job_claims.DONE).items():
            words = ref.subject.split()
            if len(words) > 1 and words[0] == "done":
                rows.append((key, ref.at, words[1].rpartition("/")[2]))
        return rows


def said_by(done: object) -> str:
    """The last line a git call complained in; a reason, not a transcript."""
    lines = str(getattr(done, "stderr", "") or "").strip().splitlines()
    return lines[-1] if lines else f"exit {getattr(done, 'returncode', '?')}"


def read_task(git: sweep.Git, project: str, base: str, path: str, blob: str) -> Task | None:
    """The task at `path`, blob `blob`, of `origin/<base>`; None when either cannot be read,
    which is the next cycle's to read, so nothing is dropped."""
    added = git("log", "--diff-filter=A", "-1", "--format=%H %ct", f"origin/{base}", "--", path)
    parts = (added.stdout or "").split()
    text = git("show", blob)
    if added.returncode != 0 or len(parts) != 2 or not parts[1].isdigit() or text.returncode != 0:
        return None
    identity = after_merge.task_id(path, parts[0])
    task = after_merge.parse(project, path, identity, text.stdout or "")
    return replace(task, commit=parts[0], added=float(parts[1]))


def read_tasks(git: sweep.Git, project: str, base: str) -> list[Task]:
    """Every file under the queue directory at `origin/<base>`, oldest commit first."""
    listed = git("ls-tree", "-r", "-z", f"origin/{base}", "--", f"{after_merge.QUEUE_DIR}/")
    tasks = []
    for entry in (listed.stdout or "").split("\0"):
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and fields[1] == "blob":
            task = read_task(git, project, base, path, fields[2])
            tasks += [task] if task else []
    return sorted(tasks, key=lambda task: (task.added, task.path))


def current(git: sweep.Git, task: Task, base: str) -> tuple[Task | None, str]:
    """`task` as origin has it this moment: `(the task as it now reads, "")`, `(None, "")`
    when the file is gone or is another task now, `(None, why)` when origin cannot say.

    A pass is minutes long, and a revert landing while an earlier task ran must stop this
    one. So this is asked of a fresh fetch, right before a task runs. An edit to a task
    not yet run is what the fresh reading carries.
    """
    fetched = git("fetch", "--quiet", "origin", base)
    if fetched.returncode != 0:
        return None, f"origin could not be read: {said_by(fetched)}"
    listed = git("ls-tree", "-z", f"origin/{base}", "--", task.path)
    fields = (listed.stdout or "").split("\t", 1)[0].split()
    if listed.returncode != 0:
        return None, f"origin could not be read: {said_by(listed)}"
    if len(fields) != 3:
        return None, ""
    fresh = read_task(git, task.project, base, task.path, fields[2])
    if fresh is None:
        return None, "origin could not be read"
    return (fresh, "") if fresh.id == task.id else (None, "")


def read_board(project: str, path: Path, git: sweep.Git) -> Board:
    """`project`'s queue after a fetch, or a `Board` saying why there is none this cycle."""
    fetched = git(*FETCH)
    if fetched.returncode != 0:
        return Board(project, path, error=f"fetch failed: {said_by(fetched)}")
    base = tb.detect_default_branch(git, fallback="main")
    toml = git("show", f"origin/{base}:.devkit.toml")
    return Board(
        project,
        path,
        base,
        after_merge.flow_of(toml.stdout or "" if toml.returncode == 0 else ""),
        tuple(read_tasks(git, project, base)),
        tuple(job_claims.read_refs(git)),
    )


def facts_for(
    board: Board, boards: dict[str, Board], host: str, docker: bool, git: sweep.Git, gh: sweep.Git
) -> after_merge.Facts:
    """`Facts` answering from `board`'s checkout, and from `boards` for any project's task."""

    def green(commit: str) -> bool:
        # See `after_merge`'s docstring: no branch yet is merged in a gated project, not yet
        # in a direct one. Once it exists, it is the answer in either.
        if git("rev-parse", "--verify", "--quiet", GREEN_REF).returncode != 0:
            return board.flow != after_merge.DIRECT
        return bool(commit) and (
            git("merge-base", "--is-ancestor", commit, "origin/green").returncode == 0
        )

    def released(commit: str) -> bool:
        return bool(commit) and bool((git("tag", "--contains", commit).stdout or "").strip())

    def merged(repo: str, number: int) -> bool:
        # `{owner}` is `gh`'s own placeholder: this checkout's owner, for a bare repo name.
        slug = repo if "/" in repo else f"{{owner}}/{repo}"
        done = gh("api", f"repos/{slug}/pulls/{number}", "--jq", ".merged")
        return done.returncode == 0 and (done.stdout or "").strip() == "true"

    def done(project: str, name: str) -> bool:
        other = boards.get(project)
        if other is None:
            return False
        return after_merge.named_done(name, other.tasks, other.successes, other.done_names, host)

    return after_merge.Facts(host, docker, green, released, merged, done)
