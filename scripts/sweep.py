#!/usr/bin/env python3
"""Cross-project ship sweep: find work stranded across the workspace's checkouts.

One `/ship` run takes one checkout from "task finished" to "PR open". This walks
every checkout in a VS Code multi-root workspace and reports which ones still have
work in them, so nothing sits forgotten on a branch (or, more often, forgotten on
the default branch) while attention is elsewhere.

There are two ways to finish a sweep, and which one to use is a real choice.

`/ship` runs per-repo with the diff in context and writes a commit message that
describes the change, because that is what a commit message is for. `--ship` here
runs unattended across the whole workspace and cannot do that: nothing reads the
diff, so it commits everything on the task branch under a message that describes
*the sweep*, and says so in the PR body. That is a worse PR and a
better outcome than the alternative it replaces, which was work sitting
uncommitted on a home branch for weeks because finishing the sweep needed an
agent per repo and attention had moved on.

Prefer `/ship` for work you intend to merge. Use `--ship` to make sure nothing is
stranded, then retitle or split the PRs it opens.

Modes:
  (default)   human-readable table -- the testing/inspection mode.
  --json      the same verdicts as JSON, for a driver to fan out over.
  --check     exit 1 when any repo needs action, 2 when any is blocked. For a
              root-level task that should fail loudly rather than print quietly.
  --branch    cut an `agent/...` branch under work stranded on a branch that
              cannot be shipped from -- a home branch, or a task branch the
              branch policy has retired. Step 1 of the sweep.
  --ship      commit what is on each task branch, push it, open its PR.
              Step 2 -- the unattended alternative to `/ship` per repo.
  --sync      park each worktree back on its home branch, fast-forward it to
              `origin/<default>`, and delete the task branches that have merged.
              Step 3 -- run it once the PRs from step 2 are merged.

**The reporting modes never touch a repository.** `--branch`, `--ship` and
`--sync` do, and all three print their plan and change nothing unless `--yes` is
also passed. `--branch` and `--sync` emit only git commands that refuse rather
than destroy: `merge --ff-only` never rewrites, `branch -d` never deletes
unmerged work, and there is no `-D` anywhere -- when `-d` refuses a branch whose
work is provably in the base, `--sync` corrects what it is asking rather than
forcing past the answer (see `sync_plan`). `--ship` is the one mode that
publishes -- it pushes and opens a
PR -- so it never force-pushes, never bypasses a hook, and touches only the task
branch it was already standing on.

The classification contract that makes "nothing stranded" checkable: `classify()`
is total -- every repo lands in exactly one verdict -- and every verdict except
`clean`/`skipped` has a non-empty `plan_for()`. Tested in `tests/test_sweep.py`.

Pure and stdlib-only. All git access goes through an injected callable so the
decision logic is unit-testable without spawning git.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]

WORKSPACE_FILE_NAME = "alex-projects.code-workspace"
# Where the ephemeral tier keeps its boxes, relative to the workspace root, and the
# separator between project and topic in a box name (`devkit--fix-thing-0810`). Defined
# here rather than in `worktree.py` because the two resolvers below need them and
# worktree.py imports this module, never the other way round.
#
# `scripts/sync-codex-hooks.py` keeps a private copy of BOXES_DIR_NAME and must: it is
# VENDORED, and a consuming project has no `sweep.py` to import. That one is a
# deliberate duplicate, not a miss.
BOXES_DIR_NAME = ".worktrees"
NAME_SEP = "--"


def default_workspace(repo_root: Path, name: str = WORKSPACE_FILE_NAME) -> Path:
    """The workspace registry for a checkout at `repo_root`, box or not.

    The file lives beside the checkouts it lists, one level above a static checkout --
    but a **box** sits one level deeper, under `<workspace>/.worktrees/`, so the naive
    answer from inside one is `<workspace>/.worktrees/alex-projects.code-workspace`,
    which nobody ever wrote.

    That mattered because a box is exactly where an agent runs these scripts. Every
    workspace-aware script had its own copy of the naive line, and five of them broke
    there in different ways: `devkit_project.py --adopt-tasks` -- the documented way to
    record an intentional task-block edit -- exited 2 naming a path that does not exist,
    while `workspace-status.py` degraded to silence, which is the same failure with no
    symptom at all. `worktree.py` was alone in resolving both, so this is its logic
    moved down to where everything else can reach it.
    """
    parent = repo_root.parent
    return (parent.parent if parent.name == BOXES_DIR_NAME else parent) / name


def source_checkout(repo_root: Path) -> Path:
    """The permanent checkout `repo_root` belongs to -- itself, unless it is a box.

    The other half of the same problem `default_workspace` solves. A script that asks
    "which registered checkout am I?" gets the box's name (`devkit--topic-0810`), which
    matches nothing in the registry, and the answer is wrong in the direction that is
    hardest to notice: `workspace-status.py` exempts devkit from its adoption check by
    comparing each registered checkout against its own root, so from a box the
    comparison failed and the status line at every session start reported **devkit
    itself** as "never vendored devkit".

    The project name is read off the box name rather than assumed, so this holds for a
    box cut from any project, not just devkit's.
    """
    parent = repo_root.parent
    if parent.name != BOXES_DIR_NAME:
        return repo_root
    return parent.parent / repo_root.name.split(NAME_SEP, 1)[0]


DEFAULT_WORKSPACE = default_workspace(REPO_ROOT)

# Checkouts in the workspace that this sweep does not manage. VanillaLand is the
# legacy Azure DevOps monolith: different host, different PR API, `develop` base,
# and it is a reference checkout rather than something we ship from.
DEFAULT_EXCLUDE: frozenset[str] = frozenset({"VanillaLand"})

Git = Callable[..., "subprocess.CompletedProcess[str]"]

# Windows only, and load-bearing for the scheduled reconcile pass.
#
# A process that has no console of its own gets Windows to allocate a **brand new
# console window** for every console child it spawns. `pythonw.exe` is exactly such a
# process, and it is what the scheduled `reconcile` runs under so the pass itself opens
# no window -- so without this flag, suppressing the parent's window turns one window
# every fifteen minutes into ~40 flickering open and shut, which is strictly worse than
# the thing it was meant to fix.
#
# Applied to every spawn in the reconcile path (git, gh, docker, the provision ladder)
# rather than only the obvious ones: the sheer count is what makes this visible, so a
# single unflagged call site inside a per-box loop brings the whole flicker back.
#
# `CREATE_NO_WINDOW` does not exist off Windows. `getattr` supplies zero there, the
# portable no-op, and keeps each `subprocess.run` call's keyword shape explicit enough
# for mypy to check on both platforms.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def console_python() -> str:
    """The console interpreter beside `sys.executable`, for spawning a Python child.

    `NO_WINDOW` is necessary and **not sufficient**, and the gap between the two is
    what put a sixteen-second `ensurepip` window on screen every night. Windows
    ignores `CREATE_NO_WINDOW` for a **GUI-subsystem** child: `pythonw.exe` has no
    console to suppress, so the flag is a no-op there and the child is left
    console-*less* -- which is precisely the condition that makes Windows allocate a
    fresh visible console for each of *its* children. The flag protects the one hop it
    is passed to and then loses the entire subtree behind it.

    A console-subsystem child spawned with `CREATE_NO_WINDOW` gets a real console that
    is merely hidden, and **every descendant inherits it**. One flagged hop to
    `python.exe` therefore covers `-m venv` and the `ensurepip` it re-spawns, `-m
    pre_commit` and each hook under it, and whatever a delegate script reaches -- none
    of which take a `creationflags` argument from here.

    So under a scheduled job, where `sys.executable` *is* `pythonw.exe`, spawn a Python
    child as `console_python()` with `creationflags=NO_WINDOW`; neither alone is
    enough. Everywhere else `sys.executable` already names a console interpreter and
    this is the identity.
    """
    executable = Path(sys.executable)
    if executable.name.lower() != "pythonw.exe":
        return sys.executable
    console = executable.with_name("python.exe")
    # An embedded install could ship `pythonw.exe` with no console twin. Falling back
    # keeps the spawn working -- with a window -- rather than raising from a hook.
    return str(console) if console.exists() else sys.executable


# --- verdicts ---------------------------------------------------------------
# Ordered roughly by how much attention each needs.
BLOCKED = "blocked"  # a human has to look; the sweep will not guess
NEEDS_BRANCH = "needs-branch"  # work sitting on a branch it cannot be shipped from
NEEDS_REBRANCH = "needs-rebranch"  # work on a task branch the policy has retired
READY = "ready"  # task branch with content -- /ship it
NEEDS_PR = "needs-pr"  # task branch pushed, PR may not exist
NEEDS_PULL = "needs-pull"  # clean on its home branch, just behind
SPENT = "spent-branch"  # parked on a task branch with nothing on it -- sync it home
CLEAN = "clean"  # nothing to do
SKIPPED = "skipped"  # not a git checkout

# Verdicts that mean "there is work here". `--check` exits non-zero on these.
ACTIONABLE: frozenset[str] = frozenset({
    BLOCKED, NEEDS_BRANCH, NEEDS_REBRANCH, READY, NEEDS_PR, NEEDS_PULL, SPENT
})  # fmt: skip
# Verdicts with no next action. Every *other* verdict must yield a plan.
TERMINAL: frozenset[str] = frozenset({CLEAN, SKIPPED})

# Verdicts each mutating mode acts on. Pairwise disjoint by construction: step 1
# moves work onto task branches, step 2 commits and publishes it, step 3 tidies up
# once the resulting PRs have merged, and none touches a repo another one owns.
BRANCHABLE: frozenset[str] = frozenset({NEEDS_BRANCH, NEEDS_REBRANCH})
SHIPPABLE: frozenset[str] = frozenset({READY, NEEDS_PR})
SYNCABLE: frozenset[str] = frozenset({SPENT, NEEDS_PULL, CLEAN})

# How many changed paths the generated PR body lists before it summarises the
# rest. A sweep that parks a long-neglected tree can carry hundreds; the point of
# the list is to make the PR skimmable, and past this it stops being.
PR_BODY_FILE_LIMIT = 50

# The `-<mmdd>` stamp `tb.branch_name` appends, plus its `-N` collision suffix.
# Stripped to recover the topic a task branch was named for.
_DATE_SUFFIX_RE = re.compile(r"-\d{4}(?:-\d+)?$")

# Per-worktree marker recording which branch this worktree calls home, written
# under the worktree's own git dir (`git rev-parse --git-path`) so two worktrees
# of one repo get separate values -- the same mechanism `tb.worktree_file` resolves.
# Needed because a *linked* worktree cannot park on the default branch: git allows
# one checkout of a branch at a time, and the primary worktree already holds it.
ANCHOR_MARKER_NAME = "agent-anchor"


@dataclass(frozen=True)
class State:
    """Everything the classifier needs about one checkout.

    Built by `inspect()` from git, or by hand in tests. `behind`/`ahead` are
    measured against `origin/<default_branch>`; `unpushed` against the branch's
    own upstream (-1 when it has none, which is different from 0).

    The fields after `remote_url` exist for `--sync`, which has to decide where a
    worktree goes *after* its task branch is spent -- a question the report modes
    never ask.
    """

    name: str
    path: str = ""
    is_git: bool = True
    host: str = "other"
    default_branch: str = ""
    branch: str = ""
    dirty: int = 0
    # The changed paths behind `dirty`, for the generated PR body. Empty is a
    # legitimate value for a hand-built State: the body degrades to the count.
    dirty_files: tuple[str, ...] = ()
    behind: int = 0
    ahead: int = 0
    upstream: str = ""
    unpushed: int = -1
    # True when this branch has an upstream *configured* that no longer resolves --
    # it was pushed once and `origin/<branch>` has since been deleted. `upstream` is
    # "" for that case and for a branch that was never pushed at all, so without this
    # the two are indistinguishable, and one of them is stranded work.
    upstream_gone: bool = False
    # True when GitHub has a *merged* PR for this branch name. The second, authoritative
    # answer to the same question `upstream_gone` asks cheaply -- see `is_retired`.
    pr_merged: bool = False
    remote_url: str = ""
    # True for a linked worktree (`.git` is a file, not a directory).
    linked: bool = False
    # This worktree's recorded home branch, from ANCHOR_MARKER_NAME; "" when unset.
    anchor: str = ""
    # Local branch names, and the managed task branches already merged into
    # origin/<default_branch> -- what `--sync` deletes.
    local_branches: tuple[str, ...] = ()
    merged_task_branches: tuple[str, ...] = ()
    # Local branches carrying commits their own upstream lacks. `branch -d` asks
    # about the upstream rather than about the base branch, so these are exactly
    # the branches it refuses to delete however merged they are -- see `sync_plan`.
    ahead_of_upstream: tuple[str, ...] = ()
    # Branches checked out in *any* worktree of this repo, this one included.
    # Reaping is repo-wide but a checkout is per-worktree, so without this a
    # sibling's live branch looks like a merged branch nobody is using.
    worktree_branches: tuple[str, ...] = ()
    # Absolute path to the ref store behind this checkout. Linked worktrees of one
    # repo share it; two clones of one remote do not. Keys `dedupe_reaps`.
    git_common_dir: str = ""


@dataclass
class Result:
    """A classified checkout: its state, its verdict, and what to do about it."""

    state: State
    verdict: str
    reason: str
    plan: list[str] = field(default_factory=list)


# The label that marks a PR as mergeable without a human: `worktree.py reconcile
# --merge --merge-label automerge` merges it locally, and the vendored
# `dependabot-automerge.yml` merges it server-side once the PR Gate passes. Only
# someone with write access can apply a label, which is what makes it an
# authorization rather than a request.
AUTOMERGE_LABEL = "automerge"

# Colour and description per label `ensure_pr` may need to create. The `automerge`
# spelling matches `dependabot-automerge.yml`'s `gh label create` exactly -- both
# use `--force`, so two spellings would flip the label back and forth per run.
LABEL_SPECS: dict[str, tuple[str, str]] = {
    AUTOMERGE_LABEL: ("0e8a16", "PR the harness may merge once the gate passes"),
}


@dataclass(frozen=True)
class Plan:
    """A mutation `--branch`/`--sync` would perform on one checkout.

    `steps` are git argv fragments (no `git`, no `-C <path>` -- the runner binds
    those), run in order, stopping at the first failure. `anchor` is the branch to
    record in ANCHOR_MARKER_NAME afterwards, "" for none. A non-empty `refusal`
    means do nothing at all and say why: an empty `steps` with no refusal is the
    already-correct case, which is not the same thing.

    `pr_title` is the one action that is not git: non-empty means open (or reuse)
    a PR once every step has succeeded. It is a separate field rather than
    another entry in `steps` so that `steps` stays homogeneous -- every existing
    caller, renderer and safety test reads it as "git argv and nothing else", and
    a `gh` fragment hiding in there would quietly falsify all of them.

    `pr_labels` are applied to the PR whether it was created or reused -- a reused
    PR may predate the caller learning to label -- and each is created in the repo
    first, because both `gh pr create --label` and `gh pr edit --add-label` fail
    outright on a label the repo has never carried.
    """

    steps: tuple[tuple[str, ...], ...] = ()
    anchor: str = ""
    refusal: str = ""
    pr_title: str = ""
    pr_body: str = ""
    pr_head: str = ""
    pr_base: str = ""
    pr_labels: tuple[str, ...] = ()

    @property
    def acts(self) -> bool:
        """True when this plan would change something -- steps, a PR, or both."""
        return bool(self.steps or self.pr_title)


# --- pure helpers -----------------------------------------------------------


def parse_workspace(text: str, exclude: frozenset[str] = DEFAULT_EXCLUDE) -> list[str]:
    """Folder names from a `.code-workspace` file, minus `exclude`, in file order.

    Parsed through `devkit_jsonc`, not `json`: the workspace file is hand-edited,
    carries comments, and now also holds the shared task block — under the stdlib
    parser every one of those is a decode error, and the `except` below would turn
    it into a silent "no checkouts" instead of a visible failure.

    Still returns [] for text that is malformed for any other reason, rather than
    raising: a broken workspace file should make the sweep report nothing, not
    crash a root-level task.
    """
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    names: list[str] = []
    for folder in payload.get("folders", []):
        if not isinstance(folder, dict):
            continue
        path = folder.get("path")
        if not isinstance(path, str) or not path:
            continue
        # `path` is workspace-relative; its last segment is the checkout name.
        name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name and name not in exclude and name not in names:
            names.append(name)
    return names


def select(names: list[str], only: list[str] | None) -> tuple[list[str], list[str]]:
    """`(kept, unknown)` -- the checkouts to sweep, and any `--only` name that is not one.

    Unknown names are returned rather than ignored. A typo'd `--only caramelli` that
    silently swept nothing would report "nothing stranded", which is the one answer
    this tool must never give wrongly.
    """
    if not only:
        return names, []
    # VS Code command inputs must resolve to one string, so the workspace's
    # multi-pick passes `--only=a,b`. The CLI's repeatable `--only a --only b`
    # remains supported; both forms normalize here.
    wanted = {name.strip() for value in only for name in value.split(",") if name.strip()}
    return [name for name in names if name in wanted], sorted(wanted - set(names))


def remote_host(url: str) -> str:
    """Which forge a remote URL points at -- decides which PR API `/ship` uses."""
    lowered = url.lower()
    if "github.com" in lowered:
        return "github"
    if "dev.azure.com" in lowered or "visualstudio.com" in lowered:
        return "azure"
    return "other" if lowered else "none"


def parse_porcelain(text: str) -> tuple[str, ...]:
    """Changed paths from `git status --porcelain`, in the order git reports them.

    The count of these is `State.dirty`; the paths themselves are what the
    generated PR body lists. Each line is `XY <path>`, so the path starts at
    column 3. A rename reads `R  old -> new`; the new path is the one that
    exists afterwards, so that is the side kept.
    """
    paths: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        # Git quotes paths containing spaces or non-ASCII; the quotes are display
        # only and would read as part of the name in the PR body.
        if len(path) > 1 and path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path:
            paths.append(path)
    return tuple(paths)


def parse_worktree_branches(text: str) -> tuple[str, ...]:
    """Branch names held by the repo's worktrees, from `git worktree list --porcelain`.

    Detached worktrees emit `detached` instead of a `branch` line and are simply
    absent from the result -- they hold no branch, so they block no delete.
    """
    prefix = "branch refs/heads/"
    return tuple(
        line[len(prefix) :].strip()
        for line in text.splitlines()
        if line.startswith(prefix) and line[len(prefix) :].strip()
    )


def parse_branch_upstreams(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(every local branch, those ahead of their upstream)`, from one `for-each-ref`.

    Reads `%(refname:short)<TAB>%(upstream:track)`. A tab is not a legal character
    in a ref name, so nothing on the left of the split can be part of a branch.

    `%(upstream:track)` is git's own answer to the question `branch -d` asks, which
    is why it is read instead of the ahead/behind counts against the *base* branch:

    - empty, or `[behind N]` -- the branch is an ancestor of its upstream, so `-d`
      takes it. Empty also covers a branch with no upstream configured at all.
    - `[gone]` -- the upstream is configured but its ref no longer resolves (the
      shape a merged-and-deleted PR branch leaves), so `-d` falls back to HEAD.
    - `[ahead N]`, with or without a `behind` -- the branch carries commits the
      upstream does not have, and `-d` refuses regardless of the base branch.
    """
    names: list[str] = []
    ahead: list[str] = []
    for line in text.splitlines():
        name, _, track = line.partition("\t")
        name = name.strip()
        if not name:
            continue
        names.append(name)
        if "ahead" in track:
            ahead.append(name)
    return tuple(names), tuple(ahead)


