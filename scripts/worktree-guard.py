#!/usr/bin/env python3
"""PreToolUse hook: give a cross-checkout edit its own box instead of refusing it.

An agent edit must never land on a checkout's **home branch**, because that is the one
act that makes a checkout outlive its task — and every state `sweep.py` hunts for
(`needs-branch`, `spent-branch`, the anchor marker, `home_ref`) follows from it. The
agent would be manufacturing the exact backlog the sweep exists to clear.

`branch-on-write.py` used to prevent that by cutting a task branch *in place*, which
solved the branch and kept the problem: the checkout still outlived the task. It is
retired, and this hook covers both session shapes in its place.

Refusing the edit would fix that and cost the turn. So this hook does the other
thing: it **spawns the box the edit should have been made in** and hands the path
back. One box per (session, project), so a session that touches three repos gets
three boxes and a session that makes forty edits in one repo gets one.

The block is still a block — a PreToolUse hook cannot rewrite a tool's arguments, so
the edit has to be re-issued at the returned path. What it is not is a dead end: by
the time the agent reads the message, the worktree exists, is on a fresh task branch
off `origin/<default>`, and has its own `COMPOSE_PROJECT_NAME` and port lease. It does
*not* have a toolchain — installing one is minutes and a hook may not take minutes — so
the message carries the provision command along with the rest of the route out.

**Silent on everything else**, which is most calls:

  - an edit inside a checkout that is already on a `claude/...` task branch: something
    deliberately put it there, and the commonest reason is "fix PR #42", where a fresh
    box would put the fix somewhere the PR never sees (see `needs_box`);
  - an edit already inside a box;
  - any path that is not under a registered checkout;
  - any machine with no multi-root workspace file, which is every CI runner and every
    fresh clone.

Wired in devkit's own `.claude/settings.json` as well as the workspace root's. In a
devkit session it is one `Path.resolve()` and out — but it fires for real the moment a
devkit session edits a sibling checkout, which is the same class of mistake and the
reason devkit runs the hooks it ships.

Pure helpers (`edited_path`, `owning_project`, `redirect_decision`, `deny_message`)
are unit-tested in `tests/test_worktree_guard.py`; `main` is the thin shell that
spawns and reports.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
# Resolved by the sys.path insert above; `scripts/precommit/` is not a package. The
# shared loader is used because `worktree.py` is importable by name but this file is
# not — see that module's docstring for why the registration order matters.
from _loader import load_by_path

import devkit_project

# Also resolved by the sys.path insert above. Read for the slug the UserPromptSubmit
# hook recorded for this session — see `session_slug`.
import task_slug

worktree = load_by_path("worktree", Path(__file__).resolve().parent / "worktree.py")

# Claude Code hook contract, matching `enforce-capped-bash.py`: 0 allows the call, 2
# blocks it and feeds stderr back to the model. A blocking hook MUST write its reason
# to stderr — stdout is not surfaced.
EXIT_ALLOW = 0
EXIT_BLOCK = 2

# Tools that write a file — the question this hook exists to answer is "is the agent
# about to change a file, and may it land where it is pointing?".
MUTATING_TOOLS = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "create_file"}
)

# Per-git-step ceiling while spawning. Lower than `worktree.apply_new`'s default
# because an agent's tool call is blocked for the duration; a `git fetch` that has not
# answered in 30s is a network problem, and the box is better cut from a stale local
# `origin/<default>` than not cut at all.
SPAWN_TIMEOUT = 30.0


def parse_hook_input(raw: str) -> dict | None:
    """Parse raw stdin into a dict, or None when absent/malformed."""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _tool_name(payload: dict) -> str:
    return str(payload.get("tool_name") or payload.get("toolName") or "")


def edited_path(payload: dict) -> str:
    """The path a mutating tool is about to write, or "".

    Tolerates snake_case and camelCase keys as the other hooks do, and reads the
    several spellings the tools use for the same argument (`file_path` for Edit and
    Write, `path` for apply_patch/create_file, `notebook_path` for NotebookEdit).
    """
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "filePath", "path", "notebook_path", "notebookPath"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _within(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or sits underneath it."""
    try:
        return child == parent or parent in child.parents
    except (OSError, ValueError):
        return False


def owning_project(target: Path, root: Path, projects: list[str]) -> str:
    """Which registered checkout contains `target`; "" when none does.

    The longest match wins, so `apt-finder-b/x.py` is attributed to `apt-finder-b`
    rather than to `apt-finder` — the two are separate checkouts of one repo and
    routing an edit to the wrong one would be worse than not routing it at all.
    """
    best = ""
    for name in projects:
        if _within(target, root / name) and len(name) > len(best):
            best = name
    return best


