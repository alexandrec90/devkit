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
  4. **Recent `agent/...` branches on origin** that none of the above already covers.

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

Interactive by default, which is unusual for this directory and is why the prompt is
written the way it is: the task runs under `log-wrap.py`, whose `stream()` gives this
process a **pipe** for stdout while leaving stdin inherited. A prompt that does not end
in a newline therefore sits in the pipe's buffer and never reaches the terminal, and the
user waits at what looks like a hung task. Every prompt here is a whole flushed line for
that reason. When stdin is not there at all -- an agent, a scheduled run -- reading it
returns EOF, and that is reported as "nothing was picked" with the non-interactive
spelling, never as an error.

Devkit-scoped (`DEVKIT_ONLY` in `devkit_project.py`) though it previews other projects:
the machine has one box registry and one port registry, so the menu has no project
dimension to pick from. Restricting the sources to checkouts with a compose stack is
what keeps devkit itself -- which has no stack, by contract -- out of its own menu.

Writes no artifact of its own: `devkit_project.py` wraps every dispatched action in
`log-wrap.py`. Tested in `tests/test_preview_task.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep
import worktree

REPO_ROOT = Path(__file__).resolve().parents[1]

# How many rows each remote source may contribute per project. The menu is read by a
# human under a terminal's worth of scrollback, so the cap is a readability budget
# rather than a cost one -- and both sources are already sorted newest-first, so what
# it drops is always the least likely row to be wanted.
PR_LIMIT = 8
BRANCH_LIMIT = 8

# A bare branch on origin that nobody has touched in this many days is not what anyone
# opened this task to look at. Six checkouts times `BRANCH_LIMIT` is forty rows, and the
# first version of this menu printed all of them: twenty-eight of the twenty-nine rows
# were `agent/devkit-upgrade-v0-10-2-...` copies of the same vendoring commit in six
# repos, none of them a UI change and none of them from this week.
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

    return sorted(found.values(), key=lambda candidate: candidate.sort_key)


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


def preview_kwargs(candidate: Candidate) -> dict:
    """The `plan_preview` arguments for one row: a live box by name, or a ref by branch.

    A box name is passed alone, with no `--branch`, and that is not a shortcut -- naming
    both makes `plan_preview` resolve the ref instead, which for a task box would cut a
    SECOND worktree on a copy of a branch that is already checked out three feet away.
    """
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
            f"--count={limit}",
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
        subject = parts[2].strip() if len(parts) > 2 else ""
        found.append((ref, parts[1].strip(), subject))
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


def collect(
    workspace: Path, fetch: bool = True, now: _dt.datetime | None = None
) -> list[Candidate]:
    """Every previewable ref on this machine, merged and ranked.

    Checkouts with no compose stack are skipped entirely rather than contributing rows
    with nothing to publish. devkit is the one that matters: it has no stack by contract,
    so every one of its boxes would otherwise appear here as a row that comes up on no
    port and answers no question anyone opened this task to ask.
    """
    root = workspace.parent
    boxes = worktree.live_boxes(root)
    candidates: list[Candidate] = []
    for project in worktree.known_projects(workspace):
        project_dir = root / project
        if not project_dir.is_dir() or not worktree.has_stack(project_dir):
            continue
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
) -> bool:
    """Preview one candidate through `worktree.py`, then say where it is. True on success."""
    verb = "Stopping" if down else "Bringing up"
    echo(f"\n{verb} {candidate.project} {candidate.ref} ...")
    if not down:
        echo("  (a box being cut for the first time builds its images -- this can take a while)")
    try:
        plan = worktree.plan_preview(
            workspace=workspace, fetch=fetch, up=not down, down=down, **preview_kwargs(candidate)
        )
    except worktree.WorktreeError as exc:
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
    echo()
    echo(url_report(candidate, plan.urls))
    primary = worktree.primary_url(plan.urls)
    if open_it and primary:
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
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="skip `git fetch` when listing branches (offline, or in a hurry)",
    )
    parser.add_argument(
        "--no-open", dest="open", action="store_false", help="print the URL but do not open it"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = args.workspace or sweep.default_workspace(REPO_ROOT)
    if not workspace.is_file():
        echo(f"no workspace registry at {workspace}")
        return 2

    if args.fetch and not args.list:
        echo("Reading boxes, open PRs and recent branches ...")
    # Trimmed ONCE, here, so every consumer numbers the same rows. Trimming the menu and
    # not `--list` would give `--pick 12` two meanings depending on which one the caller
    # had read, which is the kind of divergence nothing would ever report.
    candidates, dropped = trim(collect(workspace, fetch=args.fetch), args.limit)

    if args.list:
        if args.json:
            print(json.dumps([asdict(candidate) for candidate in candidates], indent=2))
        else:
            echo(render_menu(candidates, dropped=dropped))
        return 0

    if not candidates:
        echo(render_menu(candidates))
        return 0

    if args.all:
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
        if not serve(candidate, workspace, down=args.down, fetch=args.fetch, open_it=args.open):
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:  # pragma: no cover - a Ctrl-C at the menu is not a failure
        echo("\nCancelled.")
        raise SystemExit(0) from None
