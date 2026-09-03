#!/usr/bin/env python3
"""Ephemeral worktrees: spawn a disposable box, ship out of it, destroy it.

The static tier — `carameli`, `carameli-b`, one permanent slot each in `ports.toml`
— caps concurrency at two per project and, worse, makes every checkout *outlive* the
task that used it. That is where `sweep.py`'s workload comes from: `needs-branch`,
`needs-rebranch`, `spent-branch`, the anchor marker, `home_ref`, `dedupe_reaps` are
all states a checkout can only reach by surviving its task. A box cut fresh off
`origin/<default>` onto an `agent/...` branch and destroyed at the end cannot reach
any of them.

So this is not "sweep, but faster". It is the other half of the model:

| | Static checkout | Ephemeral box |
| --- | --- | --- |
| Lives in | `<workspace>/<project>` | `<workspace>/.worktrees/<box>` |
| Listed in the workspace file | yes | **no** — invisible to `sweep.py` by design |
| Port slot | pinned in `ports.toml` `[slots]` | leased on spawn, released on reap |
| Ends by | `sweep --sync` parking it home | `reap` deleting it |
| Stranded work is | found afterwards | **impossible**: reap refuses until it ships |

That last row is the point. `sweep.py` searches for work that got left behind;
`reap` simply will not free the box until the work has left it. Same guarantee,
enforced at the only moment it is cheap to enforce.

Modes:
  new <project>   cut a worktree on a fresh task branch off `origin/<default>`,
                  lease a port slot, seed its `.env`, install its toolchain.
                  Prints the path.
  list            every live box, its branch, its verdict, and whether it can be
                  reaped. Reuses `sweep.inspect`/`sweep.classify` — one classifier
                  for both tiers, so the two can never disagree about "has work".
  preview <ref>   check out someone ELSE's branch or PR in a box of its own and
                  bring its stack up, so a change can be *seen running* before it
                  is merged. The box sits on a `preview/...` copy of the remote
                  ref rather than on the ref itself, which is what lets it coexist
                  with the agent box that owns that branch; re-running it
                  fast-forwards onto whatever has been pushed since. Also takes a
                  live box name, which is the same request for work already here.
                  `--ui` starts only the project's `[worktree] ui_services` and
                  borrows the rest of the stack from the static checkout — the
                  cheap preview for a frontend change.
  provision <box> install the toolchain into a box that was cut without one (the
                  guard hook cuts those — see `apply_new`).
  reap <box>      tear the stack down, remove the worktree, delete the branch,
                  release the lease. **Refuses while the box still holds work.**
  reap --all      the same pass over every live box, stepping over the ones still
                  holding work rather than failing on them.
  reconcile       the unattended pass, meant for a schedule: reap every box whose
                  PR has merged, reclaim disk when the volume is low, optionally
                  merge green PRs (`--merge`), and report the boxes that need a
                  human. This is what makes the tier cost less attention than the
                  sweep instead of the same — `reap --all` already skipped boxes
                  holding work, but a person still had to remember to run it.
                  It then hands the *static* checkouts to `sweep.py --sync`, so
                  the one scheduled pass leaves the whole workspace current with
                  its remotes. `--no-checkouts` turns that half off; the reason
                  both tiers ride one schedule is in `sync_checkouts`.

`new`, `reap` and `reconcile` print their plan and change nothing unless `--yes` is
passed, the same contract `sweep.py`'s mutating modes keep.

The decision logic is pure and stdlib-only: every planner turns a `Box` plus a
`sweep.State` into argv and nothing else, so the destructive steps are asserted in
`tests/test_worktree.py` without a repo, a daemon, or a network.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import re
import shutil
import string
import subprocess
import sys
import time
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
# The host half of a teardown -- deleting the directory, and evicting whatever on this
# machine is still running out of it. Its own module because nothing in it knows what a
# `ReapPlan` is, and because this one is already past every structural limit.
import box_teardown
import devkit_ports
import devkit_project
import git_policy

# Resolved by the sys.path insert above; `scripts/hooks/` is not a package. Read for
# `[python] install_command`, `[python] version` and `[frontend]` — the same per-project
# seam the hooks use, so a box provisions the way its project says to rather than the way
# this file guesses.
import harness_config
import sweep
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]

# Ephemeral boxes live *beside* the checkouts, never inside one: a worktree nested in
# a project would show up as untracked files in that project's `git status`, which is
# the `needs-branch` verdict this whole tier exists to stop manufacturing.
#
# The name is `sweep`'s now, not this module's: resolving the workspace file from inside
# a box needs it, and that resolution had to move somewhere every workspace-aware script
# can import (see `sweep.default_workspace`). Re-exported rather than re-spelled so the
# tier's own callers keep reading `worktree.BOXES_DIR_NAME`.
BOXES_DIR_NAME = sweep.BOXES_DIR_NAME
LEASE_FILE_NAME = "leases.json"

# Mutual exclusion for the lease file's read-modify-write, held by `apply_new` and
# `apply_reap`. A directory rather than a file because `mkdir` is the one creation
# primitive that is atomic-and-failing on every platform this runs on. The window it
# closes is real: two guard hooks spawning boxes seconds apart both read the file,
# and the second write erased the first's entry — the erased box became a worktree
# no tool could see (`orphaned_boxes` is the recovery for ones already lost).
LEASE_LOCK_NAME = "leases.lock"
LEASE_LOCK_WAIT = 10.0
LEASE_LOCK_STALE = 60.0

# Mutual exclusion around a whole *spawn*, keyed by the (project, session) whose box is
# being cut and held by `worktree-guard.py`. `lease_lock` cannot serve: it is held for
# milliseconds by design, and `apply_new` takes it in the middle of the very sequence
# this has to bracket. The window is the one the guard's double registration opens — the
# user's `settings.json` and the project's both fire that hook on the same tool call, so
# two processes plan a box for the same session at the same instant and the loser's
# `git worktree add` dies on a branch the winner has just created.
#
# `stale` is minutes rather than `LEASE_LOCK_STALE`'s one, because a spawn legitimately
# holds this across a `git fetch`; `wait` has to stay under the harness's own hook
# timeout, since a guard killed mid-wait says nothing at all and the edit it was judging
# lands wherever it was pointing.
SPAWN_LOCK_PREFIX = "spawn-"
SPAWN_LOCK_WAIT = 45.0
SPAWN_LOCK_STALE = 120.0

# Separates the project from the branch topic in a box name. Two hyphens rather than
# one because project names already contain hyphens (`apt-finder`) and the box name is
# parsed back apart by `list`. Spelled in `sweep` for the same reason as BOXES_DIR_NAME:
# `sweep.source_checkout` parses a box name and this module imports that one.
NAME_SEP = sweep.NAME_SEP

# The workspace file sits beside the checkouts, one level above this repo — unless this
# repo IS a box, in which case it sits one level above `.worktrees/` instead. This module
# was the only one resolving both; `sweep.default_workspace` is that logic, moved down so
# every workspace-aware script gets it instead of its own naive copy.
DEFAULT_WORKSPACE = sweep.default_workspace(REPO_ROOT)

# Verdicts that mean the work has left the box, so the box is free to destroy.
#   spent-branch  nothing beyond the base — nothing to lose
#   needs-pr      pushed, nothing unpushed — the remote has every commit
#   clean         nothing to do (a box that never got used)
# Everything else — `ready` above all — means work is still only here.
SAFE_TO_REAP: frozenset[str] = frozenset(
    {sweep.SPENT, sweep.NEEDS_PR, sweep.CLEAN, sweep.NEEDS_PULL}
)

# `needs-pull` is `clean` a day later. `sweep.classify` reaches it only on a home branch
# with nothing dirty and nothing ahead -- "just behind" -- so the box holds no commit
# and no edit of its own, and its home branch is on origin in full. The one difference
# from `clean` is that origin has moved since, which is true of every box that has
# existed for an afternoon. Left out, a box that had merged the default branch into a
# helper branch and shipped under another PR (`pr228-merge`) came out `needs-pull`, in no
# set at all, and sat as a permanent HOLD holding nothing for three days.

# `needs-pr` is in SAFE_TO_REAP because nothing is *at stake* -- the remote has every
# commit -- which is the question `reconcile_action` needs answered before it applies its
# own PR policy on top. It is not the question `reap` and `list` ask. Those two have no
# policy: `reap --all` destroys whatever they call reapable, and `list` is where
# `workspace-status.py` gets the "N reapable (fix: reap --all --yes)" it prints at every
# session start. So a box whose PR was open and under review came out of `list` as
# reapable and out of `reconcile` as `waiting`, at the same moment, about the same box --
# and only one of the two was telling an agent to run a destructive command.
#
# Splitting the audiences is what makes them agree. A **merge** licenses an unprompted
# reap; a push does not, however completely the remote has the commits. So does a
# **close**, from the other end: the wait this set names is a wait for a person, and a
# closed PR is that person having answered. See `reap_decision`.
# `reconcile` keeps
# reaping an open PR under disk pressure or past `max_age_days`, because it has looked at
# the PR and weighed it -- it passes that `pr` down to `plan_reap` and is therefore never
# subject to the refusal in `reap_decision`.
AWAITS_A_MERGE: frozenset[str] = frozenset({sweep.NEEDS_PR})

# What `reap` and `list` may destroy with no PR in hand. `survey` subtracts the same
# set from `reapable` rather than reading this one, because a preview box is reapable
# without being in `SAFE_TO_REAP` at all; for every task verdict the two agree.
SWEEPABLE: frozenset[str] = SAFE_TO_REAP - AWAITS_A_MERGE


# The one verdict a merge can be stale about. `needs-rebranch` says "commits on a branch
# that can no longer be committed to", which is what a squash merge leaves behind and is
# also what an abandoned branch leaves behind -- the PR tells them apart. Every other
# refusing verdict means something a merge does not answer: `ready` and `needs-branch`
# are work on a branch no PR by this box's name has merged -- uncommitted edits, or
# commits that were never pushed -- and `blocked` is a state nothing here should be
# guessing at.
MERGE_CAN_BE_STALE_ABOUT: frozenset[str] = frozenset({sweep.NEEDS_REBRANCH})

# The verdicts a **landed tree** can settle, for the boxes the merge above cannot reach.
# A squash merge is invisible to commit identity, and the PR is what tells a squashed
# branch from an abandoned one -- but only while there is a PR to read. There is not,
# whenever the work left the box under some *other* branch's PR: merge the default branch
# into a helper branch cut by hand (`pr229-merge`), let the real PR squash-merge, and the
# box is `needs-branch` -- "commits on a branch it cannot be shipped from" -- with every
# line of them already on the default branch under a sha nothing here can match. No PR
# names that branch, so `MERGE_CAN_BE_STALE_ABOUT` never fires and the box is a permanent
# HOLD: the leak that arm exists to end, arriving through the verdict beside it. It cost
# a port slot for 28h on a machine holding 16 of 16, and `--force` -- the flag that also
# discards uncommitted work -- was again the only way out.
#
# `head_tree_landed` is the evidence, and it is stronger than a merged PR rather than a
# weaker substitute for one: an identical tree is not a claim that the work landed, it is
# the work, byte for byte, already reachable from `origin/<default>`. So this set may hold
# `needs-branch`, which `MERGE_CAN_BE_STALE_ABOUT` must not -- that verdict's usual
# meaning is unshipped commits, and a merge says nothing about them while a tree does.
# `holds_uncommitted` still gates it in `reapable`: a tree answers for what is committed
# and for nothing sitting on top of it.
#
# `ready` is in it for the same reason and by the same gate. The verdict has two shapes
# in `sweep.classify` -- uncommitted files on a task branch, or "N commit(s), never
# pushed" on a clean one -- and only the second can ever reach here, because every
# caller asks the tree only of a box with `state.dirty == 0`. A clean `ready` box is
# commits and nothing else, so a tree that is already on the default branch is the
# whole of what it holds. carameli's `fix-merge-pr-252` was that box: five commits, three
# of them on master under the branch that actually shipped them, its tree identical to
# the master commit it had merged, reported for two days as "5 commit(s), never pushed".
# The merge arm still never frees `ready` (`MERGE_CAN_BE_STALE_ABOUT`): a merged PR by
# this branch's name says nothing about commits made after it, while the tree is the
# commits.
#
# The second piece of evidence a caller may put behind `work_is_landed` is the merged
# PR's own head: `head_is_merged_pr_head` says the commit GitHub squashed *is* this
# HEAD, which settles the same verdicts the tree does and reaches the box the tree
# cannot -- one whose branch was behind the default branch when it merged, so the squash
# produced a tree no commit of the box's ever had. Same set, same dirt gate.
TREE_CAN_SETTLE: frozenset[str] = frozenset({sweep.NEEDS_BRANCH, sweep.NEEDS_REBRANCH, sweep.READY})

# How far back along `origin/<default>` `head_tree_landed` looks for the box's tree. The
# match, when there is one, is near the tip by construction -- the box merged the default
# branch and then its work landed on it -- so this is a bound on the cost of *not* finding
# one, and a deep enough one that a box parked for weeks is still recognised. Unfound is
# reported as not landed, which is the pre-existing refusal.
TREE_SCAN_DEPTH = 200


# A preview box is not a task box, and the distinction is worth a field because every
# destructive decision in this file turns on it. A task box is where work is *made*, so
# nothing may reset it and nothing may reap it until the work has left. A preview box is
# where someone else's work is *looked at*: it holds a disposable copy of a remote ref,
# it is refreshed by resetting onto that ref, and it is worth exactly the disk it costs.
#
# The local branch is `preview/<topic>`, never the previewed ref itself. Two worktrees
# cannot check out one local branch, and the agent's own box got there first — so a
# preview that tried to check out `agent/foo` would fail on precisely the branch anyone
# would most want to preview.
PREVIEW_KIND = "preview"
TASK_KIND = "task"
PREVIEW_VERDICT = "preview"
PREVIEW_BRANCH_PREFIX = "preview/"

# Services whose port is worth an http:// URL in the report. Everything else in the
# registry is a wire protocol a browser cannot open, and printing a URL for Postgres
# invites exactly one support question per person who tries it.
HTTP_SERVICES: frozenset[str] = frozenset(
    {"frontend", "app", "grafana", "prometheus", "minio_console"}
)


def reapable(
    verdict: str,
    *,
    pr_merged: bool = False,
    pr_closed: bool = False,
    holds_uncommitted: bool = True,
    work_is_landed: bool = False,
) -> bool:
    """May this box be destroyed? The one predicate `reap` and `reconcile` both ask.

    `SAFE_TO_REAP` alone is not the whole answer, because **a squash merge is invisible
    to the verdict**. Squashing rewrites the branch's commits, so they are never
    ancestors of the default branch no matter how thoroughly the work landed, and
    `--delete-branch` removes the upstream the classifier would otherwise have read. The
    box therefore comes out of `sweep.classify` as `needs-rebranch` — "unmerged commits
    on a retired branch" — which is true of the refs and false of the work. That verdict
    is not in `SAFE_TO_REAP`, so `reap` refused and `reconcile` returned `HOLD`, forever:
    every squash-merged box was a permanent leak of a checkout, a port lease and a volume
    set, and the only way out was `--force`, which is documented as discarding work.

    So the merged PR is consulted directly, exactly the way `branch_delete_flag` already
    consults it to choose `-D`. The two now agree about one box rather than describing it
    two different ways.

    Two independent signals then have to agree that nothing is at stake, and requiring
    both is deliberate. `holds_uncommitted` is the direct one; `MERGE_CAN_BE_STALE_ABOUT`
    is the verdict's own account of what it is complaining about. Keying on the count
    alone was tried first and `test_reconcile_never_reaps_a_box_holding_work` rejected
    it: a `ready` box means uncommitted work *by definition*, so a zero count next to
    that verdict is two fields disagreeing, and a safety property that resolves such a
    disagreement in favour of destroying is not one.

    Work that exists *only* in the box is therefore still never destroyed on the strength
    of a merge — shipping a branch and carrying on editing is ordinary, and the PR can
    merge while those edits sit there uncommitted. `holds_uncommitted` defaults to `True`
    so a caller that does not know defaults to holding: this predicate must fail towards
    keeping a box that could be destroyed, never towards destroying one that should be
    kept.

    A **preview** box answers the question by construction: every commit in it came from
    a remote ref, so there is nothing in it to strand. It is still gated on
    `holds_uncommitted` while the thing it shows is live, because someone reading a diff
    may well have poked at a file, and a cleanup pass is not the right moment to find
    out.

    **An ended PR overrides that gate, and only for a preview.** The ordinary preview
    dirt is machine-made, not a person's: the frontend container's install step rewrites
    `package-lock.json` through the bind mount the moment the stack comes up, so most
    previews are dirty within minutes of being cut and stay that way. Gating a merged
    preview on dirt therefore held it *forever* — three merged-PR previews accumulated
    27 exited containers, kept their menu rows, and leased the port registry to 16/16 —
    while protecting nothing, because `preview_refresh_steps` already discards the same
    dirt with `reset --hard` on every refresh, without asking. Once the PR the preview
    shows has merged or closed, the review it existed for is over, so the box is pure
    cost and goes. `pr_closed` is consulted **only** in this arm: for a task box a close
    is a policy signal `reap_decision` weighs, never a licence here, because
    `needs-rebranch` plus a closed PR is an abandoned branch — exactly the case
    `MERGE_CAN_BE_STALE_ABOUT` exists to keep holding.

    `SAFE_TO_REAP` is gated on it too, and that gate is **deliberately redundant**.
    `sweep.classify` tests `state.dirty` -- which counts untracked files, and, per
    `sweep.real_changes`, only the tracked paths `git diff` confirms -- before it can
    reach `spent-branch`, `needs-pr` or `clean`, so one of those verdicts already
    implies a clean tree *for
    the snapshot it was computed from*. The parameter exists for the case where that
    stops being true: a verdict cached across a step, a second snapshot taken later, a
    caller that assembles the two from different reads. Ignoring it there was the same
    "two fields disagreeing" the paragraph above refuses to resolve in favour of
    destroying, and it made `holds_uncommitted=True` -- the default, chosen so an
    ignorant caller fails towards keeping -- silently inert on the three commonest
    verdicts a box is destroyed under. A predicate whose safety argument is "the caller
    cannot construct that state" should still refuse the state.

    **A husk is reapable, and it is the one verdict decided without consulting
    `holds_uncommitted` at all.** `skipped` means the leased path is not a git checkout
    -- a removal that died partway, or a `worktree add` that never landed -- so git has
    already stopped tracking it and there is no index, no branch to commit to and no
    `/ship` that could get anything out of it. What is left is files nothing can turn
    into a commit, beside a lease still holding a port slot and a volume set. That
    verdict is in none of the sets above, so `reap` refused it and `reconcile` returned
    `HOLD` forever: the same permanent leak `MERGE_CAN_BE_STALE_ABOUT` and a closed PR
    each document, reached from a third direction -- and this is the one that has
    actually cost something. Four husks held four of sixteen port slots for four days,
    and the nightly `upgrade-project.py --all` then failed every consumer that has a
    stack with "all 16 port slots are in use".

    Not gated on `holds_uncommitted`, because nothing can read dirtiness through a
    missing `.git`: `sweep.inspect` reports 0 for a husk whatever is on disk, so the
    gate would be inert for the honest caller and permanently refusing for the ignorant
    one that takes the `True` default -- which is exactly how this leaked. The gate
    exists to protect work a commit could still rescue; a husk has no such state for it
    to be wrong about.

    `work_is_landed` is the fourth spelling of the same leak, and the first one no PR can
    answer: the work left the box under a *different* branch's PR, so there is no PR
    naming this branch for the merge arm to read. `TREE_CAN_SETTLE` carries what that
    costs and what the evidence is. Two properties keep it narrow. It is **evidence, not
    policy** -- `work_landed` is the only thing that sets it, from two observations
    that each either hold or do not: the box's exact tree on `origin/<default>`
    (`head_tree_landed`), or the box's HEAD being the very commit a merged PR squashed
    (`head_is_merged_pr_head`). An offline machine, an unreadable ref, a short scan and
    a PR gh could not describe all report `False` and leave every refusal exactly where
    it was. And it stays behind `holds_uncommitted`, because both are answers about the
    commit under the edits and never about the edits.
    """
    if verdict == sweep.SKIPPED:
        return True
    if verdict == PREVIEW_VERDICT:
        return pr_merged or pr_closed or not holds_uncommitted
    if verdict in SAFE_TO_REAP:
        return not holds_uncommitted
    if work_is_landed and not holds_uncommitted and verdict in TREE_CAN_SETTLE:
        return True
    return pr_merged and not holds_uncommitted and verdict in MERGE_CAN_BE_STALE_ABOUT


# Marks the block `new` writes into a box's `.env`. Docker Compose's dotenv parser
# takes the LAST assignment of a key, so appending is what lets the block win over a
# seeded copy of the project's own `.env` without editing the lines it came with.
MANAGED_BEGIN = "# --- devkit worktree: managed block (rewritten on every spawn) ---"
MANAGED_END = "# --- end devkit worktree block ---"

# --- reconcile ---------------------------------------------------------------
# What `reconcile` decides to do with one box. Four, because they are the four
# different things that can be true of a task, and each wants a different actor:
REAP = "reap"  # the work has left the box -- destroy it, reclaim the disk
# Not a fifth action: a HOLD the report files separately, per `stranded`. Named here so
# `render_reconcile`'s heading key cannot collide with an action.
STRANDED = "stranded"
MERGE = "merge"  # its PR is green and mergeable -- merge, then reap next pass
HOLD = "hold"  # work exists ONLY here -- never destroyed, always reported
WAIT = "wait"  # its PR is open and not mergeable yet -- someone else's move

# Free space below which reclaiming stops being optional. A box costs its project's
# whole toolchain (`.venv`, `node_modules`) plus, if it has a stack, a volume set --
# hundreds of MB each, and the point of this tier is that there are many at once. At
# or under this floor, `reconcile` also destroys boxes whose PR is merely *open*:
# every commit on them is already on the remote, so what is lost is the convenience
# of having the checkout around, and what is gained is a machine that still works.
DEFAULT_MIN_FREE_GB = 20.0

# How long a box whose PR is open may sit before it is reclaimed anyway. Re-cutting
# one is seconds (`uv` hardlinks from a global cache), so keeping a checkout alive on
# the chance a review comment arrives is a poor trade against a full disk.
DEFAULT_MAX_AGE_DAYS = 3.0

# How long a box may sit `needs-pr` with GitHub reporting **no PR at all** before the
# checkout is reclaimed. Deliberately more than double `DEFAULT_MAX_AGE_DAYS`, because
# the two waits are not the same wait: an open PR has a person attached to it, and this
# state has nobody -- a session pushed a branch and never reached `gh pr create`, which
# is what an interrupted `/ship` leaves behind every time.
#
# Until this existed the case was a permanent `WAIT`: not reapable, not mergeable, not
# aged out, and invisible to `preview-task`'s menu filters because a ref with no PR is
# one `merged_refs` can never subtract. carameli's `agent/comic-book-ui-0819` held a
# port slot, a volume set and a dropdown row for 6.8 days that way, and the only exit
# was `--force` -- the flag documented as discarding uncommitted work, spent on a box
# that has none.
#
# What the reap costs is the checkout and nothing else: `needs-pr` means every commit
# is on the remote, `branch_delete_flag` deletes only the *local* ref, and nothing here
# has ever pushed a branch deletion. The branch stays on origin, so `worktree.py resume
# <project> --branch <it>` brings the whole box back.
DEFAULT_UNCLAIMED_AGE_DAYS = 7.0

# How young a box has to be for "spent-branch and no PR" to mean a race rather than a
# finding. The verdict is read as "cut and never used", and every box is exactly that
# for the first minute of its life: the guard cuts one before the session has written
# anything into it, and `new` returns before the agent has made its first edit.
#
# A pass caught one 90 seconds after `worktree.py new` created it, destroyed the branch,
# and released the slot out from under the session holding its lease -- so the workaround
# an agent had to know was to `git commit --allow-empty` the instant it got a box. The
# scheduled pass runs every 15 minutes (`install-reconcile-task.DEFAULT_INTERVAL_MINUTES`),
# so a grace of one hour is several intervals of margin and costs a genuinely abandoned
# box four extra passes on disk -- against destroying live work, which is unrecoverable.
#
# Only the "never used" branch consults it. Every case above that one is evidence the box
# *was* used, and a merged or closed PR is not less true for being recent.
NEWBORN_GRACE_DAYS = 1.0 / 24.0

# How old a HOLD box with no PR is before the tier calls it **stranded**. A box exists to
# be shipped and destroyed; one that has held work for a day with nothing on GitHub is
# a box whose session ended without shipping, and every pass after that is the same
# HOLD line asking a person to do what the tier was set up so they would not. The
# 2026-08-30 audit found thirteen of them, the oldest ten days old, under a heading
# that said "ship it" to nobody. Stranded is not a reap: `reconcile` still holds the box,
# but it says *stranded* rather than *holding*, and the remedy it names is
# `/triage-boxes` -- an agent reading each one and shipping (`rescue --ship`) or reaping
# it -- rather than the user. A project on hold (`sweep.on_hold`) is stranded at any
# age, since nothing is meant to be in flight there at all.
STRANDED_AGE_DAYS = 1.0


def stranded(
    action: str,
    *,
    pr_open: bool,
    age_days: float,
    on_hold: bool = False,
    stranded_age_days: float = STRANDED_AGE_DAYS,
) -> bool:
    """Whether a `HOLD` is one the tier has stopped expecting a session to finish.

    Only a `HOLD` can be stranded: a `WAIT` has a PR and so a person, a `REAP` is
    leaving. Within that, an open PR means the box is still somebody's -- a session
    editing after it pushed -- so the age rule applies to boxes with **no** PR, and a
    box for a project on hold is stranded at any age because nothing should have cut
    it. Pure, so `reconcile`, `survey` and the session banner cannot disagree about
    which boxes `/triage-boxes` is for.
    """
    if action != HOLD or pr_open:
        return False
    return on_hold or age_days > stranded_age_days


def on_hold_projects(workspace: Path) -> frozenset[str]:
    """`sweep.on_hold` over the workspace file, or nothing when it cannot be read."""
    try:
        return sweep.on_hold(workspace.read_text(encoding="utf-8"))
    except OSError:
        return frozenset()


# `gh pr view` fields. `statusCheckRollup` is per-head-commit, so a stale green from
# before the last push cannot be read as current.
PR_VIEW_FIELDS = "number,url,state,labels,statusCheckRollup,headRefOid"

# How gh says "this branch has no PR", lowercased. Compared as a substring of stderr
# because gh spells the failure in prose and exits 1 for it exactly as it does for an
# auth or network failure -- see `pr_for`, which is the only place this is read.
GH_NO_PR_MESSAGE = "no pull requests found"

# Check-rollup conclusions, worst first. A rollup is only green when every check in it
# is, so the reduction takes the worst present rather than the last.
CHECKS_FAILURE = "FAILURE"
CHECKS_PENDING = "PENDING"
CHECKS_SUCCESS = "SUCCESS"
CHECKS_NONE = ""  # no checks reported at all -- unknown, never treated as green


class WorktreeError(ValueError):
    """The request names a project, box, or state this tool will not act on."""


@dataclass(frozen=True)
class Box:
    """One ephemeral worktree, as recorded in the lease file.

    `slot` is -1 for a project with no Docker tier: there is nothing to publish, so
    nothing is leased and the registry's ceiling is not spent on it.

    `session` is the agent session that spawned it, and is what makes the guard hook
    idempotent — the second edit into a project during one session finds this box
    instead of cutting a second one.

    `kind` is `task` or `preview` (see `PREVIEW_KIND`), and `tracks` is the remote branch
    a preview is a copy of — empty for a task box, which tracks nothing because its
    branch is its own. The pair is what lets `reconcile` look a preview's PR up by the
    branch under review rather than by the throwaway `preview/...` ref, and reap the
    preview when that PR lands.

    `services` is empty for every box but a UI-only preview, where it lists the compose
    services the box actually runs (the project's `[worktree] ui_services`). It is
    recorded on the lease rather than re-read from the manifest because re-serving the
    box — `preview <box name>`, or the restart-after-reboot pass — must scope its
    `compose up` the same way the first one did: a UI-only box's `.env` deliberately
    holds the *source checkout's* ports for every service it does not run, so an
    unscoped `up` here would start a second database on the port the checkout's is
    already bound to.
    """

    name: str
    project: str
    branch: str
    slot: int = -1
    session: str = ""
    created: str = ""
    kind: str = TASK_KIND
    tracks: str = ""
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProvisionStep:
    """One command that makes a fresh box runnable. Exactly one of `argv`/`shell_command`.

    `shell_command` exists only for `[python] install_command`, which is a shell string in
    `.devkit.toml` because that is the shape `session-start.sh` reads it in. Everything
    detected here is argv, so the ladder can be asserted without a shell.
    """

    label: str
    argv: tuple[str, ...] = ()
    shell_command: str = ""


@dataclass(frozen=True)
class SpawnPlan:
    """What `new` would do: git argv run in the *source checkout*, plus the env to seed.

    `provision` is detected from the SOURCE checkout but runs in the box: every file the
    ladder reads (`uv.lock`, `requirements-dev.txt`, `pyproject.toml`, `.devkit.toml`) is
    tracked, so the box is guaranteed the same answer — and detecting at plan time is
    what lets the dry run show the install before it costs three minutes.
    """

    box: Box
    path: str
    steps: tuple[tuple[str, ...], ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    provision: tuple[ProvisionStep, ...] = ()
    # The project's `[worktree] env` templates, kept so `apply_new` can re-expand
    # them if the slot is re-leased between plan and apply. Without this the plan's
    # `env` would be rebuilt from the new port while a derived value like
    # `CORS_ORIGINS` still pointed at the old one — the exact half-configured box
    # this field exists to prevent, arriving only in the race.
    env_templates: dict[str, str] = field(default_factory=dict)
    # The UI-only half of the same race guard: `[worktree] ui_env`, plus the slot the
    # box borrows its backend ports from. Defaults are the no-op that keeps every
    # other caller of `managed_env` on the single-slot path.
    ui_env_templates: dict[str, str] = field(default_factory=dict)
    donor_slot: int = -1
    # Why this box got no port lease, when the project has a stack and wanted one --
    # `lease_slot`'s refusal, empty on the ordinary path (and on a stackless project,
    # which asks for no slot in the first place). Reported, never decided on: the box
    # is cut either way, because writing a file needs no ports. What reads it is the
    # renderer, the guard's message, and `apply_preview`, which refuses to bring a
    # slotless box's stack up onto the source checkout's ports.
    slotless: str = ""
    # True when the branch predates this plan -- `resume` putting a box back on a
    # branch whose own box is gone. Reported, never decided on: every step is already
    # in `steps` by the time this is read, and the two flows differ only in what the
    # renderer and the guard's block message should say happened.
    resumed: bool = False


@dataclass(frozen=True)
class ReapPlan:
    """What `reap` would do.

    `stack_down` is a separate flag rather than another entry in `steps` for the same
    reason `sweep.Plan.pr_title` is: `steps` stays homogeneous git argv, which is what
    lets one safety test read every step in the file and mean it.
    """

    box: str
    path: str = ""
    project: str = ""
    steps: tuple[tuple[str, ...], ...] = ()
    stack_down: bool = False
    slot: int = -1
    refusal: str = ""
    warning: str = ""
    # Carried only so `apply_reap` can write the destruction ledger. Not read by any
    # decision: everything that decides has already been decided by the time a plan
    # exists. See `record_reap`.
    branch: str = ""
    verdict: str = ""
    reason: str = ""
    forced: bool = False
    dirty: int = 0
    # The session the destroyed box was leased to, so `resumable_branch` can find its
    # way back. The only field here that is read again rather than only reported: the
    # ledger is the sole record that a (session, project) pair ever had a box, and
    # without the session on the line there is nothing to match a later edit against.
    session: str = ""

    @property
    def acts(self) -> bool:
        return bool(self.steps or self.stack_down)


@dataclass(frozen=True)
class PreviewPlan:
    """What `preview` will do to one box: cut it, refresh it, or just run its stack.

    Three shapes in one object rather than three plan types, because the caller that has
    to switch on which type it got is the caller that forgets a case. An empty `spawn`
    and an empty `refresh` is the ordinary "the box is already right, start it" path.
    """

    box: Box
    path: str = ""
    spawn: SpawnPlan | None = None
    refresh: tuple[tuple[str, ...], ...] = ()
    up: bool = False
    down: bool = False
    urls: tuple[tuple[str, int, str], ...] = ()
    refusal: str = ""
    warning: str = ""


@dataclass(frozen=True)
class PullRequest:
    """What GitHub says about the PR for a box's branch. All-default means none is known.

    `checks` is the *rollup*, reduced by `rollup_conclusion` to one of the `CHECKS_*`
    constants. It is deliberately not a boolean: "no checks reported" and "every check
    passed" are different answers, and only one of them may be merged on.

    `absent` is the same distinction one layer out, and it exists because the default
    instance is **two** answers at once: `pr_for` returns it when GitHub says there is
    no PR *and* when GitHub could not be asked at all. Every reader before
    `DEFAULT_UNCLAIMED_AGE_DAYS` could conflate them safely, since both meant "do less"
    -- do not merge, do not reap on the strength of a merge. A rule that destroys on
    the *absence* of a PR reads that same emptiness in the opposite direction, so it
    needs the half that was actually observed. `absent` is set only where gh answered
    the question; an offline, unauthenticated or rate-limited `gh` leaves it False and
    the box is held, exactly as it was.
    """

    number: int = 0
    url: str = ""
    state: str = ""  # OPEN / MERGED / CLOSED, "" when there is no PR
    checks: str = CHECKS_NONE
    labels: tuple[str, ...] = ()
    absent: bool = False  # gh was asked and answered: this branch has no PR
    # The sha at the PR's head -- for a merged PR, the exact commit that was squashed.
    # "" when unknown, which `head_is_merged_pr_head` reads as "cannot say".
    head: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.state)

    @property
    def merged(self) -> bool:
        return self.state == "MERGED"

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    @property
    def is_closed(self) -> bool:
        """Closed without merging -- a terminal state a person put the PR into.

        Deliberately not spelled `not is_open` at the call sites: an empty
        `PullRequest` is not open either, and "nobody ever opened one" is the absence
        of a decision where this is a decision that was made.
        """
        return self.state == "CLOSED"


@dataclass(frozen=True)
class Reconciliation:
    """One box's outcome: what should happen to it, and why.

    `action` is one of REAP/MERGE/HOLD/WAIT. `reason` is written to be read by someone
    who has not looked at the box -- it names the PR number or the verdict that decided
    it, because a line saying only "hold" sends the reader to go and find out why.
    """

    box: str
    action: str
    reason: str
    verdict: str = ""
    pr: PullRequest = field(default_factory=PullRequest)


# --- pure helpers -----------------------------------------------------------


def boxes_root(workspace_root: Path) -> Path:
    """Where every ephemeral worktree for this workspace lives."""
    return workspace_root / BOXES_DIR_NAME


def lease_file(workspace_root: Path) -> Path:
    return boxes_root(workspace_root) / LEASE_FILE_NAME


def box_name(project: str, branch: str) -> str:
    """`carameli` + `agent/voicemail-0806` -> `carameli--voicemail-0806`.

    Also the box's `COMPOSE_PROJECT_NAME`, which is what namespaces its containers,
    network and volumes — the same identity `ports.toml` requires of a static
    checkout, so the two tiers can never collide in the Docker daemon. Compose
    accepts `[a-z0-9][a-z0-9_-]*`, which both halves already satisfy: project names
    come from directory names and the topic from `tb.slugify`.
    """
    prefix = tb.managed_branch_prefix(branch)
    topic = branch[len(prefix) :] if prefix else branch
    return f"{project}{NAME_SEP}{topic}"


def box_path(workspace_root: Path, name: str) -> Path:
    return boxes_root(workspace_root) / name


def project_of(name: str) -> str:
    """The project a box name was cut from; "" when the name is not a box name."""
    return name.split(NAME_SEP, 1)[0] if NAME_SEP in name else ""


def kind_of_branch(branch: str) -> str:
    """The kind a lease with no `kind` field must be, read off its branch name.

    Not a convenience: the field is only as reliable as the *oldest* copy of this file
    that writes the lease. `render_leases` emits every field of `Box`, but a worktree.py
    that predates `kind` parses the file into a Box that has none and writes it straight
    back — so one `worktree-guard` spawn or one scheduled `reconcile` from an unupdated
    checkout silently strips `kind` from every box in the file, and a preview reverts to
    being classified as a task box holding unshipped work. The `preview/` prefix cannot
    be stripped that way: it is in the branch name, which every version already keeps.
    """
    return PREVIEW_KIND if branch.startswith(PREVIEW_BRANCH_PREFIX) else TASK_KIND


def parse_leases(text: str) -> dict[str, Box]:
    """Boxes from the lease file's contents. Unreadable content is no boxes, not a crash.

    Falling back to empty is safe in one direction only, and it is the right one: a
    lost lease means a slot is re-offered and a `docker compose up` fails loudly on a
    taken port. Refusing to spawn because a JSON file got truncated would take the
    whole tier down instead.
    """
    try:
        payload = json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    entries = payload.get("boxes") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return {}
    boxes: dict[str, Box] = {}
    for name, raw in entries.items():
        if not isinstance(raw, dict):
            continue
        branch = str(raw.get("branch", ""))
        services = raw.get("services")
        boxes[name] = Box(
            name=name,
            project=str(raw.get("project", project_of(name))),
            branch=branch,
            slot=raw.get("slot", -1) if isinstance(raw.get("slot"), int) else -1,
            session=str(raw.get("session", "")),
            created=str(raw.get("created", "")),
            kind=str(raw.get("kind", "")) or kind_of_branch(branch),
            tracks=str(raw.get("tracks", "")),
            services=(tuple(str(s) for s in services) if isinstance(services, list) else ()),
        )
    return boxes


def render_leases(boxes: Mapping[str, Box]) -> str:
    """The lease file's contents for `boxes`, stable-ordered so diffs stay readable."""
    payload = {
        "boxes": {
            name: {k: v for k, v in asdict(boxes[name]).items() if k != "name"}
            for name in sorted(boxes)
        }
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def next_lease_slot(registry: devkit_ports.Registry, boxes: Mapping[str, Box]) -> int:
    """The lowest port slot free across BOTH tiers.

    `registry.next_free_slot()` only knows `[slots]` — the pinned checkouts — because
    that file is hand-maintained and a box is not in it. Handing out a slot that a
    live box already holds produces the exact "port is already allocated" failure the
    registry exists to prevent, so the two claim sets are unioned here.
    """
    taken = set(registry.slots.values()) | {b.slot for b in boxes.values() if b.slot >= 0}
    for candidate in range(registry.max_slots):
        if candidate not in taken:
            return candidate
    raise devkit_ports.RegistryError(
        f"all {registry.max_slots} port slots are in use ({len(registry.slots)} pinned "
        f"checkouts, {sum(1 for b in boxes.values() if b.slot >= 0)} live boxes). Reap a "
        f"box, raise registry.max_slots, or stop the project's stack publishing to the "
        f"host — a box that runs its tests inside the compose network needs no slot."
    )


def lease_slot(registry: devkit_ports.Registry | None, boxes: Mapping[str, Box]) -> tuple[int, str]:
    """`(slot, refusal)` — a port lease for a new box, or -1 and why there is none.

    **Cutting a box and running its stack are two different needs, and only the second
    one wants a slot.** `next_lease_slot` raises when all of them are taken, and for a
    year that exception aborted the whole spawn — so an agent asked to change one
    TypeScript file was refused a *checkout* because two standing previews and nine
    boxes awaiting a merge were holding the ports. It is the most-recurring entry on
    this machine's harness ledger: seven blocked edits across two sessions on
    2026-08-24 alone, none of which was going to start a container.

    So exhaustion degrades here instead of raising, and the refusal string travels with
    the plan (`SpawnPlan.slotless`) so both the renderer and the guard's message say the
    box cannot bring its stack up. `preview` deliberately does **not** go through this:
    a preview whose whole purpose is a running stack should fail loudly at the point it
    asks, not silently produce a box that cannot serve.

    What a slotless box must never do is `compose up`. Seeding copies the source
    checkout's `.env`, the managed block overrides the port keys, and with no slot there
    are no port keys to override with — so the seeded values stand and the stack binds
    the ports the *checkout* is publishing on. That is the second copy `compose_up`'s
    own docstring describes, and `apply_preview` refuses it rather than trusting every
    future caller to remember.
    """
    if registry is None:
        return -1, ""
    try:
        return next_lease_slot(registry, boxes), ""
    except devkit_ports.RegistryError as exc:
        return -1, str(exc)


# The shortest abbreviation of a session id a lease is trusted to name. Eight hex
# characters is the spelling agents actually cut by hand (`--session <first 8 of the
# id>`, read off a scratchpad path); anything shorter is too little entropy to say
# "this box is that session's".
SESSION_PREFIX_MIN = 8


def sessions_match(recorded: str, session: str) -> bool:
    """Does the lease's session id name this session?

    Exact match, or either id is a prefix of the other with the shorter side at
    least `SESSION_PREFIX_MIN` characters. The prefix case exists because a box cut
    by hand gets `--session <first 8 hex>` — an agent abbreviating its own id — and
    the abbreviation must keep naming the session it abbreviates in both directions,
    or the guard cuts that session a second box and blocks it out of its first one.
    """
    if not recorded or not session:
        return False
    if recorded == session:
        return True
    short, long = sorted((recorded, session), key=len)
    return len(short) >= SESSION_PREFIX_MIN and long.startswith(short)


def find_session_box(boxes: Mapping[str, Box], project: str, session: str) -> Box | None:
    """The box this session already has for `project`, if any.

    What makes the guard hook cheap to fire on every edit: one box per (session,
    project), not one per edit.
    """
    if not session:
        return None
    for box in boxes.values():
        if box.project == project and sessions_match(box.session, session):
            return box
    return None


def claim_refusal(box: Box, session: str, dirty: bool) -> str:
    """Why this claim must not go through unforced, or "" when it may.

    Pure, so the whole rule is testable without a worktree on disk.

    A claim is a *handover*, and the two things it is asked to do are not equally safe.
    Taking over a box whose tree is clean costs nothing: every commit is on the branch,
    and the previous session has nothing in flight to lose. Taking over one that holds
    uncommitted work makes two sessions owners of the same working tree, and the loser
    is whichever one is not the next to push -- the other session's push resets the box
    and the edits are simply gone.

    That has happened. The guard blocked an edit into a live session's box, its block
    message offered `claim --yes` as the remedy, the claim was granted silently, and
    both sessions then edited the same branch until one push discarded the other's
    working tree. The block message is the reason this refusal has to live here rather
    than in the caller's judgement: the agent reading it is being *told* to run claim,
    with nothing in front of it to say the box is somebody's live workspace.

    `dirty` is the whole test, and it is the right one because it is exactly what a
    takeover can destroy -- the same predicate `reapable` uses to refuse a reap, for the
    same reason. An unowned box (no lease session) and a re-claim by the session that
    already holds it are handovers to nobody, so neither is refused.
    """
    if not box.session or sessions_match(box.session, session):
        return ""
    if not dirty:
        return ""
    return (
        f"{box.name} is leased to session {box.session} and its tree holds uncommitted "
        f"changes, so it is somebody's live workspace rather than work that was handed "
        f"over. Claiming it makes two sessions owners of one tree, and the next push "
        f"from either discards the other's edits. Ask that session to commit or ship "
        f"first, or pass --force if you know it has stopped."
    )


def claim_box(
    workspace: Path, name: str, session: str, apply: bool = True, force: bool = False
) -> Box:
    """Re-lease a live box to `session` — the sanctioned takeover.

    The guard blocks an edit into a box leased to a different session and names this
    as the way through when the user really has handed the work over. It is a lease
    rewrite only: nothing in the worktree moves, so the new session inherits the old
    one's uncommitted state as-is.

    `force` is what gets past `claim_refusal`, which declines a box that is still
    somebody's live workspace. See there for why that refusal exists.
    """
    if not session:
        raise WorktreeError(
            "claim needs a session id; an empty one would leave the box unowned "
            "and turn the ownership gate off for it"
        )
    root = workspace.parent
    with lease_lock(root):
        boxes = live_boxes(root)
        box = boxes.get(name)
        if box is None:
            known = ", ".join(sorted(boxes)) or "(none)"
            raise WorktreeError(f"no live box called {name!r}; live boxes: {known}")
        if not force:
            # Read inside the lock and without a fetch: the question is what this
            # worktree holds right now, and the remote has no part in it.
            state = sweep.inspect(box.name, box_path(root, box.name), fetch=False)
            refusal = claim_refusal(box, session, bool(state.dirty))
            if refusal:
                raise WorktreeError(refusal)
        claimed = replace(box, session=session)
        if apply:
            boxes[name] = claimed
            write_leases(root, boxes)
    return claimed


def expand_env_templates(base: Mapping[str, str], templates: Mapping[str, str]) -> dict[str, str]:
    """`templates` with `${NAME}` resolved against `base`. Pure; never raises.

    A box gets its own ports, but a setting *derived* from one does not: seeding copies
    the source checkout's `.env` verbatim, so a value naming the primary's frontend port
    goes on naming it. That is invisible until a browser refuses the request — carameli's
    `CORS_ORIGINS` names `http://localhost:5173`, the box serves on its own port, and the
    app rejects every call its own frontend makes.

    A template naming something not in `base` is **dropped, not written half-expanded**:
    compose's dotenv parser does no substitution of its own, so a surviving `${...}`
    reaches the application as those literal characters, and a CORS origin of
    `http://localhost:${FRONTEND_HOST_PORT}` fails in a way that looks like neither a
    typo nor a missing port. Dropping it leaves the seeded line in force, which is at
    least a value someone chose.
    """
    resolved: dict[str, str] = {}
    for key, template in templates.items():
        try:
            value = string.Template(template).substitute(base)
        except (KeyError, ValueError):
            continue
        resolved[key] = value
    return resolved


def managed_env(
    box: str,
    registry: devkit_ports.Registry | None,
    slot: int,
    templates: Mapping[str, str] | None = None,
    *,
    donor_slot: int = -1,
    own_services: tuple[str, ...] = (),
    ui_templates: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The env a box's stack needs to stand apart from every other stack.

    `COMPOSE_PROJECT_NAME` is the load-bearing one: it namespaces containers, network
    and volumes, and it is what makes the `-v` in `reap` safe (see `reap_plan`).
    The port variables only matter for a project whose compose file publishes to the
    host; one that does not still gets the project name.

    `templates` is the project's `[worktree] env`, expanded last and against everything
    above it — see {@link expand_env_templates}. It comes last on purpose: a template is
    written in terms of the ports, so it cannot be one of the values it reads.

    The keyword trio is the UI-only preview's inversion of the port model. A full box
    gets every port from its own `slot`; a UI-only box runs only `own_services` and
    borrows the rest of the stack from the source checkout, so its env starts from the
    *donor's* slot — every `${APP_HOST_PORT}`-shaped template then lands on the backend
    that is actually running — and only the services the box itself starts are moved
    onto its own slot, which is what keeps two previews' frontends off one port.
    `ui_templates` (`[worktree] ui_env`) expands last of all, on top of `templates`,
    for the values that are only right in this mode (a dev-server proxy target that
    must leave the compose network, say).
    """
    env = {"COMPOSE_PROJECT_NAME": box}
    if registry is not None and slot >= 0:
        if donor_slot >= 0 and own_services:
            env.update(registry.env_for_slot(donor_slot))
            own_keys = {f"{s.upper()}{devkit_ports.ENV_SUFFIX}" for s in own_services}
            env.update({k: v for k, v in registry.env_for_slot(slot).items() if k in own_keys})
        else:
            env.update(registry.env_for_slot(slot))
    if templates:
        env.update(expand_env_templates(env, templates))
    if ui_templates:
        env.update(expand_env_templates(env, ui_templates))
    return env


def render_env(source: str, managed: Mapping[str, str]) -> str:
    """`source` with the managed block appended, replacing an earlier one.

    A fresh worktree checks out **tracked files only**, so a project whose stack reads
    a gitignored `.env` gets none and its compose run fails on missing variables. The
    fix is to seed a copy of the source checkout's file and then override the handful
    of keys that must differ — which works because compose's dotenv parser takes the
    last assignment of a duplicated key, so nothing in the seeded half needs editing.
    """
    kept: list[str] = []
    skipping = False
    for line in source.splitlines():
        if line.strip() == MANAGED_BEGIN:
            skipping = True
        elif line.strip() == MANAGED_END:
            skipping = False
        elif not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [
        MANAGED_BEGIN,
        *[f"{key}={value}" for key, value in sorted(managed.items())],
        MANAGED_END,
    ]
    return "\n".join([*kept, "", *block]) + "\n"


def venv_python(windows: bool) -> str:
    """Path to the box's own interpreter, relative to the box."""
    return ".venv/Scripts/python.exe" if windows else ".venv/bin/python"


def npm_executable(windows: bool) -> str:
    """npm's program name on this platform.

    On Windows npm ships as `npm.cmd`, a batch shim; there is no `npm.exe`. These steps
    run as argv with no shell — deliberately, so the ladder can be asserted — and argv
    resolution does not consult PATHEXT, so a bare `npm` raises `[WinError 2] The system
    cannot find the file specified`.

    That failure was *silent in effect*: `run_provision` reports a step it could not
    start as a `[warn]` and keeps the box, so every Windows box came out with no
    `node_modules` while still announcing itself provisioned. Every frontend check —
    eslint, tsc, stylelint, markdownlint — was then unrunnable in a box, so `/ship`'s
    changed-scope lint gate could not catch a frontend or Markdown defect locally and
    left it to CI.
    """
    return "npm.cmd" if windows else "npm"


def venv_step(python_version: str = "") -> ProvisionStep:
    """How a box's own `.venv` gets created, on the interpreter the project is pinned to.

    The marker files say which *dependency model* a project uses; none of them says which
    **interpreter** resolves it. So this step ran `sys.executable -m venv`, which is
    whatever Python happens to be running `worktree.py` — the workstation default. A
    project pinned to 3.12 across its `FROM python:` tag, its uv-compiled locks,
    `mypy.ini` and CI therefore got a box venv on 3.14, and the box announced itself
    provisioned: the mismatch surfaces later as a resolution or type-check failure that
    reads as a broken branch rather than as the wrong interpreter.

    The version reaching here is `[python] version` from `.devkit.toml`, or failing that
    whatever `detect_python_version` reads off the project's own build files. With one,
    `uv venv --python` picks that interpreter — and fetches it when the machine has no such
    version, which `python -m venv` structurally cannot do, since it can only ever produce
    a copy of the interpreter already running it.

    The manifest was the sole source at first, on the reasoning that devkit should not
    parse build files it does not own. That held for the projects that filled the field in
    and left every other one exactly as broken as before: carameli pinned 3.12 in three
    places devkit could see and still got a 3.14 box, because an absent field reads as
    "unpinned" rather than as "not asked".

    The unpinned branch spawns `sweep.console_python()` rather than `sys.executable`
    for the reason that helper documents: the nightly upgrade pass reaches here under
    `pythonw.exe`, `CREATE_NO_WINDOW` is ignored for a GUI child, and `venv` re-spawns
    `ensurepip` -- which is a visible console window for about sixteen seconds, once
    per box, on a machine nobody is sitting at.
    """
    if python_version:
        return ProvisionStep(
            f"create .venv (python {python_version})",
            ("uv", "venv", "--python", python_version, ".venv"),
        )
    return ProvisionStep("create .venv", (sweep.console_python(), "-m", "venv", ".venv"))


def provision_steps(
    present: frozenset[str] | set[str],
    install_command: str = "",
    frontend_dir: str = "",
    windows: bool = os.name == "nt",
    python_version: str = "",
    frontend_locked: bool = False,
) -> tuple[ProvisionStep, ...]:
    """What makes a fresh box runnable, from the marker files the project ships.

    A linked worktree checks out **tracked files only**, so a box has no `.venv` and no
    `node_modules`. Nothing else was going to create them: `session-start.sh` returns
    early on a local machine (`CLAUDE_CODE_REMOTE != true`) precisely because a static
    checkout is provisioned once by hand and never again. So a box could be cut, edited
    in, and then fail its own `/ship` — `ship.py` runs the changed-scope lint gate, and
    there was no ruff in it.

    Detection, not configuration, and the same ladder `session-start.sh` walks, in the
    same order: the manifest's `install_command` wins, then the lockfile on disk decides.
    The order matters more than the contents — a project with both `uv.lock` and a
    `pyproject.toml` must not be installed twice, and the lockfile is the pinned one.

    `uv` rather than pip because it is already this workstation's installer
    (`new-project.py` runs `uv lock`, `session-start.sh` bootstraps it) and because it
    hardlinks from a global cache, which is what makes a per-task box affordable at all:
    the second box for a project costs seconds and almost no disk.
    """
    steps: list[ProvisionStep] = []
    python = venv_python(windows)
    if install_command:
        steps.append(ProvisionStep(".devkit.toml install_command", shell_command=install_command))
    elif "uv.lock" in present:
        # uv owns ./.venv here and creates it itself, so there is no venv step. The pin
        # still has to reach it: `requires-python` in the lock is a floor, so a project
        # pinned to 3.12 resolves happily on 3.14 unless the version is passed through.
        pin = ("--python", python_version) if python_version else ()
        steps.append(
            ProvisionStep("uv sync (uv.lock)", ("uv", "sync", "--all-extras", "--all-groups", *pin))
        )
    elif "requirements-dev.txt" in present:
        locks = ["-r", "requirements-dev.txt"]
        if "requirements.txt" in present:
            locks = ["-r", "requirements.txt", *locks]
        steps.append(venv_step(python_version))
        steps.append(
            ProvisionStep(
                "uv pip install (requirements locks)",
                ("uv", "pip", "install", "--python", python, *locks),
            )
        )
    elif "pyproject.toml" in present:
        steps.append(venv_step(python_version))
        steps.append(
            ProvisionStep(
                "uv pip install -e .[dev] (unlocked pyproject)",
                ("uv", "pip", "install", "--python", python, "-e", ".[dev]"),
            )
        )
    if frontend_dir:
        # `ci` when a lockfile is there, and the difference is not about speed.
        # **`npm install` writes `package-lock.json`**, so provisioning a box left a
        # tracked file modified before anyone had edited anything -- and a box that
        # holds a tracked change is one `reapable` refuses to destroy, on any verdict,
        # forever. Two carameli preview boxes sat on a port slot each for that reason
        # alone, and the registry filling is what made the next box preview fail.
        # `ci` installs the lock exactly and never rewrites it, which is also what the
        # lock is for.
        verb = "ci" if frontend_locked else "install"
        steps.append(
            ProvisionStep(
                f"npm {verb} ({frontend_dir})",
                (
                    npm_executable(windows),
                    verb,
                    "--prefix",
                    frontend_dir,
                    "--no-audit",
                    "--no-fund",
                ),
            )
        )
    return tuple(steps)


PROVISION_MARKERS = ("uv.lock", "requirements.txt", "requirements-dev.txt", "pyproject.toml")


# The build files that pin an interpreter *unambiguously*, in the order they win. A
# `requires-python` floor is deliberately absent: `>=3.12` is satisfied by 3.14, which is
# the very skew this exists to stop.
PIN_FILES = (".python-version", "Dockerfile")

_FROM_PYTHON = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*python:(\d[^\s@]*)", re.IGNORECASE | re.MULTILINE
)


def detect_python_version(source: Path) -> tuple[str, str]:
    """The interpreter a project pins in its own build files, and which file said so.

    `[python] version` in `.devkit.toml` is the seam and still wins, but a project that
    never sets it is not therefore unpinned — carameli pins 3.12 in `FROM python:3.12-slim`
    and got a box venv on the workstation's 3.14 anyway, because the manifest field was the
    *only* thing consulted and an absent field is indistinguishable from "no pin". That is
    the same detect-first shape `install_command` already has: read what the files on disk
    say, and keep the manifest for the projects they do not describe.

    Only the two spellings that name an exact interpreter count. An `ARG`-interpolated tag
    (`FROM python:${PYTHON_VERSION}`) does not match, and neither does a pyenv virtualenv
    name in `.python-version`; both leave the box on the running interpreter, as before.

    Returns `("", "")` when nothing pins one.
    """
    for name in PIN_FILES:
        try:
            text = (source / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if name == ".python-version":
            line = text.strip().splitlines()[0].strip() if text.strip() else ""
            if line and line[0].isdigit():
                return line, name
            continue
        match = _FROM_PYTHON.search(text)
        if match:
            # `3.12-slim`, `3.12.7-bookworm` -> the version, without the variant.
            return match.group(1).split("-")[0], name
    return "", ""


def plan_provision(
    source: Path, windows: bool = os.name == "nt", quiet: bool = False
) -> tuple[ProvisionStep, ...]:
    """`provision_steps` for a real checkout: read the markers and the manifest.

    `quiet` exists for the worktree guard, whose stderr IS its block message: a
    PreToolUse hook's exit-2 stderr is shown to the agent as `PreToolUse:<tool> hook
    error`, so a plan-time warning printed here lands as the FIRST line of every block,
    ahead of the reason for the block -- and the guard never runs these steps anyway
    (`apply_new(provision=False)`). The warnings still reach whoever provisioning
    actually answers to: `worktree.py new` and `worktree.py provision` plan loudly.
    """
    present = {name for name in PROVISION_MARKERS if (source / name).is_file()}
    install_command = ""
    frontend_dir = ""
    python_version = ""
    try:
        cfg = harness_config.load(source)
        install_command = cfg.python.install_command
        python_version = cfg.python.version
        if cfg.frontend.enabled and (source / cfg.frontend.dir).is_dir():
            frontend_dir = cfg.frontend.dir
    except Exception as exc:
        # No manifest, or an unreadable one: the marker files still describe the project,
        # and a box with a Python toolchain and no frontend one beats no box at all. Said
        # out loud rather than swallowed, because the silent version of this is a box that
        # is missing exactly the frontend tier its lint gate is about to ask for.
        if not quiet:
            print(
                f"worktree: could not read {source.name}/.devkit.toml ({type(exc).__name__}); "
                f"provisioning from the lockfiles alone.",
                file=sys.stderr,
            )
    if not python_version:
        python_version, origin = detect_python_version(source)
        if python_version and not quiet:
            print(
                f"worktree: no [python] version in {source.name}/.devkit.toml; "
                f"provisioning on {python_version}, the pin in {origin}. Set the field to "
                f"override it.",
                file=sys.stderr,
            )
    # Read off the SOURCE checkout, like every other marker here: the lockfile is
    # tracked, so the box is guaranteed the same answer and the dry run shows the verb
    # that will actually run.
    frontend_locked = bool(frontend_dir) and (source / frontend_dir / "package-lock.json").is_file()
    return provision_steps(
        present,
        install_command,
        frontend_dir,
        windows=windows,
        python_version=python_version,
        frontend_locked=frontend_locked,
    )


def plan_env_templates(source: Path) -> dict[str, str]:
    """The project's `[worktree] env` templates, or none. Never raises.

    Read from the SOURCE checkout for the reason `SpawnPlan.provision` gives: the
    manifest is tracked, so the box is guaranteed the same answer, and reading it at
    plan time is what puts the derived values in the dry run.

    Silent on failure, unlike `plan_provision`'s warning. A missing toolchain leaves a
    box that cannot run its own gates, which is worth a line on stderr; a project with
    no derived `.env` values is the ordinary case and has nothing to report.
    """
    with contextlib.suppress(Exception):
        return dict(harness_config.load(source).worktree.env)
    return {}


# How many names `_unretired_branch` will try before it stops asking. Each attempt is a
# `gh` round trip, and the realistic collision depth is one: past that, something is wrong
# with the answer rather than with the name, and a box that spawns beats a box that hangs.
MAX_RETIRED_ATTEMPTS = 4


def _unretired_branch(
    slug: str,
    taken: set[str],
    today: _dt.date | None,
    prefix: str,
    retired: Callable[[str], bool] | None,
) -> str:
    """`tb.branch_name`, re-rolled while `retired` claims the name is permanently spent.

    The re-roll goes through `taken` rather than through a counter of its own, so a
    retired name is disambiguated by the same `-N` suffix a live one gets and the two
    cases cannot drift apart.

    Fails **open**: `retired` is asked inside `contextlib.suppress`, and the last
    candidate stands once the attempts run out. A lookup that raises, or a remote that
    answers slowly enough to be wrong, must not be able to stop a box being cut -- the
    branch policy still refuses a genuinely retired name at commit time, which is the
    same place it is refused today.
    """
    branch = tb.branch_name(slug, taken, today, prefix=prefix)
    if retired is None:
        return branch
    for _attempt in range(MAX_RETIRED_ATTEMPTS):
        spent = False
        with contextlib.suppress(Exception):
            spent = retired(branch)
        if not spent:
            return branch
        taken.add(branch)
        branch = tb.branch_name(slug, taken, today, prefix=prefix)
    return branch


def spawn_plan(
    project: str,
    workspace_root: Path,
    slug: str,
    default_branch: str,
    existing_branches: set[str],
    boxes: Mapping[str, Box],
    registry: devkit_ports.Registry | None = None,
    session: str = "",
    fetch: bool = True,
    today: _dt.date | None = None,
    provision: tuple[ProvisionStep, ...] = (),
    env_templates: Mapping[str, str] | None = None,
    branch_prefix: str = tb.BRANCH_PREFIX,
    retired: Callable[[str], bool] | None = None,
) -> SpawnPlan:
    """Everything `new` will run, decided without touching git.

    The branch is cut from `origin/<default_branch>`, **not** from the source
    checkout's HEAD. That is the one place this differs from `sweep.branch_plan`, and
    the reason is the whole difference between the tiers: sweep is rescuing work that
    already exists in a dirty tree, so it must branch from HEAD or clobber it. A box
    starts empty, so starting anywhere but the tip of the default branch would hand
    the task a stale base for no benefit.

    `--no-track` for the reason `tb.checkout_argv` documents at length: branching off
    a remote-tracking ref makes `origin/<default>` the new branch's upstream, and a
    later bare `git push` then lands the task's commits straight on the default
    branch.

    `branch_prefix` is how an unattended job says so in the ref itself
    (`tb.AUTOMATION_PREFIX`). It reaches nothing but the branch name: `box_name` strips
    whichever managed prefix it finds, so the box, its `COMPOSE_PROJECT_NAME` and its
    lease are spelled identically whichever namespace cut it.

    `retired` answers "has a PR from this name already merged?", and a name it claims is
    disambiguated exactly as a live collision is. `existing_branches` cannot cover that
    case on its own: a merged branch is *deleted*, locally and on the remote, so nothing
    on disk remembers it while the branch policy refuses it forever. Two sessions naming
    a task after the same file on the same day produce the same slug, which is not
    hypothetical -- it happened twice in three days (`agent/scripts-preview-task-0824`
    after devkit#211, `agent/resume-0826` after devkit#226). The cost lands as late as it
    possibly can: the box provisions, the edits apply, the suite passes, and the *first
    commit* is refused, with no `rebranch` verb to recover and a dirty box that will not
    reap. None means no lookup -- the default, and what an offline or dry-run plan gets.
    """
    branch = _unretired_branch(
        tb.slugify(slug), set(existing_branches), today, branch_prefix, retired
    )
    name = box_name(project, branch)
    path = box_path(workspace_root, name)
    slot, slotless = lease_slot(registry, boxes)

    steps: list[tuple[str, ...]] = []
    if fetch:
        steps.append(("fetch", "--quiet", "origin"))
    steps.append(
        ("worktree", "add", "--no-track", "-b", branch, str(path), f"origin/{default_branch}")
    )
    box = Box(
        name=name,
        project=project,
        branch=branch,
        slot=slot,
        session=session,
        created=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    )
    return SpawnPlan(
        box=box,
        path=str(path),
        steps=tuple(steps),
        env=managed_env(name, registry, slot, env_templates),
        provision=provision,
        env_templates=dict(env_templates or {}),
        slotless=slotless,
    )


def resume_plan(
    project: str,
    workspace_root: Path,
    branch: str,
    remote_branches: set[str],
    existing_branches: set[str],
    boxes: Mapping[str, Box],
    registry: devkit_ports.Registry | None = None,
    session: str = "",
    fetch: bool = True,
    provision: tuple[ProvisionStep, ...] = (),
    env_templates: Mapping[str, str] | None = None,
) -> SpawnPlan:
    """Everything `resume` will run: a box back on `branch`, decided without touching git.

    The counterpart to `spawn_plan`, and the difference is the whole point. `new` mints
    a branch because a task starts empty; `resume` is handed one that already carries
    commits and a PR, because its box was destroyed while the work was still open --
    which `reconcile` does deliberately under disk pressure, on the stated grounds that
    "the remote has every commit, so only the checkout is lost". That trade is only
    honest if the checkout can be got back, and until this verb existed it could not:
    every path back into the tier ran through `spawn_plan`, which can only cut a *new*
    branch off `origin/<default>`. So the next edit opened a second branch, a second
    PR and a second review of the same work, silently.

    `--track`, where `spawn_plan` is emphatic about `--no-track`, and for that same
    reason read the other way round: tracking is dangerous there because the upstream
    would be `origin/<default>`, so a bare `git push` lands the task's commits on the
    default branch. Here the upstream is `origin/<branch>` -- the branch's own remote --
    which is precisely where a bare push should go, and is what makes finishing from a
    resumed box no different from finishing from the box it replaces.

    `preview` also checks out an existing ref and deliberately does *not* do this: it
    copies the ref to a throwaway `preview/…` branch so a push can never land on someone
    else's task branch. The distinction is ownership, not mechanism. A preview is a
    stranger looking at the work; a resume is the work's own session picking it back up,
    and it needs to be able to push.
    """
    if not branch:
        raise WorktreeError("resume needs a branch -- pass --branch, or --pr N to look one up")
    if branch not in remote_branches:
        raise WorktreeError(
            f"origin has no branch {branch!r} in {project} -- nothing to resume. "
            f"A branch whose PR merged is finished work; cut a new box with "
            f"`worktree.py new {project}`."
        )
    name = box_name(project, branch)
    held = boxes.get(name)
    if held is not None:
        raise WorktreeError(
            f"{branch} is already checked out in {name} -- resume has nothing to do. "
            f"If it is leased to another session: "
            f"worktree.py claim {name} --session {session or '<your session id>'} --yes"
        )
    path = box_path(workspace_root, name)
    slot, slotless = lease_slot(registry, boxes)

    steps: list[tuple[str, ...]] = []
    if fetch:
        steps.append(("fetch", "--quiet", "origin"))
    if branch in existing_branches:
        # The local branch outlived its box: a forced reap keeps it (it may carry
        # commits no remote has), and so does a `git worktree remove` run by hand.
        # `-b` would refuse it, and re-creating it from origin would be the one move
        # that discards exactly those commits.
        steps.append(("worktree", "add", str(path), branch))
    else:
        steps.append(("worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}"))
    box = Box(
        name=name,
        project=project,
        branch=branch,
        slot=slot,
        session=session,
        created=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    )
    return SpawnPlan(
        box=box,
        path=str(path),
        steps=tuple(steps),
        env=managed_env(name, registry, slot, env_templates),
        provision=provision,
        env_templates=dict(env_templates or {}),
        slotless=slotless,
        resumed=True,
    )


def preview_box_name(project: str, ref: str, ui: bool = False) -> str:
    """`carameli` + `agent/ui-editor-0817` -> `carameli--preview-ui-editor-0817`.

    Deliberately not `box_name`'s spelling. A preview of a branch and the agent box that
    owns it exist at the same time by design, and one name for the two would be one
    `COMPOSE_PROJECT_NAME` for two stacks — the exact collision the lease tier exists to
    prevent, arriving through the door marked "convenience".

    A UI-only preview gets `carameli--preview-ui--editor-0817` — the mode joined with
    `NAME_SEP`, not a hyphen, because `tb.slugify` never emits a double hyphen and so
    no *full* preview's name can contain one: `preview-ui-<topic>` is already the full
    preview of any branch whose topic starts with `ui-`, which the example above is.
    Distinct names because the two modes coexist on purpose — a full preview of the
    same ref answers a different question and must not adopt (or reset) this box.
    """
    prefix = tb.managed_branch_prefix(ref)
    topic = ref[len(prefix) :] if prefix else ref
    mode = f"{PREVIEW_KIND}-ui{NAME_SEP}" if ui else f"{PREVIEW_KIND}-"
    return f"{project}{NAME_SEP}{mode}{tb.slugify(topic)}"


def preview_branch(ref: str, ui: bool = False) -> str:
    """The local branch a preview of `ref` checks out.

    `preview/ui/<topic>` for a UI-only preview: `tb.slugify` never emits a `/`, so the
    extra path segment cannot collide with any full preview's branch, for the same
    reason the box name's double separator cannot.
    """
    prefix = tb.managed_branch_prefix(ref)
    topic = ref[len(prefix) :] if prefix else ref
    mode = "ui/" if ui else ""
    return f"{PREVIEW_BRANCH_PREFIX}{mode}{tb.slugify(topic)}"


def preview_refresh_steps(tracks: str) -> tuple[tuple[str, ...], ...]:
    """Git argv, run **in the box**, that puts it back on the tip of `origin/<tracks>`.

    The refspec is written out in full rather than left to `git fetch origin <branch>`.
    The short form updates the remote-tracking ref only as a side effect of the default
    refspec matching, so on a repo configured to fetch a subset it silently updates
    `FETCH_HEAD` alone — and the `reset` that follows would then move the box onto
    whatever it was already on, reporting a refresh that did not happen.
    """
    return (
        ("fetch", "--quiet", "origin", f"+refs/heads/{tracks}:refs/remotes/origin/{tracks}"),
        ("reset", "--hard", f"origin/{tracks}"),
    )


def preview_refresh_decision(kind: str, dirty: int, force: bool) -> tuple[bool, str]:
    """`(refresh, why not)` — may this box be reset onto the ref it is showing?

    **A task box is never refreshed, at any `--force`.** `reset --hard` there would
    discard an agent's uncommitted work, and the promise of this whole tier is that
    nothing does. `preview <task box>` is still a useful request — bring its stack up and
    say which ports it is on — so it is served, and the reset is what is withheld.

    A preview box holds no work by construction, so the only thing a reset can cost is an
    edit someone made while reading. That is worth reporting and worth `--force`, which
    is the same bargain `reap_decision` strikes for the same kind of edit.
    """
    if kind != PREVIEW_KIND:
        return False, (
            "a task box is served as it is — only a preview box is reset onto its ref, "
            "because reset --hard in a task box would discard the work it exists to hold"
        )
    if dirty and not force:
        return False, f"{dirty} uncommitted file(s) here — pass --force to discard them and refresh"
    return True, ""


def preview_urls(
    registry: devkit_ports.Registry | None, slot: int, services: tuple[str, ...] = ()
) -> tuple[tuple[str, int, str], ...]:
    """`(service, host port, url)` for a box's slot; url is "" for a non-HTTP service.

    This is the half of the port model that never had an output. The registry has always
    known that a box on slot 3 publishes Vite on 5176, and nothing ever said so — so the
    documented way to find a box's UI was to open its seeded `.env` and do the arithmetic
    by hand, which is a step that gets skipped and then reported as "the preview didn't
    come up".

    `services` non-empty is a UI-only box (`Box.services`), and the list is filtered to
    what it actually runs: the slot prices every service, but nothing binds the others
    here, so printing them would send the reviewer to eight ports that refuse the
    connection. A service the registry does not price is silently absent either way.
    """
    if registry is None or slot < 0:
        return ()
    return tuple(
        (service, port, f"http://localhost:{port}" if service in HTTP_SERVICES else "")
        for service, port in sorted(registry.ports_for_slot(slot).items())
        if not services or service in services
    )


def primary_url(urls: Iterable[tuple[str, int, str]]) -> str:
    """The one URL worth printing on a single line: the UI if there is one.

    `preview_urls` is sorted by service name, which puts `app` first and Vite seventh —
    so a one-line summary that took the first entry would answer "where is the change I
    am reviewing" with the API root, on the mode whose whole purpose is looking at a
    frontend.
    """
    ranked = {service: rank for rank, service in enumerate(("frontend", "app"))}
    candidates = [(ranked.get(service, len(ranked)), url) for service, _, url in urls if url]
    return min(candidates)[1] if candidates else ""


def preview_spawn_plan(
    project: str,
    workspace_root: Path,
    ref: str,
    boxes: Mapping[str, Box],
    registry: devkit_ports.Registry | None = None,
    fetch: bool = True,
    provision: tuple[ProvisionStep, ...] = (),
    now: _dt.datetime | None = None,
    env_templates: Mapping[str, str] | None = None,
    ui_services: tuple[str, ...] = (),
    ui_env_templates: Mapping[str, str] | None = None,
    donor_slot: int = -1,
) -> SpawnPlan:
    """`spawn_plan` for a preview: an existing remote ref instead of a fresh branch.

    `--no-track` for the reason `spawn_plan` gives and then one more that is specific to
    this mode: a preview branch with `origin/<ref>` as its upstream turns a reflexive
    `git push` from inside the box into a push onto **someone else's task branch**, which
    is the one thing a read-only checkout must not be able to do by accident.

    `ui_services` non-empty makes it a UI-only preview: the box gets its own name and
    branch namespace (`preview_box_name(ui=True)`), records the services on its lease,
    and seeds a two-slot env — its own ports for those services, `donor_slot`'s (the
    source checkout's) for the backend it borrows. See `managed_env`.
    """
    ui = bool(ui_services)
    name = preview_box_name(project, ref, ui=ui)
    path = box_path(workspace_root, name)
    slot = next_lease_slot(registry, boxes) if registry is not None else -1
    local = preview_branch(ref, ui=ui)

    steps: list[tuple[str, ...]] = []
    if fetch:
        steps.append(("fetch", "--quiet", "origin", f"+refs/heads/{ref}:refs/remotes/origin/{ref}"))
    # `-B`, not `-b`: the branch outlives its box. A reap keeps it, and so does a
    # `git worktree remove` run by hand, so the next preview of the same ref died on
    # `fatal: a branch named 'preview/resume-0820' already exists` -- with no box, no
    # lease and no slot anywhere to point at, which reads as the task being broken
    # rather than as one stale ref. `resume_plan` preserves an orphaned *task* branch
    # for a reason that does not carry over: a task branch may hold commits no remote
    # has, and `reap` is what leaves it behind. A preview branch reaches that state by
    # one route only -- a hand-run `git worktree remove`, since `reap` already refuses
    # a preview that grew a commit -- and resetting is still the answer, because a
    # preview of `<ref>` that showed somebody's stray commits on top would not be a
    # preview of `<ref>`. That is the same force-reset `preview_refresh_steps` performs
    # on a live box at every re-open, arriving one box later. git still refuses `-B`
    # for a branch checked out in another worktree, which is the live case, and
    # adoption handles that long before anything reaches this plan.
    steps.append(("worktree", "add", "--no-track", "-B", local, str(path), f"origin/{ref}"))
    box = Box(
        name=name,
        project=project,
        branch=local,
        slot=slot,
        session="",
        created=(now or _dt.datetime.now(_dt.UTC)).isoformat(timespec="seconds"),
        kind=PREVIEW_KIND,
        tracks=ref,
        services=tuple(ui_services),
    )
    return SpawnPlan(
        box=box,
        path=str(path),
        steps=tuple(steps),
        env=managed_env(
            name,
            registry,
            slot,
            env_templates,
            donor_slot=donor_slot,
            own_services=tuple(ui_services),
            ui_templates=ui_env_templates,
        ),
        provision=provision,
        env_templates=dict(env_templates or {}),
        ui_env_templates=dict(ui_env_templates or {}),
        donor_slot=donor_slot,
    )


def preview_branch_delete_flag(state: sweep.State, copy_intact: bool | None = None) -> str:
    """`-D` for a preview branch that is still a copy, `-d` for one that is not.

    `-d` refuses any branch that is not an ancestor of the checkout's HEAD, which a
    `preview/...` branch never is — so planning `-d` here would end every preview reap in
    the `FAILED at git branch -d` that `reap_plan` documents at length for forced reaps.
    `-D` is safe *because* the branch is a copy of a remote ref, and only while it is
    one. A commit made inside a preview box exists nowhere else, and destroying it in a
    cleanup is the one thing this tier promises not to do.

    "Still a copy" is `copy_intact` — whether the tip is an ancestor of the
    `refs/remotes/origin/<tracks>` ref the preview fetched (`preview_copy_intact`).
    `state.ahead` was asked first and is the wrong field: it counts against
    `origin/<default>`, so a preview of any branch not yet merged there read as "not a
    copy", and after a squash merge it *always* did — every reap of a squash-merged
    preview failed at `git branch -d` and leaked the `preview/...` ref, which is how
    two of the three merged previews reaped on 2026-08-25 exited 1. `ahead <= 0` is
    kept as the licence that needs no lookup (every commit is on the default branch, so
    nothing can be lost) and as the fallback when the tracking ref is gone and the
    question is unanswerable (`copy_intact=None`).
    """
    if state.ahead <= 0:
        return "-D"
    if copy_intact:
        return "-D"
    return "-d"


def preview_copy_intact(git: sweep.Git, branch: str, tracks: str) -> bool | None:
    """Whether `branch` still only holds commits the ref it copied has. Never raises.

    `merge-base --is-ancestor` against `refs/remotes/origin/<tracks>` — the tracking
    ref `preview_spawn_plan` and every refresh fetch explicitly, which survives the
    remote branch's deletion after a squash merge (fetching a refspec never prunes).
    Exit 0 is yes, 1 is no (the preview grew a commit of its own), anything else —
    either ref missing, no git — is `None`: unanswerable, and the caller must not
    treat that as either answer.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        result = git(
            "merge-base",
            "--is-ancestor",
            f"refs/heads/{branch}",
            f"refs/remotes/origin/{tracks}",
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
    return None


def reap_decision(
    verdict: str,
    reason: str,
    force: bool,
    *,
    pr_merged: bool = False,
    pr_closed: bool = False,
    holds_uncommitted: bool = True,
    awaiting_pr: bool = False,
    unclaimed: bool = False,
    work_is_landed: bool = False,
) -> tuple[bool, str]:
    """`(allowed, note)` — may this box be destroyed, and what to say about it.

    This is the inversion that replaces sweeping. Work cannot be stranded in a box
    because the only way to free the box is to have got the work out of it first, and
    that is checked here rather than discovered by a sweep days later.

    `--force` is deliberately available and deliberately narrow. It discards the
    *worktree*, so uncommitted edits go; it never upgrades `branch -d` to `-D`
    (`branch_delete_flag`), so committed work survives as a local branch even when the
    box it was made in does not. Uncommitted junk should not need a human; commits
    should never be destroyed by a cleanup command.

    The merged-PR case is `reapable`'s, not `--force`'s, and so is the landed-tree case
    beside it (`work_is_landed`) — the same reasoning one verdict over. Forcing past a verdict that is
    merely *stale about a squash* spends the one flag that also discards uncommitted
    work, on the most ordinary ending a box has — which teaches the reflex on the exact
    boxes where the refusal is the point.

    `awaiting_pr` is the second refusal, and it guards a box holding no work at all.
    `needs-pr` means every commit is on the remote, so `reapable` says yes and nothing is
    lost in the sense that word usually carries — but the checkout is where a review
    comment gets answered, and destroying it the moment the branch is pushed is reaping
    on the strength of the *push*. `plan_reap` sets this for the callers with no PR in
    hand (`reap`, and `list` through `survey`); `reconcile` passes a `pr` instead and
    is never flagged, so its pressure and `max_age_days` paths still reap an open PR
    deliberately. A merge clears it, which is what `pr_merged` is doing here.

    So does a **close**, and not because the work landed -- it did not. `awaiting_pr`
    names a wait, and a closed PR is the end of one: someone read the branch and
    declined it, so there is no review left to answer in this checkout and no merge
    left to arrive. Left set, it made every closed-PR box a permanent `needs-pr` --
    reported as holding work it does not hold, leaking a checkout, a lease and a
    volume set, with `--force` as the only way out. That is the same leak `reapable`
    documents for squash merges, reached from the opposite end, and it happened here:
    two boxes whose duplicate PRs were closed on 2026-08-20 had to be forced.

    A close does **not** stand in for a merge on any task verdict, and `reapable` is
    the line: `needs-rebranch` plus a closed PR is an abandoned branch, which is
    precisely the case `MERGE_CAN_BE_STALE_ABOUT` exists to keep holding. What is
    cleared here is a *policy* refusal about a box whose commits are already on the
    remote, never the safety predicate underneath it. The one place `pr_closed` does
    reach `reapable` is its preview arm, where a close and a merge mean the same
    thing — the review the copy was cut for is over.

    `unclaimed` is the third way the wait ends and the only one nobody records on
    GitHub: no PR was ever opened, and the box is past `DEFAULT_UNCLAIMED_AGE_DAYS`.
    The wait `awaiting_pr` names is a wait for a person to act, and after a week of a
    branch sitting on the remote with nothing pointed at it, the honest reading is that
    nobody is going to. It clears the same *policy* refusal a close clears and touches
    `reapable` not at all — and it must be computed from `PullRequest.absent`, never
    from an empty PR, or an offline `gh` becomes a licence to reap. `plan_reap` is
    where that is assembled, because the age lives on the box and this function has
    only the verdict.

    Without it the state's one exit was `--force`, which is documented two paragraphs
    up as the flag that discards uncommitted work — spent on the box that by definition
    has none. A refusal whose only bypass is a bigger hammer teaches the hammer.
    """
    if awaiting_pr and not (pr_merged or pr_closed):
        # `holds_uncommitted` is re-asked here rather than left to `reapable` below,
        # because this arm returns before it. The verdicts in `AWAITS_A_MERGE` all
        # imply a clean tree, so the gate is inert for an honest caller — which is
        # exactly how the husk case leaked, and the reason to spell it out anyway:
        # nothing else stands between an unattended pass and somebody's edits.
        if unclaimed and not holds_uncommitted:
            return True, (
                f"`{verdict}` and GitHub has no PR for this branch -- past the "
                f"{DEFAULT_UNCLAIMED_AGE_DAYS:g}d mark, so there is no review left to "
                f"wait for; the commits and the remote branch are untouched"
            )
        if force:
            return True, (
                f"forced past `{verdict}` ({reason}) — the PR has not merged, so this "
                f"discards the checkout its review is still pointing at"
            )
        return False, (
            f"{verdict} — {reason}. A push is not a merge: `reconcile` reaps this box "
            f"once its PR lands, or at {DEFAULT_UNCLAIMED_AGE_DAYS:g}d if no PR is ever "
            f"opened for the branch, and until then the checkout is where review "
            f"comments get answered. Wait for it, or pass --force."
        )
    if reapable(
        verdict,
        pr_merged=pr_merged,
        pr_closed=pr_closed,
        holds_uncommitted=holds_uncommitted,
        work_is_landed=work_is_landed,
    ):
        return True, ""
    if force:
        return True, f"forced past `{verdict}` ({reason}) — uncommitted changes will be discarded"
    return False, (
        f"{verdict} — {reason}. The work is still only in this box: /ship it, or pass "
        f"--force to discard the uncommitted part (commits survive on the branch)."
    )


def branch_delete_flag(state: sweep.State, pr_merged: bool) -> str:
    """`-d` or `-D` for the box's branch — `-D` only when nothing can be lost by it.

    `-d` refuses a branch that is not an ancestor of the default branch, which is the
    correct default and also wrong for the three commonest ways a box legitimately ends:
    a squash-merged PR (the content is on the default branch but the commits are not
    ancestors of anything), a PR still open (pushed, not merged at all), and an unused
    box. In the first two every commit exists on the remote, so the local ref is a copy;
    in the third there are no commits at all. Anywhere else, `-d` is left to refuse —
    that refusal is the last guard between a cleanup command and someone's only copy.

    The third case is not hypothetical and is the commonest of them, because the guard
    hook cuts a box per (session, project) whether or not the session ends up writing
    anything. `-d` compares against the *source checkout's* HEAD, and the source is
    usually parked on some other branch, so a box branch sitting exactly on
    `origin/<default>` is "not fully merged" as far as `-d` is concerned. Every reap of
    an unused box therefore failed at its last step, exited non-zero, and left the branch
    behind — the tier accumulating `agent/ws-*` refs in every repo it touched.

    That last rule reads `state.ahead`, which is the same field `sweep.classify` turns
    into `spent`, rather than the verdict itself. The verdict is a summary and the state
    is the evidence: keyed on the verdict, a caller passing `spent` alongside a state
    carrying unpushed commits would get a `-D` for commits that exist nowhere else, and
    `test_no_reap_plan_ever_emits_a_capital_D_without_the_remote_having_it` sweeps
    exactly that combination. `is_git` gates it because `ahead` is 0 by default, so a
    state nothing could be read from must not be mistaken for an empty branch.
    """
    if pr_merged:
        return "-D"
    if state.upstream and state.unpushed == 0:
        return "-D"
    if state.is_git and state.ahead == 0:
        return "-D"
    return "-d"


def phantom_only_dirt(state: sweep.State) -> bool:
    """Whether `git worktree remove` will refuse a box every decision here calls clean.

    The two commands answer the same question with different semantics. `git worktree
    remove` re-asks it with **status** semantics; `sweep.real_changes` answered it with
    **diff** semantics. So a box whose only change is a tracked file rewritten
    byte-differently but equivalently -- detect-secrets regenerating `.secrets.baseline`
    is the one that happens daily -- is clean to `reapable`, to `reconcile_action` and to
    every refusal in `reap_decision`, and dirty to git.

    Such a box reaped in halves: the stack came down, the volumes went, and then the
    removal failed with `contains modified or untracked files`. The step is not
    retryable and the state is not self-correcting, so `reconcile` exited 1 on every
    scheduled pass thereafter and `reclaim.py` reported that as its own failure. Two
    carameli boxes did it for a day, every fifteen minutes, after `real_changes` fixed
    the *previous* half of this divergence -- boxes held forever on the same phantom.

    `--force` here discards exactly nothing, and that is what makes it narrow rather than
    a hammer: `real_changes` keeps every untracked path and every path git can show a
    diff for, so all that is left is content git has already called identical to HEAD.
    `state.dirty` must still be zero -- the same gate `reapable` passed to get here -- so
    one real edit alongside the phantom puts the box back under the ordinary refusal.
    """
    return not state.dirty and state.phantom_dirty > 0


def reap_plan(
    box: Box,
    workspace_root: Path,
    state: sweep.State,
    verdict: str,
    reason: str,
    pr_merged: bool = False,
    pr_closed: bool = False,
    force: bool = False,
    keep_stack: bool = False,
    has_stack: bool = False,
    awaiting_pr: bool = False,
    unclaimed: bool = False,
    copy_intact: bool | None = None,
    work_is_landed: bool = False,
) -> ReapPlan:
    """Everything `reap` will run, in the only order that is safe.

    The stack comes down first, while the box's compose file still exists to describe
    it; then the worktree; then the branch, which has to be deleted from the *source*
    checkout because the worktree that held it is gone by then.

    **This is the one place in the workspace that passes `-v` to `compose down`**, and
    the exception is narrow enough to state precisely: `docker-maint.py` must never do
    it because its target is a static checkout whose named volumes hold a real dev
    database costing hours to re-ingest. A box's volumes were created minutes ago by
    the box, are namespaced to `COMPOSE_PROJECT_NAME`, and leaking one per task is how
    the WSL2 VHDX becomes the next bottleneck. `-p <box>` is passed explicitly so the
    scope cannot silently widen to the source project if the box's `.env` is missing.

    **A forced reap plans no branch delete it knows `git` will refuse.** `--force`
    discards the worktree and never the commits (`reap_decision`), so on a box whose
    branch still carries commits no remote has, the only flag left is `-d` -- and `-d`
    refuses that branch by definition. Planning it anyway made the step fail *every
    time*: the worktree was already gone, so a reap that had done exactly what it was
    designed to do ended in `FAILED at git branch -d`, exit 1, and a git hint
    recommending the `-D` this design deliberately withholds. A caller reading that
    cannot tell a working `--force` from a broken one, and the honest reading of the
    exit code -- something went wrong, do it again -- is the one action that helps least.

    So the branch is kept deliberately and said so in the warning, which is where the
    rest of the forced-reap consequences are already reported. Discarding those commits
    stays possible and stays a separate, typed decision: the warning names the
    `git branch -D` that does it.

    **A husk plans no branch delete for the same reason, one step further back.** Its
    `state` was read through a missing `.git`, so `ahead`, `upstream` and `unpushed` are
    defaults rather than observations and `branch_delete_flag` can only return `-d` --
    which refuses the branch the dead box's work went to. That refusal lands in
    `apply_reap` after the tree is gone and before the lease is released, so the reap
    that finally cleared the husk would exit 1 and leak the port slot it was run to
    reclaim. The branch is named in the warning instead; it is a ref in the source
    checkout and outlives the box either way.

    **`--force` can appear on the removal step of a reap nobody forced**, and only for a
    box `git worktree remove` refuses while every decision above calls it clean. What
    that discards -- nothing -- and what it cost to find out is `phantom_only_dirt`.
    """
    path = str(box_path(workspace_root, box.name))
    allowed, note = reap_decision(
        verdict,
        reason,
        force,
        pr_merged=pr_merged,
        pr_closed=pr_closed,
        holds_uncommitted=bool(state.dirty),
        awaiting_pr=awaiting_pr,
        unclaimed=unclaimed,
        work_is_landed=work_is_landed,
    )
    if not allowed:
        return ReapPlan(box=box.name, path=path, project=box.project, refusal=note)

    if box.kind == PREVIEW_KIND and state.dirty and not force:
        # Reached only through `reapable`'s ended-PR arm: dirt alone still refuses.
        # `git worktree remove` refuses a dirty tree, so the step must carry `--force`
        # or every reap of an ended preview fails at its first step — and the discard
        # is said out loud, because it is the one thing this plan does that `--force`
        # normally gates.
        note = "; ".join(
            part
            for part in (
                note,
                f"{state.dirty} uncommitted change(s) in the preview are discarded -- "
                f"the same dirt a refresh already resets, in a copy whose review is over",
            )
            if part
        )
    remove: tuple[str, ...] = ("worktree", "remove", path)
    if force or (box.kind == PREVIEW_KIND and bool(state.dirty)) or phantom_only_dirt(state):
        remove = (*remove, "--force")
    steps: list[tuple[str, ...]] = [remove]
    warning = note
    if box.branch and not state.is_git:
        # A husk: `state` was read through a missing `.git`, so every field it could
        # decide the flag from is a default rather than an observation. `-d` is what
        # `branch_delete_flag` returns for it, and `-d` refuses a branch carrying
        # commits -- which is the ordinary case, since the branch is where the work of
        # the box that died went. That refusal lands in `apply_reap` *after* the tree
        # is gone and *before* the lease is released, so a reap that did the one thing
        # this box needed would exit 1 and leave the slot leaked exactly as before.
        warning = "; ".join(
            part
            for part in (
                note,
                f"{box.branch} is kept -- the box is a husk, so nothing here can say "
                f"whether that branch still holds work. To retire it once you have "
                f"looked: git -C {box.project} branch -d {box.branch}",
            )
            if part
        )
    elif box.branch:
        flag = (
            preview_branch_delete_flag(state, copy_intact=copy_intact)
            if box.kind == PREVIEW_KIND
            else branch_delete_flag(state, pr_merged)
        )
        if force and flag == "-d":
            warning = "; ".join(
                part
                for part in (
                    note,
                    f"{box.branch} is kept -- it carries commits no remote has, and "
                    f"--force never destroys commits. To discard those too: "
                    f"git -C {box.project} branch -D {box.branch}",
                )
                if part
            )
        elif box.kind == PREVIEW_KIND and flag == "-d":
            # The same rule as the forced arm above, one case further out: `-d` refuses
            # any branch that is not an ancestor of HEAD, which a `preview/...` branch
            # never is, so planning it ends a reap that did everything it should in
            # `FAILED at git branch -d` and exit 1. The branch is kept deliberately —
            # it carries commits the tracked ref cannot account for, and a cleanup
            # never destroys commits — and the warning names the command that does.
            warning = "; ".join(
                part
                for part in (
                    warning,
                    f"{box.branch} is kept -- it carries commits the ref it copied does "
                    f"not have, and a cleanup never destroys commits. To discard them: "
                    f"git -C {box.project} branch -D {box.branch}",
                )
                if part
            )
        else:
            steps.append(("branch", flag, box.branch))
    return ReapPlan(
        box=box.name,
        path=path,
        project=box.project,
        steps=tuple(steps),
        stack_down=has_stack and not keep_stack,
        slot=box.slot,
        warning=warning,
        branch=box.branch,
        verdict=verdict,
        reason=reason,
        forced=force,
        dirty=state.dirty,
        session=box.session,
    )


# --- reconcile: the pure decision -------------------------------------------


def rollup_conclusion(rollup: object) -> str:
    """One conclusion for a `statusCheckRollup` list; worst-present wins.

    `gh` returns a heterogeneous list: check runs carry `status`/`conclusion`, while
    legacy commit statuses carry `state`. Both spellings are read, because a repo with
    one of each would otherwise report green on the half this understood.

    Anything unrecognised counts as pending, never as success. The whole point of this
    function is gating an automatic merge, so an unparseable rollup must not open the
    gate -- and `CHECKS_NONE` (an empty rollup) is kept distinct from `SUCCESS` for the
    same reason: a repo whose gate failed to trigger has no checks at all, which is not
    the same as a gate that passed.
    """
    if not isinstance(rollup, list) or not rollup:
        return CHECKS_NONE
    worst = CHECKS_SUCCESS
    for entry in rollup:
        if not isinstance(entry, dict):
            return CHECKS_PENDING
        status = str(entry.get("status") or "")
        raw = str(entry.get("conclusion") or entry.get("state") or "")
        verdict = raw.upper()
        if status and status.upper() not in ("COMPLETED", ""):
            return CHECKS_PENDING  # still running -- nothing worse can be concluded yet
        if verdict in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            continue
        if verdict in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"):
            return CHECKS_FAILURE
        worst = CHECKS_PENDING
    return worst


def parse_pr_view(stdout: str) -> PullRequest:
    """A `PullRequest` from `gh pr view --json PR_VIEW_FIELDS`; empty when there is none.

    Every malformed shape degrades to "no PR", which is the safe direction in both of
    this function's uses: no PR means `reconcile` never merges and never reaps a box on
    the strength of a merge it cannot actually see.
    """
    try:
        payload = json.loads(stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return PullRequest()
    if not isinstance(payload, dict) or not payload.get("state"):
        return PullRequest()
    labels = payload.get("labels")
    names = (
        tuple(
            str(item.get("name", ""))
            for item in labels
            if isinstance(item, dict) and item.get("name")
        )
        if isinstance(labels, list)
        else ()
    )
    number = payload.get("number")
    return PullRequest(
        number=number if isinstance(number, int) else 0,
        url=str(payload.get("url") or ""),
        state=str(payload.get("state") or "").upper(),
        checks=rollup_conclusion(payload.get("statusCheckRollup")),
        labels=names,
        head=str(payload.get("headRefOid") or "").strip().lower(),
    )


def box_age_days(created: str, now: _dt.datetime | None = None) -> float:
    """How long a box has existed, from its lease's ISO timestamp; 0.0 when unreadable.

    Unreadable reads as *brand new*, which is the conservative direction: age only ever
    makes `reconcile` more willing to destroy something, so a timestamp nothing can
    parse must not be what licenses that.
    """
    if not created:
        return 0.0
    try:
        stamp = _dt.datetime.fromisoformat(created)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.UTC)
    now = now or _dt.datetime.now(_dt.UTC)
    return max(0.0, (now - stamp).total_seconds() / 86400.0)


def mergeable(pr: PullRequest, label: str = "") -> tuple[bool, str]:
    """`(may this PR be merged automatically, why not)`.

    Three conditions, and each rejection names itself: these repos have no branch
    protection (see `dependabot-automerge.yml`), so nothing server-side would stop a
    merge on a red gate -- this function *is* the gate, and a caller reading "not
    merged" with no reason cannot tell a red build from a missing label.
    """
    if not pr.is_open:
        return False, f"PR is {pr.state.lower() or 'absent'}, not open"
    if pr.checks != CHECKS_SUCCESS:
        detail = {
            CHECKS_FAILURE: "the gate is red",
            CHECKS_PENDING: "the gate is still running",
            CHECKS_NONE: "no checks have reported",
        }[pr.checks]
        return False, detail
    if label and label not in pr.labels:
        return False, f"not labelled `{label}`"
    return True, ""


def reconcile_action(
    verdict: str,
    reason: str,
    pr: PullRequest,
    *,
    automerge: bool = False,
    merge_label: str = "",
    pressure: bool = False,
    age_days: float = 0.0,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    holds_uncommitted: bool = True,
    newborn_grace_days: float = NEWBORN_GRACE_DAYS,
    unclaimed_age_days: float = DEFAULT_UNCLAIMED_AGE_DAYS,
    work_is_landed: bool = False,
) -> tuple[str, str]:
    """`(action, why)` for one box. Pure; every IO decision above it collapses to these.

    **The order of the first test is the whole safety property.** `HOLD` is checked
    before anything that destroys, so a box carrying work only it has is never reaped --
    not on a merged PR, not under disk pressure, not at any age. That matters because
    the two can legitimately coexist: ship a branch, keep working in the box, and the PR
    merges while uncommitted edits are still sitting there. Reaping on the strength of
    the merge alone would delete them.

    What that test asks is `reapable`, not `verdict in SAFE_TO_REAP`, and the difference
    is the squash-merged box: its commits are on the default branch under a rewritten
    sha, which no verdict can see, so it arrives here as `needs-rebranch` and used to
    HOLD forever. `holds_uncommitted` is the half of that predicate the safety property
    lives in, and it stays first: a merge never licenses destroying uncommitted edits.
    `reap_decision` asks the same predicate, because a disagreement between the two
    surfaces as this pass's "reap refused" warning and stalls the box either way.

    After that the cases are disjoint by construction:

    - **husk** -- the leased path is not a git checkout at all, so every case below it
      is about a checkout that no longer exists. It is tested first for that reason,
      and it is the only one that ignores the PR: a review has nowhere to be answered
      in a directory git stopped tracking, and what the box still holds is a port slot.
    - **merged** -- the work is on the default branch. Nothing is left to lose and the
      box is pure cost, so it goes regardless of age or pressure.
    - **closed** -- declined rather than landed, which changes where the content ended
      up and not whether the box is finished. The commits are on the remote, the PR can
      be reopened without this checkout, and nothing further will be reviewed in it. It
      used to fall through to the `needs-pr` case below and wait forever for a PR that
      already existed.
    - **open** -- every commit is on the remote, so reaping costs only the convenience
      of still having the checkout. That is worth paying for a while (`WAIT`), and not
      worth paying past `max_age_days` or under `pressure`. `automerge` is offered
      first, because a green PR that is about to be merged should be merged rather than
      have its box thrown away while it waits.
    - **no PR** -- a box that was cut and never used, which is the commonest kind: the
      guard hook cuts one per (session, project) whether or not that session writes
      anything. Nothing was ever at stake, so it goes immediately -- but not before it
      is old enough for "never used" to be a finding rather than a race. See
      `newborn_grace_days`.
    - **no PR but pushed** -- `needs-pr` with nothing on GitHub *at all*, which is the
      one case here where no person has ruled on the branch. Never merged, and not
      destroyed until `unclaimed_age_days`: whether the work is finished stays a
      person's decision, but *keeping the checkout* is not the same question, and
      answering the second one with the first held the box forever. See
      `DEFAULT_UNCLAIMED_AGE_DAYS` for what the reap costs (the checkout, and nothing
      else) and for the box it was measured on.

      It is the only reap here that turns on an **absence**, so it is the only one that
      needs `pr.absent` rather than `not pr.exists`: an unreachable `gh` produces the
      same empty `PullRequest` as a branch nobody opened a PR for, and reaping on that
      would destroy the checkout of an open, actively-reviewed PR the moment the
      network blinked. Every other arm reads a state GitHub affirmatively reported.
    """
    if not reapable(
        verdict,
        pr_merged=pr.merged,
        pr_closed=pr.is_closed,
        holds_uncommitted=holds_uncommitted,
        work_is_landed=work_is_landed,
    ):
        if verdict == PREVIEW_VERDICT:
            # Same hold, different remedy, and the report cannot tell them apart on its
            # own: it heads every HOLD row "only place it exists, ship it", which for a
            # preview is advice that cannot be taken. A preview sits on a `preview/...`
            # copy nobody will open a PR for, so an edit made in one ships nowhere and
            # the box is held until a person moves it. Left unsaid, the reader retries
            # `reap`, is refused, and the slot stays leased.
            return HOLD, (
                f"{reason} -- edits in a preview ship nowhere; copy anything worth "
                f"keeping out, then `reap --yes`"
            )
        return HOLD, f"{verdict} -- {reason}"

    if verdict == sweep.SKIPPED:
        # Every case below reads the PR to decide what the *checkout* is still worth,
        # and a husk has none: git stopped tracking the path, so there is nowhere left
        # to answer a review or finish a branch. Falling through said "the box was never
        # used", which is the one thing a husk is evidence against -- it is the remains
        # of a box that was used and whose removal died partway. What it holds now is a
        # port slot, and holding it is what took a whole night's vendoring run down.
        return REAP, f"{reason} -- a previous removal died partway and left the lease"

    if verdict == PREVIEW_VERDICT:
        # A preview is never merged and never shipped: it is a copy of a ref someone is
        # looking at. It ends when the thing it shows lands, when the disk is needed, or
        # when it has been sitting there long enough that nobody is still looking.
        if pr.merged or pr.state == "CLOSED":
            return REAP, f"preview of PR #{pr.number} ({pr.state.lower()})"
        if pressure:
            return REAP, "reclaiming disk -- a preview holds nothing of its own"
        if age_days > max_age_days:
            return REAP, f"preview is {age_days:.1f}d old (limit {max_age_days:g}d)"
        subject = f"PR #{pr.number}" if pr.number else "a branch"
        return WAIT, f"preview of {subject} -- reap it when you are done looking"

    if pr.merged:
        return REAP, f"PR #{pr.number} merged"

    if pr.is_closed:
        # Reached only for a verdict `reapable` already cleared, which for a task box
        # means the remote has every commit. So what a close settles is the *policy*
        # question this pass adds on top -- is anyone still going to use this checkout
        # -- and the answer a person recorded on GitHub is no.
        return REAP, f"PR #{pr.number} closed without merging"

    if pr.is_open:
        if automerge:
            allowed, why = mergeable(pr, merge_label)
            if allowed:
                return MERGE, f"PR #{pr.number} is green"
        else:
            why = "auto-merge is off"
        if pressure:
            return REAP, (
                f"reclaiming disk -- PR #{pr.number} is open and the remote has every "
                f"commit, so only the checkout is lost"
            )
        if age_days > max_age_days:
            return REAP, (
                f"PR #{pr.number} has been open {age_days:.1f}d (limit {max_age_days:g}d) "
                f"-- the remote has every commit, so only the checkout is lost"
            )
        return WAIT, f"PR #{pr.number} is open: {why}"

    if verdict == sweep.NEEDS_PR:
        if not pr.absent:
            # gh could not be asked (offline, unauthenticated, rate-limited), so the
            # empty PR says nothing about this branch. Wait, and say nothing about a
            # deadline that is not being counted.
            return WAIT, "branch is pushed but has no PR -- /ship it, or open one by hand"
        if age_days > unclaimed_age_days:
            return REAP, (
                f"pushed {age_days:.1f}d ago and GitHub has no PR for the branch "
                f"(limit {unclaimed_age_days:g}d) -- the remote has every commit and "
                f"keeps the branch, so only the checkout is lost (`resume` brings it back)"
            )
        return WAIT, (
            f"branch is pushed but has no PR -- /ship it, or open one by hand; the "
            f"checkout is reclaimed at {unclaimed_age_days:g}d ({age_days:.1f}d now)"
        )

    if work_is_landed:
        # `reapable` has already agreed, so this arm only stops the fall-through below
        # from reporting the reason wrongly. "The box was never used" is what a box with
        # no PR and no commits gets, and this one is its opposite: it was used, its work
        # landed under some other branch's PR, and the squash is why no verdict here can
        # see that. Saying so is the difference between a reader trusting this pass and
        # a reader going to look for the commits it just destroyed the checkout of.
        return REAP, (
            f"{verdict}, but its tree is already on the default branch -- the work "
            f"landed under another branch's PR, so the box holds nothing unshipped"
        )

    if age_days < newborn_grace_days:
        return WAIT, (
            f"{verdict} and no PR, but the box is {age_days * 24 * 60:.0f}m old -- too "
            f"young for that to mean anything but a box whose session has not written "
            f"to it yet"
        )
    return REAP, f"{verdict} and no PR -- the box was never used"


def reconcile_plan(
    rows: list[tuple[Box, str, str, PullRequest, int]],
    *,
    automerge: bool = False,
    merge_label: str = "",
    pressure: bool = False,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    unclaimed_age_days: float = DEFAULT_UNCLAIMED_AGE_DAYS,
    landed: Container[str] = (),
    now: _dt.datetime | None = None,
) -> list[Reconciliation]:
    """`reconcile_action` over every box, in name order. Pure -- the whole pass, testable.

    Takes the inspected rows rather than doing the inspecting so a full reconciliation,
    including the disk-pressure escalation and the merge gate, can be asserted without
    git, `gh`, docker, or a disk.

    The row carries the box's uncommitted-file count alongside its verdict because
    `reconcile_action` needs both to answer `reapable`, and the verdict is a summary that
    cannot be decompiled back into one: `needs-rebranch` is reported for a dirty box and
    for a clean squash-merged one alike, and those two want opposite decisions.

    `landed` is the box names `head_tree_landed` answered yes for, passed as a set beside
    the rows rather than folded into them. Every row is a tuple this function's callers
    and its tests build by hand, and widening it would rewrite each of those for a fact
    that is true of almost no box -- while `landed=()`, the default, is exactly the
    behaviour every existing caller already has.
    """
    return [
        Reconciliation(
            box=box.name,
            action=action,
            reason=why,
            verdict=verdict,
            pr=pr,
        )
        for box, verdict, reason, pr, dirty in sorted(rows, key=lambda row: row[0].name)
        for action, why in [
            reconcile_action(
                verdict,
                reason,
                pr,
                automerge=automerge,
                merge_label=merge_label,
                pressure=pressure,
                age_days=box_age_days(box.created, now),
                max_age_days=max_age_days,
                unclaimed_age_days=unclaimed_age_days,
                holds_uncommitted=bool(dirty),
                work_is_landed=box.name in landed,
            )
        ]
    ]


# --- IO ---------------------------------------------------------------------


def _compose_files() -> tuple[str, ...]:
    """The compose filenames, from `docker-maint.py` rather than a second copy.

    Loaded by path because the file is hyphenated, and through the shared loader
    because this repo has been bitten twice by hand-rolled ones (see
    `scripts/precommit/_loader.py`). A missing sibling degrades to a stated fallback,
    never to silently deciding a box has no stack — that would leak its containers.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit"))
        from _loader import load_by_path

        module = load_by_path("docker_maint", REPO_ROOT / "scripts" / "docker-maint.py")
        return tuple(module.COMPOSE_FILES)
    except (ImportError, OSError, AttributeError):
        print(
            "worktree: cannot read docker-maint.py's compose filenames; falling back to "
            "the built-in list. Stack teardown may miss a non-standard filename.",
            file=sys.stderr,
        )
        return ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def has_stack(path: Path) -> bool:
    """True when there is a compose stack in `path` to bring up and tear down."""
    return any((path / name).is_file() for name in _compose_files())


def free_gb(path: Path) -> float:
    """Free space on the volume holding `path`, in GB; -1.0 when it cannot be read.

    One syscall, which is what makes it affordable at session start and on every
    `reconcile` pass. Deliberately *not* a per-box size: measuring those means walking
    a `.venv` and a `node_modules` per box (tens of thousands of files each), and this
    number is the one the decision actually needs -- "is the machine short of disk" is
    a property of the volume, not of any one box.

    -1.0 rather than 0.0 for unreadable, so `under_pressure` can tell "no space left"
    from "cannot tell" and refuse to escalate on the second.
    """
    try:
        return shutil.disk_usage(path).free / 1_000_000_000
    except OSError:
        return -1.0


def under_pressure(free: float, floor: float) -> bool:
    """True when free space is at or under the floor and that is knowable.

    A negative `free` is `free_gb`'s "cannot tell", and must not escalate: pressure is
    what licenses destroying boxes whose PR is still open, so an unreadable volume has
    to fail toward keeping them.
    """
    return 0.0 <= free <= floor


def dir_size_bytes(path: Path) -> int:
    """Bytes under `path`, following no symlinks. Best-effort; unreadable entries are 0.

    Only ever called behind an explicit `--sizes`, because this is the expensive walk
    `free_gb` exists to avoid: a provisioned box is a `.venv` and often a
    `node_modules`, which is tens of thousands of files apiece.
    """
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda _: None):
        # `git worktree` never creates symlinked trees, but `node_modules` is full of
        # them; following one would double-count at best and loop at worst.
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            try:
                stat = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            total += stat.st_size
    return total


def pr_for(gh: sweep.Git, branch: str) -> PullRequest:
    """What GitHub says about `branch`'s PR. Empty on every failure path.

    Fails **closed** in the sense that matters: an empty `PullRequest` is neither merged
    nor open, so `reconcile_action` will not merge it and will not reap a box on the
    strength of a merge. An offline or unauthenticated `gh` therefore makes reconcile do
    less, never more -- the same asymmetry `sweep.has_merged_pr` documents.

    `--state all` because the interesting answers include MERGED and CLOSED; the default
    would hide exactly the state that licenses a reap.

    **`absent` is set from the message, because gh gives the two answers one exit code.**
    "no pull requests found for branch X" and "HTTP 401: Bad credentials" both exit 1
    with empty stdout, so the text is the only thing separating a branch GitHub has
    ruled on from a GitHub that could not be reached. Matching prose is brittle in
    exactly one direction, which is why it is done here rather than at the reader: a
    wording change costs a cleanup that does not happen, never a box destroyed on an
    answer nobody gave. Everything unrecognised stays the default, and the default is
    "not known to be absent".
    """
    if not branch:
        return PullRequest()
    try:
        result = gh("pr", "view", branch, "--json", PR_VIEW_FIELDS)
    except (OSError, subprocess.SubprocessError):
        return PullRequest()
    if result.returncode != 0:
        detail = f"{result.stderr or ''}{result.stdout or ''}".lower()
        return PullRequest(absent=GH_NO_PR_MESSAGE in detail)
    return parse_pr_view(result.stdout)


def merge_pr(gh: sweep.Git, number: int) -> tuple[bool, str]:
    """Squash-merge PR `number` and delete its remote branch. `(ok, message)`.

    Squash because a box is one task and its commits are an agent's working history,
    not a reviewed sequence worth preserving on the default branch. `--delete-branch`
    so the remote ref goes at the same moment, which is what makes the *next* pass see
    a merged PR and a reapable box rather than a stale branch nobody prunes.
    """
    try:
        result = gh("pr", "merge", str(number), "--squash", "--delete-branch")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"gh pr merge failed to run: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if pr_state(gh, number) == "MERGED":
            # The merge landed and something *after* it failed -- almost always the
            # local `--delete-branch` step, which cannot remove a ref another worktree
            # has checked out. Believing the exit code here would report a failure for
            # a merge that happened, skip the reap it licenses, and leave the box for
            # the next pass a quarter of an hour later.
            return True, (f"merged PR #{number} (squash); post-merge cleanup failed: {detail}")
        return False, detail
    return True, f"merged PR #{number} (squash, remote branch deleted)"


def pr_state(gh: sweep.Git, number: int) -> str:
    """GitHub's state for PR `number` -- `""` when it cannot be read.

    Deliberately not `pr_for`: this asks by number rather than by branch, and after a
    `--delete-branch` the branch is exactly the thing that may no longer resolve. The
    empty string is the "cannot tell" answer, so an offline or unauthenticated `gh`
    leaves `merge_pr` believing its exit code, which is the pre-existing behaviour.
    """
    try:
        result = gh("pr", "view", str(number), "--json", "state")
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    try:
        payload = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("state") or "")


def read_leases(workspace_root: Path) -> dict[str, Box]:
    path = lease_file(workspace_root)
    try:
        return parse_leases(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


@contextlib.contextmanager
def _dir_lock(path: Path, wait: float, stale: float):
    """The mkdir mutex both named locks below are made of. Yields whether it was had.

    A holder that died is broken after `stale` seconds. If the lock cannot be had
    within `wait` seconds the caller proceeds *unlocked*: this tier fails toward
    availability (see `parse_leases`), and an unlocked run is the status quo ante,
    not a new failure mode. Same for a filesystem that cannot create the lock
    directory at all. Callers that can do something better than proceed blindly read
    the yielded flag; `lease_lock`'s cannot, and ignore it.
    """
    acquired = False
    deadline = time.monotonic() + wait
    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.mkdir(path)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue  # released between mkdir and stat — retry at once
            if age > stale:
                with contextlib.suppress(OSError):
                    os.rmdir(path)
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        except OSError:
            break
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                os.rmdir(path)


@contextlib.contextmanager
def lease_lock(
    workspace_root: Path, wait: float = LEASE_LOCK_WAIT, stale: float = LEASE_LOCK_STALE
):
    """Hold the inter-process mutex around one lease read-modify-write."""
    with _dir_lock(boxes_root(workspace_root) / LEASE_LOCK_NAME, wait, stale):
        yield


def spawn_lock_name(key: str) -> str:
    """The lock directory for `key`, which is a (project, session) pair spelled out.

    Sanitised rather than hashed: this directory sits beside the boxes, and a human
    who finds one left behind by a killed hook should be able to read whose spawn it
    was without a decoder.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key).strip("._-") or "unkeyed"
    return f"{SPAWN_LOCK_PREFIX}{safe[:80]}.lock"


@contextlib.contextmanager
def spawn_lock(
    workspace_root: Path,
    key: str,
    wait: float = SPAWN_LOCK_WAIT,
    stale: float = SPAWN_LOCK_STALE,
):
    """Serialise the spawns racing for one `key`. Yields whether the lock was had.

    Held across `plan_*` + `apply_new` by the guard, so the second process to arrive
    finds the first one's box in the lease file instead of dying on the branch it
    created. A caller that did *not* get the lock has to assume a spawn it cannot see
    is in flight — see `worktree-guard.py`'s recovery, which is what the flag is for.
    """
    with _dir_lock(boxes_root(workspace_root) / spawn_lock_name(key), wait, stale) as held:
        yield held


def write_leases(workspace_root: Path, boxes: Mapping[str, Box]) -> None:
    """Replace the lease file atomically, so a reader never sees a torn write.

    A truncated read parses as *no boxes* (`parse_leases`), which re-offers every
    slot at once — `os.replace` makes that state unobservable.
    """
    path = lease_file(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
    tmp.write_text(render_leases(boxes), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def seed_env(source: Path, target: Path, env: Mapping[str, str]) -> None:
    """Write the box's `.env`: the source checkout's, plus the managed overrides."""
    try:
        existing = source.read_text(encoding="utf-8") if source.is_file() else ""
    except OSError:
        existing = ""
    try:
        target.write_text(render_env(existing, env), encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"worktree: could not write {target}: {exc}", file=sys.stderr)


def run_provision(
    path: Path, steps: tuple[ProvisionStep, ...], timeout: float = 900.0
) -> tuple[bool, list[str]]:
    """Run the install ladder in the box. `(ok, notes)`; stops at the first failure.

    Not fatal to the box. A box that exists but has no toolchain is still where the work
    belongs — the edit has somewhere to land and the branch is cut — so a failed install
    is reported and the box is kept. Deleting it would send the agent back to editing the
    static checkout, which is the outcome this whole tier exists to prevent.

    The timeout is generous because a cold `uv sync` on a large project is genuinely slow;
    the guard hook never reaches this path (see `apply_new`'s `provision` argument).
    """
    notes: list[str] = []
    for step in steps:
        try:
            if step.shell_command:
                # `[python] install_command` is a shell string by contract, authored in
                # the project's own .devkit.toml. Not agent input, and not user input.
                completed = subprocess.run(  # noqa: S602 - manifest-authored install command
                    step.shell_command,
                    shell=True,
                    cwd=str(path),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    creationflags=sweep.NO_WINDOW,
                )
            else:
                completed = subprocess.run(
                    list(step.argv),
                    cwd=str(path),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    creationflags=sweep.NO_WINDOW,
                )
        except subprocess.TimeoutExpired:
            notes.append(f"[warn] provision: {step.label} timed out after {timeout:g}s")
            return False, notes
        except OSError as exc:
            notes.append(f"[warn] provision: {step.label} could not run ({exc})")
            return False, notes
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            notes.append(f"[warn] provision: {step.label} failed: {detail[-1] if detail else ''}")
            return False, notes
        notes.append(f"provisioned: {step.label}")
    return True, notes


def prune_leases(recorded: Mapping[str, Box], live: Mapping[str, Box]) -> list[str]:
    """Lease names with no worktree left. Every write drops these.

    `live_boxes` filters them out of every *read*, so nothing acts on a stale lease — but
    the entry stayed in the file forever, and the file is what a human opens to ask why a
    slot is spoken for. Dropping them on write keeps the record and the truth converging
    instead of drifting apart one hand-removed worktree at a time.
    """
    return sorted(set(recorded) - set(live))


def _worktree_branch(box_dir: Path) -> str:
    """The branch a linked worktree is on, read without spawning git.

    A linked worktree's `.git` is a *file* naming its private gitdir, and that
    gitdir's `HEAD` names the branch. Anything else — a plain directory, a full
    clone, a detached HEAD — returns "" and is not a box. File reads only, because
    `live_boxes` runs inside a PreToolUse hook on every edit.
    """
    try:
        pointer = (box_dir / ".git").read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            return ""
        gitdir = Path(pointer[len("gitdir:") :].strip())
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    prefix = "ref: refs/heads/"
    return head[len(prefix) :] if head.startswith(prefix) else ""


def recovered_slot(env_text: str, registry: devkit_ports.Registry | None) -> int:
    """The port slot a box's seeded `.env` was rendered from, or -1.

    An adopted box (below) lost its lease before its slot could be recorded, but
    `seed_env` already wrote that slot's every port into the box's managed block —
    so the block identifies the slot. Recovering it puts the slot back into
    `next_lease_slot`'s union, without which the same ports would be leased twice.
    """
    if registry is None:
        return -1
    managed: dict[str, str] = {}
    keeping = False
    for line in env_text.splitlines():
        stripped = line.strip()
        if stripped == MANAGED_BEGIN:
            keeping = True
        elif stripped == MANAGED_END:
            keeping = False
        elif keeping and "=" in stripped:
            key, _, value = stripped.partition("=")
            managed[key] = value
    for slot in range(registry.max_slots):
        expected = registry.env_for_slot(slot)
        if expected and all(managed.get(key) == value for key, value in expected.items()):
            return slot
    return -1


def tracks_from_reflog(text: str) -> str:
    """The remote branch a `preview/...` branch was created from, off its reflog.

    `kind_of_branch` recovers the other half of a stripped preview lease, and the same
    argument applies here with a worse consequence: a copy of this file that predates
    `tracks` parses the lease into a Box without it and writes it straight back, so one
    `worktree-guard` spawn from an unupdated checkout empties the field for every box at
    once. `kind` survives that because the branch name carries it; `tracks` cannot, since
    `preview_branch` slugifies the topic and drops the `agent/` prefix that would say
    which ref this is a copy of.

    Git kept it anyway. `preview_spawn_plan` creates the branch from `origin/<ref>`, and
    that is what the creation entry of its reflog records. Reading it back is what lets
    `reconcile` ask GitHub about the branch under review instead of the throwaway
    `preview/...` ref — the difference between reaping a preview when its PR lands and
    letting it stand, with its stack up and its slot held, until `max_age_days`.
    """
    marker = "branch: Created from origin/"
    for line in text.splitlines():
        _, _, message = line.partition("\t")
        if message.startswith(marker):
            return message[len(marker) :].strip()
    return ""


def recovered_tracks(box_dir: Path, branch: str) -> str:
    """`tracks` for a preview whose lease lost it, or "" when git cannot say either.

    File reads only, for `_worktree_branch`'s reason: this runs under `live_boxes`, which
    a PreToolUse hook calls on every edit. A linked worktree's `.git` names its private
    gitdir, whose `commondir` points at the repository's — and **branch reflogs live
    there, not in the per-worktree gitdir**, which holds only `HEAD`'s.
    """
    try:
        pointer = (box_dir / ".git").read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            return ""
        gitdir = Path(pointer[len("gitdir:") :].strip())
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        repo_git = (gitdir / common) if common else gitdir
        reflog = (repo_git / "logs" / "refs" / "heads" / branch).read_text(encoding="utf-8")
    except OSError:
        return ""
    return tracks_from_reflog(reflog)


def orphaned_boxes(
    workspace_root: Path,
    recorded: Mapping[str, Box],
    registry: devkit_ports.Registry | None = None,
) -> dict[str, Box]:
    """Box worktrees the lease file has forgotten, rebuilt from what survives on disk.

    Two ways an entry goes missing while the worktree stays: the process died in
    `apply_new` between `git worktree add` and the lease write, or two unlocked
    writers raced and the loser's entry was overwritten (`lease_lock` closes that
    window for new spawns; this adopts what was already lost). Un-adopted, such a
    box is invisible to `list`, `reap --all` and `reconcile` — a checkout, branch
    and volume set nothing will ever clean, holding work nothing reports.

    The `session` cannot be rebuilt, so an adopted box is never re-found by
    `find_session_box`; the slot is recovered from the seeded `.env` where the
    registry can still identify it.
    """
    found: dict[str, Box] = {}
    try:
        entries = list(boxes_root(workspace_root).iterdir())
    except OSError:
        return found
    for entry in entries:
        name = entry.name
        if name in recorded or NAME_SEP not in name or not entry.is_dir():
            continue
        branch = _worktree_branch(entry)
        if not branch:
            continue
        try:
            env_text = (entry / ".env").read_text(encoding="utf-8")
        except OSError:
            env_text = ""
        try:
            created = _dt.datetime.fromtimestamp(entry.stat().st_mtime, _dt.UTC).isoformat(
                timespec="seconds"
            )
        except OSError:
            created = ""
        found[name] = Box(
            name=name,
            project=project_of(name),
            branch=branch,
            slot=recovered_slot(env_text, registry),
            session="",
            created=created,
        )
    return found


def live_boxes(workspace_root: Path) -> dict[str, Box]:
    """Leases whose worktree directory still exists, plus adopted orphans.

    The lease file is a record, not the truth: a `git worktree remove` run by hand
    leaves the entry behind, and a stale entry holds a port slot nobody is using. The
    directory is the truth, so it is what filters. The converse holds too — a
    worktree the file has forgotten is merged back in by `orphaned_boxes`, so every
    caller sees it and the next lease write persists it. The registry is loaded only
    once an orphan is found, keeping the steady-state cost of this read to one
    directory listing.

    A preview whose lease lost its `tracks` is repaired the same way and for the same
    reason — see `recovered_tracks`. Only a preview missing the field pays the two extra
    file reads, so the steady state is unchanged.
    """
    boxes = {
        name: box
        for name, box in read_leases(workspace_root).items()
        if box_path(workspace_root, name).is_dir()
    }
    for name, box in boxes.items():
        if box.kind == PREVIEW_KIND and not box.tracks:
            found = recovered_tracks(box_path(workspace_root, name), box.branch)
            if found:
                boxes[name] = replace(box, tracks=found)
    if orphaned_boxes(workspace_root, boxes):
        boxes.update(orphaned_boxes(workspace_root, boxes, load_registry(workspace_root)))
    return boxes


def load_registry(root: Path) -> devkit_ports.Registry | None:
    """The port registry, or None when there is none to read.

    **`ports.toml` lives in devkit's repo root, not the workspace root**, which is where
    `new-project.py` allocates from (`devkit_ports.load(DEVKIT_ROOT)`) and where this
    function used to fail to look. The consequence was invisible in exactly the way this
    tier is built to avoid: every call returned None, so every box got `slot -1`, and the
    port-lease half — `next_lease_slot`'s union across both tiers, the `*_HOST_PORT`
    variables in the seeded `.env`, "lease released (slot N)" — was dead code that no
    output ever contradicted. A box with a stack simply published on its source
    checkout's ports and collided with it.

    The workspace root is still consulted, second, for a workspace that keeps its own
    registry beside the checkouts rather than inside devkit.

    None is a real answer, not a failure: a workspace of stackless repos needs no
    registry, and a box in one still gets a `COMPOSE_PROJECT_NAME`.
    """
    for candidate in (REPO_ROOT, root):
        if (candidate / devkit_ports.REGISTRY_NAME).is_file():
            return devkit_ports.load(candidate)
    return None


def known_projects(workspace: Path) -> list[str]:
    """Registered checkouts, from the same parser `sweep` and the dispatcher use."""
    try:
        return devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorktreeError(f"cannot read the workspace registry at {workspace}: {exc}") from exc


def run_steps(
    cwd: Path, steps: tuple[tuple[str, ...], ...], timeout: float = 300.0
) -> tuple[list[str], str, str]:
    """Run git argv in `cwd`, stopping at the first failure. `(ran, failed, error)`.

    Bounded because `new` is reachable from a PreToolUse hook (`worktree-guard.py`),
    where an unbounded `git fetch` against an unreachable remote does not fail — it
    hangs the agent's tool call. A timeout is reported as an ordinary step failure, so
    the fetch-is-optional path in `apply_new` handles it like any other.
    """
    ran: list[str] = []
    for step in steps:
        rendered = "git " + " ".join(step)
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *step],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=sweep.NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return ran, rendered, f"timed out after {timeout:g}s"
        except OSError as exc:
            return ran, rendered, str(exc)
        if completed.returncode != 0:
            return ran, rendered, (completed.stderr or completed.stdout or "").strip()
        ran.append(rendered)
    return ran, "", ""


def should_seed_env(stack: bool, env_tracked: bool) -> bool:
    """Whether `new` may write the box's `.env`.

    Not when the project **tracks** its `.env`. Seeding rewrites the file, so a box
    would be dirty from the moment it was cut: it could never classify as `spent`,
    `reap` would refuse it forever, and a `/ship` from inside it would commit devkit's
    managed block as if it were the task's work. A box that is born unreapable
    defeats the one guarantee this tier has over `sweep.py`.

    Almost every project gitignores `.env` — carameli and ibkr_trader both do — so
    this is the rare path, and it is a stated skip rather than a silent one.
    """
    return stack and not env_tracked


def is_tracked(repo: Path, relative: str) -> bool:
    """True when git has `relative` under version control in `repo`.

    Unknown reads as tracked: the conservative direction, since the cost of a wrong
    "untracked" is a box that can never be reaped, and the cost of a wrong "tracked"
    is a `.env` the operator has to write once.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", relative],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            creationflags=sweep.NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return completed.returncode == 0


# What a Docker CLI says when the engine is not there to talk to. Windows names the
# missing named pipe, Linux and macOS the missing socket; both spellings appear as the
# tail of a much longer connect error, so this is a substring test rather than a match.
DAEMON_DOWN_SIGNS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "the docker daemon is not running",
    "open //./pipe/",
)

DAEMON_DOWN_NOTE = (
    "Docker's engine is not running, so nothing could be built or started. "
    "Start Docker Desktop and wait for it to say `Engine running`, then run this again. "
    "If Docker Desktop is already open, its engine has died behind the UI and only a "
    "full restart of it brings the engine back — the window looking healthy is not "
    "evidence that it is."
)


def daemon_down_note(text: str) -> str:
    """`DAEMON_DOWN_NOTE` when `text` is a Docker CLI failing to reach the engine, else "".

    The distinction is worth a function because the two failures are indistinguishable
    to the reviewer and their remedies share nothing. Every compose call in this file
    reports a non-zero exit as "the stack did not come up", which is true of a build
    error, a port collision *and* an engine that is not running — and on 2026-08-24 the
    engine had died behind a Docker Desktop window that still looked healthy, so the
    task's report sent the reader to the branch, the compose file and the port registry
    before anyone thought to ask whether Docker was up at all.

    Substring rather than exact, and lower-cased, because the sign is the tail of a
    connect error whose leading half carries an API version and a URL-encoded path that
    change between releases.
    """
    haystack = (text or "").lower()
    return DAEMON_DOWN_NOTE if any(sign in haystack for sign in DAEMON_DOWN_SIGNS) else ""


def build_env() -> dict[str, str]:
    """The environment a box's build runs in: this one, plus bake off where that works.

    **Compose v5 removed the opt-out**, so this is necessary-but-not-sufficient and
    `build_targets` is the half that actually holds. Kept because it is still the whole
    fix on Compose v2, which consuming projects and CI runners are still on, and it
    costs nothing where it is ignored. See `build_targets` for the evidence that v5
    ignores it and for what replaces it.

    Recent Compose delegates building to **`docker buildx bake`**, and bake rejects a
    plan in which two targets export the same tag:

        target app: failed to solve: image "docker.io/library/carameli-app-…": already
        exists

    An `app` and a `worker` built from one Dockerfile and sharing one `image:` are
    exactly that plan, and it is a legal, common compose file -- the classic builder
    exports the two sequentially and is fine with it. So this is a regression in the
    build path rather than a defect in the stack, and it is total: it fires before any
    container starts, so **every** preview of such a project failed, reported through
    `apply_preview` as the generic `the stack did not come up`. Measured on carameli,
    2026-08-24, against engine 29.2.0; the identical `compose build` with
    `COMPOSE_BAKE=0` succeeded.

    Turning bake off here rather than in each consumer is the narrow fix: the alternative
    asks every project with a shared-tag service to restructure a compose file that was
    never wrong, and misses the next one. What it costs is bake's parallel build, which
    a preview does once per box.

    **Inherited, never replaced.** A bare `{"COMPOSE_BAKE": "0"}` would drop `PATH`, and
    `compose_up` would report `docker is not on PATH` for every box on the machine.
    """
    return {**os.environ, "COMPOSE_BAKE": "0"}


def build_targets(
    config: Mapping[str, object] | None, services: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """One buildable service per distinct `image:` tag — the set bake can export at once.

    Compose v5 builds through `docker buildx bake` and, unlike v2, offers no way back:
    `compose build --print` emits a bake file, and `--dry-run` with `COMPOSE_BAKE=0`
    still shows every target naming its image. Two targets that export the **same** tag
    then race, and the loser dies:

        target app: failed to solve: image "docker.io/library/carameli-app-…": already
        exists

    An `app` and a `worker` built from one Dockerfile behind one `image:` are exactly
    that, and it is a legal, common compose file — the classic builder exported the two
    sequentially and never minded. Reproduced on carameli against Compose v5.0.2 /
    engine 29.2.0 on 2026-08-24, from a cleared image state, with `COMPOSE_BAKE=0` set:
    it fails every time, before any container starts, so **every** preview of such a
    project failed with the generic `the stack did not come up`.

    Naming one service per tag is what makes the plan legal again while keeping bake's
    parallelism for the tags that genuinely differ — the alternative, building every
    service in its own invocation, serialises builds that were never in conflict. The
    services left out are not skipped: they share a tag with one that is named, so the
    build they wanted has already happened, and the `up` that follows finds the image.

    Two services sharing a tag but declaring *different* builds is the one case this
    picks a winner for. It is already ambiguous — compose's own answer was whichever
    target won the export race — and a stack that means it should give them two tags.

    `services` non-empty (a UI-only box) narrows the set to what that box will start.
    A `None` config (docker could not be asked) returns empty, and `compose_up` falls
    back to the single `up --build` that was there before.
    """
    raw = (config or {}).get("services")
    seen: set[object] = set()
    targets: list[str] = []
    for name, svc in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(svc, dict) or "build" not in svc:
            continue
        if services and name not in services:
            continue
        # `compose config` resolves the default `<project>-<service>` tag, so `image` is
        # normally there; the fallback keys on the service instead, which is unique by
        # construction and so can never make two of them look like one.
        tag = svc.get("image") or ("\0unnamed", name)
        if tag in seen:
            continue
        seen.add(tag)
        targets.append(name)
    return tuple(targets)


def box_image_tags(config: Mapping[str, object] | None, project_name: str) -> tuple[str, ...]:
    """The images a box *built for itself*, which `reap` has to delete by name.

    `compose down -v` removes containers, the network and the volumes, and does not
    touch images -- `docker` has no per-project image verb, because an image is not
    owned by the project that built it. So every reaped box left its build behind, and
    nothing downstream collected them: `docker system prune -af` is the only thing that
    would, and the scheduled prune is `--idle-only`, which skips whenever the engine is
    down (`docker-maint.prune_verdict`). Measured on this workstation 2026-08-26, after
    the leak had been running for a week: 17 orphan images from 9 destroyed boxes,
    7.4 GB, against a machine reporting 22 GB free at session start.

    **The gate is that the tag carries the box's own project name**, and it is the whole
    safety argument rather than a tidiness rule. A project is free to pin a fixed
    `image:` on a built service, and that tag is then shared by every box on the machine
    -- deleting it would force a rebuild in each of them, which is the same leak
    inverted and far more annoying. A tag containing `COMPOSE_PROJECT_NAME` cannot be
    shared, because that name is unique to one box by construction. Nothing is guessed
    about *how* a project spells its tags: compose resolves them and this reads the
    answer, which is what keeps a vendored file free of one repo's naming.

    Services with no `build` are skipped for the same reason from the other end: their
    image came from a registry, is shared with every other stack on the machine, and was
    never this box's to delete.
    """
    raw = (config or {}).get("services")
    tags: list[str] = []
    for _name, svc in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(svc, dict) or "build" not in svc:
            continue
        tag = svc.get("image")
        if not isinstance(tag, str) or project_name not in tag:
            continue
        if tag not in tags:
            tags.append(tag)
    return tuple(tags)


def remove_images(tags: Sequence[str]) -> tuple[bool, str]:
    """`docker image rm` the named tags. `(ok, message)`; never fatal.

    A reap that cleaned everything else must not report failure because an image was
    already gone or the daemon went away mid-teardown -- the box is destroyed either
    way, and the caller's exit code is about the *stack*, per `apply_reap`. So the
    message says what happened and the boolean only gates the wording.
    """
    if not tags:
        return True, ""
    try:
        completed = subprocess.run(
            ["docker", "image", "rm", *tags],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            creationflags=sweep.NO_WINDOW,
        )
    except FileNotFoundError:
        return False, "docker is not on PATH — the box's images were left behind"
    except subprocess.TimeoutExpired:
        return False, "docker image rm timed out after 120s — images may survive"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return False, f"could not remove {len(tags)} box image(s): {detail}"
    return True, f"removed {len(tags)} image(s) built by the box: {', '.join(tags)}"


def compose_config(
    path: Path, project_name: str, timeout: float = 120.0
) -> Mapping[str, object] | None:
    """The fully resolved compose file as a dict, or `None` when docker cannot say.

    Every failure — no docker, a compose file that does not parse, output that is not a
    JSON object — collapses to `None` on purpose. This is a *build planning* aid, and a
    box that cannot be planned for must still be brought up the old way rather than
    failing here; `compose_up` is where a real error gets reported, with the message
    docker actually gave.
    """
    try:
        completed = subprocess.run(
            ["docker", "compose", "-p", project_name, "config", "--format", "json"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=sweep.NO_WINDOW,
            env=build_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def compose_tail(path: Path, project_name: str, timeout: float = 20.0) -> str:
    """The latest line from each of a box's containers, or `""` when docker cannot say.

    A progress fingerprint for a caller waiting on a URL. A cold box spends minutes in
    `npm install` with nothing listening, and a wait that watches only the port cannot
    tell that from a container wedged on a build -- so it prints the same line for both
    and, worse, gives up on the one that was working. The log is what separates them: an
    installing box keeps emitting, a stuck one does not.

    Every service and not one named service, because which container is the slow one is
    a fact about the project. **Sorted**, which is the part that had to be measured:
    `docker compose logs --tail 1` streams the containers concurrently, so two calls a
    second apart against an idle two-service stack came back in opposite orders (probed
    on engine 29.2.0, 2026-08-24). Returned in docker's order, a caller comparing this
    value to the last one would read that churn as progress and never time out at all.
    Sorting costs the ability to say which line is newest -- nothing here needed that --
    and buys a value that changes only when some container has actually said something.
    Runs of whitespace collapse for the same reason and it is the same measurement:
    docker pads the container-name column to the widest name it has attached to so far,
    and gets there by a race, so `beta-1  |` and `beta-1   |` came back from consecutive
    calls to an idle stack. Sorted but unsquashed, that is still churn reading as news.

    Blank lines are dropped and each line is truncated: it is printed on a progress tick,
    and one container logging a minified bundle would wrap the report into unreadability
    every fifteen seconds. What is returned is the whole set, newline-joined, because the
    caller needs both halves -- the set to compare, and a line from it to show.

    `""` on every failure, for the reason `compose_config` gives: something whose whole
    job is to make a wait more informative must never be the thing that ends it.
    """
    try:
        completed = subprocess.run(
            ["docker", "compose", "-p", project_name, "logs", "--tail", "1"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=sweep.NO_WINDOW,
            env=build_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    lines = sorted(
        " ".join(line.split())[:120] for line in completed.stdout.splitlines() if line.strip()
    )
    return "\n".join(lines)


def compose_up(
    path: Path,
    project_name: str,
    timeout: float = 1800.0,
    services: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """`compose up -d --build` scoped to one box. `(ok, message)`.

    `-p` for the same reason `compose_down` passes it, and here the consequence of
    omitting it is worse than a missed teardown: compose would fall back to the
    directory name, and a box whose `.env` seeding was skipped would start a *second*
    copy of the source checkout's stack on the ports that checkout already holds.

    `services` non-empty (a UI-only box) names what to start, with `--no-deps` so a
    frontend whose compose file declares `depends_on: app` does not drag the whole
    backend up behind it — the backend a UI-only box uses is the source checkout's,
    and its own `.env` holds that checkout's ports, so a dependency-started copy here
    would bind (or fail to bind) the donor's ports, not its own.

    Half an hour, because the first `up` in a fresh box builds the project's images from
    nothing. A timeout is reported rather than retried — `up` is idempotent, so running
    it again resumes the build instead of repeating it.

    **Two commands, not one, when the stack shares an image tag between services.**
    `build_targets` has the reason; the effect here is that the build is asked for by
    name and the `up` that follows carries no `--build`, so bake is never handed the
    duplicate-export plan it refuses. Each command gets the full `timeout`: the build is
    the half that can take half an hour, and an `up` that has nothing left to build
    cannot meaningfully spend it. Where the config cannot be read the old single
    `up --build` runs unchanged, which is also the path for every stack that has no
    duplicate to plan around.
    """
    scope = ["--no-deps", *services] if services else []
    targets = build_targets(compose_config(path, project_name), services)
    plan = (
        [
            ["build", *targets],
            ["up", "-d", *scope],
        ]
        if targets
        else [["up", "-d", "--build", *scope]]
    )
    for step in plan:
        try:
            completed = subprocess.run(
                ["docker", "compose", "-p", project_name, *step],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=sweep.NO_WINDOW,
                env=build_env(),
            )
        except FileNotFoundError:
            return False, "docker is not on PATH — the stack was not started"
        except subprocess.TimeoutExpired:
            return False, (
                f"compose {step[0]} timed out after {timeout:g}s — the build may still "
                f"be running; `docker compose -p {project_name} ps` says where it got to"
            )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            note = daemon_down_note(detail)
            return False, (f"{note}\n  ({detail})" if note else detail)
    return True, f"stack {project_name} is up"


def pr_head_branch(gh: sweep.Git, number: int) -> str:
    """The head branch of PR `number`, or "" when `gh` cannot say.

    Through the same shim `pr_for` uses, and empty on every failure path for the same
    reason: an offline or unauthenticated machine must produce "cannot resolve that PR",
    never a branch name it guessed.
    """
    try:
        result = gh("pr", "view", str(number), "--json", "headRefName")
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    try:
        payload = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    return str(payload.get("headRefName", "")) if isinstance(payload, dict) else ""


def compose_down(path: Path, project_name: str) -> tuple[bool, str]:
    """`compose down -v` scoped to one box. `(ok, message)`; a missing docker is not fatal.

    The `-v` is the box-only exception documented on `reap_plan`. `-p` is passed so the
    scope is the box's own project name and cannot fall back to the directory name or
    to a seeded `COMPOSE_PROJECT_NAME` from the source checkout's `.env`.
    """
    try:
        completed = subprocess.run(
            ["docker", "compose", "-p", project_name, "down", "-v", "--remove-orphans"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            creationflags=sweep.NO_WINDOW,
        )
    except FileNotFoundError:
        return False, "docker is not on PATH — the stack was left running"
    except subprocess.TimeoutExpired:
        return False, "compose down timed out after 300s — the stack may still be running"
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout or "").strip()
    return True, f"stack {project_name} torn down (containers, network, volumes)"


# --- modes ------------------------------------------------------------------


def retired_branch_probe(source: Path) -> Callable[[str], bool] | None:
    """ "Has a PR from this branch already merged?", asked of `source`'s own remote.

    The same question `git_policy` asks in the pre-commit hook, and deliberately the same
    code: a second implementation would be a second answer, and the whole point is that
    `spawn_plan` refuses a name for exactly the reason the commit later would.

    None when the remote is not a GitHub repo -- there is no ledger to consult, so a name
    can only be judged by `existing_branches`, which is today's behaviour and correct
    there.
    """

    def runner(argv, **kwargs):
        kwargs.setdefault("cwd", source)
        return git_policy.run_command(argv, **kwargs)

    remote = runner(["git", "remote", "get-url", git_policy.DEFAULT_REMOTE])
    repo = git_policy.github_repo(remote.stdout if remote.returncode == 0 else "")
    if not repo:
        return None
    # An *error* is not a merge. `merged_pr` already falls back from GraphQL to REST
    # before reporting one, and the branch policy asks again at commit time, so treating
    # an outage as "name is free" costs at worst the collision this exists to prevent --
    # while treating it as "name is taken" would rename every box during one.
    return lambda branch: bool(git_policy.merged_pr(runner, repo, branch).url)


def plan_new(
    project: str,
    workspace: Path,
    slug: str,
    session: str = "",
    fetch: bool = True,
    quiet: bool = False,
    branch_prefix: str = tb.BRANCH_PREFIX,
) -> SpawnPlan:
    """Resolve everything `new` needs from disk, then hand off to the pure planner."""
    root = workspace.parent
    projects = known_projects(workspace)
    source = devkit_project.resolve_project(project, projects, root)

    git = sweep.git_for(source)
    default_branch = tb.detect_default_branch(git, fallback="")
    if not default_branch:
        raise WorktreeError(
            f"cannot resolve origin/HEAD in {project} — there is no base branch to cut from"
        )
    existing = set(
        sweep._out(git("for-each-ref", "--format=%(refname:short)", "refs/heads/")).splitlines()
    )
    boxes = live_boxes(root)
    registry = load_registry(root) if has_stack(source) else None
    return spawn_plan(
        retired=retired_branch_probe(source) if fetch else None,
        project=project,
        workspace_root=root,
        slug=slug,
        default_branch=default_branch,
        existing_branches=existing,
        boxes=boxes,
        registry=registry,
        session=session,
        fetch=fetch,
        provision=plan_provision(source, quiet=quiet),
        env_templates=plan_env_templates(source),
        branch_prefix=branch_prefix,
    )


def origin_has_branch(git: sweep.Git, branch: str, network: bool = True) -> bool:
    """Whether `origin` carries `branch`. Never raises.

    `ls-remote` when the network is allowed, because it is the only authoritative
    answer and the whole question `resume` has to get right: `refs/remotes/origin/` is
    stale in exactly the direction that would hurt, holding a branch whose PR merged
    and whose remote `reconcile` then deleted. Resuming that is resuming finished work.

    The local refs are the fallback, taken on `--no-fetch` and on any `ls-remote`
    failure. An offline machine must read as "cannot confirm, so use what is on disk",
    never as "the branch is gone" -- the second turns a flat tyre into a refusal to
    resume work that is sitting on the remote perfectly intact.
    """
    if network:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            result = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
            if result.returncode == 0:
                return bool((result.stdout or "").strip())
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        result = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
        return result.returncode == 0
    return False


def plan_resume(
    project: str,
    workspace: Path,
    branch: str = "",
    pr: int = 0,
    session: str = "",
    fetch: bool = True,
    quiet: bool = False,
) -> SpawnPlan:
    """Resolve everything `resume` needs from disk and, for `--pr`, from GitHub."""
    root = workspace.parent
    projects = known_projects(workspace)
    source = devkit_project.resolve_project(project, projects, root)
    project = source.name

    git = sweep.git_for(source)
    if pr and not branch:
        branch = pr_head_branch(sweep.gh_for(source), pr)
        if not branch:
            raise WorktreeError(
                f"cannot read the head branch of PR #{pr} in {project} — is `gh` "
                f"authenticated for that remote? `--branch <name>` skips the lookup."
            )
    if not branch and session:
        branch = resumable_branch(root, project, session)
        if not branch:
            raise WorktreeError(
                f"no reaped box for session {session} in {project} whose branch is still "
                f"open on origin. Name one with --branch, or cut a fresh box: "
                f"worktree.py new {project}"
            )

    existing = set(
        sweep._out(git("for-each-ref", "--format=%(refname:short)", "refs/heads/")).splitlines()
    )
    remote = {branch} if branch and origin_has_branch(git, branch, network=fetch) else set()
    return resume_plan(
        project=project,
        workspace_root=root,
        branch=branch,
        remote_branches=remote,
        existing_branches=existing,
        boxes=live_boxes(root),
        registry=load_registry(root) if has_stack(source) else None,
        session=session,
        fetch=fetch,
        provision=plan_provision(source, quiet=quiet),
        env_templates=plan_env_templates(source),
    )


def plan_respawn(
    project: str,
    workspace: Path,
    slug: str,
    session: str = "",
    fetch: bool = True,
    quiet: bool = False,
) -> SpawnPlan:
    """A box for `(session, project)`: back onto its reaped branch, or a fresh one.

    What `worktree-guard.py` calls instead of `plan_new`, and the reason the recovery
    does not depend on anyone remembering it. A reap under disk pressure destroys a box
    whose PR is still open, on the stated grounds that only the checkout is lost; the
    next edit from that session then arrives at the guard with no box, and until this
    existed the guard did the only thing it could and cut a *new* branch. The work was
    never lost, but the session's next commit went somewhere else -- a second branch and
    a second PR for one task, with nothing in either to say so.

    An instruction file cannot close that: the agent has no way to know its box was
    destroyed, and by the time the divergence is visible it is two PRs old. So the
    decision belongs where the box is made.

    Falls back to `plan_new` on anything unexpected. Cutting a fresh box is the old
    behaviour and is always safe; a guard that raised here would block the edit outright
    over a recovery that is a convenience.
    """
    with contextlib.suppress(Exception):
        branch = resumable_branch(workspace.parent, project, session)
        if branch:
            return plan_resume(
                project, workspace, branch=branch, session=session, fetch=fetch, quiet=quiet
            )
    return plan_new(project, workspace, slug=slug, session=session, fetch=fetch, quiet=quiet)


def apply_new(
    plan: SpawnPlan, workspace: Path, timeout: float = 300.0, provision: bool = True
) -> tuple[bool, list[str]]:
    """Create the box. `(ok, notes)`; nothing is recorded unless the worktree exists.

    `timeout` is per git step. The guard hook lowers it, because there it is an
    agent's tool call that is waiting.

    `provision=False` is that same call's other concession. A cold `uv sync` is minutes,
    and a PreToolUse hook that takes minutes is one the agent experiences as a hang and
    the harness eventually kills — leaving a half-installed box and no message. So the
    guard cuts the box and *names* the provision command instead of running it, and this
    stays the default everywhere a human is the one waiting.
    """
    root = workspace.parent
    source = root / plan.box.project
    notes: list[str] = []
    boxes_root(root).mkdir(parents=True, exist_ok=True)

    _, failed, error = run_steps(source, plan.steps, timeout=timeout)
    if failed:
        # A failed `fetch` is a stale base, not a failure: the worktree still gets cut
        # from whatever `origin/<default>` says locally, which is what an offline
        # machine has. Anything else leaves nothing behind to clean up, because the
        # lease is only written after the worktree exists.
        if failed.startswith("git fetch"):
            notes.append(
                f"fetch failed ({error.splitlines()[0] if error else 'no detail'}) — "
                f"the box is cut from a possibly stale origin/<default>"
            )
            _, failed, error = run_steps(source, plan.steps[1:], timeout=timeout)
        if failed:
            notes.append(f"FAILED at `{failed}`: {error}")
            return False, notes

    path = Path(plan.path)

    # The lease is written as soon as the worktree exists — before seeding and
    # provisioning — so the window in which a killed process leaves a worktree the
    # file has never heard of is as narrow as it can be. The slot is re-checked
    # under the lock because it was chosen at *plan* time, and the fetch and
    # worktree-add between plan and here are seconds in which a concurrent spawn
    # can record the same slot first.
    box = plan.box
    env = plan.env
    with lease_lock(root):
        recorded = read_leases(root)
        boxes = live_boxes(root)
        dropped = prune_leases(recorded, boxes)
        if box.slot >= 0 and any(b.slot == box.slot for b in boxes.values()):
            registry = load_registry(root)
            try:
                slot = next_lease_slot(registry, boxes) if registry is not None else -1
            except devkit_ports.RegistryError as exc:
                slot = box.slot
                notes.append(f"[warn] slot {box.slot} is now shared with another box: {exc}")
            if slot != box.slot:
                notes.append(
                    f"slot {box.slot} was taken while the box was being cut — re-leased slot {slot}"
                )
                box = replace(box, slot=slot)
                env = managed_env(
                    box.name,
                    registry,
                    slot,
                    plan.env_templates,
                    donor_slot=plan.donor_slot,
                    own_services=box.services,
                    ui_templates=plan.ui_env_templates,
                )
        boxes[box.name] = box
        write_leases(root, boxes)
    if dropped:
        notes.append(f"released {len(dropped)} stale lease(s): {', '.join(dropped)}")

    stack = has_stack(path)
    if plan.slotless:
        # The box exists and is writable; only its stack is unavailable. Said here rather
        # than raised at plan time, because an agent routed into a box wanted somewhere
        # to edit, not a container. See `lease_slot`.
        notes.append(
            f"[warn] no port slot was free, so this box has no ports of its own and "
            f"`compose up` here would bind the {box.project} checkout's — it is refused "
            f"until one is. {plan.slotless} Then re-cut the box, or "
            f"`worktree.py reap <box> --yes` one that is done and let the next spawn "
            f"take its slot."
        )
    if should_seed_env(stack, is_tracked(path, ".env")):
        seed_env(source / ".env", path / ".env", env)
        notes.append(f"seeded {path.name}/.env (COMPOSE_PROJECT_NAME={box.name})")
    elif stack:
        notes.append(
            f"[warn] .env is tracked in {box.project}, so it was left alone — this "
            f"box shares the source checkout's COMPOSE_PROJECT_NAME and ports. Export "
            f"{' '.join(f'{k}={v}' for k, v in sorted(env.items()))} when running "
            f"compose here, or gitignore .env so future boxes can be seeded."
        )

    if provision and plan.provision:
        _, provision_notes = run_provision(path, plan.provision)
        notes.extend(provision_notes)
    elif plan.provision:
        notes.append(
            f"not provisioned - run `python {Path(__file__).resolve()} provision "
            f"{box.name} --yes` before running its tests or /ship"
        )
    return True, notes


def serve_preview(
    box: Box,
    workspace_root: Path,
    *,
    up: bool = True,
    down: bool = False,
    force: bool = False,
    fetch: bool = True,
) -> PreviewPlan:
    """The plan for a box that already exists: refresh it if it may be, then run it.

    A box whose lease outlived its checkout — a removal that died partway leaves the
    directory without its `.git` link — is refused rather than served. Serving one used
    to succeed vacuously: no compose file means `up` resolves to False, nothing starts,
    and the registry still publishes the slot's URLs, so the caller waited out its whole
    ready timeout on ports nothing had been asked to answer.
    """
    path = box_path(workspace_root, box.name)
    state = sweep.inspect(box.name, path, fetch=False)
    if not state.is_git:
        subject = box.tracks or box.branch
        return PreviewPlan(
            box=box,
            path=str(path),
            refusal=(
                f"{box.name} is leased but {path} is not a git checkout -- a removal "
                f"died partway and left a husk, so there is nothing to bring up. The "
                f"branch survives in {box.project}'s own repo; preview it by ref "
                f"instead (`worktree.py preview {box.project} --branch {subject}`), "
                f"and `worktree.py list` says what state the box is in."
            ),
        )
    allowed, why = preview_refresh_decision(box.kind, state.dirty, force)
    refresh = preview_refresh_steps(box.tracks) if allowed and box.tracks and fetch else ()
    return PreviewPlan(
        box=box,
        path=str(path),
        refresh=refresh,
        up=up and has_stack(path),
        down=down,
        urls=preview_urls(load_registry(workspace_root), box.slot, box.services),
        warning="" if allowed or not box.tracks else why,
    )


def plan_preview(
    target: str,
    workspace: Path,
    *,
    branch: str = "",
    pr: int = 0,
    fetch: bool = True,
    provision: bool = False,
    up: bool = True,
    down: bool = False,
    force: bool = False,
    ui: bool = False,
) -> PreviewPlan:
    """Resolve `preview`'s arguments against disk and, for `--pr`, against GitHub.

    `target` is either a live box — serve what is already here, which is the answer when
    the agent ran on this machine and its box is still standing — or a project, in which
    case `--pr`/`--branch` names the ref to check out. That second form is the one that
    does not need the work to be local at all: the branch is on the remote, so a review
    can start the moment the agent pushes rather than after someone merges it.

    `ui` cuts the cheap kind of preview: only the project's `[worktree] ui_services`
    are started, and the rest of the stack is borrowed from the source checkout — the
    mode for looking at a frontend change without paying for a second database, worker
    and object store. It changes only how a *new* box is cut; a live box named as
    `target` is served as whatever it already is, because the services it runs are on
    its lease, not in this flag.
    """
    root = workspace.parent
    boxes = live_boxes(root)

    if not branch and not pr:
        existing = boxes.get(target)
        if existing is None:
            known = ", ".join(sorted(boxes)) or "(none)"
            raise WorktreeError(
                f"{target!r} is not a live box, and no ref was named. Either pass "
                f"--pr <n> / --branch <name> to preview a ref of the {target!r} project, "
                f"or name a live box: {known}"
            )
        return serve_preview(existing, root, up=up, down=down, force=force, fetch=fetch)

    projects = known_projects(workspace)
    source = devkit_project.resolve_project(target, projects, root)
    project = source.name
    ref = branch
    if pr:
        ref = pr_head_branch(sweep.gh_for(source), pr)
        if not ref:
            raise WorktreeError(
                f"cannot read the head branch of PR #{pr} in {project} — is `gh` "
                f"authenticated for that remote? `--branch <name>` skips the lookup."
            )

    ui_services: tuple[str, ...] = ()
    ui_env: dict[str, str] = {}
    donor_slot = -1
    if ui:
        with contextlib.suppress(Exception):
            worktree_cfg = harness_config.load(source).worktree
            ui_services = tuple(worktree_cfg.ui_services)
            ui_env = dict(worktree_cfg.ui_env)
        if not ui_services:
            raise WorktreeError(
                f"{project} has no [worktree] ui_services in .devkit.toml, so there is "
                f"nothing to scope a UI-only preview to. Name the compose services that "
                f'make up its UI tier there (e.g. ui_services = ["frontend"]), or run a '
                f"full preview."
            )

    name = preview_box_name(project, ref, ui=ui)
    existing = boxes.get(name)
    if existing is not None:
        return serve_preview(existing, root, up=up, down=down, force=force, fetch=fetch)

    registry = load_registry(root) if has_stack(source) else None
    if ui:
        donor_slot = registry.slots.get(project, -1) if registry is not None else -1
        if donor_slot < 0:
            raise WorktreeError(
                f"a UI-only preview borrows the {project} checkout's running stack, "
                f"which needs that checkout pinned in ports.toml [slots] — it is not, "
                f"so there is no backend to point the UI at. Run a full preview instead."
            )
    spawn = preview_spawn_plan(
        project=project,
        workspace_root=root,
        ref=ref,
        boxes=boxes,
        registry=registry,
        fetch=fetch,
        provision=plan_provision(source) if provision else (),
        env_templates=plan_env_templates(source),
        ui_services=ui_services,
        ui_env_templates=ui_env,
        donor_slot=donor_slot,
    )
    return PreviewPlan(
        box=spawn.box,
        path=spawn.path,
        spawn=spawn,
        up=up and has_stack(source),
        down=down,
        urls=preview_urls(registry, spawn.box.slot, spawn.box.services),
    )


def apply_preview(
    plan: PreviewPlan, workspace: Path, timeout: float = 300.0
) -> tuple[bool, list[str]]:
    """Cut or refresh the preview box, then start (or stop) its stack. `(ok, notes)`."""
    if plan.refusal:
        return False, [plan.refusal]
    notes: list[str] = []
    path = Path(plan.path)

    if plan.spawn is not None:
        ok, spawn_notes = apply_new(
            plan.spawn, workspace, timeout=timeout, provision=bool(plan.spawn.provision)
        )
        notes.extend(spawn_notes)
        if not ok:
            return False, notes
    elif plan.refresh:
        _, failed, error = run_steps(path, plan.refresh, timeout=timeout)
        if failed:
            # A refresh that could not run leaves the box on the commit it was already
            # showing, which is a stale preview and not a broken one — say which, because
            # a UI that does not show the change is otherwise read as the change failing.
            notes.append(
                f"[warn] not refreshed: `{failed}` failed ({error}); the box is still on "
                f"the commit it was last set to"
            )
        else:
            notes.append(f"refreshed onto origin/{plan.box.tracks}")

    if plan.down:
        ok, message = compose_down(path, plan.box.name)
        notes.append(message if ok else f"[warn] {message}")
        return ok, notes

    if plan.up and plan.box.slot < 0:
        # `plan.up` already implies `has_stack`, so a slot is exactly what this box is
        # missing — and without one its `.env` carries the seeded copy's ports, which are
        # the source checkout's. Starting here is the "second copy of the source
        # checkout's stack" `compose_up`'s docstring names, so refuse instead.
        notes.append(
            f"[warn] {plan.box.name} holds no port slot, so its stack was NOT started: "
            f"its .env still names the {plan.box.project} checkout's ports and compose "
            f"would bind those. Free a slot (`worktree.py reap <box> --yes`, or raise "
            f"registry.max_slots in ports.toml) and cut it again."
        )
        return False, notes

    if plan.up:
        ok, message = compose_up(path, plan.box.name, services=plan.box.services)
        notes.append(message if ok else f"[warn] {message}")
        if not ok:
            return False, notes
        if plan.box.services:
            notes.append(
                f"ui-only: started {', '.join(plan.box.services)}; everything else is "
                f"borrowed from the {plan.box.project} checkout's stack on its usual "
                f"ports — bring that up if it is not already running"
            )
    return True, notes


def inspect_box(
    box: Box, workspace_root: Path, fetch: bool = False
) -> tuple[sweep.State, str, str]:
    """`(state, verdict, reason)` for one box, through `sweep`'s classifier.

    Deliberately the same classifier the static tier uses. A second one would be a
    second opinion about "does this hold unshipped work", and the two would disagree
    exactly when it mattered.

    A **preview** box is the one thing that classifier has no vocabulary for: it sits on
    a `preview/...` branch that is not a task branch and never will be, so every verdict
    it can return is a complaint about a state that is this box's whole point.
    `needs-branch` on a preview would make `reconcile` HOLD it forever and `reap` refuse
    it, which is the leak the ephemeral tier exists to avoid. So the kind answers first,
    and the state is still read, because `dirty` is what protects an edit made in there.
    """
    state = sweep.inspect(box.name, box_path(workspace_root, box.name), fetch=fetch)
    if box.kind == PREVIEW_KIND:
        subject = box.tracks or box.branch
        return state, PREVIEW_VERDICT, f"a read-only copy of {subject}, held for review"
    verdict, reason = sweep.classify(state)
    return state, verdict, reason


def plan_reap(
    name: str,
    workspace: Path,
    force: bool = False,
    keep_stack: bool = False,
    fetch: bool = True,
    pr: PullRequest | None = None,
) -> ReapPlan:
    """Everything `reap` will run for one box, resolved from disk.

    `pr` short-circuits the lookup for a caller that already made it. `reconcile` asks
    GitHub about every box in order to decide what to do with it, and asking a second
    time here would double a pass's network cost for an answer it is already holding.

    The lookup is `pr_for` rather than `sweep.has_merged_pr`: same one `gh` call, and
    it answers with the PR's *state* instead of a single boolean, which is what lets a
    closed PR clear `awaiting_pr` (see `reap_decision`). Asking the same question as
    `reconcile` also means the two stop being able to disagree about one box -- the
    disagreement `AWAITS_A_MERGE` was added to stop appearing between `list` and
    `reconcile`. `--no-fetch` skips it and therefore sees neither a merge nor a close,
    exactly as before.

    `awaiting_pr` still reads the *caller's* `pr`, never the looked-up one: it asks
    whether whoever called this had a PR in hand, and answering it from a lookup made
    here would turn the flag off for every caller and delete the refusal.
    """
    root = workspace.parent
    boxes = read_leases(root)
    box = boxes.get(name)
    path = box_path(root, name)
    if box is None:
        if not path.is_dir():
            known = ", ".join(sorted(boxes)) or "(none)"
            raise WorktreeError(f"no box called {name!r}; live boxes: {known}")
        # A worktree with no lease: created by hand, or the lease file was lost. Reap
        # it anyway — refusing would leave the only cleanup path as `rm -rf`.
        box = Box(name=name, project=project_of(name), branch="", slot=-1)

    state, verdict, reason = inspect_box(box, root, fetch=fetch)
    found = pr
    if found is None and fetch and box.branch and state.host == "github":
        found = pr_for(sweep.gh_for(path), box.branch)
    found = found or PullRequest()
    return reap_plan(
        box=box,
        workspace_root=root,
        state=state,
        verdict=verdict,
        reason=reason,
        pr_merged=found.merged,
        pr_closed=found.is_closed,
        force=force,
        keep_stack=keep_stack,
        has_stack=has_stack(path),
        awaiting_pr=pr is None and verdict in AWAITS_A_MERGE,
        # Both halves are observations rather than absences: `absent` is set only where
        # gh answered "no pull requests found" (so `--no-fetch`, an offline machine and
        # a non-GitHub host all leave it False and the refusal stands), and the age is
        # the box's own. `reconcile` reaches the same decision through
        # `reconcile_action`; asking it here too is what keeps `reap` from refusing a
        # box the scheduled pass would destroy a quarter of an hour later.
        unclaimed=found.absent and box_age_days(box.created) > DEFAULT_UNCLAIMED_AGE_DAYS,
        copy_intact=(
            preview_copy_intact(sweep.git_for(root / box.project), box.branch, box.tracks)
            if box.kind == PREVIEW_KIND and box.branch and box.tracks and state.is_git
            else None
        ),
        work_is_landed=work_landed(sweep.git_for(path), state, verdict, found),
    )


REAP_LEDGER = "logs/worktree-reaped.log"


def reap_ledger_line(plan: ReapPlan, stamp: str) -> str:
    """One ledger record for a destroyed box. Pure, so the format is testable.

    Tab-separated `key=value`, one line, no wrapping: this file is read by grepping for
    a box name months after the box stopped existing, which rules out both JSON (an
    interrupted write leaves an unparseable document) and prose.
    """
    fields = (
        ("box", plan.box),
        ("branch", plan.branch or "-"),
        ("verdict", plan.verdict or "-"),
        ("forced", "yes" if plan.forced else "no"),
        ("dirty", str(plan.dirty)),
        ("session", plan.session or "-"),
        ("path", plan.path or "-"),
        ("reason", " ".join((plan.reason or "-").split())),
    )
    return stamp + "\t" + "\t".join(f"{key}={value}" for key, value in fields)


def record_reap(plan: ReapPlan, root: Path = REPO_ROOT) -> Path | None:
    """Append one line to `logs/worktree-reaped.log`. Returns the path, or None.

    **The only durable record that a box ever existed.** Nothing else in the workspace
    keeps one: `leases.json` is the live set and the entry is deleted by the reap
    itself, `reconcile.log` is overwritten by the next pass fifteen minutes later, and
    `reap` writes nothing at all. So when a box disappears, the questions that follow --
    which pass took it, was it forced, did it hold uncommitted files at the time -- have
    no source to answer them from, and the incident that prompted this one could not be
    attributed to a mechanism even with every log on the machine in hand.

    Append-only and never rotated, for the same reason. A record of destruction that the
    next destruction overwrites is not a record; this file grows by one line per box,
    which is a few kilobytes a year.

    Best-effort: a box that has been destroyed has been destroyed, and failing the reap
    over the bookkeeping would leave the caller believing the opposite.
    """
    path = root / REAP_LEDGER
    stamp = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(reap_ledger_line(plan, stamp) + "\n")
    except OSError:
        return None
    return path


def parse_ledger_line(line: str) -> dict[str, str]:
    """One ledger line back into its fields. Pure; `{}` for anything unparseable.

    Tolerant on purpose. This file is append-only and never rotated, so it still holds
    lines written before `session` existed as a field, and will hold lines written
    after the next field is added. A reader that insisted on a fixed shape would go
    blind on the oldest half of its own record.
    """
    stamp, _, rest = line.partition("\t")
    if not rest.strip():
        return {}
    fields = {"stamp": stamp.strip()}
    for chunk in rest.split("\t"):
        key, sep, value = chunk.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def reaped_branches(project: str, session: str, ledger: str) -> list[str]:
    """Branches whose box was reaped out from under `(session, project)`, newest first.

    Pure, taking the ledger's text rather than its path, because the interesting cases
    are all shapes of line rather than states of the filesystem.

    `-` is the ledger's spelling of an absent field, and a box with no branch recorded
    is a box there is no way back to; both are skipped rather than returned as a branch
    literally called `-`.
    """
    found: list[str] = []
    for line in ledger.splitlines():
        fields = parse_ledger_line(line)
        branch = fields.get("branch", "-")
        if branch in ("", "-"):
            continue
        if fields.get("session", "-") != session or project_of(fields.get("box", "")) != project:
            continue
        found.append(branch)
    found.reverse()
    return found


def branch_is_merged(git: sweep.Git, branch: str, default_branch: str) -> bool:
    """Whether `origin/<branch>` is already contained in `origin/<default_branch>`.

    Local refs only, no network: this runs inside `worktree-guard.py`, where the caller
    waiting on the answer is an agent's Edit. It is also the *right* question rather
    than a proxy for it — "has this work landed" is what decides whether resuming the
    branch continues a task or reopens a finished one, and neither the PR's state nor
    the branch's continued existence on the remote answers it as directly.

    False on any failure. An unreadable ref means the merge cannot be demonstrated, and
    the safe reading of that is "not merged": it costs a box on a branch that turns out
    to be finished, where the opposite costs a second PR for work already in flight.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        result = git(
            "merge-base",
            "--is-ancestor",
            f"refs/remotes/origin/{branch}",
            f"refs/remotes/origin/{default_branch}",
        )
        return result.returncode == 0
    return False


def head_tree_landed(git: sweep.Git, default_branch: str, depth: int = TREE_SCAN_DEPTH) -> bool:
    """Whether the box's HEAD tree already appears on `origin/<default_branch>`.

    The question `branch_is_merged` asks about *commits*, asked about **content** instead,
    for the boxes where commit identity has been destroyed and no PR names the branch to
    say so (`TREE_CAN_SETTLE`). A tree object is the whole checked-out state hashed, so a
    hit is not evidence that the work landed -- it is the work, already reachable from the
    default branch, byte for byte. There is nothing left for a reap to lose.

    Local refs only and no network, like `branch_is_merged`, and for the same reason: the
    callers are a cleanup decision and a scheduled pass, neither of which should turn on
    whether a fetch succeeded. That does mean the answer is only as fresh as the last
    fetch -- which can only ever *withhold* a reap, since an unfetched default branch has
    strictly fewer commits to match.

    False on any failure, on an empty `default_branch`, and on a tree that is simply not
    in the scanned window. Every one of those is "the landing cannot be demonstrated",
    and the safe reading of that is the refusal the caller already had.
    """
    if not default_branch:
        return False
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        head = git("rev-parse", "HEAD^{tree}")
        tree = head.stdout.strip() if head.returncode == 0 else ""
        if not tree:
            return False
        history = git(
            "log",
            f"--max-count={depth}",
            "--format=%T",
            f"refs/remotes/origin/{default_branch}",
        )
        if history.returncode != 0:
            return False
        return tree in history.stdout.split()
    return False


def head_is_merged_pr_head(git: sweep.Git, pr: PullRequest) -> bool:
    """Whether the box's HEAD is the exact commit a merged PR squashed.

    The tree scan's blind spot, closed from the other side. A branch that was *behind*
    the default branch when its PR squash-merged produces a squash commit whose tree no
    commit of the box's ever had -- the squash carries the default branch's newer files
    too -- so `head_tree_landed` cannot match it, and a box on a hand-named branch
    (`needs-branch`) has no merge arm to fall back on. GitHub, though, records which
    commit it merged: `headRefOid` on a merged PR. When that is this HEAD, every commit
    the box has is one the PR carried, by identity rather than by content.

    Only a **merged** PR counts, and only an exact match: an open PR's head is a promise,
    a closed one's is a refusal, and a HEAD one commit past the merged head is work the
    PR never saw. Unknown -- no PR, no `headRefOid`, a `git` that cannot answer -- is
    `False`, the refusal the caller already had.
    """
    if not (pr.merged and pr.head):
        return False
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        head = git("rev-parse", "HEAD")
        if head.returncode != 0:
            return False
        return head.stdout.strip().lower() == pr.head
    return False


def commits_landed(git: sweep.Git, state: sweep.State, pr: PullRequest | None = None) -> bool:
    """Whether every commit the box's HEAD carries is already on the default branch.

    The two landing proofs (`head_tree_landed`, `head_is_merged_pr_head`) asked together
    and **without the dirty gate**, which is the half `work_landed` adds. Split out for
    `rescue`, which asks a different question of the same facts: not "may this box be
    destroyed" but "does the uncommitted work sit on commits that already landed" --
    when it does, the fresh branch is cut from `origin/<default>` and the dirt carried
    over, and when it does not the commits are replayed on top. A dirty box answers
    `work_landed` with a flat no, and that no is the right answer to the reap question
    and no answer at all to this one.
    """
    if head_tree_landed(git, state.default_branch):
        return True
    return pr is not None and head_is_merged_pr_head(git, pr)


def work_landed(
    git: sweep.Git,
    state: sweep.State,
    verdict: str,
    pr: PullRequest | None = None,
) -> bool:
    """The `work_is_landed` every caller hands `reapable`, computed one way.

    `plan_reap`, `reconcile` and `survey` used to spell the gate out separately, which
    is how a verdict could be settled by one and held by another. Gated on the verdict
    and on cleanliness before any git call is made, because these are the only calls a
    pass adds and a `needs-pr` or dirty box -- the ordinary endings -- must not pay for
    a question whose answer `reapable` would then ignore.

    `pr` is optional because `survey` has none in hand; it then answers from the tree
    alone, which can only withhold a reap, never grant one the others would refuse.
    """
    if verdict not in TREE_CAN_SETTLE or state.dirty:
        return False
    return commits_landed(git, state, pr)


# --- rescue: the rebranch verb --------------------------------------------------

# The verdicts `rescue` will move work out from under. `needs-branch` and
# `needs-rebranch` are the two `sweep.BRANCHABLE` states -- a branch the policy will not
# let a commit land on -- and `ready` is included so `rescue --ship` is one command for a
# stranded box whatever shape it is in: a box already on a live task branch has nothing
# to rebranch and goes straight to the ship half.
RESCUABLE: frozenset[str] = frozenset({sweep.NEEDS_BRANCH, sweep.NEEDS_REBRANCH, sweep.READY})

# The `-<mmdd>` (and `-N`) stamp `tb.branch_name` appends, as it survives in a box name.
# Stripped before the box's own name is used as a topic, so a rescue of
# `devkit--fix-pr-256-0828` cuts `agent/fix-pr-256-0830`, not `agent/fix-pr-256-0828-0830`.
BOX_STAMP_RE = re.compile(r"-\d{4}(?:-\d+)?$")


@dataclass(frozen=True)
class RescuePlan:
    """What `rescue` would do to one box: the fresh branch, and how to get onto it.

    `steps` run **in the box**, not in the source checkout -- every other plan here
    binds its git to the project checkout, because a reap acts on the box from outside
    it; a rescue is the one verb whose subject is the box's own working tree, dirt
    included. `rollback` is what `apply_rescue` runs, best-effort and in order, when a
    step fails, so a rebase that stops on a conflict leaves the box exactly as it was
    found rather than half-moved with a `.git/rebase-merge` in it.

    `landed` is `commits_landed`'s answer, carried so the report can say which of the
    two rebase shapes was chosen and why.
    """

    box: str
    path: str = ""
    project: str = ""
    old_branch: str = ""
    branch: str = ""
    steps: tuple[tuple[str, ...], ...] = ()
    rollback: tuple[tuple[str, ...], ...] = ()
    refusal: str = ""
    verdict: str = ""
    reason: str = ""
    dirty: int = 0
    landed: bool = False

    @property
    def acts(self) -> bool:
        return bool(self.steps)


def box_topic(name: str) -> str:
    """The slug a box was named for: `devkit--fix-pr-256-0828` -> `fix-pr-256`."""
    _, sep, rest = name.partition(NAME_SEP)
    return BOX_STAMP_RE.sub("", rest if sep else name) or name


def rescue_refusal(box: Box, state: sweep.State, verdict: str, reason: str) -> str:
    """Why `rescue` cannot help this box, or "" when it can. Each refusal names the verb
    that can: a merged or empty box wants `reap`, a pushed one wants `/ship`, and a
    preview cannot be shipped at all."""
    if box.kind == PREVIEW_KIND:
        return (
            f"{box.name} is a preview -- a read-only copy of {box.tracks or state.branch}. "
            f"Edits in a preview ship nowhere; copy anything worth keeping out, then "
            f"`reap {box.name} --force --yes`"
        )
    if not state.is_git:
        return f"{reason} -- a husk holds nothing to rescue; `reap` it"
    if verdict not in RESCUABLE:
        remedy = "`/ship` finishes it" if verdict == sweep.NEEDS_PR else "`reap` it"
        return (
            f"{verdict} -- {reason}; rescue moves work off a branch it cannot be "
            f"shipped from, and this box is not on one ({remedy})"
        )
    if not state.branch or not state.default_branch:
        return f"{reason} -- no branch or no default branch to cut from"
    return ""


def rescue_plan(
    box: Box,
    state: sweep.State,
    verdict: str,
    reason: str,
    *,
    landed: bool = False,
    today: _dt.date | None = None,
) -> RescuePlan:
    """Move a stranded box's work onto a branch it can ship from.

    A retired or hand-named branch cannot accept the next commit. The fresh branch must
    be rebased, not merely cut from HEAD, because HEAD may carry pre-squash history that
    already landed. With `landed`, `--onto origin/<default> HEAD` replays no commits and
    carries only autostashed dirt; otherwise git drops commits the squash made empty and
    keeps the rest.

    This stage commits and pushes nothing. `--ship` does so only after re-inspection.
    A `ready` box needs no move, but still returns a plan for that shipping half.
    """
    plan = RescuePlan(
        box=box.name,
        project=box.project,
        old_branch=state.branch,
        branch=state.branch,
        verdict=verdict,
        reason=reason,
        dirty=state.dirty,
        landed=landed,
    )
    refusal = rescue_refusal(box, state, verdict, reason)
    if refusal:
        return replace(plan, refusal=refusal)
    if verdict == sweep.READY:
        # Already on a live task branch: nothing to move. `--ship` takes it from here.
        return plan

    topic = sweep.branch_topic(state.branch) or box_topic(box.name)
    taken = set(state.local_branches) | {state.branch}
    name = tb.branch_name(tb.slugify(topic), taken, today)
    base = f"origin/{state.default_branch}"
    rebase = (
        ("rebase", "--autostash", "--onto", base, "HEAD")
        if landed
        else ("rebase", "--autostash", base)
    )
    return replace(
        plan,
        branch=name,
        steps=(("checkout", "-b", name), rebase),
        rollback=(("rebase", "--abort"), ("checkout", state.branch), ("branch", "-D", name)),
    )


def apply_rescue(plan: RescuePlan, workspace: Path) -> tuple[bool, list[str]]:
    """Run the rescue in the box; on success, record the new branch in the lease.

    The lease must follow the checked-out branch: `reconcile` keys lifecycle decisions
    on it. An ordinary failed step rolls back best-effort. An autostash conflict is the
    exception: rebase reports success after leaving markers, so the new branch and lease
    stay aligned while shipping stops for manual resolution.
    """
    root = workspace.parent
    cwd = Path(plan.path)
    ran, failed, error = run_steps(cwd, plan.steps)
    notes = list(ran)
    if failed:
        notes.append(f"FAILED at `{failed}`: {error}")
        for step in plan.rollback:
            _, undo_failed, _ = run_steps(cwd, (step,))
            if not undo_failed:
                notes.append(f"rolled back: git {' '.join(step)}")
        notes.append(f"the box is back on {plan.old_branch}; resolve the conflict by hand")
        return False, notes
    _, marker_check, marker_error = run_steps(cwd, (("diff", "--check"),))
    marker_lines = [
        line for line in marker_error.splitlines() if "leftover conflict marker" in line.lower()
    ]
    if marker_check and marker_lines:
        markers = "\n".join(marker_lines)
        notes.append(f"FAILED at `git diff --check`: {markers}")
        with lease_lock(root):
            boxes = live_boxes(root)
            box = boxes.get(plan.box)
            if box is not None:
                boxes[plan.box] = replace(box, branch=plan.branch)
                write_leases(root, boxes)
        notes.append(f"lease now records {plan.branch} (was {plan.old_branch})")
        notes.append("autostash left conflict markers; rescue stopped before shipping")
        return False, notes
    with lease_lock(root):
        boxes = live_boxes(root)
        box = boxes.get(plan.box)
        if box is not None:
            boxes[plan.box] = replace(box, branch=plan.branch)
            write_leases(root, boxes)
    notes.append(f"lease now records {plan.branch} (was {plan.old_branch})")
    return True, notes


def rescue_commit_message(plan: RescuePlan, message: str = "") -> str:
    """The subject for a rescued commit: `message` verbatim, or an honest default.

    Mechanical for the reason `sweep.commit_message` is: nothing here has read the
    diff, so the subject says what the commit *is* -- work the tier found stranded in a
    box and moved onto a branch it could ship from -- and the PR body says the same.
    """
    if message.strip():
        return message.strip()
    topic = sweep.branch_topic(plan.branch) or box_topic(plan.box)
    return f"rescue({topic}): ship work left in {plan.box}"


def rescue_pr_body(plan: RescuePlan, state: sweep.State, limit: int = 40) -> str:
    """The body for a rescued PR: where the work was, and what moved."""
    lines = [
        f"Opened by `worktree.py rescue --ship` on box `{plan.box}`. The box sat on "
        f"`{plan.old_branch}` ({plan.verdict}: {plan.reason}), which could not be shipped "
        f"from, so its work was moved onto `{plan.branch}` on top of "
        f"`origin/{state.default_branch}`.",
        "",
        "**Nothing has reviewed this.** No diff was read, so the title describes the "
        "rescue rather than the change. Retitle, split, or close it once you have looked.",
    ]
    if state.dirty_files:
        shown = state.dirty_files[:limit]
        lines += ["", f"Changed paths ({state.dirty}):", ""]
        lines += [f"- `{path}`" for path in shown]
        if len(state.dirty_files) > len(shown):
            lines.append(f"- …and {len(state.dirty_files) - len(shown)} more")
    return "\n".join(lines)


def rescue_ship_plan(plan: RescuePlan, state: sweep.State, message: str = "") -> sweep.Plan:
    """The `--ship` half: commit what the rescued box holds, push it, open the PR.

    Built after `apply_rescue` and from a **re-inspected** state, so the branch it
    pushes is the one the rebase left the box on and the dirt count is what survived
    the autostash. Refuses the same things `sweep.ship_plan` refuses, in its words: the
    box must be on a live task branch with something to ship. Hooks are not bypassed --
    the project's pre-commit gate runs on the commit, which is why the box has to be
    provisioned first.
    """
    if not sweep.is_task_branch(state.branch):
        return sweep.Plan(refusal=f"{state.branch} is not a {tb.BRANCH_PREFIX} task branch")
    if not state.dirty and not state.ahead and state.unpushed == 0:
        return sweep.Plan(refusal="nothing to ship -- the rescued branch is clean and pushed")
    subject = rescue_commit_message(replace(plan, branch=state.branch), message)
    steps: list[tuple[str, ...]] = []
    if state.dirty:
        steps.append(("add", "-A"))
        steps.append(("commit", "-m", subject))
    steps.append(("push", "-u", "origin", state.branch))
    return sweep.Plan(
        steps=tuple(steps),
        pr_title=subject,
        pr_body=rescue_pr_body(replace(plan, branch=state.branch), state),
        pr_head=state.branch,
        pr_base=state.default_branch,
    )


def rescue(
    workspace: Path,
    name: str,
    *,
    apply: bool = False,
    ship: bool = False,
    message: str = "",
    fetch: bool = True,
) -> tuple[bool, RescuePlan, list[str]]:
    """`rescue <box> [--ship]` end to end. `(ok, plan, notes)`.

    Inspects, plans, applies, and -- with `ship` -- re-inspects and ships, each half
    reporting into `notes`. A dry run stops after the plan, so the fresh branch name and
    the rebase shape are printed before anything moves.
    """
    root = workspace.parent
    boxes = live_boxes(root)
    box = boxes.get(name)
    if box is None:
        known = ", ".join(sorted(boxes)) or "(none)"
        raise WorktreeError(f"no live box called {name!r}; live boxes: {known}")
    path = box_path(root, name)
    state, verdict, reason = inspect_box(box, root, fetch=fetch)
    pr = (
        pr_for(sweep.gh_for(path), box.branch or state.branch)
        if fetch and (box.branch or state.branch) and state.host == "github"
        else PullRequest()
    )
    landed = state.is_git and commits_landed(sweep.git_for(path), state, pr)
    plan = replace(rescue_plan(box, state, verdict, reason, landed=landed), path=str(path))
    if plan.refusal:
        return False, plan, []
    notes: list[str] = []
    if not apply:
        return True, plan, notes
    if plan.acts:
        ok, notes = apply_rescue(plan, workspace)
        if not ok:
            return False, plan, notes
    if not ship:
        return True, plan, notes
    return _ship_rescued(box.name, path, plan, message, notes), plan, notes


def _ship_rescued(name: str, path: Path, plan: RescuePlan, message: str, notes: list[str]) -> bool:
    """The `--ship` half of `rescue`: re-inspect the moved box and ship it through the sweep."""
    after = sweep.inspect(name, path, fetch=False)
    shipping = rescue_ship_plan(plan, after, message)
    if shipping.refusal:
        notes.append(f"ship refused: {shipping.refusal}")
        return False
    applied = sweep.apply_plan(name, path, shipping)
    notes.extend(applied.ran)
    if applied.failed:
        notes.append(f"FAILED at `{applied.failed}`: {applied.error}")
        return False
    if applied.pr_url:
        notes.append(f"{'opened' if applied.pr_created else 'PR'}: {applied.pr_url}")
    return True


def render_rescue(plan: RescuePlan, applied: bool, notes: list[str]) -> str:
    if plan.refusal:
        return f"{plan.box}: refused -- {plan.refusal}"
    if not plan.acts:
        lines = [f"{plan.box} is already on {plan.branch} ({plan.verdict}) -- nothing to move"]
    else:
        verb = "Rescued" if applied else "Would rescue"
        how = (
            "its commits already landed, so the dirt moves onto the default branch"
            if plan.landed
            else "its commits are replayed onto the default branch"
        )
        lines = [f"{verb} {plan.box}: {plan.old_branch} -> {plan.branch} ({how})"]
        for n, step in enumerate(plan.steps, 1):
            lines.append(f"    {n}. git -C {plan.box} {' '.join(step)}")
    lines.extend(f"  {note}" for note in notes)
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


def resumable_branch(
    workspace_root: Path,
    project: str,
    session: str,
    ledger_root: Path | None = None,
) -> str:
    """The branch this session should be put back onto in `project`, or "".

    The join between the two halves of the recovery: `record_reap` wrote down that a
    box existed and whose it was, and this reads it back the moment the same session
    tries to edit the same project again. Everything else the tier knows about that box
    was deleted along with it.

    Newest first, and the first branch that is neither merged nor gone wins. A session
    that has had two boxes reaped in one project wants the one it was working in last;
    the older ones are skipped rather than refused, because a merged branch behind a
    live one is the ordinary shape of a long session, not a conflict.

    Never raises and never blocks for long: a missing ledger, an unreadable one and a
    project with no git are all "" -- the caller then cuts a fresh box, which is the
    behaviour that existed before this function did.
    """
    # The reader half of `record_reap`, so it resolves the ledger the same way: with the
    # workspace it is asking about, not with whichever devkit copy is running.
    root = artifact_root(workspace_root) if ledger_root is None else ledger_root
    try:
        ledger = (root / REAP_LEDGER).read_text(encoding="utf-8")
    except OSError:
        return ""
    candidates = reaped_branches(project, session, ledger)
    if not candidates:
        return ""
    source = workspace_root / project
    git = sweep.git_for(source)
    default_branch = tb.detect_default_branch(git, fallback="")
    for branch in candidates:
        if default_branch and branch_is_merged(git, branch, default_branch):
            continue
        if origin_has_branch(git, branch, network=False):
            return branch
    return ""


def branch_already_gone(failed: str, error: str) -> bool:
    """Whether a failed reap step was a branch delete of a branch that no longer exists."""
    return failed.startswith("git branch -") and "not found" in error.lower()


def steps_after(steps: tuple[tuple[str, ...], ...], failed: str) -> tuple[tuple[str, ...], ...]:
    """The steps that follow the one `run_steps` reported as `failed`."""
    for index, step in enumerate(steps):
        if "git " + " ".join(step) == failed:
            return steps[index + 1 :]
    return ()


def _carry_past_deleted_branch(
    source: Path, plan: ReapPlan, failed: str, error: str, notes: list[str]
) -> tuple[str, str]:
    """Run the reap steps after a `git branch -D` that found no branch; `(failed, error)`.

    A branch delete that finds no branch has nothing left to do, not something left
    undone. The scheduled pass failed a whole reap on `git branch -D <name>` --
    "branch not found" -- after something else had already deleted the branch, and
    then held the box, its slot and its volumes over a step whose goal was met.
    """
    while failed and branch_already_gone(failed, error):
        notes.append(f"`{failed}`: the branch was already deleted; carried on")
        more, failed, error = run_steps(source, steps_after(plan.steps, failed))
        notes.extend(more)
    return failed, error


def _remove_box_by_hand(
    plan: ReapPlan, source: Path, failed: str, error: str, notes: list[str]
) -> tuple[str, str]:
    """Finish a `git worktree remove` that failed, by deleting the tree ourselves.

    Only reached when `box_teardown.fallback_applies` says the failure was the
    filesystem refusing rather than git refusing — the caller owns that judgement, and
    a dirty-tree refusal must never arrive here.

    Returns the `(failed, error)` pair the caller carries on with: unchanged but for the
    direct delete's own complaint when even that could not remove the box, and otherwise
    the verdict of the plan's remaining steps, since the removal it stopped at is now
    done. Extracted so `apply_reap` keeps one arm here instead of five.
    """
    first_line = error.splitlines()[0] if error else "no detail"
    fallback_error, evicted = box_teardown.force_remove_box(Path(plan.path))
    notes.extend(evicted)
    if fallback_error:
        return failed, f"{error} (direct delete also failed: {fallback_error})"
    _, prune_failed, prune_error = run_steps(source, (("worktree", "prune"),))
    if prune_failed:
        return prune_failed, prune_error
    notes.append(
        f"`{failed}` failed ({first_line}); deleted the tree directly and pruned the record"
    )
    remaining = tuple(step for step in plan.steps if step[:2] != ("worktree", "remove"))
    ran, failed, error = run_steps(source, remaining)
    notes.extend(ran)
    return failed, error


def apply_reap(plan: ReapPlan, workspace: Path) -> tuple[bool, list[str]]:
    """Destroy the box. `(ok, notes)`. The lease is released only once it is gone.

    A failed stack teardown does **not** stop the git cleanup, and does not report
    success either. Both halves of that matter: aborting would leave the box in place
    forever over a daemon that happened to be down, while carrying on quietly would
    leak a container set and a volume set per task — which is the thing that makes the
    WSL2 VHDX the next bottleneck. So the box goes, and the exit code says the stack
    needs a look.

    **The images the box built are read before the teardown and deleted after it**, and
    both halves of that order are load-bearing. `compose config` is what resolves the
    tags (`box_image_tags`), and it can only be asked while the box's compose file is
    still on disk — which the `git worktree remove` below ends. The delete has to come
    after `down`, because an image with a container on it is in use. Between those two
    points is the only window where both are true.

    A failed image removal does not fail the reap either, for the reason the stack
    teardown gives: the box is destroyed and the slot is reclaimed regardless, and a
    leaked image costs disk rather than work. It does count toward `stack_ok`, because
    "the stack needs a look" is exactly what it means.
    """
    root = workspace.parent
    notes: list[str] = []
    stack_ok = True
    if plan.stack_down:
        images = box_image_tags(compose_config(Path(plan.path), plan.box), plan.box)
        stack_ok, message = compose_down(Path(plan.path), plan.box)
        notes.append(f"{'' if stack_ok else '[warn] '}{message}")
        if not stack_ok:
            notes.append(
                f"the box was still removed, but its containers and volumes may survive "
                f"as project {plan.box} — check `docker compose ls` and prune by hand"
            )
        if stack_ok and images:
            images_ok, image_note = remove_images(images)
            notes.append(f"{'' if images_ok else '[warn] '}{image_note}")
            stack_ok = stack_ok and images_ok

    source = root / plan.project
    ran, failed, error = run_steps(source, plan.steps)
    notes.extend(ran)
    if (
        failed
        and failed.startswith("git worktree remove")
        and box_teardown.fallback_applies(Path(plan.path), error)
    ):
        failed, error = _remove_box_by_hand(plan, source, failed, error, notes)
    failed, error = _carry_past_deleted_branch(source, plan, failed, error, notes)
    if failed:
        notes.append(f"FAILED at `{failed}`: {error}")
        return False, notes
    # After the tree is gone and before the lease entry is: this is the last moment the
    # box is still described by anything, and the first moment it is certainly destroyed.
    # `artifact_root` rather than `REPO_ROOT`: the record belongs to the workspace whose
    # box this was, which is this repo for every real run and a `tmp_path` for a test --
    # see `artifact_root` for what defaulting to the module constant forged.
    ledger = record_reap(plan, artifact_root(root))
    if ledger is not None:
        notes.append(f"recorded in {ledger}")

    with lease_lock(root):
        recorded = read_leases(root)
        boxes = live_boxes(root)
        boxes.pop(plan.box, None)
        write_leases(root, boxes)
    notes.append(f"lease released (slot {plan.slot})" if plan.slot >= 0 else "lease released")
    dropped = [name for name in prune_leases(recorded, boxes) if name != plan.box]
    if dropped:
        notes.append(f"released {len(dropped)} stale lease(s): {', '.join(dropped)}")
    return stack_ok, notes


def survey(workspace: Path, fetch: bool = False, sizes: bool = False) -> list[dict]:
    """Every live box with its verdict and whether it can be reaped.

    `sizes` adds the on-disk cost per box, behind a flag because it is the expensive
    walk `free_gb` exists to avoid — see `dir_size_bytes`. Off by default so
    `workspace-status.py` can keep calling this at every session start.
    """
    root = workspace.parent
    registry = load_registry(root)
    paused = on_hold_projects(workspace)
    rows: list[dict] = []
    for name, box in sorted(live_boxes(root).items()):
        state, verdict, reason = inspect_box(box, root, fetch=fetch)
        path = box_path(root, name)
        is_reapable = (
            reapable(
                verdict,
                holds_uncommitted=bool(state.dirty),
                work_is_landed=work_landed(sweep.git_for(path), state, verdict),
            )
            and verdict not in AWAITS_A_MERGE
        )
        holding = not is_reapable and verdict not in AWAITS_A_MERGE
        row = {
            "box": name,
            "project": box.project,
            "branch": box.branch or state.branch,
            "slot": box.slot,
            "session": box.session,
            "created": box.created,
            "age_days": round(box_age_days(box.created), 2),
            "verdict": verdict,
            "reason": reason,
            # Leased-idle vs leased-active, which `session` alone cannot tell apart: a
            # lease says who owns the box, not whether anything is in flight in it. It
            # is what `claim_refusal` decides on, and a reader picking a box to take
            # over needs the same fact in front of them before they run the command.
            "dirty": bool(state.dirty),
            # `reapable(...)` rather than the set, so a preview — which the set cannot
            # contain without making `spent-branch` mean two things — is not reported as
            # a box holding work. `AWAITS_A_MERGE` is then subtracted for the same reason
            # `plan_reap` sets `awaiting_pr`: this row feeds `reap --all` and the session
            # banner, neither of which has a PR in hand, so a pushed box under review must
            # not be advertised as free to destroy.
            # `work_is_landed` is asked here for the same reason `AWAITS_A_MERGE` is
            # subtracted: this column is what a reader picks their next command from, and
            # a box `reap` will destroy without a flag must not be printed as one holding
            # work. Gated exactly as `plan_reap` and `reconcile` gate it, so all three
            # answer alike -- from the tree alone, since this row has no PR in hand, which
            # is the same asymmetry `pr_merged` already has here.
            "reapable": is_reapable,
            # The same predicate `reconcile` applies, from the tree alone: this row has
            # no PR in hand, so a box still being edited after its PR opened reads as
            # stranded here once it is a day old, where `reconcile` -- which asks
            # GitHub -- would not. The banner over-reports in that one case and the
            # reconcile log is the authority; the asymmetry is `pr_merged`'s again.
            "stranded": stranded(
                HOLD if holding else WAIT,
                pr_open=False,
                age_days=box_age_days(box.created),
                on_hold=box.project in paused,
            ),
            "kind": box.kind,
            "tracks": box.tracks,
            "urls": [list(entry) for entry in preview_urls(registry, box.slot, box.services)],
            "path": str(path),
        }
        if sizes:
            row["bytes"] = dir_size_bytes(path)
        rows.append(row)
    return rows


def checkout_sync_summary(results: list[sweep.Result], code: int) -> dict:
    """What a `--sync` pass over the static checkouts did, as data rather than prose.

    `sweep.run_mode` hands back rendered text and an exit code: the right shape for a
    person reading a terminal, the wrong one for a report that also has to be JSON and
    has to be asserted on. This turns the same pass into rows.

    It is also where the exit code is **reinterpreted**, which is the part worth
    knowing. `run_mode` returns 1 from a dry run that merely *found* something to do,
    while `reconcile`'s contract is that non-zero means a failure -- a scheduled runner
    that reddens on a healthy pass is a runner whose alerts nobody reads. Only 2, a git
    step that actually failed under `--yes`, is a failure here.

    `held` is derived from the verdict rather than from the plan's refusal text, and
    the two cannot disagree: `sweep.sync_plan` refuses everything outside
    `sweep.SYNCABLE`, and every one of those refusals means the same thing -- unshipped
    work is sitting in that checkout and the sync stepped over it.
    """
    rows = [
        {
            "checkout": result.state.name,
            "branch": result.state.branch,
            "verdict": result.verdict,
            "reason": result.reason,
            "held": result.verdict not in sweep.SYNCABLE,
        }
        for result in results
        if result.verdict != sweep.SKIPPED
    ]
    return {"rows": rows, "failed": code == 2}


def sync_checkouts(
    workspace: Path,
    *,
    apply: bool = False,
    fetch: bool = True,
) -> tuple[int, dict]:
    """Park every static checkout on its home branch, current with `origin/<default>`.

    The static tier's half of the unattended pass, and the reason it exists is a
    failure worth writing down: a PR merged, and the checkout it was written in stayed
    parked on the spent task branch for days. Nothing was lost and nothing was red --
    the local `master` simply never advanced, so the next session opened on a tree
    that predated the merge and could not see the work it was asked to continue. The
    fix was one command nobody was going to remember to run, which is the definition of
    something that belongs on a schedule.

    **Why it rides `reconcile`'s schedule instead of getting its own.** The two tiers
    stay disjoint in what they touch -- boxes are decided here, checkouts are decided
    by `sweep.classify`, and neither tool learns about the other's tier -- but they
    share one trigger, one log and one thing to enable. A second scheduled task is a
    second thing that can be disabled without anyone noticing, and this workspace has
    already had exactly that happen to the first one.

    Ordered after the box pass on purpose: reaping a merged box deletes its branch in
    the *source checkout*, so syncing afterwards reads a checkout the pass has already
    finished with rather than one it is halfway through.

    Nothing here can strand work. `sweep.sync_plan` acts only on `SYNCABLE` verdicts --
    a checkout holding uncommitted changes or unpushed commits is refused and reported,
    never parked -- and its steps are `merge --ff-only` and `branch -d`, both of which
    refuse rather than destroy. This adds no new authority to the scheduled run; it runs
    the same plan `sweep.py --sync` has always printed.

    An **open PR** is not on that refusal list, and the difference is worth stating
    because the sentence here used to say it was. A checkout that is clean and fully
    pushed with a PR open is `sweep.PARKED`: the work is on the remote and under review,
    so parking the checkout home costs it its position and nothing else, and the branch
    survives -- `sync_plan` scopes its `branch -d` to `SPENT`. Holding instead was the
    older behaviour and it had no exit: the verdict was `needs-pr`, which is not
    syncable and which nothing in the sweep ever resolves, so a checkout that reached it
    stayed there and its home branch stopped advancing. That is the state this pass
    exists to clear, so it is the one it must not step over.

    That schedule is now the *only* caller. The workspace once carried a `Ship:
    Sync Worktrees (step 3)` task that hand-cranked the same plan, and a one-click
    duplicate of a job that already runs every fifteen minutes is a second owner
    for one tier's lifecycle -- the thing the box/checkout split exists to avoid.
    It was retired once this pass had been syncing every checkout unattended for
    long enough to prove it; `sweep.py --sync` remains reachable on the CLI.
    """
    try:
        registry = workspace.read_text(encoding="utf-8")
    except OSError as exc:
        # A pass that cannot read the registry has not decided anything, so it must not
        # report a clean sweep of the checkouts it never looked at.
        return 1, {"rows": [], "failed": True, "report": f"cannot read {workspace}: {exc}"}

    names = sweep.parse_workspace(registry)
    if not names:
        return 0, {"rows": [], "failed": False, "report": ""}

    results = sweep.sweep(workspace.parent, names, fetch=fetch)
    report, code = sweep.run_mode(workspace.parent, results, "sync", apply=apply, fetch=fetch)
    summary = checkout_sync_summary(results, code)
    summary["report"] = report
    return (1 if summary["failed"] else 0), summary


def reconcile(
    workspace: Path,
    *,
    apply: bool = False,
    automerge: bool = False,
    merge_label: str = "",
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    unclaimed_age_days: float = DEFAULT_UNCLAIMED_AGE_DAYS,
    fetch: bool = True,
    keep_stack: bool = False,
    checkouts: bool = True,
) -> tuple[int, dict]:
    """One unattended pass over the workspace: boxes reaped, checkouts brought current.

    This is the half of the ephemeral tier that makes it cost less attention than the
    sweep rather than the same. `reap --all` already skips boxes holding work, but
    something has to *run* it, and "something" was a human reading the session-start
    line — so a merged PR left its box, its branch, its port slot and its volume set in
    place until someone remembered. This closes that: PR merged, box gone, disk back.

    Ordered so a box can finish its whole life in a single pass. A PR that becomes
    mergeable is merged, and the merge updates the `PullRequest` in hand, so the same
    box is re-decided as `merged` and reaped immediately after — rather than waiting a
    further interval to notice what this pass just did.

    Disk pressure is measured **once, before anything is destroyed**, so the escalation
    is a property of the pass rather than something that switches on halfway through
    and treats the last boxes differently from the first.

    `unclaimed_age_days` is the one exit that does not go through a PR at all: a branch
    pushed by an interrupted `/ship`, for which GitHub affirmatively reports no PR, had
    no ending — `needs-pr` said *wait for the merge* about a merge nobody was going to
    make, and the box held its slot until a human found it. Past that age the checkout
    is reclaimed; the commits and the remote branch are untouched, so `resume` restores
    the box in full. It is deliberately far longer than `max_age_days`, because an open
    PR has a person attached to it and this state has nobody.

    The static checkouts follow, through `sync_checkouts`, unless `checkouts=False`.
    That half destroys nothing and is refused on anything holding work; it exists so a
    merged PR advances the local default branch without a person remembering to sweep.

    Returns `(exit_code, report)`. Non-zero only for a failure — a box that is holding
    work is this tool working, not failing, and a scheduled runner that reddened on one
    would be a runner whose alerts nobody reads. The same is true of a checkout the
    sync stepped over.
    """
    root = workspace.parent
    boxes = live_boxes(root)
    paused = on_hold_projects(workspace)
    free = free_gb(boxes_root(root) if boxes_root(root).is_dir() else root)
    pressure = under_pressure(free, min_free_gb)

    rows: list[tuple[Box, str, str, PullRequest, int]] = []
    landed: set[str] = set()
    for name, box in sorted(boxes.items()):
        state, verdict, reason = inspect_box(box, root, fetch=fetch)
        # A preview's PR is the one for the branch it is SHOWING. Looking it up by
        # `box.branch` would ask GitHub about the throwaway `preview/...` ref, get
        # nothing back, and leave the preview standing after the work it shows merged.
        subject = box.tracks or box.branch
        pr = (
            pr_for(sweep.gh_for(box_path(root, name)), subject)
            if fetch and subject and state.host == "github"
            else PullRequest()
        )
        # Asked only for the verdicts it can settle, and only of a clean box, so a pass
        # over sixteen boxes adds two git calls for the rare one rather than thirty-two
        # for the ordinary ones. Same gate as `plan_reap`'s, so `reap` and this pass
        # cannot describe one box differently.
        if work_landed(sweep.git_for(box_path(root, name)), state, verdict, pr):
            landed.add(name)
        rows.append((box, verdict, reason, pr, state.dirty))

    outcomes: list[dict] = []
    worst = 0
    for decision in reconcile_plan(
        rows,
        automerge=automerge,
        merge_label=merge_label,
        pressure=pressure,
        max_age_days=max_age_days,
        unclaimed_age_days=unclaimed_age_days,
        landed=landed,
    ):
        notes: list[str] = []
        action = decision.action
        pr = decision.pr

        if action == MERGE and apply:
            ok, message = merge_pr(sweep.gh_for(box_path(root, decision.box)), pr.number)
            notes.append(message if ok else f"[warn] {message}")
            if ok:
                # The merge is the fact that licenses the reap, so re-decide on it
                # rather than on the state read before the merge happened.
                pr = replace(pr, state="MERGED")
                action = REAP
            else:
                worst = 1

        if action == REAP:
            try:
                doomed = plan_reap(
                    decision.box, workspace, keep_stack=keep_stack, fetch=fetch, pr=pr
                )
            except WorktreeError as exc:
                notes.append(f"[warn] {exc}")
                worst = 1
                doomed = None
            if doomed is not None:
                if doomed.refusal:
                    # `reconcile_action` already cleared this box, so a refusal here is
                    # the two classifiers disagreeing — report it, never force past it.
                    notes.append(f"[warn] reap refused: {doomed.refusal}")
                    action = HOLD
                    worst = 1
                elif apply:
                    ok, reap_notes = apply_reap(doomed, workspace)
                    notes.extend(reap_notes)
                    if not ok:
                        worst = 1

        outcomes.append(
            reconcile_outcome(decision, action, pr, notes, boxes.get(decision.box), paused)
        )

    # Last, and after the reaps: a reap deletes its box's branch in the source
    # checkout, so the sync reads a checkout this pass has finished with.
    synced: dict = {}
    if checkouts:
        code, synced = sync_checkouts(workspace, apply=apply, fetch=fetch)
        worst = max(worst, code)

    menu = refresh_preview_menu(workspace, apply=apply, fetch=fetch)
    plug_menu = refresh_plug_menu(apply=apply)

    report = {
        "applied": apply,
        "free_gb": round(free, 1),
        "min_free_gb": min_free_gb,
        "pressure": pressure,
        "automerge": automerge,
        "boxes": outcomes,
        "checkouts": synced,
        "preview_menu": menu,
        "plug_menu": plug_menu,
    }
    return worst, report


def refresh_preview_menu(workspace: Path, *, apply: bool, fetch: bool = True) -> str:
    """Rebuild the preview tasks' dropdown options. The path written, or "" for anything else.

    A rider on this pass rather than a schedule of its own, and that is the whole design:
    the options file is what the two `Preview:` dropdowns read, `rioj7.command-variable`
    can only read a *file*, and the only writer used to be the previous click on one of
    those tasks. So the list a person picked from was however stale their last preview
    was -- open PRs missing for days, and boxes on branches that had long since merged
    still offered. This pass already runs every fifteen minutes, already fetches, and has
    just finished reaping exactly the boxes whose rows should go: it knows more about
    what belongs in that menu than any other scheduled thing on the machine.

    Loaded by path and INSIDE the function on purpose. `preview-task.py` imports this
    module, so importing it at the top of this one is a cycle; and it is hyphenated, so
    it cannot be a plain `import` either way. `worktree.py` is the lower layer of the two
    and this call is the one place the arrow points back, which is a good reason to keep
    it narrow and total -- never raising, and never affecting `worst`. A menu that could
    not be rebuilt is a stale dropdown; a reconcile that failed on one is a machine that
    stops reaping boxes because a *convenience* broke.
    """
    if not apply:
        return ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
        from _loader import load_by_path

        preview_task = load_by_path(
            "preview_task", Path(__file__).resolve().parent / "preview-task.py"
        )
        written = preview_task.refresh_menu(workspace, fetch=fetch)
    except Exception:
        return ""
    return str(written) if written else ""


def refresh_plug_menu(*, apply: bool) -> str:
    """Rebuild the plug/unplug checklist's options. The path written, or "" for anything else.

    The second rider on this pass, and it is here for the first one's reason with one
    addition of its own: those rows are pre-*ticked* from the workspace registry, so a
    stale file is not merely a short list but a checklist that disagrees with the state
    it claims to show -- and unticking a row is how a project leaves the registry.

    Takes no workspace: `plug-projects.py` resolves the live workspace file and its own
    `logs/` from module constants, so the checkout this module was loaded from is
    already the one whose menu gets written. Loaded by path and inside the function for
    the reason above it -- the name is hyphenated, and this is a convenience whose
    failure must never reach `worst`. `refresh_menu` is itself total and returns None
    rather than writing when `gh` could not be reached, so an outage leaves the previous
    menu in place instead of one offering to create repositories that already exist.
    """
    if not apply:
        return ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
        from _loader import load_by_path

        plug_projects = load_by_path(
            "plug_projects", Path(__file__).resolve().parent / "plug-projects.py"
        )
        written = plug_projects.refresh_menu()
    except Exception:
        return ""
    return str(written) if written else ""


# --- reporting --------------------------------------------------------------


def human_bytes(size: int) -> str:
    """`1536000000` -> `1.5 GB`. Base 1000, matching what a disk's label claims."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} GB"


def slot_summary(rows: list[dict], registry: devkit_ports.Registry | None) -> str:
    """`slots: 15/16 held -- ...` -- how close the workspace is to refusing the next box.

    Every row already prints the slot it holds and nothing printed how many were left,
    which is the number that decides whether the next box can be cut at all. A slot is
    held for a box's whole life, running or stopped, so a machine with an empty
    `docker ps` can still be one box from full -- and the tier's own refusal
    (`next_lease_slot`) is the first place anyone would find that out, which is the
    worst place.

    `ports.toml` cannot answer it either, and reading it is actively misleading: its
    `[slots]` table names only the pinned static checkouts. A reader told that some
    slots were "already allocated" went looking for eleven of them in a file that lists
    four, found nothing, and concluded the allocation was phantom. Both halves are named
    here for that reason.

    Empty string when there is no registry -- a workspace of stackless repos leases no
    slots, and a summary of nothing is a line that only has to be read past.
    """
    if registry is None:
        return ""
    # No range filter on the pins: `devkit_ports.validate` refuses to load a registry
    # whose `[slots]` names a slot outside `[0, max_slots)`, so one cannot reach here.
    pinned = set(registry.slots.values())
    leased = {row["slot"] for row in rows if row.get("slot", -1) >= 0}
    free = [slot for slot in range(registry.max_slots) if slot not in pinned | leased]
    held = registry.max_slots - len(free)
    tail = (
        f"free: {', '.join(str(slot) for slot in free)}"
        if free
        else "free: none -- the next box that needs a stack cannot be cut until one is released"
    )
    return (
        f"slots: {held}/{registry.max_slots} held -- "
        f"{len(pinned)} pinned to checkouts in {devkit_ports.REGISTRY_NAME}, "
        f"{len(leased)} leased by boxes below; {tail}"
    )


def reconcile_outcome(
    decision: Reconciliation,
    action: str,
    pr: PullRequest,
    notes: list[str],
    held_box: Box | None,
    paused: frozenset[str],
) -> dict:
    """One row of the reconcile report, for the box `decision` is about.

    `stranded` is a HOLD the tier has stopped expecting anyone to finish. Not an
    action: the box is still held, exactly as before. What changes is the report,
    which files it under a heading whose remedy is `/triage-boxes` rather than under
    "ship it", addressed to a person who does not.
    """
    age = box_age_days(held_box.created) if held_box else 0.0
    return {
        "box": decision.box,
        "action": action,
        "reason": decision.reason,
        "verdict": decision.verdict,
        "pr": pr.number or None,
        "pr_url": pr.url,
        "pr_state": pr.state,
        "checks": pr.checks,
        "age_days": round(age, 2),
        "stranded": stranded(
            action,
            pr_open=pr.is_open,
            age_days=age,
            on_hold=bool(held_box and held_box.project in paused),
        ),
        "notes": notes,
    }


def survey_sections(rows: list[dict]) -> list[str]:
    """The two lists under the survey table: stranded boxes, then boxes still held.

    Stranded comes first and apart, with `/triage-boxes` as its remedy: a "not reapable
    yet" list reads as "ship these" to whoever is in the box, and a stranded box has
    nobody in it.
    """
    lost = [row for row in rows if not row["reapable"] and row.get("stranded")]
    held = [row for row in rows if not row["reapable"] and not row.get("stranded")]
    stranded_heading = (
        f"stranded -- older than {STRANDED_AGE_DAYS:g}d with no PR, or for a project on "
        f"hold; /triage-boxes ships or deletes each"
    )
    lines: list[str] = []
    for group, heading in ((lost, stranded_heading), (held, "not reapable yet")):
        if not group:
            continue
        lines.append("")
        lines.append(f"{len(group)} box(es) {heading}:")
        lines.extend(f"  {row['box']} [{row['verdict']}] -- {row['reason']}" for row in group)
    return lines


def render_survey(rows: list[dict], registry: devkit_ports.Registry | None = None) -> str:
    if not rows:
        return "No ephemeral boxes. `worktree.py new <project>` cuts one."
    sized = any("bytes" in row for row in rows)
    header = ("BOX", "BRANCH", "SLOT", "AGE", "VERDICT", "REAPABLE")
    table = [(*header, "SIZE") if sized else header]
    for row in rows:
        cells = (
            row["box"],
            row["branch"] or "-",
            str(row["slot"]) if row["slot"] >= 0 else "-",
            f"{row.get('age_days', 0):.1f}d",
            row["verdict"],
            "yes" if row["reapable"] else "no",
        )
        table.append((*cells, human_bytes(row["bytes"])) if sized else cells)
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    previews = [row for row in rows if row.get("kind") == PREVIEW_KIND]
    if previews:
        lines.append("")
        lines.append(f"{len(previews)} preview(s) — someone else's branch, held for review:")
        for row in previews:
            best = primary_url(row.get("urls", ()))
            where = f" -> {best}" if best else ""
            lines.append(f"  {row['box']} shows {row.get('tracks') or row['branch']}{where}")
    lines.extend(survey_sections(rows))
    summary = slot_summary(rows, registry)
    if summary:
        lines.append("")
        lines.append(summary)
    return "\n".join(lines)


RECONCILE_LOG = "logs/reconcile.log"


def artifact_root(root: Path, repo_root: Path | None = None) -> Path:
    """Where a run's `logs/` artifacts belong: with the workspace the run acted on.

    `REPO_ROOT` is bound at import to whichever devkit copy is executing, so every CLI
    path wrote its artifacts *here* regardless of which workspace it was pointed at.
    That is right for the scheduled job, whose workspace is this one, and wrong for
    every other caller -- and the caller it was most wrong for is the test suite. A
    `pytest tests/ -q` run drove `main(["reconcile", ...])` and `main(["reap", ...])`
    against a `tmp_path` workspace, overwrote this machine's real `reconcile.log` with
    `No ephemeral boxes and no checkouts to sync` at a moment when a box had been live
    for five minutes, and appended three fixture rows to the append-only reap ledger.

    Not cosmetic, because of what reads them: `workspace-status.scheduler_line` takes
    `reconcile.log`'s timestamp as its evidence *precisely because* `schtasks` reports a
    disabled task as healthy. A test run therefore made a dead scheduler look alive --
    the exact failure that line was written to catch.

    So the artifacts follow the workspace. This repo keeps them whenever it is inside the
    workspace being acted on, which is every real invocation; a workspace somewhere else
    gets its own `logs/`, and nothing a test drives can reach this one. `repo_root` is
    resolved at call time rather than defaulted, so patching `REPO_ROOT` still works.
    """
    here = REPO_ROOT if repo_root is None else repo_root
    try:
        if here.resolve().is_relative_to(root.resolve()):
            return here
    except (OSError, ValueError):
        return here
    return root


def write_reconcile_log(rendered: str, code: int, root: Path = REPO_ROOT) -> Path | None:
    """Persist a reconcile pass to `logs/reconcile.log`. Returns the path, or None.

    The scheduled run is **windowless** (`pythonw.exe`, so no console flashes up every
    fifteen minutes), which means its stdout goes nowhere at all. Without this the one
    thing in the workspace that destroys checkouts unattended would have no record of
    what it did, and a run that started failing would fail in complete silence.

    Overwritten per run and written on success as well as failure, per the
    failure-artifact rule in `.claude/rules/engineering.md`: a log that only appears on
    failure is one you cannot distinguish from a job that never ran.

    Best-effort — a reconcile pass that did its work must not report failure because a
    log file could not be written.
    """
    path = root / RECONCILE_LOG
    stamp = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# devkit worktree reconcile\n# {stamp}  exit={code}\n\n{rendered}\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError:
        return None
    return path


def outcome_heading(row: dict) -> str:
    """Which heading of the reconcile report a row is filed under.

    Stranded holds get a heading of their own, ahead of the ordinary ones: a HOLD says
    "ship it" to whoever is in the box, and a stranded box has nobody in it.
    """
    if row["action"] == HOLD and row.get("stranded"):
        return STRANDED
    return row["action"]


def render_reconcile(report: dict) -> str:
    """The reconcile report: what changed, what is waiting, and what needs you.

    Ordered by who has to act, not by box name. `hold` comes first because it is the
    only line in the whole tier that is a request -- everything else is either already
    done or waiting on GitHub, and a report that buries the one actionable item under
    nine informational ones is a report that trains you to skim past it.
    """
    applied = report.get("applied")
    outcomes = report.get("boxes") or []
    checkouts = report.get("checkouts") or {}
    if not outcomes and not checkouts.get("rows"):
        return "No ephemeral boxes and no checkouts to sync. Nothing to reconcile."

    by_action: dict[str, list[dict]] = {}
    for row in outcomes:
        by_action.setdefault(outcome_heading(row), []).append(row)

    verb = "Reconciled" if applied else "Would reconcile"
    lines = [f"{verb} {len(outcomes)} box(es)."]

    free = report.get("free_gb")
    if isinstance(free, int | float) and free >= 0:
        note = " -- RECLAIMING (open PRs reaped too)" if report.get("pressure") else ""
        lines.append(f"  disk: {free:.1f} GB free, floor {report.get('min_free_gb')} GB{note}")
    if not report.get("automerge"):
        lines.append("  auto-merge: off -- merging a green PR is yours to do")

    headings = (
        (
            STRANDED,
            f"stranded -- held for over {STRANDED_AGE_DAYS:g}d with no PR, or for a project "
            f"on hold; /triage-boxes ships or deletes each",
        ),
        (HOLD, "holding work -- only place it exists, ship it"),
        (MERGE, "merge"),
        (REAP, "reaped" if applied else "would reap"),
        (WAIT, "waiting"),
    )
    for action, heading in headings:
        rows = by_action.get(action)
        if not rows:
            continue
        lines.append(f"\n  {heading}:")
        for row in rows:
            url = f"  {row['pr_url']}" if row.get("pr_url") else ""
            lines.append(f"    {row['box']} -- {row['reason']}{url}")
            lines.extend(f"        {note}" for note in row.get("notes") or [])
    lines.extend(render_checkout_sync(checkouts, applied=bool(applied)))
    if applied:
        menu = report.get("preview_menu")
        lines.append(
            f"  preview menu: refreshed ({menu})"
            if menu
            else "  preview menu: [warn] not refreshed -- the Preview: dropdowns are stale"
        )
        plug_menu = report.get("plug_menu")
        lines.append(
            f"  plug menu: refreshed ({plug_menu})"
            if plug_menu
            else "  plug menu: [warn] not refreshed -- the Plug / Unplug checklist is stale"
        )
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


def render_checkout_sync(summary: dict, *, applied: bool) -> list[str]:
    """The static tier's section of the reconcile report: a count, then the exceptions.

    Deliberately one line when all is well. This runs every fifteen minutes and the
    healthy answer -- "four checkouts, all current" -- is the answer almost every time;
    printed as four rows it becomes something a reader learns to skip, and the box
    section above it is what they actually opened the log for.

    So only two things get their own lines, and both are requests: a checkout the sync
    stepped over because it is holding work, and a failure. The failure case carries
    `sweep`'s own report verbatim rather than a summary of it -- this is the one moment
    the log has to be enough to diagnose from, and the git command that failed is in
    there.
    """
    rows = summary.get("rows") or []
    if not rows:
        return []
    held = [row for row in rows if row.get("held")]
    verb = "synced" if applied else "would sync"
    lines = [f"\n  checkouts: {len(rows) - len(held)} {verb}, {len(held)} holding work"]
    for row in held:
        lines.append(f"    {row['checkout']} [{row['verdict']}] -- {row['reason']}")
    if summary.get("failed"):
        lines.append("    [warn] a step failed -- sweep's own report follows")
        lines.extend(f"    {line}" for line in (summary.get("report") or "").splitlines())
    return lines


def reap_argument_faults(box: str, every: bool, force: bool) -> list[str]:
    """Why this `reap` invocation is refused before anything is inspected; [] when it is fine.

    `--all --force` is the one worth spelling out. `--force` on a named box is a decision
    about *that* box, made by someone who just read its refusal. Applied to a sweep it is
    a decision about boxes the caller has not looked at yet, and its blast radius is every
    uncommitted change in every one of them — which is exactly the "cleanup command
    destroyed my work" outcome `reap_decision` is built to make impossible.
    """
    faults = []
    if every and box:
        faults.append(f"pass a box name or --all, not both (got {box!r} and --all)")
    if not every and not box:
        faults.append("name a box to reap, or pass --all")
    if every and force:
        faults.append(
            "--all never forces. Forcing is a per-box decision made after reading that "
            "box's refusal; reap the ones holding work by name."
        )
    return faults


def render_provision(
    box: str, steps: tuple[ProvisionStep, ...], applied: bool, notes: list[str]
) -> str:
    if not steps:
        return (
            f"{box}: nothing to install — no uv.lock, requirements-dev.txt or pyproject.toml, "
            f"and no [python] install_command in .devkit.toml"
        )
    lines = [f"{'Provisioned' if applied else 'Would provision'} {box}"]
    for n, step in enumerate(steps, 1):
        lines.append(f"    {n}. {step.shell_command or ' '.join(step.argv)}")
    lines.extend(f"  {note}" for note in notes)
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


def render_spawn(plan: SpawnPlan, applied: bool, notes: list[str]) -> str:
    if plan.resumed:
        verb = "Resumed" if applied else "Would resume"
    else:
        verb = "Created" if applied else "Would create"
    lines = [f"{verb} {plan.box.name}"]
    lines.append(f"  path    {plan.path}")
    lines.append(f"  branch  {plan.box.branch}")
    # A slot of -1 has two very different meanings and the renderer used to give both
    # the reassuring one: a stackless project asks for no lease, while a *stack* project
    # that could not get one has a box whose `compose up` is refused.
    if plan.box.slot >= 0:
        slot = str(plan.box.slot)
    elif plan.slotless:
        slot = "- (NONE FREE -- this box cannot start its stack)"
    else:
        slot = "- (no Docker tier)"
    lines.append(f"  slot    {slot}")
    for n, step in enumerate(plan.steps, 1):
        lines.append(f"    {n}. git -C {plan.box.project} {' '.join(step)}")
    if plan.env:
        lines.append("  env     " + ", ".join(f"{k}={v}" for k, v in sorted(plan.env.items())))
    for install in plan.provision:
        rendered = install.shell_command or " ".join(install.argv)
        lines.append(f"  install {install.label}: {rendered}")
    lines.extend(f"  {note}" for note in notes)
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


def render_preview(plan: PreviewPlan, applied: bool, notes: list[str]) -> str:
    if plan.refusal:
        return f"{plan.box.name}: refused -- {plan.refusal}"
    subject = plan.box.tracks or plan.box.branch
    verb = "Previewing" if applied else "Would preview"
    lines = [f"{verb} {subject} in {plan.box.name}"]
    lines.append(f"  path    {plan.path}")
    lines.append(f"  slot    {plan.box.slot if plan.box.slot >= 0 else '- (no Docker tier)'}")
    steps = 0
    for step in plan.spawn.steps if plan.spawn else ():
        steps += 1
        lines.append(f"    {steps}. git -C {plan.box.project} {' '.join(step)}")
    for step in plan.refresh:
        steps += 1
        lines.append(f"    {steps}. git -C {plan.box.name} {' '.join(step)}")
    if plan.down:
        lines.append(f"    {steps + 1}. docker compose -p {plan.box.name} down -v --remove-orphans")
    elif plan.up:
        scope = f" --no-deps {' '.join(plan.box.services)}" if plan.box.services else ""
        lines.append(f"    {steps + 1}. docker compose -p {plan.box.name} up -d --build{scope}")
    if plan.warning:
        lines.append(f"  [warn] {plan.warning}")
    if plan.urls and not plan.down:
        lines.append("  open")
        for service, port, url in plan.urls:
            lines.append(f"    {service:<15} {url or f'localhost:{port}'}")
    lines.extend(f"  {note}" for note in notes)
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


def render_reap(plan: ReapPlan, applied: bool, notes: list[str]) -> str:
    if plan.refusal:
        return f"{plan.box}: refused -- {plan.refusal}"
    lines = [f"{'Reaped' if applied else 'Would reap'} {plan.box}"]
    if plan.warning:
        lines.append(f"  [warn] {plan.warning}")
    first = 1
    if plan.stack_down:
        first = 2
        lines.append(f"    1. docker compose -p {plan.box} down -v --remove-orphans")
    for n, step in enumerate(plan.steps, first):
        lines.append(f"    {n}. git -C {plan.project} {' '.join(step)}")
    lines.extend(f"  {note}" for note in notes)
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


# --- entrypoint -------------------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """The flags every mode takes, added to each SUBparser rather than the top level.

    Deliberately not shared through `parents=`, and deliberately not on the top-level
    parser. argparse only accepts a top-level option *before* the subcommand, so
    `worktree.py new demo --yes` — the spelling this tool's own docstring, its `--help`
    epilog and the guard hook's block message all use — was rejected with
    "unrecognized arguments: --yes", after the dry run had already printed a plan that
    looked like it was about to run. `parents=` fixes the position but reintroduces the
    defaults through the back door: a subparser copy re-applies its own default over a
    value already parsed, so `--yes` before the subcommand would be silently undone.

    One function called per subparser is the version with neither failure mode.
    """
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print what this would do and change nothing (the default)",
    )
    apply_mode.add_argument("--yes", dest="dry_run", action="store_false", help="actually run it")
    fetch_mode = parser.add_mutually_exclusive_group()
    fetch_mode.add_argument("--fetch", dest="fetch", action="store_true", default=True)
    fetch_mode.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="skip the network (a `new` box may start from a stale base)",
    )


def _add_rescue_parser(sub: argparse._SubParsersAction) -> None:
    rescuer = sub.add_parser(
        "rescue",
        help="move a stranded box's work onto a fresh task branch, and --ship it",
    )
    rescuer.add_argument("box")
    rescuer.add_argument(
        "--ship",
        action="store_true",
        help="after the move, commit the box's work, push the branch and open its PR",
    )
    rescuer.add_argument(
        "--message",
        default="",
        help="commit subject and PR title for --ship (default: a mechanical rescue(...) line)",
    )
    add_common_args(rescuer)


def _run_rescue(args: argparse.Namespace) -> int:
    ok, rescued, notes = rescue(
        args.workspace,
        args.box,
        apply=not args.dry_run,
        ship=args.ship,
        message=args.message,
        fetch=args.fetch,
    )
    if args.json:
        payload = {
            "plan": asdict(rescued),
            "applied": not args.dry_run,
            "ok": ok,
            "notes": notes,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_rescue(rescued, applied=not args.dry_run, notes=notes))
    return 0 if ok else 1


def provision_target(root: Path, name: str) -> tuple[str, Path]:
    """The tree `provision <name>` means: a live box, or a static checkout.

    A box wins the name, because it is the common case and the one this verb was written
    for. A checkout was not reachable at all, and nothing else in the workspace
    provisions one: `new` provisions the box it just cut, and `session-start.sh` returns
    early on a local machine precisely because a static checkout is provisioned once by
    hand. So a freshly cloned project had no verb -- `plan_provision` already took a
    plain path, and only this resolver insisted on a lease.

    A directory with no repository in it is refused rather than provisioned: a mistyped
    box name would otherwise be read as "some empty folder", and the install ladder would
    run its whole length in a tree with nothing to install.
    """
    boxes = live_boxes(root)
    box = boxes.get(name)
    if box is not None:
        return box.name, box_path(root, box.name)
    candidate = Path(name)
    path = candidate if candidate.is_absolute() else root / name
    if (path / ".git").exists():
        return path.name, path
    known = ", ".join(sorted(boxes)) or "(none)"
    raise WorktreeError(f"no live box or checkout called {name!r}; live boxes: {known}")


def _run_provision(args: argparse.Namespace) -> int:
    root = args.workspace.parent
    name, path = provision_target(root, args.box)
    steps = plan_provision(path)
    notes: list[str] = []
    ok = True
    if steps and not args.dry_run:
        ok, notes = run_provision(path, steps)
    if args.json:
        payload = {
            "box": name,
            "steps": [asdict(step) for step in steps],
            "applied": not args.dry_run,
            "ok": ok,
            "notes": notes,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_provision(name, steps, applied=not args.dry_run, notes=notes))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    new = sub.add_parser("new", help="cut a fresh box for a project")
    new.add_argument("project")
    new.add_argument("--slug", default="", help="topic for the branch name (default: the project)")
    new.add_argument("--session", default="", help="tag the lease with an agent session id")
    new.add_argument(
        "--auto",
        action="store_true",
        help=f"an unattended job cut this, not a session: name the branch {tb.AUTOMATION_PREFIX}... "
        "so a UI-review menu can leave it out",
    )
    install = new.add_mutually_exclusive_group()
    install.add_argument(
        "--provision",
        dest="provision",
        action="store_true",
        default=True,
        help="install the box's toolchain after cutting it (the default)",
    )
    install.add_argument(
        "--no-provision",
        dest="provision",
        action="store_false",
        help="cut the box only; its tests and /ship will not run until it is provisioned",
    )
    add_common_args(new)

    back = sub.add_parser(
        "resume", help="put a box back on an existing branch whose own box is gone"
    )
    back.add_argument("project")
    where = back.add_mutually_exclusive_group()
    where.add_argument("--branch", default="", help="the branch to check out, as it is on origin")
    where.add_argument("--pr", type=int, default=0, help="resume the head branch of this PR")
    back.add_argument(
        "--session",
        default="",
        help="lease the box to this session; with neither --branch nor --pr, "
        "the branch is read from the reap ledger",
    )
    reinstall = back.add_mutually_exclusive_group()
    reinstall.add_argument(
        "--provision",
        dest="provision",
        action="store_true",
        default=True,
        help="install the box's toolchain after cutting it (the default)",
    )
    reinstall.add_argument(
        "--no-provision",
        dest="provision",
        action="store_false",
        help="cut the box only; its tests and /ship will not run until it is provisioned",
    )
    add_common_args(back)

    survey_parser = sub.add_parser("list", help="every live box and whether it can be reaped")
    survey_parser.add_argument(
        "--sizes",
        action="store_true",
        help="also measure each box on disk (walks .venv/node_modules — slow)",
    )
    add_common_args(survey_parser)

    fix = sub.add_parser(
        "reconcile",
        help="unattended pass: reap boxes whose PR merged, reclaim disk, report the rest",
    )
    # `--no-merge` is the default and looks redundant, and is not: the workspace picker
    # feeding this must supply one real token in every branch, because an empty string
    # reaches argparse as a stray positional and is rejected. Same reason
    # `new-project.py` carries `--dry-run` alongside `--yes`.
    merging = fix.add_mutually_exclusive_group()
    merging.add_argument(
        "--merge",
        dest="automerge",
        action="store_true",
        default=False,
        help="also squash-merge open PRs whose gate is green (off by default)",
    )
    merging.add_argument(
        "--no-merge",
        dest="automerge",
        action="store_false",
        help="clean up only; merging a PR stays a human decision (the default)",
    )
    fix.add_argument(
        "--merge-label",
        default="",
        help="with --merge, only merge PRs carrying this label (default: any green PR)",
    )
    fix.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_GB,
        help=(
            f"free-space floor; at or under it, boxes with an OPEN PR are reaped too "
            f"(default {DEFAULT_MIN_FREE_GB:g})"
        ),
    )
    fix.add_argument(
        "--max-age-days",
        type=float,
        default=DEFAULT_MAX_AGE_DAYS,
        help=(
            f"reap a box whose PR has been open longer than this, without waiting for "
            f"disk pressure (default {DEFAULT_MAX_AGE_DAYS:g})"
        ),
    )
    fix.add_argument(
        "--unclaimed-age-days",
        type=float,
        default=DEFAULT_UNCLAIMED_AGE_DAYS,
        help=(
            f"reclaim a box this old whose branch is pushed and for which GitHub reports "
            f"no PR at all -- the commits and the remote branch survive, so `resume` "
            f"brings it back (default {DEFAULT_UNCLAIMED_AGE_DAYS:g})"
        ),
    )
    fix.add_argument("--keep-stack", action="store_true", help="leave Docker stacks running")
    # Paired with its negation for the same reason `--no-merge` is: the scheduled
    # command names every knob it passes, so `schtasks /query` shows what is on.
    static = fix.add_mutually_exclusive_group()
    static.add_argument(
        "--checkouts",
        dest="checkouts",
        action="store_true",
        default=True,
        help="also run sweep.py --sync over the static checkouts (the default)",
    )
    static.add_argument(
        "--no-checkouts",
        dest="checkouts",
        action="store_false",
        help="boxes only; static checkouts stay wherever they are parked",
    )
    add_common_args(fix)

    look = sub.add_parser(
        "preview", help="run someone else's branch or PR in a box of its own, and open it"
    )
    look.add_argument("target", help="a project (with --pr/--branch), or a live box name")
    ref = look.add_mutually_exclusive_group()
    ref.add_argument("--pr", type=int, default=0, help="preview the head branch of this PR")
    ref.add_argument("--branch", default="", help="preview this branch as it is on origin")
    stack = look.add_mutually_exclusive_group()
    stack.add_argument(
        "--up",
        dest="up",
        action="store_true",
        default=True,
        help="bring the box's compose stack up afterwards (the default)",
    )
    stack.add_argument(
        "--no-up",
        dest="up",
        action="store_false",
        help="check the ref out and print its ports; start nothing",
    )
    look.add_argument(
        "--ui",
        action="store_true",
        help="start only the project's [worktree] ui_services, borrowing the backend "
        "from the static checkout's running stack — the cheap preview for a UI change",
    )
    look.add_argument(
        "--down", action="store_true", help="stop this preview's stack, keeping the box"
    )
    look.add_argument(
        "--provision",
        action="store_true",
        help="also install the host toolchain (a compose stack does not need it)",
    )
    look.add_argument(
        "--force",
        action="store_true",
        help="refresh a preview box that has uncommitted edits, discarding them",
    )
    add_common_args(look)

    provision = sub.add_parser("provision", help="install an existing box's toolchain")
    provision.add_argument("box")
    add_common_args(provision)

    takeover = sub.add_parser(
        "claim", help="re-lease a box to another session (a sanctioned takeover)"
    )
    takeover.add_argument("box")
    takeover.add_argument(
        "--session", required=True, help="the full session id taking the box over"
    )
    takeover.add_argument(
        "--force",
        action="store_true",
        help="claim a box that still holds uncommitted work (two owners, one tree)",
    )
    add_common_args(takeover)

    _add_rescue_parser(sub)

    reap = sub.add_parser("reap", help="destroy a box once its work has shipped")
    # Optional so `--all` can stand alone, and checked below rather than through
    # `nargs="?"` alone: argparse cannot express "exactly one of a positional and a flag",
    # and a `reap` with neither would otherwise reap a box called "".
    reap.add_argument("box", nargs="?", default="")
    reap.add_argument(
        "--all",
        dest="every",
        action="store_true",
        help="reap every box whose work has left it; skips the rest and says which",
    )
    reap.add_argument(
        "--force",
        action="store_true",
        help="discard uncommitted changes; never destroys commits (see branch_delete_flag)",
    )
    reap.add_argument("--keep-stack", action="store_true", help="leave the Docker stack running")
    add_common_args(reap)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.workspace.is_file():
        print(f"worktree: no workspace file at {args.workspace}", file=sys.stderr)
        return 2
    # Every path this tool prints is derived from the workspace's parent, and most of
    # them are paths someone else has to act on -- an agent re-issuing an edit, a `cd`
    # before /ship. A relative `--workspace` made all of them relative to a cwd the
    # reader does not necessarily share.
    args.workspace = args.workspace.resolve()

    try:
        if args.mode == "list":
            rows = survey(args.workspace, fetch=args.fetch, sizes=args.sizes)
            print(
                json.dumps(rows, indent=2)
                if args.json
                else render_survey(rows, load_registry(args.workspace))
            )
            return 0

        if args.mode == "reconcile":
            code, report = reconcile(
                args.workspace,
                apply=not args.dry_run,
                automerge=args.automerge,
                merge_label=args.merge_label,
                min_free_gb=args.min_free_gb,
                max_age_days=args.max_age_days,
                unclaimed_age_days=args.unclaimed_age_days,
                fetch=args.fetch,
                keep_stack=args.keep_stack,
                checkouts=args.checkouts,
            )
            rendered = json.dumps(report, indent=2) if args.json else render_reconcile(report)
            print(rendered)
            write_reconcile_log(rendered, code, artifact_root(args.workspace.parent))
            return code

        if args.mode == "new":
            plan = plan_new(
                args.project,
                args.workspace,
                slug=args.slug or args.project,
                session=args.session,
                fetch=args.fetch,
                branch_prefix=tb.AUTOMATION_PREFIX if args.auto else tb.BRANCH_PREFIX,
            )
            notes: list[str] = []
            ok = True
            if not args.dry_run:
                ok, notes = apply_new(plan, args.workspace, provision=args.provision)
            if args.json:
                print(
                    json.dumps(
                        {
                            "box": asdict(plan.box),
                            "path": plan.path,
                            "env": plan.env,
                            "applied": not args.dry_run,
                            "ok": ok,
                            "notes": notes,
                        },
                        indent=2,
                    )
                )
            else:
                print(render_spawn(plan, applied=not args.dry_run, notes=notes))
            return 0 if ok else 2

        if args.mode == "resume":
            plan = plan_resume(
                args.project,
                args.workspace,
                branch=args.branch,
                pr=args.pr,
                session=args.session,
                fetch=args.fetch,
            )
            notes = []
            ok = True
            if not args.dry_run:
                ok, notes = apply_new(plan, args.workspace, provision=args.provision)
            if args.json:
                print(
                    json.dumps(
                        {
                            "box": asdict(plan.box),
                            "path": plan.path,
                            "env": plan.env,
                            "resumed": plan.resumed,
                            "applied": not args.dry_run,
                            "ok": ok,
                            "notes": notes,
                        },
                        indent=2,
                    )
                )
            else:
                print(render_spawn(plan, applied=not args.dry_run, notes=notes))
            return 0 if ok else 2

        if args.mode == "preview":
            pv = plan_preview(
                args.target,
                args.workspace,
                branch=args.branch,
                pr=args.pr,
                fetch=args.fetch,
                provision=args.provision,
                up=args.up and not args.down,
                down=args.down,
                force=args.force,
                ui=args.ui,
            )
            notes = []
            ok = not pv.refusal
            if ok and not args.dry_run:
                ok, notes = apply_preview(pv, args.workspace)
            applied = not args.dry_run and not pv.refusal
            if args.json:
                print(
                    json.dumps(
                        {
                            "box": asdict(pv.box),
                            "path": pv.path,
                            "urls": [list(entry) for entry in pv.urls],
                            "refusal": pv.refusal,
                            "warning": pv.warning,
                            "applied": applied,
                            "ok": ok,
                            "notes": notes,
                        },
                        indent=2,
                    )
                )
            else:
                print(render_preview(pv, applied=applied, notes=notes))
            return 0 if ok else 1

        if args.mode == "claim":
            claimed = claim_box(
                args.workspace,
                args.box,
                args.session,
                apply=not args.dry_run,
                force=args.force,
            )
            if args.json:
                print(json.dumps({"box": asdict(claimed), "applied": not args.dry_run}, indent=2))
            else:
                verb = "would be leased" if args.dry_run else "now leased"
                print(f"{claimed.name} {verb} to session {claimed.session}")
                if args.dry_run:
                    print("\nDry run -- nothing was changed. Re-run with --yes to apply.")
            return 0

        if args.mode == "rescue":
            return _run_rescue(args)
        if args.mode == "provision":
            return _run_provision(args)

        for problem in reap_argument_faults(args.box, args.every, args.force):
            print(f"worktree: {problem}", file=sys.stderr)
            return 2

        targets = sorted(live_boxes(args.workspace.parent)) if args.every else [args.box]
        results = []
        worst = 0
        for name in targets:
            doomed = plan_reap(
                name,
                args.workspace,
                force=args.force,
                keep_stack=args.keep_stack,
                fetch=args.fetch,
            )
            notes = []
            ok = not doomed.refusal
            if ok and not args.dry_run:
                ok, notes = apply_reap(doomed, args.workspace)
            applied = not args.dry_run and not doomed.refusal
            # A box that is holding work is the tool working, not failing. Naming one box
            # exits 1 on a refusal because the caller asked for that box specifically;
            # `--all` is a pass over everything reapable, so the boxes it steps over are
            # the expected case and must not redden a batch that did its job.
            if not ok and not (args.every and doomed.refusal):
                worst = 1
            results.append((doomed, applied, notes))
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "box": doomed.box,
                            "refusal": doomed.refusal,
                            "warning": doomed.warning,
                            "applied": applied,
                            "ok": not doomed.refusal,
                            "notes": notes,
                        }
                        for doomed, applied, notes in results
                    ]
                    if args.every
                    else {
                        "box": results[0][0].box,
                        "refusal": results[0][0].refusal,
                        "warning": results[0][0].warning,
                        "applied": results[0][1],
                        "ok": worst == 0,
                        "notes": results[0][2],
                    },
                    indent=2,
                )
            )
        elif args.every and not results:
            print("No ephemeral boxes to reap.")
        else:
            print(
                "\n\n".join(
                    render_reap(doomed, applied=applied, notes=notes)
                    for doomed, applied, notes in results
                )
            )
        return worst
    except (WorktreeError, devkit_project.ProjectError, devkit_ports.RegistryError) as exc:
        print(f"worktree: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
