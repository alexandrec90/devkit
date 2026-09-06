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
| `refresh` | rewrite the option file both dropdowns read |
| `list` | print what a refresh would write |

**This is not the box tier and must not become it.** `worktree.py` cuts a box at
`<workspace>/.worktrees/`, leases it a port and a `COMPOSE_PROJECT_NAME`, provisions its
toolchain and reaps it on a schedule; `agent-box.py spawn` is still the verb for a
session that runs a compose stack. What is here is a plain git worktree: no lease, no
provisioning, no reaper, and nothing to collide with the ports a static checkout holds.
The one thing shared is how a terminal tab is opened, which is `agent_box.open_agent` --
two copies of that would be two answers to "which window does the agent open in".

The menu is a scan, not live state: `rioj7.command-variable` can read a file and cannot
run a command, so `worktree.reconcile` rewrites it on its fifteen-minute pass exactly as
it does for `preview-task.py` and `fix-prs.py`. `new` and `remove` also rewrite it as
they finish, which the other two menus have no equivalent of and this one needs: the
worktree you just cut is the one you are most likely to want in the delete list, and a
quarter of an hour is a long time to be unable to undo a click.

Every decision is in `scripts/agent_worktrees.py`, pure and separately tested; what is
here spawns git and terminals, and takes a runner so the tests do not.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import agent_worktrees as aw
import devkit_project
import sweep
import task_branch as tb
import task_input
import worktree

# `agent-box.py` is hyphenated, so it cannot be a plain import. Loaded by path for the
# one thing worth sharing rather than copying -- see the module docstring.
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

agent_box = load_by_path("agent_box", REPO_ROOT / "scripts" / "agent-box.py")

# The file both dropdowns read. Under `logs/` for `fix-prs.MENU_CACHE`'s reason: machine
# state with the lifetime of a reconcile pass, gitignored, worth nothing to a fresh clone.
MENU_CACHE = REPO_ROOT / "logs" / "agent-worktrees.json"

# What the `new` picker offers. `none` is CLI-only and deliberately not a row: the task's
# whole purpose is opening an agent, and a worktree with nothing in it is `git worktree
# add`, which needs no dropdown.
AGENTS = ("claude", "codex", "none")

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
    found = []
    for name, path, branch in aw.nested(project_dir, listed.stdout or ""):
        inner = sweep.git_for(Path(path))
        status = inner("status", "--porcelain")
        dirty = len((status.stdout or "").strip().splitlines()) if status.returncode == 0 else 0
        found.append(aw.Tree(name, path, branch, dirty, unpushed_count(inner, branch)))
    return found


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


def scan(workspace: Path) -> tuple[dict[str, list[aw.Tree]], dict[str, list[tuple[str, str]]]]:
    """Every registered checkout's worktrees and base branches.

    Every checkout is listed even when it has neither, because the reader's question is
    "where are my worktrees", and one that silently drops out when it is empty is
    indistinguishable from one the scan could not reach.
    """
    text = workspace.read_text(encoding="utf-8")
    root = workspace.parent
    names = [name for name in devkit_project.known_projects(text) if (root / name).is_dir()]
    return (
        {name: trees_for(root / name) for name in names},
        {name: recent_bases(root / name) for name in names},
    )


def refresh_menu(workspace: Path, path: Path | None = None) -> Path | None:
    """Rebuild the option file. The path on success, None on any failure.

    Total, like `write_menu` and for the stronger reason: `worktree.reconcile` calls this
    at the end of every pass, and a menu that could not be built must never fail a
    reconcile that reaped boxes correctly. `OSError` is a workspace file that cannot be
    read and `ValueError` one that cannot be parsed as a registry -- named rather than
    caught as `Exception`, so a bug in the shapes above still surfaces as a traceback
    instead of an empty dropdown nobody can account for.

    A scan that found NO checkouts writes nothing, for `plug-projects.refresh_menu`'s
    reason: `sweep.parse_workspace` answers a registry it cannot parse with an empty list
    rather than a raise, so "no projects" is what a truncated workspace file looks like
    from here -- and overwriting a good menu with an empty one turns a transient bad read
    into two dropdowns that offer nothing until the next pass.
    """
    try:
        trees, bases = scan(workspace)
        if not trees:
            return None
        return aw.write_menu(aw.menu_payload(trees, bases), path or MENU_CACHE)
    except (OSError, ValueError):
        return None


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
    refresh_menu(workspace)
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
    refresh_menu(workspace)
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

    again = sub.add_parser("refresh", help="rewrite the menu file and stop")
    shown = sub.add_parser("list", help="print the worktrees and stop")
    for one in (new, gone, again, shown):
        one.add_argument("--workspace", type=Path, default=worktree.DEFAULT_WORKSPACE)
    return parser


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
        if args.verb == "refresh":
            written = refresh_menu(workspace)
            print(f"agent-worktree: wrote {written}" if written else "agent-worktree: not written")
            return EXIT_OK if written else EXIT_FAILED
        if args.verb == "list":
            print(render(scan(workspace)[0]))
            return EXIT_OK
        if args.verb == "new":
            pick = aw.parse_pick(args.pick)
            if pick is None:
                print("agent-worktree: no checkout picked -- nothing to do")
                return EXIT_OK
            return create(pick[0], workspace, args.slug, pick[1], args.agent, runner)
        picks = [p for p in (aw.parse_pick(t) for t in aw.split_picks(args.picks)) if p]
        if not picks:
            print("agent-worktree: nothing ticked -- nothing to do")
            return EXIT_OK
        return remove(picks, workspace, args.force == "force", runner)
    except devkit_project.ProjectError as exc:
        print(f"agent-worktree: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