def parse_ahead_behind(text: str) -> tuple[int, int]:
    """(behind, ahead) from `rev-list --left-right --count base...HEAD`; (0, 0) if unparseable."""
    parts = text.split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def is_task_branch(branch: str) -> bool:
    """True for a task branch whose lifecycle devkit is allowed to manage.

    Everything else is a *home* branch: the default branch, or a long-lived
    worktree anchor like `carameli-b`. The distinction is the whole basis of the
    branch axis below, so it is one predicate rather than a repeated startswith.
    """
    return tb.is_managed_task_branch(branch)


def is_retired(state: State) -> bool:
    """True for work sitting on a task branch that can never be committed to again.

    The shape a merged PR leaves behind: the branch was pushed, GitHub deleted it on
    merge, and `git fetch --prune` dropped the remote-tracking ref -- so the upstream
    is still configured but no longer resolves. `git_policy.evaluate_pre_commit`
    permanently retires exactly this branch, so *every* commit to it is refused.

    That makes work on it stranded in the same way work on a home branch is stranded:
    the branch underneath it is wrong, and the fix is a fresh one. The difference is
    only how it got there, which is why `--branch` handles both.

    Work is required. A retired branch with nothing on it is merely `spent`, and
    `--sync` deletes it -- cutting a branch to preserve nothing would leave a second
    dead branch behind.

    Two signals, because the cheap one is only a convention. `upstream_gone` reads the
    `branch.<name>.remote` entry git happens to leave behind, and anything that rewrites
    branch config clears it: an explicit `--unset-upstream`, a re-`checkout -b` over the
    same name, a worktree re-point. devkit hit that -- PR #22 merged, config entry empty,
    so the checkout read as READY and `--ship` planned a commit `git_policy` rejects on
    every run, reporting "git refused, the state needs a human" for a state that is
    entirely mechanical. `pr_merged` is the same question asked of the same authority the
    *hook* consults, which is what stops classification and enforcement disagreeing.
    """
    retired = state.upstream_gone or state.pr_merged
    return is_task_branch(state.branch) and retired and bool(state.dirty or state.ahead)


