#!/usr/bin/env python3
"""Cut and destroy the worktrees Claude Code's `--worktree` flag lives in -- for Codex too.

`claude --worktree <topic>` cuts `.claude/worktrees/<topic>`, enters it, and offers
keep-or-remove on the way out. **Codex has no such flag**, and that is the whole reason
this exists: a Codex session that wants isolation has to be handed a worktree by
somebody, and handing it one somewhere *else* would mean two conventions on one machine.
So this cuts in the same directory the built-in does -- which is also where a remote
Claude session spawns, so the delete verb can see those too.

| verb | what it does |
| --- | --- |
| `new` | cut `agent/<slug>-<mmdd>`, a worktree for it, and open Claude or Codex there |
| `remove` | destroy the ticked worktrees, and their local branches when nothing is lost |
| `rows` | print the delete dropdown's rows, from a scan, and stop |
| `bases` | print the base-branch dropdown's rows and stop |
| `tree-projects` | print the delete task's checkout rows, recording the scan behind them |
| `base-projects` | print the new-worktree task's checkout rows, and record its scan |
| `list` | print the worktrees, for a terminal |

**This is not the box tier and must not become it.** `worktree.py` cuts a box at
`<workspace>/.worktrees/`, leases it a port and a `COMPOSE_PROJECT_NAME`, provisions its
toolchain and reaps it on a schedule; `agent-box.py spawn` is still the verb for a
session that runs a compose stack. What is here is a plain git worktree: no lease, no
provisioning, no reaper, and nothing to collide with the ports a static checkout holds.
The one thing shared is how a terminal tab is opened, which is `agent_box.open_agent` --
two copies of that would be two answers to "which window does the agent open in".

The menus are live, and there is no file under them. Both are `shellCommand.execute`
running `rows` or `bases` at the moment the picker opens, which is a scan of
`git worktree list --porcelain` per checkout, fanned out. What that replaced was a JSON
file only `worktree.reconcile` wrote, so a worktree cut a minute ago was not in the
delete list and one destroyed a minute ago still was -- and `new` and `remove` each had
to rewrite it as they finished to make the common case bearable. Nothing rewrites
anything now: a worktree is in the list because it exists, which is also why
`fix-prs.py` can cut into this tier without knowing this file exists.

**Each dropdown asks which checkouts first, and that costs no second scan.**
`tree-projects` and `base-projects` are the first stage: they run the fan-out once,
record it through `picker_scan`, and draw one row per checkout with what it holds. The
second stage is `rows` or `bases` with `--checkouts`, which filters that record by the
token it was handed and rescans only what was ticked when the token names no write --
so the list a person picks from is never older than the click that opened it. See
`scripts/picker_scan.py` for why the token is the whole design.

Every decision is in `scripts/agent_worktrees.py`, pure and separately tested; what is
here spawns git and terminals, and takes a runner so the tests do not.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import agent_worktrees as aw
import devkit_project
import picker_rows
import picker_scan
import sweep
import task_branch as tb
import task_input
import worktree

# `agent-box.py` is hyphenated, so it cannot be a plain import. Loaded by path for the
# one thing worth sharing rather than copying -- see the module docstring.
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

agent_box = load_by_path("agent_box", REPO_ROOT / "scripts" / "agent-box.py")

# How many checkouts `scan` reads at once. A worktree scan is three `git` calls per
# checkout and one more per worktree found, which is seconds in a row; the picker runs
# it while somebody watches.
SCAN_WORKERS = 8

# What the `new` picker offers. `none` is CLI-only and deliberately not a row: the task's
# whole purpose is opening an agent, and a worktree with nothing in it is `git worktree
# add`, which needs no dropdown.
AGENTS = ("claude", "codex", "none")

# What `picker_scan` files each dropdown's checkout-stage write under. Two names for
# one scan on purpose: the two dropdowns are separate tasks, each clicked on its own, so
# each records the half it will be asked for rather than sharing a write neither click
# can be sure the other made.
SCAN_NAMES = {"trees": "worktree-trees", "bases": "worktree-bases"}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def trees_for(project_dir: Path) -> list[aw.Tree]:
    """Every worktree under this checkout's `.claude/worktrees/`, with what it holds.

    The two counts are what the delete dropdown warns with, so they are read here rather
    than at removal time: a row that says `clean and pushed` is the difference between a
    checkbox list and a guess.
    """
    git = sweep.git_for(project_dir)
    listed = git("worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return []
    nested = aw.nested(project_dir, listed.stdout or "")
    if not nested:
        return []

    def read(entry: tuple[str, str, str]) -> aw.Tree:
        name, path, branch = entry
        inner = sweep.git_for(Path(path))
        status = inner("status", "--porcelain")
        dirty = len((status.stdout or "").strip().splitlines()) if status.returncode == 0 else 0
        return aw.Tree(name, path, branch, dirty, unpushed_count(inner, branch))

    # Fanned out for `scan`'s reason, one level in: a checkout with eight worktrees is
    # sixteen git calls, and this runs while somebody watches a quick-pick open.
    with futures.ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(nested))) as pool:
        return list(pool.map(read, nested))


def unpushed_count(git, branch: str) -> int:
    """Commits this worktree holds that no remote does. 0 when the question has no answer.

    Falls back to the default branch when there is no upstream, because a branch that was
    never pushed has no `@{u}` and is precisely the case where unpushed work is most
    likely. A detached HEAD gets 0: `removal_decision` still sees the dirty count, and
    counting commits against a base nobody chose would refuse removals for no reason.
    """
    if not branch:
        return 0
    for ref in (f"{branch}@{{u}}", f"origin/{tb.detect_default_branch(git)}"):
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


def recent_bases(project_dir: Path) -> list[tuple[str, str]]:
    """`(branch, note)` for origin's branches, most recently committed first.

    Remote branches rather than local ones, because the base is a *start point* and a
    stale local `main` is the one start point nobody means. The default branch is pinned
    first whatever its date -- it is the answer nine times out of ten, and a dropdown
    that buries it under yesterday's task branches is asking a question it already knows
    the answer to.
    """
    git = sweep.git_for(project_dir)
    default = tb.detect_default_branch(git)
    done = git(
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:lstrip=3)%09%(committerdate:relative)",
        "refs/remotes/origin",
    )
    if done.returncode != 0:
        return [(default, "the default branch")]
    rows = [(default, "the default branch")]
    for line in (done.stdout or "").splitlines():
        name, _, when = line.partition("\t")
        if name and name not in ("HEAD", default) and len(rows) <= aw.BASE_LIMIT:
            rows.append((name, f"last commit {when}"))
    return rows


def scan(
    workspace: Path, projects: list[str] | None = None
) -> tuple[dict[str, list[aw.Tree]], dict[str, list[tuple[str, str]]]]:
    """Every registered checkout's worktrees and base branches.

    Every checkout is listed even when it has neither, because the reader's question is
    "where are my worktrees", and one that silently drops out when it is empty is
    indistinguishable from one the scan could not reach.

    Concurrent because both dropdowns now run this when they open rather than reading a
    file a scheduled pass wrote. The per-checkout calls share nothing and both helpers
    answer empty rather than raising, so the pool cannot fail where the loop would not.

    `projects` narrows it to the checkouts a caller already knows it wants -- the second
    picker stage, when the scan its first stage recorded is not the one it was handed.
    A parameter rather than a filter on the result, because each name in it costs a
    `git worktree list` and a `for-each-ref`: discarding those afterwards would spend
    the whole fan-out the narrowing exists to avoid. Names the registry does not know
    are dropped, so a stale pick cannot reach the filesystem as a path.
    """
    text = workspace.read_text(encoding="utf-8")
    root = workspace.parent
    names = [name for name in devkit_project.known_projects(text) if (root / name).is_dir()]
    if projects is not None:
        wanted = set(projects)
        names = [name for name in names if name in wanted]
    if not names:
        return {}, {}
    with futures.ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(names))) as pool:
        found = list(
            pool.map(lambda name: (trees_for(root / name), recent_bases(root / name)), names)
        )
    return (
        {name: trees for name, (trees, _) in zip(names, found, strict=True)},
        {name: bases for name, (_, bases) in zip(names, found, strict=True)},
    )


def create(project: str, workspace: Path, slug: str, base: str, agent: str, runner) -> int:
    """Cut the branch and the worktree, then hand it to the agent.

    `--no-track` and a fetch first, both copied from `worktree.spawn_plan` and for its
    reasons: branching off a remote-tracking ref would make `origin/<base>` the new
    branch's upstream, so a later bare `git push` lands the task's commits on the base
    branch, and a base that was not fetched is however stale this checkout last was.
    """
    root = workspace.parent
    source = devkit_project.resolve_project(
        project, devkit_project.known_projects(workspace.read_text(encoding="utf-8")), root
    )
    git = sweep.git_for(source)
    runner(["git", "-C", str(source), "fetch", "--quiet", "origin"], check=False)
    ref = base or tb.detect_default_branch(git)
    if git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{ref}").returncode != 0:
        print(f"agent-worktree: origin has no branch '{ref}'", file=sys.stderr)
        return EXIT_USAGE
    existing = known_branches(git)
    branch = tb.branch_name(tb.slugify(slug or project), existing)
    name = branch.partition("/")[2]
    path = source / aw.WORKTREES_DIR / name
    done = runner(
        [
            "git",
            "-C",
            str(source),
            "worktree",
            "add",
            "--no-track",
            "-b",
            branch,
            str(path),
            f"origin/{ref}",
        ],
        check=False,
    )
    if done.returncode != 0:
        print("agent-worktree: the worktree was not cut; nothing to open", file=sys.stderr)
        return EXIT_FAILED
    print(f"{branch} off origin/{ref}\n  {path}")
    return agent_box.open_agent(agent, path, branch, runner)


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
        print("    tick 'Force', or run this verb with --force, to discard it")
        return EXIT_FAILED
    argv = ["git", "-C", str(source), "worktree", "remove", tree.path]
    if verdict == aw.FORCE:
        argv.insert(-1, "--force")
    if runner(argv, check=False).returncode != 0:
        print(f"  {project}: git refused to remove {tree.name}", file=sys.stderr)
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


def remove(picks: list[tuple[str, str]], workspace: Path, forced: bool, runner) -> int:
    """Every ticked worktree in turn. The worst exit code, so one refusal is still reported."""
    root = workspace.parent
    projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    worst = EXIT_OK
    for project, name in picks:
        source = devkit_project.resolve_project(project, projects, root)
        found = next((tree for tree in trees_for(source) if tree.name == name), None)
        if found is None:
            print(f"  {project}: no worktree called {name} (the menu was stale)")
            continue
        worst = max(worst, remove_one(project, source, found, forced, runner))
    return worst


def render(trees: dict[str, list[aw.Tree]]) -> str:
    """`list`, for the terminal. The same rows the delete dropdown would draw."""
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

    gone = sub.add_parser("remove", help="destroy the ticked worktrees")
    gone.add_argument("--picks", default="", help="ticked rows joined by a space")
    gone.add_argument(
        "--force",
        default="keep",
        choices=("keep", "force"),
        help="`force` discards uncommitted and unpushed work; `keep` refuses to",
    )

    again = sub.add_parser("rows", help="print the delete dropdown's rows and stop")
    from_refs = sub.add_parser("bases", help="print the base-branch dropdown's rows and stop")
    tree_scope = sub.add_parser(
        "tree-projects", help="print the delete task's CHECKOUT rows and stop"
    )
    base_scope = sub.add_parser(
        "base-projects", help="print the new-worktree task's CHECKOUT rows and stop"
    )
    shown = sub.add_parser("list", help="print the worktrees and stop")
    # Wherever a checkout stage can precede one: the two row verbs read it to narrow,
    # and `new`/`remove` read it to notice the two stages disagreeing.
    for one in (new, gone, again, from_refs):
        one.add_argument(
            "--checkouts",
            default="",
            help=(
                f"ticked checkouts, `<project>{picker_scan.SEP}<scan token>` joined by "
                f"`{picker_scan.LIST_SEP}` -- what the checkout picker returns"
            ),
        )
    for one in (new, gone, again, from_refs, tree_scope, base_scope, shown):
        one.add_argument("--workspace", type=Path, default=worktree.DEFAULT_WORKSPACE)
    return parser


# The four verbs that draw a quick-pick, and which half of one scan each reads.
# `draw` dispatches on this rather than `main` carrying four branches: they differ in
# two values and nothing else, and `main` is already the widest function in the file.
PICKER_VERBS = {
    "rows": "trees",
    "bases": "bases",
    "tree-projects": "trees",
    "base-projects": "bases",
}


def draw(workspace: Path, verb: str, checkouts: str) -> list[str]:
    """The rows for whichever picker verb was asked for.

    The `-projects` pair is stage one: it runs the fan-out, records it under the half's
    own scan name, and draws a checkout per row. The bare pair is stage two, which
    filters that record. Both are in one function because "which half" is the only
    thing that varies across the four, and the caller's stdout IS the quick-pick.
    """
    half = PICKER_VERBS[verb]
    if not verb.endswith("-projects"):
        return picked_rows(workspace, checkouts, half)
    trees, bases = scan(workspace)
    if half == "trees":
        return aw.tree_project_rows(
            trees, picker_scan.write(SCAN_NAMES[half], aw.tree_entries(trees))
        )
    return aw.base_project_rows(bases, picker_scan.write(SCAN_NAMES[half], aw.base_entries(bases)))


def picked_rows(workspace: Path, checkouts: str, half: str) -> list[str]:
    """Stage two for either dropdown: `half` is "trees" or "bases".

    One function for both because the two differ only in which half of the scan they
    read and which builder renders it -- and the part worth having in one place is the
    token check, which is the whole safety property. A token that names no write is a
    miss, and a miss rescans the ticked checkouts rather than serving anything older;
    see `picker_scan`.

    An empty `checkouts` is the verb typed by hand with no first stage in front of it,
    and answers with the whole machine the way it did before there was one.
    """
    projects, token = picker_scan.parse_projects(checkouts)
    if projects:
        cached = picker_scan.read(SCAN_NAMES[half], token)
        if cached is not None:
            return picker_scan.select(cached, projects) or aw.empty_rows(half)
    # A miss, or a verb typed by hand with no first stage in front of it. `projects`
    # narrows the rescan in the first case and is empty in the second, which `scan`
    # reads as "every checkout" -- the answer this verb gave before there was a stage.
    found = scan(workspace, projects or None)
    return aw.tree_rows(found[0]) if half == "trees" else aw.base_rows(found[1])


def strayed_picks(picks: list[tuple[str, str]], checkouts: str) -> list[str]:
    """Ticked rows whose checkout the first stage did not return.

    Nothing in the two stages can produce one: stage two draws only the checkouts stage
    one returned. So a stray is evidence the chain itself misfired -- the extension
    resolves `${input:...}` from the value it recorded when that input LAST ran, so an
    input order that stopped putting the checkout stage first would quietly filter by
    the previous click's checkouts. On the delete task that would mean offering
    somebody a list of worktrees they did not ask to see, which is the wrong place in
    this repo to be quietly wrong.
    """
    ticked, _ = picker_scan.parse_projects(checkouts)
    if not ticked:
        return []
    return sorted({project for project, _name in picks} - set(ticked))


def stray_report(strayed: list[str], noun: str) -> str:
    """What to print when a pick names a checkout the first stage did not."""
    return (
        f"agent-worktree: ticked {noun} from {', '.join(strayed)}, which the checkout "
        "picker did not return -- the two picker stages disagree, so nothing was done. "
        "See `.claude/rules/vscode-tasks.md` on the order the inputs have to appear in."
    )


def main(argv: list[str] | None = None, runner=subprocess.run) -> int:
    raw = sys.argv[1:] if argv is None else argv
    # Ahead of `argparse`, per `.claude/rules/vscode-tasks.md`: a dismissed picker that
    # reached the parser would be a usage error, which is a red icon, a toast and a
    # `logs/` artifact for a run the user called off.
    dismissed = task_input.cancelled_inputs(raw)
    if dismissed:
        print(task_input.cancel_report("agent-worktree", dismissed))
        return EXIT_OK

    args = build_parser().parse_args(raw)
    workspace = args.workspace.resolve()
    if not workspace.is_file():
        print(f"agent-worktree: no workspace file at {workspace}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.verb in PICKER_VERBS:
            # `getattr` because the two stage-ONE verbs take no `--checkouts`: they are
            # what a checkout stage is, so there is nothing in front of them to narrow by.
            picker_rows.emit(draw(workspace, args.verb, getattr(args, "checkouts", "")))
            return EXIT_OK
        if args.verb == "list":
            print(render(scan(workspace)[0]))
            return EXIT_OK
        if args.verb == "new":
            pick = aw.parse_pick(args.pick)
            if pick is None:
                print("agent-worktree: no checkout picked -- nothing to do")
                return EXIT_OK
            strayed = strayed_picks([pick], args.checkouts)
            if strayed:
                print(stray_report(strayed, "a base branch"), file=sys.stderr)
                return EXIT_USAGE
            return create(pick[0], workspace, args.slug, pick[1], args.agent, runner)
        picks = [p for p in (aw.parse_pick(t) for t in aw.split_picks(args.picks)) if p]
        if not picks:
            print("agent-worktree: nothing ticked -- nothing to do")
            return EXIT_OK
        strayed = strayed_picks(picks, args.checkouts)
        if strayed:
            print(stray_report(strayed, "worktrees"), file=sys.stderr)
            return EXIT_USAGE
        return remove(picks, workspace, args.force == "force", runner)
    except devkit_project.ProjectError as exc:
        print(f"agent-worktree: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