def needs_box(branch: str) -> bool:
    """True when an edit landing on `branch` would land on a *home* branch.

    The rule that replaces `branch-on-write.py`. That hook answered the same question
    by cutting a branch in place; this one answers it by routing the edit to a box,
    which is strictly better on the axis that matters — a box is disposable, so the
    checkout never outlives the task and never reaches any of the states `sweep.py`
    exists to find.

    Two cases decline, and both are cases where someone has already made the decision:

    - **already on a `claude/...` task branch.** Something deliberately put the
      checkout there, and the commonest reason is the one `branch-on-write.py` was
      rewritten for: "fix PR #42, it has conflicts" means checking that PR's branch
      out and editing it. Routing to a fresh box would put the fix somewhere the PR
      never sees.
    - **a branch git would not name.** Detached HEAD, or a git call that failed. The
      two are indistinguishable from here, and guessing would block edits on a machine
      where git is simply unavailable — so this declines and `sweep.py`, which is still
      running, is what catches a detached HEAD.
    """
    return bool(branch) and not worktree.sweep.is_task_branch(branch)


def current_branch(checkout: Path) -> str:
    """The branch `checkout` has checked out; "" when git will not say.

    Spawned per edit that targets a static checkout, which sounds expensive and is not:
    once the first such edit is blocked, every subsequent edit of the session goes to
    the box path and returns at the `.worktrees/` test above without reaching this.
    """
    try:
        result = worktree.subprocess.run(
            ["git", "-C", str(checkout), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=worktree.sweep.NO_WINDOW,
        )
    except (OSError, worktree.subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def redirect_decision(
    target: str,
    cwd: str,
    root: Path,
    projects: list[str],
    branch_of: Callable[[Path], str] | None = None,
) -> tuple[str, str] | None:
    """`(project, path relative to that checkout)` when this edit needs its own box.

    None — allow, silently — for every case someone else already owns:

    - a path under `.worktrees/`: the edit is already in a box, which is the whole
      point of having sent it there;
    - a session inside the checkout it is editing, when that checkout is already on a
      task branch — see `needs_box`;
    - anything outside a registered checkout, including the workspace file itself and
      any scratch directory beside the projects.

    A session inside a checkout parked on a **home** branch is no longer among them.
    That was the case `branch-on-write.py` owned, and with that hook retired an edit
    there would land on the home branch with nothing underneath it — so it is routed
    like any other. `branch_of` is injected so the whole decision stays unit-testable
    without a repo on disk.
    """
    if not target:
        return None
    try:
        base = Path(cwd) if cwd else Path.cwd()
        resolved = (
            (base / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        )
        root = root.resolve()
        here = base.resolve()
    except (OSError, ValueError, RuntimeError):
        return None

    if _within(resolved, worktree.boxes_root(root)):
        return None
    project = owning_project(resolved, root, projects)
    if not project:
        return None
    if _within(here, root / project):
        lookup = branch_of or current_branch
        if not needs_box(lookup(root / project)):
            return None
    try:
        relative = resolved.relative_to(root / project)
    except ValueError:
        return None
    return project, str(relative)


def session_slug(session: str, recorded: str = "") -> str:
    """The branch topic for a box the guard cut.

    `recorded` is what `task-slug.py` wrote for this session on the UserPromptSubmit
    that preceded this edit, and it is the whole reason that hook exists: a PreToolUse
    hook sees a tool call, never the prompt, so without it every guard-cut box was
    named `ws-<8 hex of session id>` and every resulting PR title said nothing about
    what the PR did.

    The fallback stays for the cases where there is genuinely nothing to read — a
    session whose slug file was pruned, a tool call with no session id, a workspace
    that has not wired the slug hook — and says what it honestly is.
    """
    return recorded or (f"ws-{session[:8]}" if session else "ws")


def deny_message(
    project: str,
    relative: str,
    box_path: str,
    box: str,
    notes: list[str],
    spawned: bool = True,
    inside: bool = False,
) -> str:
    """What the agent reads. The path first, because that is the actionable part.

    `spawned` distinguishes the first edit into a project from the fortieth. Both are
    blocked and both name the same box, but "a box has been spawned" is simply untrue
    on the reuse path, and a message that misdescribes what just happened is how an
    agent concludes it is in a loop.

    `inside` distinguishes the two reasons an edit gets here, which need different
    opening sentences. From outside the checkout the problem is *where the session is*;
    from inside it the session is in the right repo and the problem is that the
    checkout is parked on a home branch. Telling a session sitting in `carameli` that
    it "is not inside carameli" reads as a bug in the hook and invites working around
    it.

    Every remaining step is spelled out as a command, including the two that are not
    obvious from inside a session that is somewhere else:

    - **the box has no toolchain.** A worktree checks out tracked files, so there is no
      `.venv`; the guard does not wait for an install (see `worktree.apply_new`). An
      agent that goes straight to `/ship` hits the changed-scope lint gate with no ruff.
    - **`/ship` is read from the repo it runs in.** Its first step is a relative
      `python scripts/ship.py`, so run from here it would preflight *this* checkout and
      report that the box's branch is not the current one. "ship from inside the box"
      was accurate and, from a session that cannot cd, not actionable.
    """
    devkit_worktree = Path(__file__).parent / "worktree.py"
    lines = [
        (
            f"Blocked: {project} is parked on a home branch, so an edit to {relative} "
            f"would land on it with no task branch under it."
            if inside
            else f"Blocked: this session is not inside {project}, so an edit to {relative} "
            f"would land on that checkout's home branch with no task branch under it."
        ),
        "",
        (
            "A box has been spawned for it. Re-issue the edit against:"
            if spawned
            else "This session already has a box for this project. Re-issue the edit against:"
        ),
        f"    {Path(box_path) / relative}",
        "",
        f"The box is on a fresh claude/... branch cut from origin/<default>, with its own "
        f"COMPOSE_PROJECT_NAME ({box}) and port lease, so its stack cannot collide with "
        f"{project}'s.",
        "",
        # ASCII only, like every other hook's runtime output: this text is read back
        # through a pipe whose encoding is the console's, and an em dash arrives as a
        # replacement character on a cp1252 Windows terminal.
        "It has no .venv or node_modules yet - install them before running its tests or "
        "shipping it:",
        f"    python {devkit_worktree} provision {box} --yes",
        "",
        f"Then run every /ship step with the BOX as the working directory, because each "
        f"one reads the repo it is run in (`cd {box_path}`), and reap it once the PR "
        f"exists:",
        f"    python {devkit_worktree} reap {box} --yes",
        "",
        f"Reap refuses while the box still holds unshipped work, so nothing can be "
        f"stranded in it. For a task worth naming, `worktree.py new {project} "
        f"--slug <topic> --yes` cuts a better-named one.",
    ]
    if notes:
        lines += ["", *[f"note: {note}" for note in notes]]
    return "\n".join(lines)


def failure_message(project: str, relative: str, error: str) -> str:
    """When spawning failed. Still a block, because allowing the edit is the bad outcome.

    Naming the manual command matters more than usual here: the whole promise of this
    hook is that being blocked is never a dead end, and a spawn that failed is the one
    case where the agent has to finish the job itself.
    """
    return "\n".join(
        [
            f"Blocked: an edit to {project}/{relative} from outside that checkout would land "
            f"on its home branch with no task branch under it.",
            "",
            f"Spawning a box for it failed: {error}",
            "",
            "Cut one by hand and re-issue the edit there:",
            f"    python {Path(__file__).parent / 'worktree.py'} new {project} --slug <topic> --yes",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    workspace = Path(args[args.index("--workspace") + 1]) if "--workspace" in args else None
    # Resolved for the same reason `worktree.main` resolves it: the box path in the block
    # message is built from `workspace.parent`, and the agent has to be able to use it
    # from wherever its next tool call runs.
    workspace = (workspace or worktree.DEFAULT_WORKSPACE).resolve()
    # No multi-root registry means no cross-checkout edit is possible: a CI runner, a
    # fresh clone, anyone else's machine. Silence is the correct answer, not an error.
    if not workspace.is_file():
        return EXIT_ALLOW

    payload = parse_hook_input(sys.stdin.read())
    if payload is None or _tool_name(payload) not in MUTATING_TOOLS:
        return EXIT_ALLOW

    try:
        projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    except OSError:
        return EXIT_ALLOW

    cwd = str(payload.get("cwd") or "")
    root = workspace.parent
    decision = redirect_decision(edited_path(payload), cwd, root, projects)
    if decision is None:
        return EXIT_ALLOW
    project, relative = decision
    session = str(payload.get("session_id") or payload.get("sessionId") or "")
    inside = _within(Path(cwd or "."), (root / project).resolve())

    existing = worktree.find_session_box(worktree.live_boxes(root), project, session)
    if existing is not None:
        print(
            deny_message(
                project,
                relative,
                str(worktree.box_path(root, existing.name)),
                existing.name,
                [],
                spawned=False,
                inside=inside,
            ),
            file=sys.stderr,
        )
        return EXIT_BLOCK

    try:
        slug = session_slug(session, task_slug.read(root, session))
        plan = worktree.plan_new(project, workspace, slug=slug, session=session, fetch=True)
        ok, notes = worktree.apply_new(plan, workspace, timeout=SPAWN_TIMEOUT, provision=False)
    except Exception as exc:
        print(failure_message(project, relative, f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return EXIT_BLOCK

    if not ok:
        print(failure_message(project, relative, "; ".join(notes) or "no detail"), file=sys.stderr)
        return EXIT_BLOCK

    print(
        deny_message(project, relative, plan.path, plan.box.name, notes, inside=inside),
        file=sys.stderr,
    )
    return EXIT_BLOCK


if __name__ == "__main__":
    sys.exit(main())
