#!/usr/bin/env python3
"""Cut and destroy the worktrees an agent CLI's `--worktree` flag lives in.

`claude --worktree <topic>` cuts `.claude/worktrees/<topic>`, enters it, and offers
keep-or-remove on the way out. **Codex has no such flag**, and that is the whole reason
this exists: a Codex session that wants isolation has to be handed a worktree by
somebody, and handing it one somewhere *else* would mean two conventions on one machine.
So this cuts in the same directory the built-in does -- which is also where a remote
Claude session spawns, so the delete verb can see those too.

| verb | what it does |
| --- | --- |
| `new` | cut `agent/<slug>-<mmdd>`, a worktree for it, and open Claude or Codex there |
| `remove` | destroy the named worktrees, and their local branches when nothing is lost |
| `list` | print the worktrees, for a terminal |

**This is not the box tier and must not become it.** `worktree.py` cuts a box at
`<root>/.worktrees/`, leases it a port and a `COMPOSE_PROJECT_NAME`, provisions its
toolchain and reaps it on a schedule; `agent-box.py spawn` is still the verb for a
session that runs a compose stack. What is here is a plain git worktree: no lease and no
reaper. Its toolchain is the git hook's (`worktree_env`), and `worktree.py provision`'s
where the hook left it unprovisioned.

**Port selection belongs to the project's dev server.** New frontend projects receive
`templates/features/frontend/frontend/src/worktreePort.ts`; its docstring owns the wiring
and collision handling. Existing projects need to adopt it themselves. This command
does not configure their server or allocate a port lease.
The one thing shared is how a terminal tab is opened, which is `agent_tabs.open_agent` --
two copies of that would be two answers to "which window does the agent open in".

There is no file under `list` or `remove`: both read `git worktree list --porcelain` per
checkout, fanned out, at the moment they run. A worktree is listed because it exists,
which is also why `fix-prs.py` can cut into this tier without knowing this file exists.

Every decision is in `scripts/agent_worktrees.py`, pure and separately tested; what is
here spawns git and terminals, and takes a runner so the tests do not.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_models
import agent_tabs
import agent_worktrees as aw
import box_teardown
import devkit_project
import fix_reports
import sweep
import task_branch as tb
import tree_provision

# How many checkouts `scan` reads at once. A worktree scan is four `git` calls per
# checkout and a `git status` per worktree found.
SCAN_WORKERS = 8

# How many `git status` calls run at once, across every checkout. Wider than `SCAN_WORKERS`
# because the wait is on the disk and not on a core: 96 cold trees took 25s at a width of
# 8 and 8s at 32, and warm ones 2.1s at any width. Shared, because eight checkouts with a
# pool each would be a hundred git processes on eight cores for one listing.
STATUS_WORKERS = 24
STATUS_SLOTS = threading.BoundedSemaphore(STATUS_WORKERS)
# `--no-optional-locks`: a bare `git status` refreshes the index and takes `index.lock`
# to do it, in trees a live session may be committing from. A listing must not be able
# to fail somebody's `git add`.
STATUS = ("--no-optional-locks", "status", "--porcelain")

# What `new` can open. `none` cuts the worktree and opens nothing.
AGENTS = ("claude", "codex", "none")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# Seconds one `gh pr list` may take when `create` names a taken branch's PR. A lookup
# that hangs costs the cut nothing but the PR number.
GH_TIMEOUT = 20


def trees_for(project_dir: Path) -> list[aw.Tree]:
    """Every agent-CLI worktree of this checkout, in any tier, with what it holds.

    The two counts are what `list` and `remove` warn with, so they are read here rather
    than left to `git worktree remove`: an entry that says `clean and pushed` is the
    difference between a removal and a guess.
    """
    git = sweep.git_for(project_dir)
    listed = git("worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return []
    nested = aw.nested(project_dir, listed.stdout or "")
    if not nested:
        return []
    default = tb.detect_default_branch(git)
    ahead = ahead_counts(git, default)

    def read(entry: tuple[str, str, str]) -> aw.Tree:
        name, path, branch = entry
        inner = sweep.git_for(Path(path))
        with STATUS_SLOTS:
            status = inner(*STATUS)
        dirty = len((status.stdout or "").strip().splitlines()) if status.returncode == 0 else 0
        if ahead is not None and (not branch or branch in ahead):
            unpushed = ahead.get(branch, 0)
        else:
            unpushed = unpushed_count(inner, branch, default)
        return aw.Tree(name, path, branch, dirty, unpushed)

    # Fanned out for `scan`'s reason, one level in: a checkout with eight worktrees is
    # eight `git status` calls.
    with futures.ThreadPoolExecutor(max_workers=min(STATUS_WORKERS, len(nested))) as pool:
        return list(pool.map(read, nested))


# One line per local branch: its name, its upstream, how it stands against that
# upstream, and `<ahead> <behind>` against the default branch.
AHEAD_FORMAT = (
    "--format=%(refname:short)%09%(upstream:short)%09%(upstream:track)"
    "%09%(ahead-behind:origin/{default})"
)
AHEAD_OF_UPSTREAM = re.compile(r"\[ahead (\d+)")


def ahead_counts(git, default: str) -> dict[str, int] | None:
    """`unpushed_count` for every local branch at once; None when this git cannot say.

    Two `rev-list` calls and a `symbolic-ref` per worktree, across the machine's 257
    worktrees, was 770 of the 1050 git processes behind a 25-second listing: a
    `for-each-ref` per checkout answers all of them. `%(ahead-behind:)` is new in git
    2.41, so an older git fails the call and `trees_for` counts per worktree.
    """
    done = git("for-each-ref", AHEAD_FORMAT.format(default=default), "refs/heads")
    if done.returncode != 0:
        return None
    counts: dict[str, int] = {}
    for line in (done.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) == 4 and fields[0]:
            counts[fields[0]] = branch_ahead(*fields[1:])
    return counts


def branch_ahead(upstream: str, track: str, against_default: str) -> int:
    """One `ahead_counts` line's answer, by `unpushed_count`'s rule.

    The upstream decides when it exists; a branch with none, or whose upstream is gone,
    counts against the default branch. `track` is empty both for no upstream and for an
    upstream it is level with, which is why `upstream` is read at all.
    """
    if upstream and track != "[gone]":
        found = AHEAD_OF_UPSTREAM.match(track)
        return int(found.group(1)) if found else 0
    ahead, _, _ = against_default.partition(" ")
    return int(ahead) if ahead.isdigit() else 0


def unpushed_count(git, branch: str, default: str | None = None) -> int:
    """Commits this worktree holds that no remote does. 0 when the question has no answer.

    Falls back to the default branch when there is no upstream, because a branch that was
    never pushed has no `@{u}` and is precisely the case where unpushed work is most
    likely. A detached HEAD gets 0: `removal_decision` still sees the dirty count, and
    counting commits against a base nobody chose would refuse removals for no reason.
    `default` saves the lookup when the caller already made it.
    """
    if not branch:
        return 0
    for ref in (f"{branch}@{{u}}", f"origin/{default or tb.detect_default_branch(git)}"):
        done = git("rev-list", "--count", f"{ref}..HEAD")
        if done.returncode == 0:
            return int((done.stdout or "0").strip() or 0)
    return 0


def known_branches(git) -> set[str]:
    """Every branch name this checkout could collide with, local and on origin.

    Origin's included because `tb.branch_name` disambiguates against what it is shown,
    and a name that is free locally but taken on the remote fails at the *push*, which
    is after the work rather than before it.
    """
    done = git("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin")
    names = {line.strip() for line in (done.stdout or "").splitlines() if line.strip()}
    return {name.removeprefix("origin/") for name in names}


def same_task(plain: str, branch: str, existing: set[str]) -> list[str]:
    """The taken names `tb.branch_name` moved past from `plain` to `branch`, oldest first.

    `agent/x-1008-2` exists because `agent/x-1008` did: the same slug, cut the same day,
    which is most often the same task. Empty when `branch` is the plain name.
    """
    if plain == branch:
        return []
    taken = [name for name in existing if re.fullmatch(rf"{re.escape(plain)}(?:-\d+)?", name)]
    return sorted((name for name in taken if name != branch), key=lambda name: (len(name), name))


def prior_work(source: Path, names: list[str], runner) -> list[str]:
    """One line per taken name, naming its PR when `gh` knows one.

    A devkit ledger sweep cut `data-lake:agent/archive-read-retry-1008-2` at 21:28 while
    #55 from `...-1008` -- the same fix -- was open; #55 merged twelve minutes later, and
    the sweep's PR opened as a duplicate and was closed. The suffix was the one sign, and
    nothing said it out loud.
    """
    lines = []
    for name in names:
        pr = ""
        try:
            listing = ["gh", "pr", "list", "--head", name, "--state", "all", "--limit", "1"]
            done = runner(
                [*listing, "--json", "number,state,url"],
                cwd=source,
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_TIMEOUT,
            )
            found = json.loads(done.stdout or "[]") if done.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired, ValueError):
            found = []
        if found and isinstance(found[0], dict):
            pr = f" -- PR #{found[0].get('number')} ({found[0].get('state')}) {found[0].get('url')}"
        lines.append(
            f"agent-worktree: {name} was taken{pr}; if it carries this task, "
            "the work is there, not in this tree"
        )
    return lines


def scan(root: Path) -> dict[str, list[aw.Tree]]:
    """Every registered checkout under `root`, and its worktrees.

    Every checkout is listed even when it has none, because the reader's question is
    "where are my worktrees", and one that silently drops out when it is empty is
    indistinguishable from one the scan could not reach.

    Concurrent because the per-checkout calls share nothing and `trees_for` answers empty
    rather than raising, so the pool cannot fail where the loop would not.
    """
    names = [name for name in devkit_project.known_projects() if (root / name).is_dir()]
    if not names:
        return {}
    with futures.ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(names))) as pool:
        found = list(pool.map(lambda name: trees_for(root / name), names))
    return dict(zip(names, found, strict=True))


def create(
    project: str, root: Path, slug: str, base: str, launch: agent_models.Launch, runner
) -> int:
    """Cut the branch and the worktree, then hand it to the agent.

    `--no-track` and a fetch first, both copied from `worktree.spawn_plan` and for its
    reasons: branching off a remote-tracking ref would make `origin/<base>` the new
    branch's upstream, so a later bare `git push` lands the task's commits on the base
    branch, and a base that was not fetched is however stale this checkout last was.
    """
    source = devkit_project.resolve_project(project, devkit_project.known_projects(), root)
    git = sweep.git_for(source)
    runner(["git", "-C", str(source), "fetch", "--quiet", "origin"], check=False)
    ref = base or tb.detect_default_branch(git)
    if git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{ref}").returncode != 0:
        print(f"agent-worktree: origin has no branch '{ref}'", file=sys.stderr)
        return EXIT_USAGE
    existing = known_branches(git)
    stem = tb.slugify(slug or project)
    branch = tb.branch_name(stem, existing)
    taken = same_task(tb.branch_name(stem, set()), branch, existing)
    name = branch.partition("/")[2]
    path = aw.default_root(source) / name
    # `--quiet`: git's checkout progress was most of what this printed (80a224ea).
    cut = ("worktree", "add", "--quiet", "--no-track", "-b", branch, str(path), f"origin/{ref}")
    done = runner(["git", "-C", str(source), *cut], check=False)
    if done.returncode != 0:
        print("agent-worktree: the worktree was not cut; nothing to open", file=sys.stderr)
        return EXIT_FAILED
    print(f"{branch} off origin/{ref}\n  {path}")
    fix_reports.inherit_origin(path)  # fixer work when cut by a fixer: it merges once green
    # The git hook provisions a tree only when its checkout has a `.venv`, which keeps an
    # unused project's `worktree add` fast. A tree cut here is one somebody works in, so
    # a cold checkout's is provisioned too: a fixer's social-scraper tree came up with no
    # `.venv` and no line saying why, and no test could run in it. The hook's mark covers
    # the `.venv` alone, so a frontend tree is provisioned for its `node_modules` too.
    if tree_provision.needs_provision(path):
        tree_provision.provision(path, runner)
    opened = agent_tabs.open_agent(launch, path, branch, runner)
    # Last, so a caller reading the tail of the output still sees it.
    for line in prior_work(source, taken, runner):
        print(line)
    return opened


def remove_one(project: str, source: Path, tree: aw.Tree, forced: bool, runner) -> int:
    """Destroy one worktree, and its local branch when `git branch -d` will take it.

    `-d` rather than `-D` even under `--force`: forcing is about the worktree, which is
    disposable by construction, and says nothing about a branch whose commits may be the
    only copy. A branch git refuses to delete is named and left, which is a line in the
    terminal rather than a lost afternoon.
    """
    verdict, reason = aw.removal_decision(tree, forced)
    if verdict == aw.KEEP:
        print(f"  kept {tree.name}: {reason}")
        print("    run this verb with `--force force` to discard it")
        return EXIT_FAILED
    argv = ["git", "-C", str(source), "worktree", "remove", tree.path]
    if verdict == aw.FORCE:
        argv.insert(-1, "--force")
    removed = runner(argv, capture_output=True, text=True, check=False)
    if removed.returncode != 0:
        # Git drops the record even when the filesystem refused a file, so a refusal left
        # here is a directory and a branch no later `remove` can name.
        said = (removed.stderr or "").strip()[-200:]
        prune = ["git", "-C", str(source), "worktree", "prune"]
        error = box_teardown.finish_refused_removal(
            Path(tree.path), said, lambda: runner(prune, check=False)
        )
        if error is None or error:
            print(f"  {project}: could not remove {tree.name}: {error or said}", file=sys.stderr)
            return EXIT_FAILED
    print(f"  removed {tree.name}")
    if tree.branch:
        gone = runner(
            ["git", "-C", str(source), "branch", "-d", tree.branch],
            capture_output=True,
            text=True,
            check=False,
        )
        kept = "kept" if gone.returncode else "deleted"
        print(f"    {kept} the local branch {tree.branch}; origin's copy is untouched")
    return EXIT_OK


def remove(picks: list[tuple[str, str]], root: Path, forced: bool, runner) -> int:
    """Every named worktree in turn. The worst exit code, so one refusal is still reported."""
    projects = devkit_project.known_projects()
    worst = EXIT_OK
    for project, name in picks:
        source = devkit_project.resolve_project(project, projects, root)
        found = next((tree for tree in trees_for(source) if tree.name == name), None)
        if found is None:
            print(f"  {project}: no worktree called {name}")
            continue
        worst = max(worst, remove_one(project, source, found, forced, runner))
    return worst


def render(trees: dict[str, list[aw.Tree]]) -> str:
    """`list`, for the terminal: the fullest checkout first."""
    lines = []
    for project in sorted(trees, key=lambda name: (-len(trees[name]), name)):
        lines.append(f"{project}: {len(trees[project]) or 'no'} worktree(s)")
        lines += [f"  {t.name} -- {t.branch or 'detached'} -- {t.state()}" for t in trees[project]]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="verb", required=True)

    new = sub.add_parser("new", help="cut a branch and worktree and open an agent in it")
    new.add_argument("--pick", default="", help=f"`<project>{aw.PICK_SEP}<base branch>`")
    new.add_argument(
        "--slug", default="", help="what the branch is about; blank names it after the project"
    )
    new.add_argument("--agent", default="claude", choices=AGENTS)
    # `remove` takes neither: destroying a worktree opens no session.
    agent_models.add_arguments(new)

    gone = sub.add_parser("remove", help="destroy the named worktrees")
    gone.add_argument(
        "--picks",
        default="",
        help=f"`<project>{aw.PICK_SEP}<worktree>` tokens joined by a space, as `list` names them",
    )
    gone.add_argument(
        "--force",
        default="keep",
        choices=("keep", "force"),
        help="`force` discards uncommitted and unpushed work; `keep` refuses to",
    )

    shown = sub.add_parser("list", help="print the worktrees and stop")
    for one in (new, gone, shown):
        # `--workspace` is accepted and read only as the root's alias: scheduled jobs and
        # old command lines still pass it.
        sweep.projects_root.add_flags(one)
    return parser


def main(argv: list[str] | None = None, runner=subprocess.run) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    root = sweep.projects_root.from_flags(args.root, args.workspace).resolve()

    try:
        if args.verb == "list":
            print(render(scan(root)))
            return EXIT_OK
        if args.verb == "new":
            pick = aw.parse_pick(args.pick)
            if pick is None:
                print("agent-worktree: no checkout named -- nothing to do")
                return EXIT_OK
            launch = agent_models.Launch.parse(args.agent, args.model, args.effort)
            return create(pick[0], root, args.slug, pick[1], launch, runner)
        picks = [p for p in (aw.parse_pick(t) for t in aw.split_picks(args.picks)) if p]
        if not picks:
            print("agent-worktree: nothing named -- nothing to do")
            return EXIT_OK
        return remove(picks, root, args.force == "force", runner)
    except (devkit_project.ProjectError, devkit_project.project_registry.RegistryError) as exc:
        print(f"agent-worktree: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
