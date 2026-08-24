#!/usr/bin/env python3
"""Pick a branch to look at, and bring its stack up. Backs "Preview: Open a UI Branch".

`worktree.py preview` already does the hard half -- cut a box on a copy of a ref, seed
its port lease, start its compose stack, print the URLs the slot publishes. What it
does not do is answer the question a reviewer actually arrives with, which is not
"preview `agent/comic-book-ui-0820`" but **"show me the thing I asked for"**. Getting
from one to the other meant knowing the ref, knowing the flag that takes it, and knowing
the tool existed at all -- three things that have to be true at once, in a session that
is usually already over. So the reviewing half kept being delegated to whichever agent
had done the work, who spun a dev server up by hand, or did not, or did and never said
on which port.

This is that half, as one clickable task: enumerate everything on this machine that
could be looked at, print it as a numbered menu, and preview the row that gets picked.

**The menu is the point, not the previewing.** Four sources feed it, ranked by how
close each already is to being on screen:

  1. **Preview boxes already standing.** The cheapest row, and the one that fixes the
     failure that prompted this script: a reboot stops every container, and Docker
     Desktop's own autostart brings back only what it feels like. The box, its branch
     and its port lease all survived -- so re-serving it is a `compose up` and nothing
     else, and it lands back on the same port it had before.
  2. **Live task boxes.** The agent's own worktree, still here, still holding the work.
     Preview serves one AS IT IS -- never resetting it, per `preview_refresh_decision`
     -- so this is how you look at a change that has not been pushed yet.
  3. **Open pull requests**, via `gh`. The review-before-merge case, and the only source
     that works when the machine that made the change was not this one.
  4. **Recent `agent/...` branches on origin** that none of the above already covers,
     minus the ones an unattended job cut for itself -- see `IGNORED_REF_PREFIXES`.

A row that several sources agree on is merged, not repeated: a standing preview of the
head branch of PR #164 is one row that says both. `merge_candidates` owns that, and is
pure, because "the same branch reached by two routes" is exactly the shape that is
tedious to reproduce by hand and cheap to assert.

Usage:
    python preview-task.py                 # menu, then preview the row you pick
    python preview-task.py --list          # print the menu and exit (agents: --json)
    python preview-task.py --pick 3        # take row 3 without asking
    python preview-task.py --all           # re-serve every standing preview (post-reboot)
    python preview-task.py --down          # menu, then STOP the picked row's stack
    python preview-task.py --pick-ref carameli:agent/comic-book-ui-0820  # what the task sends
    python preview-task.py --refresh       # rebuild the dropdown's option file and exit
    python preview-task.py --pick 3 --no-wait  # return when the containers start, not
                                               # when what they serve answers

**The VS Code task asks with two dropdowns, and this menu is what it falls back to.**
Typing a row number into a terminal is the wrong verb for a thing every real caller
reaches by clicking a task, so the task resolves one `${input:...}` -- a checkout, then
the refs belonging to it -- and sends the answer as `--pick-ref <project>:<ref>`. The
colon is a safe separator rather than a hopeful one: `git check-ref-format` refuses a ref
that contains one.

The extension that draws those lists (`rioj7.command-variable`) cannot run a command to
build them; it can only read a **file**. So `write_menu` saves one on every run of this
script, which makes the dropdown the previous scan rather than the current one. On a
machine that has never run this, `--refresh` writes the file and picks nothing -- and so
does `Preview: Restart Standing Previews`, which is the task that asks no question.

That is admitted rather than hidden. Every checkout's list ends in a `Rescan` row whose
description carries the timestamp the list was built at, and picking it lands in the
terminal menu below with the scan already done -- `main` collects before it looks at the
pick, so the file is rewritten either way. "The branch I just pushed is missing" costs
one click and never dead-ends. A ref picked from a row this scan no longer produces is
still served, as a plain branch: a stale row is a good guess about a ref, and
`worktree.py` is the half that knows whether one resolves.

Interactive when nothing has picked for it, which is unusual for this directory and is
why the prompt is written the way it is: the task runs under `log-wrap.py`, whose
`stream()` gives this process a **pipe** for stdout while leaving stdin inherited. A
prompt that does not end in a newline therefore sits in the pipe's buffer and never
reaches the terminal, and the user waits at what looks like a hung task. Every prompt
here is a whole flushed line for that reason. When stdin is not there at all -- an
agent, a scheduled run -- reading it returns EOF, and that is reported as "nothing was
picked" with the non-interactive spelling, never as an error.

Devkit-scoped (`DEVKIT_ONLY` in `devkit_project.py`) though it previews other projects:
the machine has one box registry and one port registry, so the *task* is owned by no
single checkout. The checkout is a dimension inside the answer instead -- a column in
the terminal menu, and the first of the two dropdowns. Restricting the sources to
checkouts with a compose stack is what keeps devkit itself -- which has no stack, by
contract -- out of its own menu.

Writes no artifact of its own: `devkit_project.py` wraps every dispatched action in
`log-wrap.py`. Tested in `tests/test_preview_task.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import sweep
import task_branch as tb
import worktree

REPO_ROOT = Path(__file__).resolve().parents[1]

# How many rows each remote source may contribute per project. The menu is read by a
# human under a terminal's worth of scrollback, so the cap is a readability budget
# rather than a cost one -- and both sources are already sorted newest-first, so what
# it drops is always the least likely row to be wanted.
PR_LIMIT = 8
BRANCH_LIMIT = 8

# How many refs to ask git for per `BRANCH_LIMIT` slot, so that `ignored_ref` has
# something left to keep. Four, because a nightly job cutting one branch per consumer
# fills a whole page of `--sort=-committerdate` on its own, and the cost of asking for
# more is a longer string from a command already being run.
BRANCH_OVERFETCH = 4

# A bare branch on origin that nobody has touched in this many days is not what anyone
# opened this task to look at. Six checkouts times `BRANCH_LIMIT` is forty rows, and the
# first version of this menu printed all of them: twenty-eight of the twenty-nine rows
# were the nightly upgrade sweep's copies of the same vendoring commit in six repos,
# none of them a UI change and none of them from this week. Those refs now name
# themselves (`tb.AUTOMATION_PREFIX`) and are dropped outright rather than aged out,
# which is what this cap was standing in for -- it is back to being about age alone.
#
# Boxes and open PRs deliberately bypass it. A box exists because someone is working in
# it, and an open PR is open -- neither needs a date to justify its row, and cutting one
# on age would hide exactly the long-running review this task exists to serve.
BRANCH_MAX_AGE_DAYS = 3.0

# Rows the menu will print before it starts saying "and N more". Not silent: `trim`
# returns the count it dropped and `render_menu` prints it, per the no-silent-caps
# clause in the lint policy -- a truncated list that does not admit it reads as
# "that is everything", which is the one thing it must not read as.
MENU_LIMIT = 20

# Branch namespaces worth offering. `agent/` is what `worktree.py` cuts and what every
# shipped change is on; `preview/` is deliberately absent, because a preview branch is
# this tool's own scratch copy and previewing one would nest a copy of a copy.
BRANCH_NAMESPACES = ("agent",)

# Head-branch namespaces that are never the UI change anyone opened this task to see:
# dependabot's bumps, and every branch an unattended job cut for itself. Both are the
# case that actually happened rather than a category invented here -- every open bump PR
# earned a row above the branches a human might want, and the nightly vendoring sweep
# supplied twenty-eight of this menu's first twenty-nine rows.
#
# The reason this is a *namespace* test and not a slug match is that the two are
# indistinguishable as text: `agent/auto-merge-label-0823` is a task somebody gave an
# agent. `tb.AUTOMATION_PREFIX` is a path segment the job puts there itself, which is
# the only spelling that cannot be arrived at by accident -- see `upgrade_branch_stem`.
#
# A STANDING preview of such a ref keeps its row. Someone typed the ref to bring that
# box up, so it is no longer a discovered row, and hiding what is already serving on a
# port would leave a preview nothing in this menu could stop.
IGNORED_REF_PREFIXES = ("dependabot/", tb.AUTOMATION_PREFIX)

# Where a row came from, and the order the menu puts them in. The ranking is "how few
# seconds until it is on screen", which is also how likely each is to be the answer.
KIND_STANDING = "standing"
KIND_BOX = "box"
KIND_PR = "pr"
KIND_BRANCH = "branch"

# Standing previews first, and then **recency decides** -- a box, a PR and a branch of
# the same age are interchangeable to the person reading, because the `kind` column
# already tells them what each will cost. Ranking the kinds against each other instead,
# which the first version did, put a box cut 39 hours ago above the PR opened an hour
# ago; the change someone has just asked to look at is always the newest thing on the
# list, so any ordering that does not float it is answering a different question.
KIND_RANK = {KIND_STANDING: 0}
OTHER_RANK = 1

KIND_NOTE = {
    KIND_STANDING: "preview box standing",
    KIND_BOX: "agent box (served as-is)",
    KIND_PR: "open PR",
    KIND_BRANCH: "branch on origin",
}

# The file the VS Code dropdown reads its options from. Under `logs/` because it is
# machine state with exactly the lifetime of `logs/reconcile.log` -- gitignored, owned by
# whichever run writes it next, and worth nothing to a fresh clone.
MENU_CACHE = REPO_ROOT / "logs" / "preview-menu.json"

# `--pick-ref <project>:<ref>`, one token because a VS Code input resolves to one string.
# The separator is safe rather than merely conventional: `git check-ref-format` refuses a
# ref containing a colon, so the first one is always the one between the two halves.
PICK_SEP = ":"

# The ref half of a pick that means "this list is stale, look again".
RESCAN = "__rescan__"

# How long to keep waiting for the URL the box publishes, and how often to say so.
#
# `compose up -d` returns when the CONTAINER has started, which for a dev server is a
# long way before the thing it serves exists. Measured on a cold full preview of
# carameli, 2026-08-23: 3m15s from the click to a page, of which the frontend container
# spent its first 57s running `npm install` into the box's own empty `node_modules`
# volume and then 0.6s starting Vite. The task opened the browser at second 137 -- on a
# refused connection, with the remaining minute spent in silence -- so the mode that is
# working correctly and the mode that has failed looked exactly alike.
#
# The wait is therefore part of the report rather than something the reviewer does by
# reloading: every tick prints the elapsed total, which is also the only place the cost
# of a cold box is ever stated in seconds.
READY_TIMEOUT = 420.0
READY_POLL = 2.0
READY_TICK = 15.0

# A probe that gets *any* HTTP status has its answer -- the server is up. Only a refused
# connection, a DNS failure or a timeout means "not yet".
PROBE_TIMEOUT = 3.0

# `127.0.0.1`, never `localhost`: this machine resolves `localhost` to `::1` first, and
# a compose port published on IPv4 only leaves an IPv6 connect hanging until it times
# out -- which would read here as "the donor stack is down" for a stack that is up.
LOOPBACK = "127.0.0.1"


@dataclass(frozen=True)
class Candidate:
    """One row of the menu: a ref someone might want on screen, and how to get it there.

    `ref` is the branch **under review** and is the identity of the row -- a PR, a live
    box and a remote branch that all name the same branch are one candidate, not three.
    `box` is the live box already serving it, empty when there is none; when it is set,
    `worktree.py preview` takes the box name and needs no ref at all, which is what makes
    a standing row a bare `compose up`.
    """

    project: str
    ref: str
    kind: str
    box: str = ""
    slot: int = -1
    pr: int = 0
    title: str = ""
    updated: str = ""  # ISO 8601, or "" when no source could date it

    @property
    def sort_key(self) -> tuple[int, float, str]:
        # Newest first within the rank, by NEGATED epoch seconds -- one ascending sort,
        # so kind stays the primary key and recency the secondary without a second pass.
        return (KIND_RANK.get(self.kind, OTHER_RANK), -_epoch(self.updated), self.ref)


def _parse(stamp: str) -> _dt.datetime | None:
    """One ISO 8601 timestamp as an aware datetime, or None if it will not parse.

    The single parser for every stamp this file handles, and it is the fix for a real
    ordering bug rather than mere tidiness: the sources do not agree on a spelling. `gh`
    reports `2026-08-21T14:15:32Z` while git's `iso-strict` reports the same instant as
    `2026-08-21T10:15:32-04:00`, so the string comparison this used to do ranked a PR
    four hours stale above a box committed to two minutes ago. Compare instants, never
    the text -- a naive stamp is read as UTC, which is the only assumption available.
    """
    if not stamp:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def _epoch(stamp: str) -> float:
    """`stamp` as POSIX seconds; `-inf` when it will not parse.

    `-inf` rather than 0 so that -- once `sort_key` negates it -- an undated row lands
    at the END of its rank. Zero would date it to 1970, which sorts the same way today
    and stops doing so the moment anything is dated before it.
    """
    parsed = _parse(stamp)
    return parsed.timestamp() if parsed else float("-inf")


# --- pure assembly ----------------------------------------------------------


def strip_remote(refname: str, remote: str = "origin") -> str:
    """`origin/agent/foo` -> `agent/foo`. Anything else is returned unchanged."""
    prefix = f"{remote}/"
    return refname[len(prefix) :] if refname.startswith(prefix) else refname


def box_ref(box: worktree.Box) -> str:
    """The branch a box is showing: what a preview tracks, or a task box's own branch.

    The distinction is the whole reason `Box.tracks` exists. A preview's own branch is a
    throwaway `preview/...` copy that nobody asked about, so keying a row on it would put
    the same change on screen twice under two names -- once as the PR and once as the
    copy of it that is already running.
    """
    return box.tracks or box.branch


def ignored_ref(ref: str) -> bool:
    """Whether `ref` is one the menu never offers on its own: a bot's, or a job's."""
    return ref.startswith(IGNORED_REF_PREFIXES)


def keeps_row(candidate: Candidate) -> bool:
    """Whether a merged row survives `IGNORED_REF_PREFIXES`.

    The kind is the whole test, because the prefixes describe how a ref was *discovered*
    rather than what it contains. A standing preview was asked for by name and is serving
    on a port right now; dropping it would leave a running box that nothing in this menu
    could stop, which is worse than the row it saves. Everything else here -- a task box,
    an open PR, a branch on origin -- is this script guessing that someone might want it,
    and for these prefixes that guess is always wrong.
    """
    return candidate.kind == KIND_STANDING or not ignored_ref(candidate.ref)


def merge_candidates(
    project: str,
    boxes: list[worktree.Box],
    prs: list[dict],
    branches: list[tuple[str, str, str]],
    dates: dict[str, str] | None = None,
) -> list[Candidate]:
    """One `Candidate` per distinct ref, folding in whatever each source knows about it.

    Order of application is order of authority, and it is not arbitrary: a box is the
    only source that can say "this is already running", so it wins the `kind`, while a
    PR is the only source with a human-written title, so it wins that even on a row a
    box has already claimed. The result is the row that says the most true things at
    once -- `standing / PR #164 / "Comic book UI"` -- rather than three rows that each
    say one of them.

    `dates` maps a LOCAL branch to its last commit, and a box row prefers it to
    `box.created`. The two answer different questions and only one of them is the one
    being asked: a box cut yesterday and committed to a minute ago is the freshest thing
    on this machine, and dating it by when it was cut sinks it below every PR opened
    since. `box.created` remains the fallback for a box whose branch git cannot date.
    """
    found: dict[str, Candidate] = {}
    dates = dates or {}

    for box in boxes:
        ref = box_ref(box)
        if not ref:
            continue
        kind = KIND_STANDING if box.kind == worktree.PREVIEW_KIND else KIND_BOX
        found[ref] = Candidate(
            project=project,
            ref=ref,
            kind=kind,
            box=box.name,
            slot=box.slot,
            updated=dates.get(box.branch) or box.created,
        )

    for entry in prs:
        ref = str(entry.get("headRefName") or "")
        if not ref:
            continue
        current = found.get(ref)
        title = str(entry.get("title") or "")
        number = int(entry.get("number") or 0)
        updated = str(entry.get("updatedAt") or "")
        if current is None:
            found[ref] = Candidate(
                project=project, ref=ref, kind=KIND_PR, pr=number, title=title, updated=updated
            )
        else:
            # `updated` stays the box's: the question a standing row answers is "how long
            # has this been up", and a comment posted on the PR does not change it.
            found[ref] = Candidate(**{**asdict(current), "pr": number, "title": title})

    for ref, updated, subject in branches:
        if ref in found:
            continue
        found[ref] = Candidate(
            project=project, ref=ref, kind=KIND_BRANCH, title=subject, updated=updated
        )

    return sorted(
        (candidate for candidate in found.values() if keeps_row(candidate)),
        key=lambda candidate: candidate.sort_key,
    )


def age(stamp: str, now: _dt.datetime | None = None) -> str:
    """`2026-08-20T11:02:00Z` -> `18h`. Empty for anything that will not parse.

    Deliberately lossy and deliberately total: this is a menu column, so a stamp that a
    source spelled in a shape `fromisoformat` refuses must cost the row its age and
    never the whole menu.
    """
    parsed = _parse(stamp)
    if parsed is None:
        return ""
    delta = (now or _dt.datetime.now(_dt.UTC)) - parsed
    minutes = delta.total_seconds() / 60
    if minutes < 0:
        return "now"
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 60 * 48:
        return f"{minutes / 60:.0f}h"
    return f"{minutes / 1440:.0f}d"


def fresh(stamp: str, days: float = BRANCH_MAX_AGE_DAYS, now: _dt.datetime | None = None) -> bool:
    """Whether `stamp` is within `days` of now. An unparseable stamp is kept, not cut.

    Kept, because the cost of the two mistakes is not symmetric: a stale row is one
    extra line in a menu, and a dropped row is a branch someone cannot reach from the
    task at all, with nothing on screen to say why.
    """
    parsed = _parse(stamp)
    if parsed is None:
        return True
    return (now or _dt.datetime.now(_dt.UTC)) - parsed <= _dt.timedelta(days=days)


def trim(candidates: list[Candidate], limit: int = MENU_LIMIT) -> tuple[list[Candidate], int]:
    """`(rows to print, how many were dropped)`. The count is what stops the cap lying."""
    if limit <= 0 or len(candidates) <= limit:
        return (candidates, 0)
    return (candidates[:limit], len(candidates) - limit)


def describe(candidate: Candidate, now: _dt.datetime | None = None) -> str:
    """The right-hand column: what this row is, plus its PR number and age when known."""
    parts = [KIND_NOTE.get(candidate.kind, candidate.kind)]
    if candidate.kind == KIND_STANDING and candidate.slot >= 0:
        parts[0] = f"{parts[0]} (slot {candidate.slot})"
    if candidate.pr:
        parts.append(f"PR #{candidate.pr}")
    stamp = age(candidate.updated, now)
    if stamp:
        parts.append(stamp)
    return " - ".join(parts)


def render_menu(
    candidates: list[Candidate], now: _dt.datetime | None = None, dropped: int = 0
) -> str:
    """The numbered menu, one row per candidate, columns sized to the widest entry."""
    if not candidates:
        return (
            "Nothing to preview: no live boxes, no open PRs and no agent/ branches pushed\n"
            f"in the last {BRANCH_MAX_AGE_DAYS:g} days, for any checkout with a compose stack."
        )
    width_project = max(len(c.project) for c in candidates)
    width_ref = min(48, max(len(c.ref) for c in candidates))
    lines = []
    for index, candidate in enumerate(candidates, start=1):
        title = f'  "{candidate.title}"' if candidate.title else ""
        lines.append(
            f"{index:>3}) {candidate.project:<{width_project}}  "
            f"{candidate.ref:<{width_ref}}  {describe(candidate, now)}{title}"
        )
    if dropped:
        lines.append(
            f"     ... and {dropped} older row(s) not shown -- `--limit 0` prints them all."
        )
    return "\n".join(lines)


def pick_value(candidate: Candidate) -> str:
    """The token the dropdown returns for one row: `<project>:<ref>`."""
    return f"{candidate.project}{PICK_SEP}{candidate.ref}"


def parse_pick(text: str) -> tuple[str, str]:
    """`carameli:agent/foo` -> `("carameli", "agent/foo")`; a bare ref -> `("", ref)`.

    The bare form is for a person or an agent typing `--pick-ref` by hand, where naming
    the checkout twice is a tax on the caller and `resolve_pick` can find it anyway.
    """
    project, sep, ref = text.strip().partition(PICK_SEP)
    return (project, ref) if sep else ("", project)


def unresolved(text: str) -> bool:
    """True when `--pick-ref` carries VS Code's own placeholder rather than an answer.

    Escaping either dropdown does not abort the task: VS Code runs the command anyway,
    with the `${input:previewRow}` token left in the argv as literal text. That text can
    never name a row -- `git check-ref-format` refuses `$` and `{`, so no real pick
    starts with `${` -- which is what lets it be read as the cancel it is. Without this
    it split on its colon like any pick and went to `worktree.py` as project `"${input"`,
    which died three files away in `resolve_project` with a traceback.
    """
    return text.lstrip().startswith("${")


def resolve_pick(text: str, candidates: list[Candidate]) -> Candidate | None:
    """The row a `--pick-ref` value names, or None when the value asks for a rescan.

    A value matching no row is not an error, and that is the whole reason this is a
    function rather than an index lookup: the dropdown is drawn from a file the previous
    run wrote, so a row can be picked minutes after `reconcile` reaped the box that put
    it there, or after its PR merged and its branch was deleted. What survives all of
    that is the ref, which is the only thing `plan_preview` needs -- so an unmatched pick
    becomes a plain branch row and fails, if it fails at all, against git rather than
    against a menu that was right an hour ago.

    Raises ValueError only for a value nothing could be made of, which after the fallback
    above means a bare ref that matched nothing: it names no checkout to serve it from.
    """
    project, ref = parse_pick(text)
    if ref == RESCAN:
        return None
    for candidate in candidates:
        if candidate.ref == ref and project in ("", candidate.project):
            return candidate
    if project and ref:
        return Candidate(project=project, ref=ref, kind=KIND_BRANCH)
    raise ValueError(f"--pick-ref {text!r} matches no row and names no checkout to serve it from")


def rescan_row(project: str, as_of: str) -> dict[str, str]:
    """The row every checkout's list ends in: the way out of a stale options file.

    It is also what stops a checkout with nothing found from drawing an EMPTY pick list,
    which is the state the branch you pushed thirty seconds ago arrives in -- the one
    moment the list is most wrong is the one moment it would otherwise offer no way to
    correct it.
    """
    return {
        "value": f"{project}{PICK_SEP}{RESCAN}",
        "label": "Rescan",
        "description": f"this list was built {as_of}",
        "detail": "picks nothing -- rescans boxes, PRs and branches, then asks in the terminal",
    }


def menu_row(candidate: Candidate, now: _dt.datetime | None = None) -> dict[str, str]:
    """One dropdown entry, with every field a string. See `menu_payload` for why."""
    return {
        "value": pick_value(candidate),
        "label": candidate.ref,
        "description": describe(candidate, now),
        "detail": candidate.title,
    }


def project_note(found: list[Candidate], as_of: str) -> str:
    """The first dropdown's second column: what picking this checkout will offer."""
    if not found:
        return f"nothing to preview -- as of {as_of}"
    standing = sum(1 for candidate in found if candidate.kind == KIND_STANDING)
    note = f"{len(found)} to look at"
    if standing:
        note += f", {standing} already standing"
    return f"{note} -- as of {as_of}"


