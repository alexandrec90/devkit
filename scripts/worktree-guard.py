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

  - an edit inside a checkout on a managed task branch **that carries commits of its
    own**: something deliberately put it there, and the commonest reason is "fix PR
    #42", where a fresh box would put the fix somewhere the PR never sees. A task
    branch with nothing on it is *not* one of these (see `needs_box`);
  - an edit already inside a box;
  - an edit to a **git-ignored** path (`.env`, `.local/`, `logs/`): it cannot land on
    any branch, so there is no branch for a box to protect;
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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # For annotations only. At runtime the module is bound by `load_by_path` below;
    # mypy cannot see through that, but it can resolve `scripts/worktree.py` by name.
    from worktree import Box

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


def needs_box(branch: str, protects_open_work: bool = True) -> bool:
    """True when an edit landing on `branch` would land somewhere nothing owns.

    The rule that replaces `branch-on-write.py`. That hook answered the same question
    by cutting a branch in place; this one answers it by routing the edit to a box,
    which is strictly better on the axis that matters — a box is disposable, so the
    checkout never outlives the task and never reaches any of the states `sweep.py`
    exists to find.

    Two cases decline, and both are cases where someone has already made the decision:

    - **on a managed task branch that carries commits of its own**
      (`protects_open_work`). Something deliberately put the checkout there, and the
      commonest reason is the one `branch-on-write.py` was rewritten for: "fix PR #42,
      it has conflicts" means checking that PR's branch out and editing it. Routing to
      a fresh box would put the fix somewhere the PR never sees.
    - **a branch git would not name.** Detached HEAD, or a git call that failed. The
      two are indistinguishable from here, and guessing would block edits on a machine
      where git is simply unavailable — so this declines and `sweep.py`, which is still
      running, is what catches a detached HEAD.

    **A task branch with no commits of its own is not one of them**, and used to be.
    Being on a managed task branch was the whole test, so the *first* session to leave one
    checked out turned this hook off for every session afterwards — the checkout became
    shared, unguarded space until someone parked it back on a home branch. Two sessions
    landed in one checkout that way, and neither could see it: one inherited a branch
    whose PR had already merged (`sweep.py` calls that state `spent-branch`), which
    protects no PR because there is nothing left on it to protect.

    So the question is not "is this a task branch" but "is there work here a box would
    strand". `protects_open_work` answers it, and the caller resolves it lazily —
    `branch_has_own_commits` costs a `git rev-list` and only a task branch can reach it.
    """
    if not branch:
        return False
    if not worktree.sweep.is_task_branch(branch):
        return True
    return not protects_open_work


def _git(checkout: Path, *args: str):
    """Run git in `checkout`, capturing stdout. Never raises; never opens a window."""
    return worktree.subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=worktree.sweep.NO_WINDOW,
    )


def path_is_ignored(checkout: Path, target: Path) -> bool:
    """True when `target` is git-ignored inside `checkout`.

    The premise of every block this hook issues is that the edit "would land on the home
    branch" — and for an ignored path that premise is simply false. `.env`, `.local/`,
    `logs/` and the rest of a checkout's untracked local state cannot land on any branch,
    so routing them to a box does not protect a branch from anything. It costs the turn
    and delivers nothing: the box gets its *own* seeded `.env`, so re-issuing the edit
    there writes the value into a worktree that is destroyed without ever shipping it,
    and the file the agent was actually asked to configure stays unchanged.

    `git check-ignore` is the right oracle rather than a hard-coded list of names,
    because what is ignored is per-project and already written down in `.gitignore`.
    It consults the index by default (no `--no-index`), so a *tracked* file that also
    matches an ignore rule reports as not-ignored and still gets a box — tracked is
    tracked, and that is the case the hook exists for.

    **Fails closed**: any error means "not ignored", i.e. route it. A hook that cannot
    read the repo must not start letting edits through on the strength of a failed
    subprocess.
    """
    try:
        result = _git(checkout, "check-ignore", "--quiet", "--", str(target))
    except (OSError, worktree.subprocess.SubprocessError):
        return False
    return result.returncode == 0