def home_ref(state: State) -> str:
    """The branch this worktree parks on between tasks; "" when it cannot be resolved.

    In order: the recorded anchor; the current branch if it is already a home
    branch; the default branch for a primary worktree; and for a linked worktree
    with none of those, a local branch named after the checkout directory. The
    last fallback is what lets a never-before-synced worktree find its way home
    (`carameli-b` the directory -> `carameli-b` the branch) without guessing at a
    branch that does not exist.

    A linked worktree cannot fall back to the default branch the way a primary can
    -- the primary already has it checked out and git permits only one holder --
    so exhausting the list returns "" and `sync_plan` refuses rather than guesses.
    """
    if state.anchor:
        return state.anchor
    if state.branch and not is_task_branch(state.branch):
        return state.branch
    if not state.linked:
        return state.default_branch
    return state.name if state.name in state.local_branches else ""


def classify(state: State) -> tuple[str, str]:
    """(verdict, reason) for one checkout. Total: every state gets exactly one verdict.

    The branch axis is what matters, and it splits on `is_task_branch`, not on
    "is this the default branch". Work on *any* home branch is stranded: `/ship`
    would happily open a PR from a long-lived worktree anchor like `carameli-b`
    (ship.py's `is_shippable` only refuses the default branch), which quietly
    turns a permanent worktree branch into a one-off PR branch. Both cases need
    the same fix -- a task branch cut underneath the work -- so both get
    `needs-branch` and `--branch` handles them identically.

    On a task branch, content means shippable and the only question is how far
    along it is. *No* content means the branch is spent -- merged, or cut and
    never used -- which is `spent-branch` rather than `clean` because the worktree
    is still parked on it and `--sync` has something to do.
    """
    if not state.is_git:
        return SKIPPED, "not a git checkout"
    if not state.default_branch:
        return BLOCKED, "cannot resolve origin/HEAD -- no base branch to ship against"
    if not state.branch:
        return BLOCKED, "detached HEAD -- check out a branch before shipping"
    if state.host == "none":
        return BLOCKED, "no origin remote -- nowhere to push"

    if not is_task_branch(state.branch):
        # An anchor is not the default branch, so say which branch is affected --
        # "on carameli-b" and "on main" need different fixes from the reader.
        where = state.branch
        if state.branch != state.default_branch:
            where += f" (not a {tb.BRANCH_PREFIX} task branch)"
        if state.dirty and state.ahead:
            return NEEDS_BRANCH, (
                f"{state.dirty} uncommitted file(s) and {state.ahead} unpushed commit(s) on {where}"
            )
        if state.dirty:
            return NEEDS_BRANCH, f"{state.dirty} uncommitted file(s) on {where}"
        if state.ahead:
            return NEEDS_BRANCH, f"{state.ahead} commit(s) committed straight to {where}, unpushed"
        if state.behind:
            return NEEDS_PULL, f"{state.behind} commit(s) behind origin/{state.default_branch}"
        return CLEAN, "up to date"

    # Task branch.
    # Checked before `dirty`, and that order is the whole fix: a retired branch with
    # uncommitted work otherwise reads as READY, and `--ship` plans a commit the
    # branch policy rejects every single time. The sweep then reports "git refused,
    # the state needs a human" for a state it could have resolved itself.
    if is_retired(state):
        carried = []
        if state.dirty:
            carried.append(f"{state.dirty} uncommitted file(s)")
        if state.ahead:
            carried.append(f"{state.ahead} unmerged commit(s)")
        # Name the signal that actually fired. "Its remote branch is gone" is a lie on
        # the `pr_merged` path -- there the config entry never survived to go missing --
        # and a reason that misdescribes the state sends the reader looking for the
        # wrong thing.
        why = "whose PR merged" if state.pr_merged else "whose remote branch is gone"
        return NEEDS_REBRANCH, (
            f"{' and '.join(carried)} on {state.branch}, {why} "
            f"-- the branch is retired and cannot be committed to"
        )
    if state.dirty:
        return READY, f"{state.dirty} uncommitted file(s) on a task branch"
    if state.ahead == 0:
        # Nothing beyond the base: merged and spent, or cut and never used. Either
        # way the worktree should not still be sitting here.
        return SPENT, f"no commits beyond {state.default_branch} -- branch is spent"
    if not state.upstream:
        return READY, f"{state.ahead} commit(s), never pushed"
    if state.unpushed > 0:
        return READY, f"{state.unpushed} commit(s) not yet pushed to {state.upstream}"
    return NEEDS_PR, f"{state.ahead} commit(s) pushed to {state.upstream} -- confirm a PR is open"