def menu_payload(
    candidates: list[Candidate],
    projects: list[str],
    now: _dt.datetime | None = None,
) -> dict:
    """The dropdown's options file: the checkouts, and each one's rows keyed by name.

    Two shapes here are load-bearing rather than stylistic, because the extension builds
    its list by evaluating one expression **per field** against rising indices until one
    *throws*:

      - every row carries all four of `value`, `label`, `description` and `detail`, and
        each as a string. A field that resolves to `undefined` on a row that exists does
        not end the list -- it appends ten thousand blank entries and then draws them.
      - the rows are an array under a key per checkout, so the expression that reads them
        ends in a property access. `rows[project][i].value` raises past the end, which is
        what the extension is watching for; a bare `list[i]` would merely be undefined.

    Every checkout with a stack is listed even when it contributed no row, so a checkout
    can always be picked -- see `rescan_row`. Checkouts are ordered by their freshest
    row, for the same reason `sort_key` puts recency above kind: the change someone has
    just asked to look at is the newest thing on the machine.
    """
    stamp = now or _dt.datetime.now(_dt.UTC)
    as_of = stamp.astimezone().strftime("%Y-%m-%d %H:%M")
    grouped: dict[str, list[Candidate]] = {project: [] for project in projects}
    for candidate in candidates:
        grouped.setdefault(candidate.project, []).append(candidate)

    def freshest(project: str) -> tuple[float, str]:
        rows = grouped[project]
        return (-max((_epoch(row.updated) for row in rows), default=float("-inf")), project)

    entries, rows = [], {}
    for project in sorted(grouped, key=freshest):
        found = sorted(grouped[project], key=lambda candidate: candidate.sort_key)
        rows[project] = [menu_row(candidate, stamp) for candidate in found]
        rows[project].append(rescan_row(project, as_of))
        entries.append(
            {"name": project, "label": project, "description": project_note(found, as_of)}
        )
    return {"generated": stamp.isoformat(), "asOf": as_of, "projects": entries, "rows": rows}