def current_branch(checkout: Path) -> str:
    """The branch `checkout` has checked out; "" when git will not say.

    Spawned per edit that targets a static checkout, which sounds expensive and is not:
    once the first such edit is blocked, every subsequent edit of the session goes to
    the box path and returns at the `.worktrees/` test above without reaching this.
    """
    try:
        result = _git(checkout, "branch", "--show-current")
    except (OSError, worktree.subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def branch_has_own_commits(checkout: Path) -> bool:
    """True when `checkout`'s HEAD carries commits `origin/<default>` does not.

    The signal behind `needs_box`'s `protects_open_work`: a task branch with commits of
    its own has somewhere for an edit to belong — an open PR, or one about to exist. A
    task branch with none is either freshly cut or already merged, and in both cases a
    box strands nothing.

    Local only, deliberately. The honest question is "has this branch got an open PR",
    and asking GitHub would answer it exactly — but this runs in a PreToolUse hook on
    every edit that reaches a static checkout, where a network round trip is latency the
    agent experiences as a hang. `git rev-list` against the *already fetched*
    `origin/<default>` costs milliseconds and agrees with the PR in every case that
    matters; the disagreement is a branch pushed and merged since the last fetch, which
    the next fetch resolves.

    **Fails closed** — every error returns True, meaning "decline, leave the edit
    alone". A hook that cannot read the repo must not start diverting edits into boxes
    on the strength of a failed subprocess, and declining is what this hook did for
    every task branch before the distinction existed.
    """
    try:
        default = worktree.sweep.tb.detect_default_branch(
            lambda *args: _git(checkout, *args), fallback="main"
        )
        probe = _git(checkout, "rev-list", "--count", f"origin/{default}..HEAD")
    except (OSError, worktree.subprocess.SubprocessError, ValueError):
        return True
    if probe.returncode != 0:
        return True
    try:
        return int(probe.stdout.strip()) > 0
    except ValueError:
        return True


def redirect_decision(
    target: str,
    cwd: str,
    root: Path,
    projects: list[str],
    branch_of: Callable[[Path], str] | None = None,
    commits_of_own: Callable[[Path], bool] | None = None,
    ignored: Callable[[Path, Path], bool] | None = None,
) -> tuple[str, str] | None:
    """`(project, path relative to that checkout)` when this edit needs its own box.

    None — allow, silently — for every case someone else already owns:

    - a path under `.worktrees/`: the edit is already in a box, which is the whole
      point of having sent it there. Whether it is in the *right* box is not this
      function's question — `main` asks `foreign_box` before calling here, so by the
      time this allow fires the ownership check has already passed;
    - a checkout on a managed task branch **that carries commits of its own** —
      see `needs_box`. **Whether or not the session is inside it**: the reason to
      decline is that something deliberately put that branch there and a box would
      bypass it, and where the editor happens to sit says nothing about that. A task
      branch with no commits of its own is not covered: it is either freshly cut or
      already merged, so there is no PR for a box to bypass;
    - anything outside a registered checkout, including the workspace file itself and
      any scratch directory beside the projects;
    - a **git-ignored** path inside a checkout — see `path_is_ignored`. It cannot land
      on the home branch, so there is no branch for a box to protect, and the box would
      swallow the edit whole.

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
    checkout = root / project
    # Asked before the branch is read, and it is the cheaper order: an ignored path is
    # allowed on one subprocess and never consults the branch, while for everything else
    # this is one `check-ignore` added to a path that was already going to spawn a box.
    if (ignored or path_is_ignored)(checkout, resolved):
        return None
    lookup = branch_of or current_branch
    has_commits = commits_of_own or branch_has_own_commits
    branch = lookup(checkout)
    # Resolved lazily and at most once: only a task branch can be protected, and the
    # probe is a subprocess in a hook that runs on every edit.
    protects = worktree.sweep.is_task_branch(branch) and has_commits(checkout)
    if _within(here, checkout):
        if not needs_box(branch, protects):
            return None
    elif protects:
        # The "fix PR #42" case, reached from *outside* the checkout -- which is how a
        # workspace-level session reaches it, and how `upgrade-project.py` leaves one:
        # parked on `claude/devkit-upgrade-<mmdd>` holding the adoption its commit gate
        # rejected. A box cut from `origin/<default>` puts the fix somewhere that
        # branch and its PR never see, which is the one outcome the decline exists to
        # prevent -- and the session being elsewhere does not change that.
        #
        # Asymmetric with the branch above on purpose. Inside, a branch git will not
        # name declines (git may simply be unavailable, and `sweep.py` still catches a
        # detached HEAD). From outside, silence is not consent: only a name git
        # positively reports as a task branch is allowed through.
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


def block_reason(project: str, relative: str, branch: str, inside: bool) -> str:
    """The opening line: why this edit is being routed, naming the branch it was judged on.

    Three different facts arrive here and they used to share one sentence. A session
    sitting in `carameli` on a freshly cut `claude/...` branch was therefore told the
    checkout was "parked on a home branch" — false on both counts — and did the
    reasonable thing with a message that contradicts what it can see: it read the hook
    as broken, spent its turn reporting the bug, and never re-issued the edit in the box
    that was waiting for it. A block message that misdescribes the state it blocked on
    costs more than no message, because the agent has no way to tell a wrong reason from
    a wrong decision.

    So each case says what it actually saw, and every one of them names the branch. The
    task-branch case additionally has to say *why* a task branch was not enough, since
    that is the half nothing else in the harness explains: `needs_box` asks whether there
    is work here a box would strand, not whether the name looks managed.
    """
    lands = f"an edit to {relative} would land on it with no task branch under it."
    if worktree.sweep.is_task_branch(branch):
        return (
            f"Blocked: {project} is on '{branch}', which is a task branch but carries no "
            f"commits of its own - so it is either freshly cut or already merged, there is "
            f"no open work on it for this edit to belong to, and a box strands nothing. (A "
            f"task branch WITH commits is left alone; this one has none.)"
        )
    if not branch:
        return (
            f"Blocked: git would not name a branch for {project} (detached HEAD, or git did "
            f"not answer) and this session is not inside that checkout, so {lands}"
        )
    if inside:
        return f"Blocked: {project} is parked on '{branch}', a home branch, so {lands}"
    return (
        f"Blocked: this session is not inside {project}, which is on '{branch}' "
        f"(a home branch), so {lands}"
    )


def deny_message(
    project: str,
    relative: str,
    box_path: str,
    box: str,
    notes: list[str],
    spawned: bool = True,
    inside: bool = False,
    branch: str = "",
    reason: str = "",
) -> str:
    """What the agent reads. The path first, because that is the actionable part.

    `spawned` distinguishes the first edit into a project from the fortieth. Both are
    blocked and both name the same box, but "a box has been spawned" is simply untrue
    on the reuse path, and a message that misdescribes what just happened is how an
    agent concludes it is in a loop.

    `inside` and `branch` decide the opening sentence, which is `block_reason`'s whole
    job: the reasons an edit reaches here are different states and a message that names
    the wrong one reads as a bug in the hook and invites working around it. Telling a
    session sitting in `carameli` that it "is not inside carameli" was the first version
    of that mistake; telling one on a freshly cut task branch that it is "parked on a
    home branch" was the second.

    Every remaining step is spelled out as a command, including the two that are not
    obvious from inside a session that is somewhere else:

    - **the box has no toolchain.** A worktree checks out tracked files, so there is no
      `.venv`; the guard does not wait for an install (see `worktree.apply_new`). An
      agent that goes straight to `/ship` hits the changed-scope lint gate with no ruff.
    - **`/ship` is read from the repo it runs in.** Its first step is a relative
      `python scripts/ship.py`, so run from here it would preflight *this* checkout and
      report that the box's branch is not the current one. "ship from inside the box"
      was accurate and, from a session that cannot cd, not actionable.

    `reason`, when non-empty, replaces the `block_reason` opening entirely: the
    foreign-box case is not a branch judgement, so every sentence `block_reason` can
    produce would misdescribe it, which is this docstring's own first lesson.
    """
    devkit_worktree = Path(__file__).parent / "worktree.py"
    lines = [
        reason or block_reason(project, relative, branch, inside),
        "",
        (
            "A box has been spawned for it. Re-issue the edit against:"
            if spawned
            else "This session already has a box for this project. Re-issue the edit against:"
        ),
        f"    {Path(box_path) / relative}",
        "",
        f"The box is on a fresh agent/... branch cut from origin/<default>, with its own "
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
        f"one reads the repo it is run in (`cd {box_path}`).",
        "",
        "Do NOT reap the box afterwards. `worktree.py reconcile` runs on a schedule and "
        "destroys it once its PR has actually MERGED; reaping at /ship time would do it "
        "on the strength of the push instead, which is the one moment the work exists "
        "only here if the PR was never created.",
        "",
        f"For a task worth naming, `worktree.py new {project} --slug <topic> --yes` cuts "
        f"a better-named one.",
    ]
    if notes:
        lines += ["", *[f"note: {note}" for note in notes]]
    return "\n".join(lines)


def failure_message(
    project: str,
    relative: str,
    error: str,
    inside: bool = False,
    branch: str = "",
    reason: str = "",
) -> str:
    """When spawning failed. Still a block, because allowing the edit is the bad outcome.

    Naming the manual command matters more than usual here: the whole promise of this
    hook is that being blocked is never a dead end, and a spawn that failed is the one
    case where the agent has to finish the job itself.

    Opens through `block_reason` for the same reason `deny_message` does: this line used
    to assert the session was outside the checkout and the branch was a home branch,
    and it is read in exactly the situation where an agent is already deciding whether
    to trust the hook.
    """
    return "\n".join(
        [
            reason or block_reason(project, relative, branch, inside),
            "",
            f"Spawning a box for it failed: {error}",
            "",
            "Cut one by hand and re-issue the edit there:",
            f"    python {Path(__file__).parent / 'worktree.py'} new {project} --slug <topic> --yes",
        ]
    )


def resolve_target(target: str, cwd: str) -> Path | None:
    """The edit's absolute target, or None when it cannot be resolved.

    Same resolution `redirect_decision` performs; split out so `main` can ask "is this
    under the box tier" before that function's checkout logic, which returns None for
    every box path and so cannot carry the ownership question.
    """
    if not target:
        return None
    try:
        base = Path(cwd) if cwd else Path.cwd()
        return (
            (base / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        )
    except (OSError, ValueError, RuntimeError):
        return None


def foreign_box(
    resolved: Path, root: Path, boxes: Mapping[str, Box], session: str
) -> tuple[Box, str] | None:
    """`(box, path relative to it)` when this edit targets a box leased to someone else.

    None — allow — for everything under `.worktrees/` that is not that:

    - the session's own box, including one whose lease records a hand-abbreviated
      session id (`sessions_match`);
    - an **unowned** box (empty lease session): an adopted orphan's lease cannot name
      an owner, so there is nobody to defend and blocking would dead-end every box
      that survived a lost lease file;
    - a payload with no session id: with no name to compare, a block would lock every
      box against a harness that simply did not send one;
    - a path in no live box at all — `leases.json`, the `slugs/` directory, a stray
      folder.

    This check exists because the allow at the top of `redirect_decision` used to be
    unconditional, and a second session that found a live box through `worktree.py
    list` could adopt it wholesale: two sessions' edits interleaved in one worktree
    until one of them watched files change under it mid-turn.
    """
    if not session:
        return None
    for box in boxes.values():
        home = worktree.box_path(root, box.name).resolve()
        if not _within(resolved, home):
            continue
        if not box.session or worktree.sessions_match(box.session, session):
            return None
        try:
            relative = str(resolved.relative_to(home))
        except ValueError:
            relative = resolved.name
        return box, relative
    return None


def foreign_box_reason(box: Box, relative: str) -> str:
    """The opening line for a foreign-box block: an ownership fact, not a branch one."""
    return (
        f"Blocked: {box.name} is a box leased to a different session, so an edit to "
        f"{relative} there would interleave two sessions' work in one worktree - the "
        f"collision the box tier exists to prevent."
    )


def claim_hint(box: Box, session: str) -> str:
    """The sanctioned takeover, spelled as the command that performs it."""
    return (
        f"if the user really has handed that box's work to this session, take its lease "
        f"over instead: python {Path(__file__).parent / 'worktree.py'} claim {box.name} "
        f"--session {session} --yes"
    )


def route_to_own_box(
    project: str,
    relative: str,
    workspace: Path,
    root: Path,
    session: str,
    boxes: Mapping[str, Box],
    inside: bool = False,
    branch: str = "",
    reason: str = "",
    extra_notes: tuple[str, ...] = (),
) -> int:
    """Block toward the session's box for `project`, reusing or spawning it.

    The one blocking flow both entry points share: an edit that would land on a
    checkout's home branch, and an edit aimed into another session's box. Always
    returns `EXIT_BLOCK`; the message is the variable part.
    """
    existing = worktree.find_session_box(boxes, project, session)
    if existing is not None:
        print(
            deny_message(
                project,
                relative,
                str(worktree.box_path(root, existing.name)),
                existing.name,
                list(extra_notes),
                spawned=False,
                inside=inside,
                branch=branch,
                reason=reason,
            ),
            file=sys.stderr,
        )
        return EXIT_BLOCK

    try:
        slug = session_slug(session, task_slug.read(root, session))
        plan = worktree.plan_new(project, workspace, slug=slug, session=session, fetch=True)
        ok, notes = worktree.apply_new(plan, workspace, timeout=SPAWN_TIMEOUT, provision=False)
    except Exception as exc:
        print(
            failure_message(
                project,
                relative,
                f"{type(exc).__name__}: {exc}",
                inside=inside,
                branch=branch,
                reason=reason,
            ),
            file=sys.stderr,
        )
        return EXIT_BLOCK

    if not ok:
        print(
            failure_message(
                project,
                relative,
                "; ".join(notes) or "no detail",
                inside=inside,
                branch=branch,
                reason=reason,
            ),
            file=sys.stderr,
        )
        return EXIT_BLOCK

    print(
        deny_message(
            project,
            relative,
            plan.path,
            plan.box.name,
            [*notes, *extra_notes],
            inside=inside,
            branch=branch,
            reason=reason,
        ),
        file=sys.stderr,
    )
    return EXIT_BLOCK


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
    session = str(payload.get("session_id") or payload.get("sessionId") or "")

    # The box tier first: `redirect_decision` allows everything under `.worktrees/`,
    # so the ownership question has to be asked before it swallows the path. The
    # lease read happens only for edits actually aimed at the box tier — the common
    # checkout edit never pays it here.
    target = resolve_target(edited_path(payload), cwd)
    if target is not None and _within(target, worktree.boxes_root(root).resolve()):
        boxes = worktree.live_boxes(root)
        conflict = foreign_box(target, root, boxes, session)
        if conflict is None:
            return EXIT_ALLOW
        box, relative = conflict
        return route_to_own_box(
            box.project,
            relative,
            workspace,
            root,
            session,
            boxes,
            reason=foreign_box_reason(box, relative),
            extra_notes=(claim_hint(box, session),),
        )

    # The branch is what the decision turns on, so it is also what the block message has
    # to name -- and reading it a second time would be a second subprocess per blocked
    # edit and, worse, could report a different branch than the one that was judged.
    # `redirect_decision` calls its lookup at most once, so recording it is enough.
    observed: list[str] = []

    def observe(checkout: Path) -> str:
        observed.append(current_branch(checkout))
        return observed[-1]

    decision = redirect_decision(edited_path(payload), cwd, root, projects, branch_of=observe)
    if decision is None:
        return EXIT_ALLOW
    project, relative = decision
    branch = observed[-1] if observed else ""
    inside = _within(Path(cwd or "."), (root / project).resolve())

    return route_to_own_box(
        project,
        relative,
        workspace,
        root,
        session,
        worktree.live_boxes(root),
        inside=inside,
        branch=branch,
    )


if __name__ == "__main__":
    sys.exit(main())