def plan_for(state: State, verdict: str) -> list[str]:
    """Ordered next actions for a verdict. Non-empty for every non-terminal verdict.

    This list is the "nothing stranded" contract: if a repo is actionable, the
    sweep says concretely what unblocks it rather than leaving the reader to
    work it out from the state columns.
    """
    if verdict in TERMINAL:
        return []
    if verdict == BLOCKED:
        return ["inspect by hand -- the sweep will not guess at this state"]
    if verdict == NEEDS_PULL:
        # On the default branch this is a plain pull; on an anchor there is nothing
        # to pull *from* (its upstream is not origin/<default>), so it fast-forwards
        # onto the default branch instead. Both are what `--sync` emits.
        if state.branch == state.default_branch:
            return [f"git -C {state.name} pull --ff-only", "or: sweep.py --sync --yes"]
        return [
            f"git -C {state.name} merge --ff-only origin/{state.default_branch}",
            "or: sweep.py --sync --yes",
        ]
    if verdict == NEEDS_REBRANCH:
        return [
            f"cut a fresh {tb.BRANCH_PREFIX}... branch under the work -- {state.branch} "
            f"is retired (its remote branch is gone), so git refuses every commit to it "
            f"-- sweep.py --branch --yes",
            "then: /ship -- or sweep.py --ship --yes",
        ]
    if verdict == SPENT:
        home = home_ref(state)
        if not home:
            return [
                f"cannot resolve a home branch for the linked worktree {state.name} -- "
                f"check out its anchor branch by hand, then sweep.py --sync"
            ]
        return [
            f"git -C {state.name} checkout {home} && git merge --ff-only "
            f"origin/{state.default_branch}",
            f"git -C {state.name} branch -d {state.branch} (spent)",
            "or: sweep.py --sync --yes",
        ]

    steps: list[str] = []
    if verdict == NEEDS_BRANCH:
        # task_branch owns the naming so a swept branch is indistinguishable from
        # one `worktree.py` cut for a box.
        steps.append(
            f"cut a {tb.BRANCH_PREFIX}... branch off HEAD (task_branch.branch_name) "
            f"so the work leaves {state.branch} -- sweep.py --branch --yes"
        )
    if state.behind:
        # Rebase pre-PR, merge post-PR: rebasing a branch with an open PR detaches
        # its review threads.
        rewrite = "merge" if verdict == NEEDS_PR else "rebase"
        steps.append(
            f"git fetch && git {rewrite} origin/{state.default_branch} "
            f"({state.behind} behind; conflict -> stop and report, never auto-resolve)"
        )
    if verdict == NEEDS_PR:
        steps.append("confirm an open PR exists for this branch; open one if not")
    else:
        steps.append("/ship -- review the diff, commit, push, open the PR")
    return steps


# --- mutation plans ---------------------------------------------------------
# Both are pure: they turn a State into git argv and nothing else, so every
# destructive-looking step is asserted in a test without a repo on disk.


def branch_plan(state: State, slug: str = "sweep", today: _dt.date | None = None) -> Plan:
    """Step 1: get stranded work onto an `agent/...` branch it can be shipped from.

    The new branch is cut from HEAD, not from `origin/<default>`, so a dirty tree
    comes along untouched (`tb.checkout_base` makes the same call for the same
    reason: resetting onto the base could clobber uncommitted work).

    When the home branch carried commits, it is reset to `origin/<default>` here
    and only here. This is the one moment that is provably safe -- the task branch
    was just cut from that exact HEAD, so the commits have a second home before
    the first one moves. Leaving it would strand the home branch permanently
    diverged: once the PR merges as a squash or a merge commit, those commits are
    not ancestors of `origin/<default>` and `--sync`'s fast-forward can never
    succeed again.

    A *retired* task branch (`is_retired`) is the second way in, and it differs on
    both counts. Nothing is reset: the branch is already merged, so `--sync` reaps
    it, and forcing it would be rewriting a ref the sweep does not own. Nothing is
    anchored either -- the branch it came from is a task branch, and recording that
    as the worktree's home would send `--sync` to park on a dead branch. The topic
    is taken from the branch name instead of the slug, so the new branch reads as
    the continuation it is rather than as an unrelated `agent/sweep-<mmdd>`.
    """
    retired = is_retired(state)
    if is_task_branch(state.branch) and not retired:
        return Plan(refusal=f"already on a task branch ({state.branch})")
    if not state.branch or not state.default_branch:
        return Plan(refusal="no branch to cut from -- resolve the blocked state first")

    # The retired branch was named when the work started, from the task's own prompt.
    # That is a better topic than any slug one workspace-wide sweep could supply.
    topic = branch_topic(state.branch) if retired else ""
    name = tb.branch_name(tb.slugify(topic or slug), set(state.local_branches), today)
    steps: list[tuple[str, ...]] = [("checkout", "-b", name)]
    if state.ahead and not retired:
        steps.append(("branch", "-f", state.branch, f"origin/{state.default_branch}"))
    # Remember where the work came from so `--sync` can put the worktree back.
    return Plan(steps=tuple(steps), anchor="" if retired else state.branch)


def branch_topic(branch: str) -> str:
    """What a managed task branch is about: `codex/voicemail-0802` -> `voicemail`.

    Strips the prefix and the `-<mmdd>` stamp `tb.branch_name` adds, plus any `-N`
    disambiguator. Returns "" for anything that is not a task branch.
    """
    if not is_task_branch(branch):
        return ""
    prefix = tb.managed_branch_prefix(branch)
    return _DATE_SUFFIX_RE.sub("", branch[len(prefix) :])


def commit_message(state: State) -> str:
    """The subject line for a swept commit.

    Deliberately mechanical and deliberately not a description. Nothing here has
    read the diff, so a subject claiming to summarise it would be a fabrication --
    the honest thing is to say what the commit *is*: work that was stranded, and
    is now parked somewhere it can be reviewed.

    The scope comes from the *branch*, not from a caller-supplied slug. The branch
    was named when the work started -- by `worktree.py`, from the slug `task_slug.py`
    recorded for that prompt -- so it carries real intent from the moment there was
    some. A slug
    passed at sweep time cannot: one sweep spans every checkout in the workspace,
    so a single slug would stamp the same claimed topic onto unrelated repos.
    """
    topic = branch_topic(state.branch)
    # `sweep` is `--branch`'s own default, meaning "no topic was given" rather than
    # a topic that happens to be called sweep. Repeating it as a scope adds nothing.
    scope = f"({topic})" if topic and topic != "sweep" else ""
    return f"sweep{scope}: park stranded work ({state.dirty} file(s))"