def parse_choice(text: str, count: int) -> tuple[str, int]:
    """`("pick", n)`, `("all", 0)`, `("quit", 0)`, or `("again", 0)` for a retry.

    Separated from the reading so the whole grammar can be asserted without a terminal.
    A blank line is `quit` rather than `again`: pressing Enter at a menu is how someone
    who opened the wrong task gets out of it, and looping there would trap them.
    """
    answer = text.strip().lower()
    if answer in ("", "q", "quit", "n", "no"):
        return ("quit", 0)
    if answer in ("a", "all"):
        return ("all", 0)
    if answer.isdigit() and 1 <= int(answer) <= count:
        return ("pick", int(answer))
    return ("again", 0)


def preview_kwargs(candidate: Candidate, ui: bool = False) -> dict:
    """The `plan_preview` arguments for one row: a live box by name, or a ref by branch.

    A box name is passed alone, with no `--branch`, and that is not a shortcut -- naming
    both makes `plan_preview` resolve the ref instead, which for a task box would cut a
    SECOND worktree on a copy of a branch that is already checked out three feet away.

    Under `--ui` the row always goes by ref, box or no box: what the box in the row runs
    is the full stack (or is the box whose branch is under review), and the request is
    for the cheap kind, which has its own name and lease. `plan_preview(ui=True)` finds
    a standing UI box for the ref by that name, so re-picking a row stays idempotent.
    """
    if ui:
        return {"target": candidate.project, "branch": candidate.ref, "ui": True}
    if candidate.box:
        return {"target": candidate.box}
    return {"target": candidate.project, "branch": candidate.ref}


def url_report(candidate: Candidate, urls: tuple[tuple[str, int, str], ...]) -> str:
    """The block printed once a stack is up: what to open, and what else the slot serves.

    The primary URL is repeated on its own line above the list on purpose. The list is
    sorted by service name, so the frontend is seventh in carameli's -- and the whole
    failure this script exists to fix is a reviewer not being told, in one unmissable
    line, where the change is.
    """
    rule = "=" * 66
    subject = f"{candidate.project}  {candidate.ref}"
    if candidate.pr:
        subject += f"  (PR #{candidate.pr})"
    lines = [rule, f"  {subject}"]
    primary = worktree.primary_url(urls)
    if primary:
        lines.append(f"  OPEN THIS ->  {primary}")
    if urls:
        lines.append("")
        for service, port, url in urls:
            lines.append(f"    {service:<16} {url or f'localhost:{port}'}")
    if not primary and not urls:
        lines.append("  no ports are published for this checkout -- nothing to open")
    lines.append(rule)
    return "\n".join(lines)


# --- reading the machine ----------------------------------------------------


def echo(line: str = "") -> None:
    """Print a whole line, flushed. See the module docstring for why both halves matter."""
    print(line, flush=True)


def probe(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether anything answers `url`. An HTTP error status counts as an answer.

    A 404 or a 502 means a server accepted the connection and replied, which is the
    question being asked -- a Vite dev server that is up returns 404 for a path its
    router does not know, and a UI-only box whose borrowed backend is down returns 502
    through the proxy. Treating either as "not ready" would wait out the whole timeout
    on a preview that was already on screen.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_ready(
    url: str,
    timeout: float = READY_TIMEOUT,
    poll: float = READY_POLL,
    tick: float = READY_TICK,
    check=probe,
    sleep=time.sleep,
    clock=time.monotonic,
) -> tuple[bool, float]:
    """Poll `url` until it answers. `(ready, seconds waited)`, printing a line per tick.

    Every collaborator is injected because the alternative is a test that really sleeps:
    the thing worth asserting is the *shape* of the wait -- that it returns the moment
    the probe succeeds, that it gives up rather than hanging forever, and that it says
    so while waiting -- and none of that needs a clock that advances by itself.
    """
    started = clock()
    spoken = 0.0
    while True:
        if check(url):
            return True, clock() - started
        waited = clock() - started
        if waited >= timeout:
            return False, waited
        if waited - spoken >= tick:
            spoken = waited
            echo(f"  ... {waited:.0f}s: {url} is not answering yet (the container is up)")
        sleep(poll)


def port_is_open(port: int, host: str = LOOPBACK, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether something is listening on `host:port`. False on any failure."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def donor_warning(project: str, workspace: Path, listening=port_is_open) -> str:
    """The line to print when a UI-only preview's borrowed backend is not running.

    A UI-only box starts the frontend and nothing else, so its API calls go to the
    STATIC checkout's stack across the host bridge. When that stack is down the preview
    still comes up and still opens -- the dev server is fine -- and every request in it
    fails, which reads as the branch being broken rather than as the backend being
    absent. `plan_preview` only checks that the checkout is *pinned* in `ports.toml`,
    because a pin is all it needs to compute the port; whether anything is listening on
    it is a question about right now, so it is asked here.

    Empty when the backend answers, when the project publishes no `app` port, or when
    the registry cannot be read: a warning that fires on its own uncertainty is one
    people learn to scroll past. `load_registry` returns **None** for a workspace that
    keeps no `ports.toml` at all, and a malformed one raises -- both are the same
    "cannot tell" as an unpinned checkout, so all three land on the empty string.
    """
    try:
        registry = worktree.load_registry(workspace.parent)
        if registry is None:
            return ""
        slot = registry.slots.get(project, -1)
        if slot < 0:
            return ""
        port = registry.ports_for_slot(slot).get("app", 0)
    except (OSError, ValueError):  # ValueError covers devkit_ports.RegistryError
        return ""
    if not port or listening(port):
        return ""
    return (
        f"[warn] {project}'s own stack is not up on port {port}, and a UI-only preview "
        f"borrows its backend -- the page will load and every API call in it will fail. "
        f"Run 'Docker: Start Stack' for {project}, or preview without --ui."
    )


def open_prs(project_dir: Path, limit: int = PR_LIMIT) -> list[dict]:
    """Open PRs for one checkout, newest first. Empty on every failure path.

    Empty rather than raising, for the reason `pr_head_branch` gives: an unauthenticated
    or offline machine must lose the PR rows and keep the menu, because the box rows are
    the ones that work with no network and they are also the ones most likely to be
    wanted.
    """
    try:
        result = sweep.gh_for(project_dir)(
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,headRefName,updatedAt",
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def recent_branches(
    project_dir: Path, limit: int = BRANCH_LIMIT, fetch: bool = True
) -> list[tuple[str, str, str]]:
    """`(ref, iso date, subject)` for the newest `agent/...` refs on origin.

    Remote-tracking refs, not local ones, and the difference is not pedantic: `preview`
    checks a ref out from `origin/<ref>`, so a branch that exists only locally is a row
    that could be picked and could not be served. `--prune` is what stops a merged and
    deleted branch from lingering in the menu for weeks.

    `ignored_ref` is applied here as well as in `merge_candidates`, and this is the
    application that matters: git sorts and counts before this process sees anything, so
    filtering only downstream would spend the whole `--count` budget on refs about to be
    dropped -- and the sweep that cuts them runs nightly in every consumer, so `limit`
    automation branches in a row is the normal case, not the pathological one. Over-fetch
    and cap afterwards, rather than `--exclude`, which is younger than the git a consumer
    may be on.
    """
    git = sweep.git_for(project_dir)
    if fetch:
        try:
            git("fetch", "--prune", "--quiet", "origin")
        except OSError:
            return []
    patterns = [f"refs/remotes/origin/{namespace}" for namespace in BRANCH_NAMESPACES]
    try:
        result = git(
            "for-each-ref",
            "--sort=-committerdate",
            f"--count={max(limit, 0) * BRANCH_OVERFETCH}",
            "--format=%(refname:short)%09%(committerdate:iso-strict)%09%(contents:subject)",
            *patterns,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    found = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ref = strip_remote(parts[0].strip())
        if ignored_ref(ref):
            continue
        subject = parts[2].strip() if len(parts) > 2 else ""
        found.append((ref, parts[1].strip(), subject))
        if limit > 0 and len(found) >= limit:
            break
    return found


def local_branch_dates(project_dir: Path) -> dict[str, str]:
    """`{local branch: last commit, ISO}` for every `agent/...` and `preview/...` head.

    One `for-each-ref` for the whole repository rather than a `git log` per box: a
    worktree shares its parent's ref store, so every box's branch is already a local head
    here, and the per-box spelling would be nine subprocesses to learn what one call
    knows. Empty on failure -- `merge_candidates` falls back to `Box.created`, which is
    worse but never absent.
    """
    namespaces = [*BRANCH_NAMESPACES, worktree.PREVIEW_BRANCH_PREFIX.rstrip("/")]
    try:
        result = sweep.git_for(project_dir)(
            "for-each-ref",
            "--format=%(refname:short)%09%(committerdate:iso-strict)",
            *[f"refs/heads/{namespace}" for namespace in namespaces],
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    dates = {}
    for line in (result.stdout or "").splitlines():
        ref, _, stamp = line.partition("\t")
        if ref and stamp:
            dates[ref.strip()] = stamp.strip()
    return dates


def stack_projects(workspace: Path) -> list[str]:
    """The checkouts a preview can be served from: registered, present, and with a stack.

    Checkouts with no compose stack are left out entirely rather than contributing rows
    with nothing to publish. devkit is the one that matters: it has no stack by contract,
    so every one of its boxes would otherwise appear here as a row that comes up on no
    port and answers no question anyone opened this task to ask.
    """
    root = workspace.parent
    return [
        project
        for project in worktree.known_projects(workspace)
        if (root / project).is_dir() and worktree.has_stack(root / project)
    ]


def collect(
    workspace: Path, fetch: bool = True, now: _dt.datetime | None = None
) -> list[Candidate]:
    """Every previewable ref on this machine, merged and ranked."""
    root = workspace.parent
    boxes = worktree.live_boxes(root)
    candidates: list[Candidate] = []
    for project in stack_projects(workspace):
        project_dir = root / project
        branches = [
            entry for entry in recent_branches(project_dir, fetch=fetch) if fresh(entry[1], now=now)
        ]
        candidates.extend(
            merge_candidates(
                project,
                [box for box in boxes.values() if box.project == project],
                open_prs(project_dir),
                branches,
                local_branch_dates(project_dir),
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.sort_key)


def write_menu(payload: dict, path: Path | None = None) -> Path | None:
    """Save the dropdown's options, atomically. The path on success, None on any failure.

    Never raises, and that is the point: this runs on the way to bringing a stack up, and
    a preview that worked is not worth failing because the *next* one's menu could not be
    cached. The cost of a swallowed error is one stale dropdown, which the `Rescan` row
    in every list already exists to answer.

    The destination defaults at CALL time rather than in the signature, so a test can
    point `MENU_CACHE` somewhere disposable and the caller in `main` -- which passes no
    path -- follows it there.
    """
    path = path or MENU_CACHE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(".json.tmp")
        scratch.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        scratch.replace(path)
    except OSError:
        return None
    return path


def read_choice(
    candidates: list[Candidate], now: _dt.datetime | None = None, dropped: int = 0
) -> tuple[str, int]:
    """Show the menu and read one answer, retrying on anything unparseable.

    EOF is `quit`, not an error: the non-interactive callers that produce it -- an agent,
    a scheduled run -- have `--pick` and `--list`, and `main` names them when this
    returns.
    """
    echo(render_menu(candidates, now, dropped))
    echo()
    standing = sum(1 for candidate in candidates if candidate.kind == KIND_STANDING)
    while True:
        prompt = f"Pick a number 1-{len(candidates)}"
        if standing:
            prompt += f", `a` for all {standing} standing preview(s)"
        echo(f"{prompt}, or Enter to quit:")
        line = sys.stdin.readline()
        if line == "":
            return ("quit", 0)
        action, index = parse_choice(line, len(candidates))
        if action != "again":
            return (action, index)
        echo("  not one of the options -- try again.")


def serve(
    candidate: Candidate,
    workspace: Path,
    down: bool = False,
    fetch: bool = True,
    open_it: bool = True,
    ui: bool = False,
    wait: bool = True,
) -> bool:
    """Preview one candidate through `worktree.py`, then say where it is. True on success.

    The three things printed around the `worktree.py` call are the whole difference
    between this and running that tool by hand, and each answers a question a reviewer
    asked out loud: how long did that take, why is the page empty, and where is it.
    """
    verb = "Stopping" if down else "Bringing up"
    mode = " (UI only)" if ui and not down else ""
    echo(f"\n{verb} {candidate.project} {candidate.ref}{mode} ...")
    if not down:
        echo("  (a box being cut for the first time builds its images -- this can take a while)")
    if ui and not down:
        warning = donor_warning(candidate.project, workspace)
        if warning:
            echo(f"  {warning}")
    started = time.monotonic()
    try:
        plan = worktree.plan_preview(
            workspace=workspace,
            fetch=fetch,
            up=not down,
            down=down,
            **preview_kwargs(candidate, ui=ui),
        )
    except (worktree.WorktreeError, devkit_project.ProjectError) as exc:
        # ProjectError is a stale or hand-typed pick naming a checkout the workspace
        # does not list -- a wrong answer, not a wrong program, so no traceback.
        echo(f"  failed: {exc}")
        return False
    ok, notes = worktree.apply_preview(plan, workspace)
    for note in notes:
        echo(f"  {note}")
    if not ok:
        echo(f"  failed: {plan.refusal or 'the stack did not come up'}")
        return False
    if down:
        echo(f"  {candidate.ref} stopped; its box and port lease are kept.")
        return True
    if not plan.up:
        # `plan.up` false on an up-run means the checkout has no compose stack, so
        # nothing was started -- and the slot's URLs may still be published, so waiting
        # on one would spend the whole READY_TIMEOUT on a port nothing was asked to
        # answer, reporting a working preview the entire time.
        echo(
            f"  nothing was started: {plan.path} has no compose stack to bring up. "
            f"`python scripts/worktree.py list` says what state the box is in."
        )
        return False
    echo(f"  containers started in {time.monotonic() - started:.0f}s")
    primary = worktree.primary_url(plan.urls)
    ready = True
    if wait and primary:
        echo(f"  waiting for {primary} -- a cold box installs its dependencies first ...")
        ready, waited = wait_for_ready(primary)
        echo(
            f"  {primary} answered after {waited:.0f}s"
            if ready
            else f"  [warn] {primary} still silent after {waited:.0f}s -- it may still be "
            f"starting: `docker compose -p {plan.box.name} logs -f` says what it is doing"
        )
        echo(f"  total: {time.monotonic() - started:.0f}s")
    echo()
    echo(url_report(candidate, plan.urls))
    # Never opened on a URL that has not answered: a tab on a refused connection is the
    # failure this whole wait exists to stop reporting, and reloading it by hand is the
    # step the reviewer should not have to know to take.
    if open_it and primary and ready:
        try:
            webbrowser.open(primary)
        except OSError:  # pragma: no cover - a headless host has no browser to fail with
            pass
    return True


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preview-task.py",
        description="Pick a branch, PR or box to look at, and bring its stack up.",
    )
    parser.add_argument("--workspace", type=Path, default=None, help="the .code-workspace registry")
    parser.add_argument("--list", action="store_true", help="print the menu and exit")
    parser.add_argument("--json", action="store_true", help="with --list, emit JSON")
    parser.add_argument("--pick", type=int, default=0, help="take row N without asking")
    parser.add_argument(
        "--pick-ref",
        default="",
        metavar="PROJECT:REF",
        help="take this ref without asking -- what the VS Code dropdown sends",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the dropdown's option file and exit, picking nothing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MENU_LIMIT,
        help=f"rows to show (default {MENU_LIMIT}); 0 shows every one",
    )
    parser.add_argument(
        "--all", action="store_true", help="re-serve every standing preview (after a reboot)"
    )
    parser.add_argument("--down", action="store_true", help="stop the picked row's stack instead")
    parser.add_argument(
        "--ui",
        action="store_true",
        help="UI-only preview: start just the project's [worktree] ui_services, borrowing "
        "the backend from the static checkout's running stack",
    )
    parser.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="skip `git fetch` when listing branches (offline, or in a hurry)",
    )
    parser.add_argument(
        "--no-open", dest="open", action="store_false", help="print the URL but do not open it"
    )
    parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="return as soon as the containers start, without waiting for the URL to answer",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = args.workspace or sweep.default_workspace(REPO_ROOT)
    if not workspace.is_file():
        echo(f"no workspace registry at {workspace}")
        return 2

    if args.ui and args.all:
        # `--all` re-serves boxes as whatever their leases say they are; `--ui` names
        # how to CUT one. Combined they would cut a UI twin of every standing full
        # preview, which nobody asked for and eight rows deep is hard to undo.
        echo("--ui picks one ref; --all re-serves standing boxes as they are. Drop one.")
        return 2

    if args.pick_ref and unresolved(args.pick_ref):
        # Before the scan: a cancelled run should cost nothing and touch nothing.
        echo("Nothing picked -- the dropdown was cancelled.")
        return 0

    if args.fetch and not args.list:
        echo("Reading boxes, open PRs and recent branches ...")
    everything = collect(workspace, fetch=args.fetch)
    # Cached before anything can fail, and from the UNTRIMMED scan: the dropdown has no
    # screen to run out of, so the row `--limit` drops from a terminal menu is exactly the
    # row that only the dropdown can still offer.
    written = write_menu(menu_payload(everything, stack_projects(workspace)))
    if args.refresh:
        echo(
            f"Dropdown options written to {written}" if written else "Could not write the options."
        )
        return 0 if written else 1

    # Trimmed ONCE, here, so every consumer numbers the same rows. Trimming the menu and
    # not `--list` would give `--pick 12` two meanings depending on which one the caller
    # had read, which is the kind of divergence nothing would ever report.
    candidates, dropped = trim(everything, args.limit)

    if args.list:
        if args.json:
            print(json.dumps([asdict(candidate) for candidate in candidates], indent=2))
        else:
            echo(render_menu(candidates, dropped=dropped))
        return 0

    picked = None
    if args.pick_ref:
        try:
            picked = resolve_pick(args.pick_ref, everything)
        except ValueError as exc:
            echo(str(exc))
            return 2
        if picked is None:
            # The `Rescan` row picks nothing on purpose, and the scan it asked for has
            # already happened above. All that is left of it is to ask, which is what the
            # terminal menu below already is.
            echo("\nRescanned. Pick from the fresh list below -- the dropdown gets it next time.")

    if picked is not None:
        chosen = [picked]
    elif not candidates:
        echo(render_menu(candidates))
        return 0
    elif args.all:
        chosen = [c for c in candidates if c.kind == KIND_STANDING]
        if not chosen:
            echo("No preview boxes are standing; there is nothing to bring back up.")
            return 0
    elif args.pick:
        if not 1 <= args.pick <= len(candidates):
            echo(f"--pick {args.pick} is out of range; the menu has {len(candidates)} row(s).")
            echo(render_menu(candidates, dropped=dropped))
            return 2
        chosen = [candidates[args.pick - 1]]
    else:
        action, index = read_choice(candidates, dropped=dropped)
        if action == "quit":
            echo("Nothing picked. (--list, --pick N and --all take no input.)")
            return 0
        chosen = (
            [c for c in candidates if c.kind == KIND_STANDING]
            if action == "all"
            else [candidates[index - 1]]
        )

    failures = 0
    for candidate in chosen:
        served = serve(
            candidate,
            workspace,
            down=args.down,
            fetch=args.fetch,
            open_it=args.open,
            ui=args.ui,
            wait=args.wait,
        )
        if not served:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:  # pragma: no cover - a Ctrl-C at the menu is not a failure
        echo("\nCancelled.")
        raise SystemExit(0) from None