def pr_body(state: State, limit: int = PR_BODY_FILE_LIMIT) -> str:
    """The body for a swept PR: where the work came from, and what is in it.

    The provenance line is the part worth writing down. Six months on, a PR
    full of unrelated files is a mystery unless it says it was swept off a home
    branch rather than authored as one change.
    """
    home = home_ref(state) or state.branch
    lines = [
        f"Opened automatically by `sweep.py --ship`. The work was sitting "
        f"uncommitted on `{home}`, where it could not be shipped from, and was "
        f"parked on `{state.branch}` so it is not lost.",
        "",
        "**Nothing has reviewed this.** No diff was read, so the title describes "
        "the sweep rather than the change. Retitle, split, or close it once you "
        "have looked.",
    ]
    if state.dirty_files:
        shown = state.dirty_files[:limit]
        lines += ["", f"Changed paths ({state.dirty}):", ""]
        lines += [f"- `{path}`" for path in shown]
        if len(state.dirty_files) > len(shown):
            lines.append(f"- …and {len(state.dirty_files) - len(shown)} more")
    return "\n".join(lines)


def ship_plan(state: State, verdict: str) -> Plan:
    """Step 2: commit whatever is on a task branch, push it, and open its PR.

    The mode that exists because the previous split -- branch here, commit and PR
    by hand per repo -- left a workspace half-swept whenever attention moved on.
    What it gives up is real and is stated in every artifact it creates: no diff
    was read, so the commit subject and PR title describe the sweep rather than
    the change, and the PR body says so in as many words.

    Refuses anything not already on a task branch. `--branch` is what moves work
    there, and doing it implicitly here would mean one `--yes` could take work
    from an untouched home branch all the way to a published PR.

    Hooks are not bypassed: no `--no-verify`, no `--force`. A pre-commit gate that
    rejects the tree stops that checkout with its failure reported, which is the
    correct outcome -- the sweep publishes work, it does not launder it past the
    project's own checks.
    """
    if verdict == SKIPPED:
        return Plan()
    if verdict == BLOCKED:
        return Plan(refusal="blocked -- inspect by hand")
    if verdict == NEEDS_REBRANCH:
        # Distinct from the message below: this checkout *is* on a task branch, and
        # saying otherwise sends the reader looking for the wrong problem.
        return Plan(
            refusal=(
                f"{state.branch} is retired (its remote branch is gone) and cannot be "
                f"committed to -- run --branch first to cut a fresh branch under the work"
            )
        )
    if verdict not in SHIPPABLE:
        return Plan(
            refusal=(
                f"{verdict} -- nothing on a task branch to ship"
                + (" (run --branch first)" if verdict in BRANCHABLE else "")
            )
        )
    # Defensive: SHIPPABLE is only reachable from a task branch, and a plan that
    # pushed a home branch to a PR would be the one unrecoverable mistake here.
    if not is_task_branch(state.branch):
        return Plan(refusal=f"{state.branch} is not a {tb.BRANCH_PREFIX} task branch")

    steps: list[tuple[str, ...]] = []
    if state.dirty:
        # `-A` respects .gitignore, so this stages what the project already
        # considers its own -- but it is still everything, which is exactly the
        # bargain of an unattended sweep and why the PR says so.
        steps.append(("add", "-A"))
        steps.append(("commit", "-m", commit_message(state)))
    # Push when there is anything the remote has not got: uncommitted work we are
    # about to commit, commits never pushed (unpushed < 0 means no upstream), or
    # commits ahead of the upstream. `-u` sets the upstream the first time.
    if state.dirty or state.unpushed != 0:
        steps.append(("push", "-u", "origin", state.branch))
    return Plan(
        steps=tuple(steps),
        pr_title=commit_message(state),
        pr_body=pr_body(state),
        pr_head=state.branch,
        pr_base=state.default_branch,
    )


def sync_plan(state: State, verdict: str, fetch: bool = True) -> Plan:
    """Step 2: park a checkout on its home branch, current, with the spent branches gone.

    Refuses outright on anything holding unshipped work. That is the ordering the
    two steps depend on -- syncing a checkout that still has a PR in flight would
    move it off the branch under review -- and it is why `--sync` is safe to run
    over the whole workspace while some repos are mid-flight.

    The reap is `branch -d`, never `-D`, and a branch whose upstream would make
    `-d` refuse gets that upstream unset first rather than the refusal forced --
    the comment on the loop below is where that decision lives.
    """
    if verdict == SKIPPED:
        return Plan()
    if verdict == BLOCKED:
        return Plan(refusal="blocked -- inspect by hand")
    if verdict not in SYNCABLE:
        return Plan(refusal=f"{verdict} -- unshipped work here; /ship it before syncing")

    home = home_ref(state)
    if not home:
        return Plan(
            refusal=(
                f"cannot resolve a home branch: {state.name} is a linked worktree on a "
                f"task branch with no recorded anchor and no local branch named "
                f"{state.name}. Check out its anchor by hand once and re-run."
            )
        )

    steps: list[tuple[str, ...]] = []
    if fetch:
        steps.append(("fetch", "--prune", "origin"))
    if state.branch != home:
        steps.append(("checkout", home))
    # Unconditional: `behind` was measured against the *old* HEAD, so after a
    # checkout it says nothing about where `home` sits. `--ff-only` makes an
    # already-current branch a no-op and a diverged one an error, never a merge.
    steps.append(("merge", "--ff-only", f"origin/{state.default_branch}"))

    # Everything merged, plus the spent branch we are standing on (trivially
    # merged, so `-d` takes it). Never `home` itself, and never `-D`: an unmerged
    # branch surviving this is the correct outcome, not a failure to force past.
    #
    # Never a branch a *sibling* worktree is on either. Reaping is repo-wide while
    # a checkout is per-worktree, so `carameli-b` sees the branch `carameli` is
    # working on, merged into origin/master, and reads it as abandoned. Git would
    # refuse the delete, but a plan that proposes it is a plan that reports a
    # failure on a healthy workspace -- and the dry run stops being trustworthy.
    live_elsewhere = {b for b in state.worktree_branches if b != state.branch}
    doomed: list[str] = []
    for candidate in (*state.merged_task_branches, state.branch if verdict == SPENT else ""):
        if not is_task_branch(candidate) or candidate in doomed:
            continue
        if candidate != home and candidate not in live_elsewhere:
            doomed.append(candidate)
    for branch in doomed:
        # `-d` consults the branch's *upstream* when it has one, and only falls back
        # to HEAD when it does not -- so a branch git itself listed as merged into
        # origin/<default> is still refused if it carries commits `origin/<branch>`
        # never got. Rebasing a task branch onto the base after its PR merged leaves
        # exactly that: the rebase comes up empty, the local branch lands on the base
        # tip, and the remote branch still holds the pre-rebase commits. Two of those
        # failed a sweep with "not fully merged" for work that was entirely merged.
        #
        # Unsetting first is what makes `-d` ask the question this mode is actually
        # about -- is the work in the base branch? -- instead of a question about
        # pushing. It is not a way around the refusal: `-d` still checks, now against
        # HEAD, which the preceding steps just fast-forwarded to origin/<default>. A
        # branch holding real work is refused exactly as before, which is why this is
        # an unset and not a `-D`. Nothing is lost either way: deleting a branch drops
        # its `branch.<name>.*` config anyway, so on the path that succeeds the unset
        # changes nothing at all.
        if branch in state.ahead_of_upstream:
            steps.append(("branch", "--unset-upstream", branch))
        steps.append(("branch", "-d", branch))

    # Only linked worktrees need the record: a primary can always fall back to the
    # default branch, so storing it there would just be a value that can go stale.
    return Plan(steps=tuple(steps), anchor=home if state.linked else "")


def dedupe_branch_names(pairs: list[tuple[State, Plan]]) -> list[Plan]:
    """The plans, in order, with each new branch name claimed by one checkout only.

    `branch_plan` picks a free name by checking `state.local_branches`, which was
    read during `inspect()` -- before any plan ran. Two linked worktrees of one
    repo share a ref store, so they see the same branch list, and both pick the
    same name; the first `checkout -b` creates it and the second dies on `fatal: a
    branch named 'agent/sweep-0802' already exists`, leaving that worktree still
    stranded while the run reports success everywhere else.

    Same shape as `dedupe_reaps` and the same resolution: first claim wins,
    matching the order `run_mode` applies them in. The loser is bumped to the
    `-N` suffix `tb.branch_name` would have given it had it known.
    """
    claimed: dict[str, set[str]] = {}
    plans: list[Plan] = []
    for state, plan in pairs:
        # No key means git would not say; treat the checkout as its own store
        # rather than merging every unknown into one bucket.
        seen = claimed.setdefault(state.git_common_dir or f"?{state.name}", set())
        steps: list[tuple[str, ...]] = []
        for step in plan.steps:
            if step[:2] == ("checkout", "-b"):
                name = step[2]
                while name in seen:
                    name = _bump(name, seen)
                seen.add(name)
                step = ("checkout", "-b", name)
            steps.append(step)
        plans.append(replace(plan, steps=tuple(steps)))
    return plans


def _bump(name: str, taken: set[str]) -> str:
    """`name` with the next free `-N` suffix -- `tb.branch_name`'s convention."""
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def dedupe_reaps(pairs: list[tuple[State, Plan]]) -> list[Plan]:
    """The plans, in order, with each `branch -d` claimed by exactly one checkout.

    `merged_task_branches` is repo-wide but a plan is per-worktree, so a branch
    that is merged and idle lands in the plan of *every* linked sibling sharing
    the ref store. The first to run deletes it and the rest fail on a branch that
    is already gone -- a healthy workspace reporting a failure, which is the same
    thing `live_elsewhere` exists to prevent one step earlier in `sync_plan`.

    First claim wins, matching the order `run_mode` applies them in. If that
    checkout's plan fails before reaching the delete, the branch survives to the
    next sweep -- a reap deferred, never a reap done to the wrong repo.
    """
    claimed: dict[str, set[str]] = {}
    plans: list[Plan] = []
    for state, plan in pairs:
        # No key means git would not say; treat the checkout as its own store
        # rather than merging every unknown into one bucket.
        seen = claimed.setdefault(state.git_common_dir or f"?{state.name}", set())
        steps: list[tuple[str, ...]] = []
        for step in plan.steps:
            if step[:2] == ("branch", "-d"):
                if step[2] in seen:
                    continue
                seen.add(step[2])
            # The unset that precedes a delete belongs to that delete's claim. Left
            # behind on its own it would run against a branch the winning sibling
            # has already deleted, and `--unset-upstream` on a branch that is gone
            # is fatal -- aborting the loser's plan before it has even parked. It
            # sits immediately before its own `-d`, so at this point the name is in
            # `seen` only when another checkout claimed it.
            elif step[:2] == ("branch", "--unset-upstream") and step[2] in seen:
                continue
            steps.append(step)
        plans.append(replace(plan, steps=tuple(steps)))
    return plans


def dedupe_note(results: list[Result]) -> dict[str, list[str]]:
    """Checkout names grouped by shared remote, for the >1 case only.

    `carameli`/`carameli-b` and `ibkr_trader`/`ibkr_trader-b` are separate
    checkouts of the same GitHub repo. Both can strand work independently, so
    both are swept -- but a reader counting open PRs needs to know two rows can
    land on one repo.
    """
    by_remote: dict[str, list[str]] = {}
    for result in results:
        url = result.state.remote_url
        if url:
            by_remote.setdefault(url, []).append(result.state.name)
    return {url: names for url, names in by_remote.items() if len(names) > 1}


def exit_code(results: list[Result]) -> int:
    """0 all clear, 1 something needs action, 2 something is blocked."""
    verdicts = {result.verdict for result in results}
    if BLOCKED in verdicts:
        return 2
    return 1 if verdicts & ACTIONABLE else 0


# --- git IO -----------------------------------------------------------------


def git_for(path: Path) -> Git:
    """A `git(*args)` callable bound to one checkout."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
            creationflags=NO_WINDOW,
        )

    return git


def _out(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout.strip() if result.returncode == 0 else ""


def common_dir(git: Git, path: Path) -> str:
    """Absolute path to the ref store this checkout deletes branches from.

    The identity that matters for reaping. `--git-common-dir` answers `.git` for a
    primary worktree and the *primary's* git dir for a linked one, so linked
    siblings collapse to one key while two clones of the same remote stay
    distinct -- which is the whole point, since separate clones each hold their
    own copy of a merged branch and must each reap it.
    """
    raw = _out(git("rev-parse", "--git-common-dir"))
    if not raw:
        return ""
    # Relative for a primary, absolute for a linked worktree; `/` handles both.
    return str((path / raw).resolve())


def anchor_path(git: Git, path: Path) -> Path | None:
    """Where this worktree's anchor marker lives, or None if git will not say.

    `rev-parse --git-path` resolves to the *worktree's* git dir
    (`.git/worktrees/<name>/`) for a linked checkout and to `.git/` for a primary,
    which is exactly the per-worktree scoping the marker needs. The returned path
    is relative to the repo, and we run git with `-C path`, so join it back onto
    `path` when it is not absolute.
    """
    raw = _out(git("rev-parse", "--git-path", ANCHOR_MARKER_NAME))
    if not raw:
        return None
    marker = Path(raw)
    return marker if marker.is_absolute() else path / marker


def read_anchor(git: Git, path: Path) -> str:
    """The recorded home branch for this worktree; "" when unset or unreadable."""
    marker = anchor_path(git, path)
    if marker is None or not marker.is_file():
        return ""
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_anchor(git: Git, path: Path, branch: str) -> None:
    """Record `branch` as this worktree's home. Best-effort: a failure to write
    only costs the dirname fallback in `home_ref`, never correctness."""
    marker = anchor_path(git, path)
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{branch}\n", encoding="utf-8")
    except OSError:
        pass


def has_merged_pr(gh: Git, branch: str) -> bool:
    """True when GitHub reports a merged PR for `branch`. Same query `git_policy` runs.

    Fails **open** on every error path -- a non-zero `gh`, unparseable JSON, an
    unexpected shape, and `gh` not being installed at all. The asymmetry is deliberate:
    missing a retirement costs nothing new (git still refuses the commit and names the
    reason, which is today's behaviour), while inventing one would send `--branch` to
    cut a branch under work that did not need it, on the say-so of an offline or
    unauthenticated `gh`.

    "Every error path" has to include the one where the callable *raises*.
    `gh_for` is a bare `subprocess.run`, so a machine with no `gh` on PATH gets a
    `FileNotFoundError` out of the middle of `inspect()` -- a traceback, in the tool
    whose entire contract is that it reports rather than guesses. Nothing caught it
    because every other caller reads a returncode.
    """
    try:
        result = gh(
            "pr", "list", "--head", branch, "--state", "merged", "--limit", "1", "--json", "number"
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return isinstance(payload, list) and bool(payload)


def inspect(
    name: str,
    path: Path,
    git: Git | None = None,
    gh: Git | None = None,
    fetch: bool = True,
) -> State:
    """Read one checkout's git state. `fetch=False` skips the network (stale counts)."""
    dot_git = path / ".git"
    if not dot_git.exists():
        return State(name=name, path=str(path), is_git=False)
    git = git or git_for(path)

    if fetch:
        git("fetch", "--quiet", "origin")

    remote_url = _out(git("remote", "get-url", "origin"))
    default_branch = tb.detect_default_branch(git, fallback="")
    branch = _out(git("branch", "--show-current"))
    dirty_files = parse_porcelain(git("status", "--porcelain").stdout or "")

    behind = ahead = 0
    if default_branch:
        behind, ahead = parse_ahead_behind(
            _out(git("rev-list", "--left-right", "--count", f"origin/{default_branch}...HEAD"))
        )

    upstream = _out(git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"))
    unpushed = -1
    if upstream:
        raw = _out(git("rev-list", "--count", "@{u}..HEAD"))
        unpushed = int(raw) if raw.isdigit() else -1
    # `@{u}` fails identically for "never pushed" and "pushed, then the remote branch
    # was deleted" -- and the second is what a merged PR leaves behind. Deleting the
    # remote branch does not clear `branch.<name>.remote`, so the surviving config
    # entry is what tells the two apart. Read only when `@{u}` came back empty: for a
    # live branch the question is already answered.
    upstream_gone = bool(
        branch and not upstream and _out(git("config", "--get", f"branch.{branch}.remote"))
    )
    # The config entry is a convention, not a guarantee (see `is_retired`), so when it
    # is absent ask GitHub directly. Gated hard, because this is a network round trip
    # per checkout and a ten-repo sweep runs it serially: only for a task branch with no
    # upstream that is actually carrying work -- the one state where the answer changes
    # a verdict. Everything else is already decided, or is `spent` either way.
    pr_merged = False
    if (
        fetch
        and not upstream
        and not upstream_gone
        and is_task_branch(branch)
        and (dirty_files or ahead)
    ):
        pr_merged = has_merged_pr(gh or gh_for(path), branch)

    # One listing answers both questions -- which branches exist, and which of them
    # `branch -d` will refuse -- so the two can never disagree about a branch.
    local_branches, ahead_of_upstream = parse_branch_upstreams(
        _out(
            git(
                "for-each-ref",
                "--format=%(refname:short)%09%(upstream:track)",
                "refs/heads/",
            )
        )
    )
    merged: tuple[str, ...] = ()
    if default_branch:
        merged = tuple(
            line
            for line in _out(
                git(
                    "branch",
                    "--merged",
                    f"origin/{default_branch}",
                    "--format=%(refname:short)",
                )
            ).splitlines()
            if is_task_branch(line)
        )

    return State(
        name=name,
        path=str(path),
        is_git=True,
        host=remote_host(remote_url),
        default_branch=default_branch,
        branch=branch,
        dirty=len(dirty_files),
        dirty_files=dirty_files,
        behind=behind,
        ahead=ahead,
        upstream=upstream,
        unpushed=unpushed,
        upstream_gone=upstream_gone,
        pr_merged=pr_merged,
        remote_url=remote_url,
        # A linked worktree's `.git` is a file pointing at the primary's git dir.
        linked=dot_git.is_file(),
        anchor=read_anchor(git, path),
        local_branches=local_branches,
        merged_task_branches=merged,
        ahead_of_upstream=ahead_of_upstream,
        worktree_branches=parse_worktree_branches(_out(git("worktree", "list", "--porcelain"))),
        git_common_dir=common_dir(git, path),
    )


# --- executing a plan -------------------------------------------------------


def gh_for(path: Path) -> Git:
    """A `gh(*args)` callable bound to one checkout.

    `gh` reads the repo from the working directory -- there is no `-C` -- so the
    binding is `cwd` rather than an argument.
    """

    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gh", *args],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
            creationflags=NO_WINDOW,
        )

    return gh


def ensure_pr(gh: Git, plan: Plan) -> tuple[str, bool, str]:
    """Open a PR for `plan.pr_head`, or find the one already open.

    Opened ready for review, not as a draft. A draft is a claim about *intent* --
    "not finished yet" -- and these are finished: the work is on the branch and the
    branch is pushed. What is unreviewed is the description, and the body says so in
    its own words rather than relying on a status flag to imply it. Drafts also
    suppress review requests and CODEOWNERS notifications, which is the opposite of
    useful for a PR nobody has looked at.

    Returns `(url, created, error)`. Reusing an existing PR is a success, not a
    conflict: `--ship` has to be safe to re-run over a workspace where some
    checkouts were shipped on an earlier pass, and `gh pr create` on a branch that
    already has one fails with a message that reads like a real error.

    A label failure is an error, not a shrug, and the URL is still returned beside
    it: the PR is real either way, but a plan that asked for `automerge` and did
    not get it is a PR that silently waits for the review it was labelled to skip.
    """
    existing = gh("pr", "view", plan.pr_head, "--json", "url", "--jq", ".url")
    if existing.returncode == 0 and existing.stdout.strip():
        url = existing.stdout.strip()
        return url, False, _apply_labels(gh, plan, url)

    for label in plan.pr_labels:
        ensured = _ensure_label(gh, label)
        if ensured.returncode != 0:
            return "", False, (ensured.stderr or ensured.stdout or "").strip()

    label_args = [arg for label in plan.pr_labels for arg in ("--label", label)]
    created = gh(
        "pr",
        "create",
        "--base",
        plan.pr_base,
        "--head",
        plan.pr_head,
        "--title",
        plan.pr_title,
        "--body",
        plan.pr_body,
        *label_args,
    )
    if created.returncode != 0:
        return "", False, (created.stderr or created.stdout or "").strip()
    # `gh pr create` prints the URL as its last line of output.
    lines = [line.strip() for line in created.stdout.splitlines() if line.strip()]
    return (lines[-1] if lines else ""), True, ""


def _ensure_label(gh: Git, label: str):
    """Create `label` in the repo, idempotently -- `--force` also makes an existing
    label converge on this spelling of its colour and description."""
    color, description = LABEL_SPECS.get(label, ("ededed", ""))
    return gh("label", "create", label, "--force", "--color", color, "--description", description)


def _apply_labels(gh: Git, plan: Plan, url: str) -> str:
    """Add `plan.pr_labels` to an existing PR; "" on success, the failure otherwise."""
    for label in plan.pr_labels:
        ensured = _ensure_label(gh, label)
        if ensured.returncode != 0:
            detail = (ensured.stderr or ensured.stdout or "").strip()
            return f"PR exists at {url}, but creating label `{label}` failed: {detail}"
        added = gh("pr", "edit", plan.pr_head, "--add-label", label)
        if added.returncode != 0:
            detail = (added.stderr or added.stdout or "").strip()
            return f"PR exists at {url}, but labelling it `{label}` failed: {detail}"
    return ""


@dataclass
class Applied:
    """What running one checkout's plan actually did."""

    name: str
    plan: Plan
    ran: list[str] = field(default_factory=list)
    failed: str = ""
    error: str = ""
    pr_url: str = ""
    pr_created: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.plan.refusal


def apply_plan(
    name: str,
    path: Path,
    plan: Plan,
    git: Git | None = None,
    gh: Git | None = None,
) -> Applied:
    """Run a plan's steps in order, stopping at the first failure.

    Stopping matters: the steps are ordered so that a later one is only safe once
    the earlier ones succeeded (nothing is deleted before the checkout that moved
    off it), so continuing past a failure is how a safe plan turns unsafe. The PR
    is the last thing of all, for the same reason: a PR opened on a branch whose
    push failed points at a ref the remote does not have.
    """
    result = Applied(name=name, plan=plan)
    if plan.refusal:
        return result
    git = git or git_for(path)
    for step in plan.steps:
        completed = git(*step)
        rendered = "git " + " ".join(step)
        if completed.returncode != 0:
            result.failed = rendered
            result.error = (completed.stderr or completed.stdout or "").strip()
            return result
        result.ran.append(rendered)
    if plan.pr_title:
        url, created, error = ensure_pr(gh or gh_for(path), plan)
        if error:
            result.failed = "gh pr create"
            result.error = error
            return result
        result.pr_url = url
        result.pr_created = created
        result.ran.append(f"gh pr create ({'opened' if created else 'reused'}) {url}")
    if plan.anchor:
        write_anchor(git, path, plan.anchor)
    return result


def sweep(root: Path, names: list[str], fetch: bool = True) -> list[Result]:
    """Inspect and classify every named checkout under `root`."""
    results: list[Result] = []
    for name in names:
        state = inspect(name, root / name, fetch=fetch)
        verdict, reason = classify(state)
        results.append(Result(state, verdict, reason, plan_for(state, verdict)))
    return results


# --- reporting --------------------------------------------------------------


def _base_column(state: State) -> str:
    if not state.default_branch:
        return "?"
    return f"-{state.behind}/+{state.ahead}"


def render(results: list[Result]) -> str:
    """The human-readable report: one row per checkout, then the plans."""
    rows = [("PROJECT", "BRANCH", "DIRTY", "vs BASE", "VERDICT")]
    for result in results:
        state = result.state
        rows.append(
            (
                state.name,
                state.branch or ("-" if state.is_git else "n/a"),
                str(state.dirty) if state.is_git else "-",
                _base_column(state) if state.is_git else "-",
                result.verdict,
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))

    actionable = [r for r in results if r.verdict in ACTIONABLE]
    if actionable:
        lines.append("")
        lines.append(f"{len(actionable)} checkout(s) need action:")
        for result in actionable:
            lines.append(f"\n  {result.state.name} [{result.verdict}] -- {result.reason}")
            for i, step in enumerate(result.plan, 1):
                lines.append(f"    {i}. {step}")
    else:
        lines.append("")
        lines.append("Nothing stranded -- every checkout is clean.")

    shared = dedupe_note(results)
    if shared:
        lines.append("")
        lines.append("Note -- checkouts sharing a remote (one repo, two rows):")
        for url, group in sorted(shared.items()):
            lines.append(f"  {', '.join(group)} -> {url}")
    return "\n".join(lines)


def render_plans(mode: str, planned: list[tuple[Result, Plan]], applied: bool) -> str:
    """The `--branch`/`--sync` report -- the same text whether or not `--yes` ran it.

    Deliberately identical in both modes: a dry run is only trustworthy if what it
    prints is what the real run does, so the plan is rendered from the same `Plan`
    objects the runner consumes.
    """
    verb = "Applied" if applied else "Would run"
    lines = [f"{mode}: {verb.lower()} the following."]
    acting = [(r, p) for r, p in planned if p.acts]
    refused = [(r, p) for r, p in planned if p.refusal]

    if acting:
        for result, plan in acting:
            lines.append(f"\n  {result.state.name} [{result.verdict}] -- {result.reason}")
            n = 0
            for n, step in enumerate(plan.steps, 1):
                lines.append(f"    {n}. git -C {result.state.name} {' '.join(step)}")
            if plan.pr_title:
                lines.append(
                    f"    {n + 1}. gh pr create --base {plan.pr_base} "
                    f"--head {plan.pr_head} --title {plan.pr_title!r}"
                )
                lines.append("       (reuses the existing PR if this branch already has one)")
            if plan.anchor:
                lines.append(f"    -> record home branch '{plan.anchor}' for this worktree")
    else:
        lines.append("\n  Nothing to do -- no checkout is in a state this mode acts on.")

    if refused:
        lines.append(f"\n{len(refused)} checkout(s) skipped:")
        for result, plan in refused:
            lines.append(f"  {result.state.name} -- {plan.refusal}")
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


def render_applied(results: list[Applied]) -> str:
    """What actually happened, including the failures. Failures never abort the
    sweep: one repo that will not fast-forward should not stop the other four."""
    lines: list[str] = []
    failures = [r for r in results if r.failed]
    for result in results:
        if result.failed:
            lines.append(f"  {result.name}: FAILED at `{result.failed}`")
            for line in result.error.splitlines():
                lines.append(f"      {line}")
        elif result.ran:
            lines.append(f"  {result.name}: {len(result.ran)} step(s) ok")
            if result.pr_url:
                lines.append(
                    f"      PR {'opened' if result.pr_created else 'already open'} {result.pr_url}"
                )
    if failures:
        lines.append(
            f"\n{len(failures)} checkout(s) failed. Nothing was forced -- git refused, "
            f"which means the state needs a human. Fix and re-run."
        )
    return "\n".join(lines)


def run_mode(
    root: Path,
    results: list[Result],
    mode: str,
    apply: bool,
    fetch: bool = True,
    slug: str = "sweep",
) -> tuple[str, int]:
    """Plan (and optionally apply) `--branch` or `--sync` across the swept checkouts.

    Returns the report and the exit code: 0 clean, 1 something still needs action,
    2 a step failed.
    """
    planned: list[tuple[Result, Plan]] = []
    for result in results:
        if mode == "branch":
            plan = (
                branch_plan(result.state, slug=slug)
                if result.verdict in BRANCHABLE
                else Plan(refusal=f"{result.verdict} -- not stranded on a home branch")
            )
        elif mode == "ship":
            # No slug: the branch it is standing on already carries the topic.
            plan = ship_plan(result.state, result.verdict)
        else:
            plan = sync_plan(result.state, result.verdict, fetch=fetch)
        # A skipped non-git directory is noise in this report, not a refusal.
        if result.verdict == SKIPPED:
            continue
        planned.append((result, plan))

    # Both dedupes run before `render_plans`, so the dry run shows the steps that
    # will actually run. A plan printed here and skipped there is how the dry run
    # stops being worth reading.
    if mode == "branch":
        # Siblings sharing a ref store would otherwise pick the same new name and
        # the second `checkout -b` would die on it.
        deduped = dedupe_branch_names([(result.state, plan) for result, plan in planned])
        planned = [(result, plan) for (result, _), plan in zip(planned, deduped, strict=True)]
    elif mode == "sync":
        deduped = dedupe_reaps([(result.state, plan) for result, plan in planned])
        planned = [(result, plan) for (result, _), plan in zip(planned, deduped, strict=True)]

    report = render_plans(mode, planned, applied=apply)
    if not apply:
        return report, 1 if any(p.acts for _, p in planned) else 0

    ran = [
        apply_plan(result.state.name, root / result.state.name, plan)
        for result, plan in planned
        if plan.acts
    ]
    detail = render_applied(ran)
    if detail:
        report = f"{report}\n\nResults:\n{detail}"
    return report, 2 if any(r.failed for r in ran) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help=f"the .code-workspace file listing the checkouts (default: {DEFAULT_WORKSPACE.name})",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="checkout name to skip; repeatable (default: VanillaLand)",
    )
    # Paired for the same reason as --dry-run/--yes: the VS Code picker has to emit
    # a real token on the "every checkout" branch too, and an empty string would
    # reach argparse as a stray positional and be rejected.
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--all",
        dest="every",
        action="store_true",
        default=True,
        help="sweep every checkout in the workspace (the default)",
    )
    scope.add_argument(
        "--only",
        action="append",
        default=None,
        help=(
            "restrict every mode to these checkouts; repeatable and comma-delimited "
            "values are accepted"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when any checkout needs action, 2 when any is blocked",
    )
    # Paired for the same reason as --dry-run/--yes below: the VS Code picker has
    # to emit a real token on both branches.
    fetch_mode = parser.add_mutually_exclusive_group()
    fetch_mode.add_argument(
        "--fetch",
        dest="fetch",
        action="store_true",
        default=True,
        help="fetch every remote before reading its state (the default)",
    )
    fetch_mode.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="skip `git fetch` (fast, but ahead/behind counts may be stale)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--branch",
        action="store_true",
        help="step 1: cut an agent/... branch under work stranded on a home branch",
    )
    mode.add_argument(
        "--ship",
        action="store_true",
        help=(
            "step 2: commit whatever sits on a task branch, push it, and open its PR. "
            "Nothing reads the diff, so the message describes the sweep, not the change"
        ),
    )
    mode.add_argument(
        "--sync",
        action="store_true",
        help="step 3: park each worktree on its home branch, fast-forward, drop merged branches",
    )
    # `--dry-run` is redundant with the default and exists anyway: the VS Code task
    # picks one of these two strings, and passing "" instead would reach argparse as
    # a stray positional and be rejected. Same reason new-project.py carries it.
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print what --branch/--sync would do, and change nothing (the default)",
    )
    apply_mode.add_argument(
        "--yes",
        dest="dry_run",
        action="store_false",
        help="actually run --branch/--sync",
    )
    parser.add_argument(
        "--slug",
        default="sweep",
        help=(
            "--branch only: topic for the branch names it cuts (default: sweep -> "
            "agent/sweep-<mmdd>). There is no prompt to derive one from here, so "
            "pass what the stranded work is about when it is worth naming. --ship "
            "ignores it and takes its topic from the branch each checkout is on, "
            "which one sweep cannot supply -- it spans every repo in the workspace"
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.dry_run and not (args.branch or args.ship or args.sync):
        parser.error("--yes has no effect without --branch, --ship or --sync")

    if not args.workspace.is_file():
        print(f"sweep: no workspace file at {args.workspace}", file=sys.stderr)
        return 2
    exclude = frozenset(args.exclude) if args.exclude is not None else DEFAULT_EXCLUDE
    names = parse_workspace(args.workspace.read_text(encoding="utf-8"), exclude)
    if not names:
        print(f"sweep: no checkouts listed in {args.workspace.name}", file=sys.stderr)
        return 2
    known = ", ".join(names)
    names, unknown = select(names, args.only)
    if unknown:
        print(
            f"sweep: --only named {', '.join(unknown)}, which is not in "
            f"{args.workspace.name}. Known checkouts: {known}",
            file=sys.stderr,
        )
        return 2

    results = sweep(args.workspace.parent, names, fetch=args.fetch)

    if args.branch or args.ship or args.sync:
        report, code = run_mode(
            args.workspace.parent,
            results,
            "branch" if args.branch else "ship" if args.ship else "sync",
            apply=not args.dry_run,
            fetch=args.fetch,
            slug=args.slug,
        )
        print(report)
        return code

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": r.state.name,
                        "verdict": r.verdict,
                        "reason": r.reason,
                        "plan": r.plan,
                        "state": asdict(r.state),
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        print(render(results))

    return exit_code(results) if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
