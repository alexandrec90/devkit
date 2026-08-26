#!/usr/bin/env python3
"""Run a generic project action in a chosen checkout — the backend for the shared tasks.

Every task that is *generic* (test, lint, sync the agent context) used to be copied
into each project's `.vscode/tasks.json`, where the copies drifted. They now live once
in `alex-projects.code-workspace` and route through here with `--project`, so there is
one definition and the project is an argument rather than a duplicated file.

**This dispatches to the project's own script; it does not reimplement one.** The
scripts genuinely differ per project by design — `templates/core/scripts/*.tmpl` ships
the baseline, devkit's own copies diverge deliberately (see `lint-all.py`'s docstring),
and carameli's have grown Docker/CI/frontend handling. What is shared is the *CLI
contract*: a conforming project exposes `scripts/<name>.py` accepting the arguments in
`ACTIONS`. That contract is what makes one task work everywhere, and `--check` reports
who satisfies it.

A project that does not ship the script gets a named error listing who does, rather
than a `FileNotFoundError` from a wrong cwd. Every active checkout, including IBKR's
uv-based project, now exposes these small contract entrypoints; implementation remains
local because the projects genuinely have different test and lint pipelines.

**Not every action is generic, and that is fine.** An `Action` may name the checkouts it
applies to (`projects=`), which is what let the last project-level `.vscode/tasks.json`
files be deleted outright. Defining a task here once, rather than in the repo, keeps one
quick-pick entry per action without pretending a Playwright run or an IBKR backtest is
something every project can do. (This also read "a task is duplicated once per
worktree", which was the `-b` tier's doing: every repo was checked out twice, so a
repo-level task appeared twice with no way to tell the copies apart. That tier is gone —
agent work lands in an ephemeral box instead — but the argument for defining tasks here
survives it.)
`projects` restricts both halves: the dispatcher refuses an out-of-scope checkout, and
`--check` stops demanding the script from projects the action was never meant for.

Pure helpers (`resolve_project`, `in_scope`, `plan_command`) are unit-tested in
`tests/test_devkit_project.py`; `main` is the thin subprocess shell around them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
# The workspace file is the project registry: it lists the checkouts and, since the
# task de-duplication, carries the shared task block that calls this script. Resolved
# through `sweep` because this script is run from inside an ephemeral box as a matter
# of course — `--adopt-tasks` is the documented way to record a task-block edit, and
# the naive `REPO_ROOT.parent` made it exit 2 there, naming `.worktrees/<file>`.
DEFAULT_WORKSPACE = sweep.default_workspace(REPO_ROOT)

# Checkouts that are not projects in this sense. VanillaLand is the legacy reference
# monolith — it ships no harness and nothing here applies to it.
NOT_PROJECTS: frozenset[str] = frozenset({"VanillaLand"})


class ProjectError(ValueError):
    """The project name is unknown, or does not implement the requested action."""


@dataclass(frozen=True)
class Action:
    """One generic action: which script implements it, and how it is announced."""

    script: str  # relative to the owner's root, e.g. "scripts/lint-all.py"
    label: str  # shown by notify-wrap and in the terminal title
    args: tuple[str, ...] = ()  # fixed arguments before any the caller adds
    # Who implements it. PROJECT: each checkout ships its own (they differ by design,
    # and the shared thing is the CLI contract). DEVKIT: one implementation here, run
    # *with cwd set to the chosen checkout* — for work that is the same everywhere but
    # still has to happen inside a specific repo, like the git sync.
    owner: str = "project"
    # Whether this action rewrites tracked files with output nobody authored. Those
    # changes strand on a home branch unless something ships them -- see
    # `autofix_ship_plan`.
    autofix: bool = False
    # A file-rewriting action may label the PR it produces. Keeping this metadata on the
    # action makes auto-merge an explicit per-producer decision: an empty tuple preserves
    # lint's existing unlabelled review path, while Codex context declares that its
    # deterministic mirror is safe for the shared workflow to land after the gate.
    autofix_slug: str = ""
    autofix_labels: tuple[str, ...] = ()
    # Which checkouts this action applies to; empty means every one of them. Naming the
    # checkout (`("carameli",)`) is how a genuinely project-specific action lives here
    # instead of in that repo's `.vscode/tasks.json`. Names rather than a capability
    # probe because the workspace pickers already list checkouts by name, and a stale
    # entry fails loudly here.
    projects: tuple[str, ...] = ()


PROJECT = "project"
DEVKIT = "devkit"

# Project scopes for actions that only one repo can run.
#
# These were worktree *pairs* -- `("carameli", "carameli-b")` -- back when every repo was
# checked out twice so two tasks could run at once. The `-b` tier is gone: an agent's
# work now lands in an ephemeral box (`worktree.py`), which gives unbounded concurrency
# instead of exactly two and does not leave a checkout outliving its task. What is left
# is a one-element scope, kept as a tuple because `Action.projects` is a tuple and a
# second checkout of some repo may well come back.
CARAMELI = ("carameli",)
IBKR = ("ibkr_trader",)

# The source checkout, as a scope. One action is genuinely devkit-only rather than
# merely implemented here: the live-CLI hook smokes live in `tests/`, which is the tree
# `sync-devkit.py` never vendors, so no consumer has the file to run. Scoped rather than
# DEVKIT-owned-and-unscoped, because those differ in what happens when someone picks a
# consumer -- owning it here would run pytest in that repo and collect nothing, and a
# suite that collects nothing exits green.
DEVKIT_ONLY = ("devkit",)

# Checkouts with a database and an Alembic tree. Scoped by NAME like the pairs above
# rather than probed for the script, because `--check`'s whole job is to report a project
# that is MISSING its `db-revision.py` — a probe for that same file could never say so.
#
# devkit is absent because it has no database at all (`.devkit.toml` declares
# `[db] enabled = false`), and an unscoped action would report it, plus every future
# `bare` preset, as permanently non-conforming.
#
# A NEWLY GENERATED project with alembic is not in here until someone adds it, and until
# then it has no one-click migration task. That is the deliberate trade: the alternative
# is a task in the generated `.vscode/tasks.json`, which is the per-worktree duplicate
# this whole arrangement removes. `new-project.py` cannot extend this tuple — it is
# devkit source, not the workspace registry the generator already maintains.
DB_PROJECTS = CARAMELI + IBKR

# The shared contract. Adding an entry here is the *only* place a new generic task
# needs defining — the workspace task block passes the key through verbatim.
ACTIONS: dict[str, Action] = {
    # --- implemented by each checkout ---
    "test": Action("scripts/run-tests.py", "Test: Run Suite"),
    # Scope is a picker argument, not a second action -- `lintScope` in the workspace
    # file answers "everything or changed?" and the answer travels as `--changed` or as
    # nothing at all. There used to be a `lint-changed` twin here, and it bought a second
    # task, a second icon and a second label for one flag; `test` has never had one, and
    # the two now differ only in the script they call. Empty picker tokens are dropped in
    # `plan_command`, which is what lets the wide branch pass no argument.
    "lint": Action(
        "scripts/lint-all.py",
        "Lint: Run",
        autofix=True,
        autofix_slug="lint-autofix",
    ),
    "sync-codex": Action(
        "scripts/sync-codex-context.py",
        "Agent: Sync Codex Context",
        autofix=True,
        autofix_slug="codex-context-sync",
        autofix_labels=(sweep.AUTOFIX_LABEL, sweep.AUTOMERGE_LABEL),
    ),
    "sync-devkit": Action("scripts/sync-devkit.py", "Harness: Check Drift"),
    # --- implemented once, here ---
    "sync-branch": Action("scripts/git-sync-keep.py", "Git: Sync Branch", owner=DEVKIT),
    # Stack lifecycle. DEVKIT-owned with a per-project override rather than
    # PROJECT-owned, which is the same shape `docker-prune` already uses: the compose
    # topologies genuinely differ (carameli waits on healthchecks, ibkr_trader scopes
    # to its `ibkr`/`app` profiles) and `docker-maint.py` delegates to a repo's own
    # script when it ships one. PROJECT-owned would have been wrong here — devkit and
    # a `bare` preset have no stack at all, so requiring the script of everyone would
    # make the shared contract unsatisfiable for the checkouts that correctly lack it.
    "docker-up": Action(
        "scripts/docker-maint.py", "Docker: Start Stack", ("up", "--build"), owner=DEVKIT
    ),
    "docker-down": Action("scripts/docker-maint.py", "Docker: Stop Stack", ("down",), owner=DEVKIT),
    # DEVKIT-owned because the invocation is byte-identical in every consumer: the
    # vendored tier lives at the same path everywhere and pytest's `testpaths`
    # excludes it everywhere. A PROJECT-owned copy would be four identical scripts.
    "test-hooks": Action("scripts/hook-tests.py", "Test: Harness Hook Tests", owner=DEVKIT),
    # The paid counterpart, and PROJECT-owned in a scope of one rather than DEVKIT-owned:
    # the script is devkit's own, so `--check` should demand it of devkit and of nobody
    # else. It launches a real CLI, which is why it is a separate action instead of a
    # flag on `test-hooks` -- a cost that can be reached by mistyping an argument to the
    # free task is a cost nobody consented to.
    "test-hooks-live": Action(
        "scripts/hook-tests-live.py",
        "Test: Harness Hook Tests — Live CLI",
        projects=DEVKIT_ONLY,
    ),
    "docker-restart-engine": Action(
        "scripts/docker-maint.py", "Docker: Restart Engine", ("restart-engine",), owner=DEVKIT
    ),
    "docker-fix": Action(
        "scripts/docker-maint.py", "Docker: Fix Stalled Desktop", ("fix",), owner=DEVKIT
    ),
    "docker-prune": Action(
        "scripts/docker-maint.py", "Docker: Prune + Compact VHDX", ("prune",), owner=DEVKIT
    ),
    # Machine scope rather than Docker scope, and that distinction is the reason it is a
    # separate action instead of another `docker-maint.py` mode. What makes the laptop
    # slow after a few hours is three unrelated things -- containers bind-mounting Windows
    # paths spinning `com.docker.backend.exe` across the 9p bridge, temp trees nobody
    # collects, and a commit charge that grows the pagefile onto the same volume -- and
    # only the first is Docker's. `docker-maint.py prune` cannot see the other two.
    #
    # `--yes` is baked in because a task the user clicks to clean up should clean up; the
    # script's own default is a dry run, which is what an agent gets when it runs it.
    #
    # DEVKIT_ONLY rather than DEVKIT-owned-and-unscoped, and the difference is not
    # cosmetic here: an unscoped action is run once per *selected checkout*, and this one
    # has no project dimension at all -- it sweeps `%TEMP%`, stops every container on the
    # machine and reconciles the box registry. Picking three checkouts would sweep the
    # same machine three times, the second and third finding nothing and reading as a
    # no-op. Scoped, the task pins `--project devkit` and there is no picker to get wrong.
    "reclaim": Action(
        "scripts/reclaim.py", "Machine: Reclaim Resources", ("--yes",), projects=DEVKIT_ONLY
    ),
    # Reviewing a UI change before its PR merges: host Vite on the picked branches'
    # frontends -- `npm run dev`, one port each, no Docker anywhere in it.
    #
    # There used to be a second, clickable preview here (`preview`, on
    # `scripts/preview-task.py`) that answered the same question with a compose stack per
    # branch: a box, a port lease, an image build and an `npm ci` into a fresh named
    # volume, about three minutes cold. It carried this label, so the task called
    # *Open a UI Branch* was the expensive one, and the cheap one that does exactly what
    # the label promises sat below it under a name nobody reads as the same question.
    # Looking at a UI change needs node, the branch's files and a free port; the stack it
    # was buying is one the reviewer never calls. `worktree.py preview` is still the CLI
    # verb for the full-stack kind -- it is just not a thing anyone can click by mistake.
    #
    # DEVKIT_ONLY for `reclaim`'s reason rather than its own, and it is worth repeating
    # because this looks far more like a per-project action than it is: the menu it picks
    # from is assembled from the box registry and the port registry, and there is exactly
    # ONE of each on this machine. Run once per selected checkout it would print the same
    # machine-wide menu two or three times over. The project dimension lives INSIDE the
    # menu instead, as the first dropdown -- so no picker here, per the
    # literal-over-single-option convention: the task pins `--project devkit`.
    "preview-ui-host": Action(
        "scripts/preview-ui-host.py", "Preview: Open a UI Branch", projects=DEVKIT_ONLY
    ),
    # The teardown half of the pair above, and the reason it is a task at all rather than
    # something the serving task promises to do on its way out: a Vite server outlives the
    # run that started it whenever the terminal is closed instead of interrupted, and the
    # `finally` clause that would have stopped it never runs. Three nets catch that inside
    # `preview-ui-host.py`; this is the fourth, the one a human can click when they want
    # every server on the machine gone now, regardless of which terminal owns it.
    #
    # DEVKIT_ONLY and no picker for the same reason as its siblings -- there is one
    # registry of running servers on this machine, so there is no checkout to pick.
    "preview-ui-stop": Action(
        "scripts/preview-ui-host.py",
        "Preview: Stop Host UI Servers",
        ("--stop",),
        projects=DEVKIT_ONLY,
    ),
    # Cutting a devkit release. DEVKIT_ONLY because a release is devkit's own act and
    # has no project dimension at all -- run per selected checkout it would try to tag
    # this repo two or three times, and the second attempt would refuse a tag that now
    # exists. The consumers appear at the END of the run, as `upgrade-project.py --all`,
    # which is a different thing from the task being scoped to them.
    #
    # It is an ACTIONS entry rather than a hand-written task like `Devkit: Upgrade
    # Projects` because it needs the wrapping more than that one does, not less: the run
    # spans a PR gate and a workflow dispatch, so its useful output arrives minutes apart
    # and the reader is not watching. `plan_command` gives it the notify toast and the
    # `logs/` artifact for free, and the level picker rides through as a trailing
    # argument the same way `db-revision`'s `-m` does.
    "release": Action("scripts/release-pipeline.py", "Devkit: Cut Release", projects=DEVKIT_ONLY),
    # --- scoped to one repo's worktree pair ---
    #
    # These are the tasks that used to live in `carameli/.vscode/tasks.json` and
    # `ibkr_trader/.vscode/tasks.json`. Nothing about them became generic — a Playwright
    # run and an IBKR backtest are not things devkit or a `bare` preset can do. What
    # changed is where the duplication was: a task defined in a repo is rendered once per
    # *worktree folder*, so every one of these appeared twice in the quick-pick with no
    # way to tell the copies apart, and the two copies drifted whenever the worktrees sat
    # on different branches. Here they are defined once and the checkout is a picker.
    #
    # `projects=` is what keeps `--check` honest about it: without the scope, every one of
    # these would be demanded of every checkout and each project would report five or six
    # phantom gaps.
    "test-target": Action("scripts/run-tests.py", "Test: Run Carameli Target", projects=CARAMELI),
    "e2e": Action("scripts/run-e2e.py", "Test: Run Browser E2E", projects=CARAMELI),
    # The carameli <-> VanillaLand local integration suite, both directions. carameli
    # owns the orchestrator: it boots the VanillaLand-side harness (VS_REPO_DIR in its
    # `.env.local-e2e`), runs tests/local_e2e, then the .NET outbound driver. VanillaLand
    # cannot own an action — it is in NOT_PROJECTS — so carameli fronts for the pair.
    "local-e2e": Action(
        "scripts/local-e2e.py", "Test: Run Local Integration E2E", projects=CARAMELI
    ),
    "ngrok": Action("scripts/start-ngrok.py", "Start: ngrok + Sync URLs", projects=CARAMELI),
    # Takes no argument on purpose. The script encodes every master in
    # `frontend/assets-src/comic-book/` that has no export yet and skips the rest, so
    # the task is "I dropped pictures in, pick them up" rather than a prompt for a
    # filename the picker cannot validate. Naming one is still a CLI call, where
    # `--label` and `--max-edge` live.
    "encode-art": Action(
        "scripts/encode-comic-art.py", "Assets: Encode Comic-Book Art", projects=CARAMELI
    ),
    "vnc": Action("scripts/vnc-viewer.py", "IBKR: Open Gateway VNC Viewer", projects=IBKR),
    "ingest": Action("scripts/ingest-task.py", "Ingest: Run Source", projects=IBKR),
    "snapshot-monthly": Action(
        "scripts/snapshot-monthly.py", "Snapshot: Run Monthly", projects=IBKR
    ),
    # One script, two subcommands — the OOS run fixes its own warm-up and simulation
    # starts, so it cannot just be `backtest` with different picker answers.
    "backtest": Action("scripts/backtest-task.py", "Backtest: Run", ("run",), projects=IBKR),
    "backtest-oos": Action(
        "scripts/backtest-task.py", "Backtest: OOS (Honest Per-Fold)", ("oos",), projects=IBKR
    ),
    # --- scoped to the checkouts that have a database ---
    #
    # The last task to leave a `.vscode/tasks.json`, and the only one that was in the
    # GENERATOR template rather than a live repo. PROJECT-owned because the two
    # implementations genuinely differ and neither is a wrapper for the other: carameli
    # runs alembic inside its app container (PgBouncer sits in front of Postgres, so the
    # DDL connection has to bypass the pooler, and the container's env is the single
    # source of that URL), while ibkr_trader runs it on the host through uv against
    # Postgres on 5433, where there is no pooler and the `app` profile is a scheduler
    # rather than a dev shell. Same CLI — `-m "<message>"` — different bodies, which is
    # exactly the split `run-tests.py` and `lint-all.py` already use.
    "db-revision": Action("scripts/db-revision.py", "DB: New Migration", projects=DB_PROJECTS),
}

# Wrapper every generated project ships (`templates/core/scripts/notify-wrap.py`). When
# present the command routes through it so a long task still notifies on completion;
# when absent the command runs bare rather than failing.
NOTIFY_WRAP = "scripts/notify-wrap.py"

# The failure artifact, taken from **devkit's** checkout rather than the target's. Two
# reasons, and the second is the load-bearing one: a checkout that has not yet pulled
# the release adding `scripts/log-wrap.py` still gets an artifact, and the wrapper that
# runs is the one this dispatcher was tested against rather than whatever vintage the
# target vendored. Unconditional, unlike NOTIFY_WRAP above -- it is a MANIFEST entry of
# devkit's own, so its absence is a broken checkout and should say so loudly instead of
# quietly dropping every task's artifact.
LOG_WRAP = "scripts/log-wrap.py"


# --- pure helpers -----------------------------------------------------------


def known_projects(workspace_text: str) -> list[str]:
    """Checkout names from the workspace registry, minus the non-projects.

    Reuses `sweep.parse_workspace` rather than re-reading the file: one parser for
    the registry means the sweep and the task dispatcher can never disagree about
    which checkouts exist.
    """
    return sweep.parse_workspace(workspace_text, NOT_PROJECTS)


def project_selection(value: str) -> list[str]:
    """Checkout names from a comma-delimited task input, preserving order.

    VS Code command inputs must return one string.  The multi-pick used by the shared
    test task therefore joins its selections with commas; the original single-project
    CLI remains a one-item instance of the same format.
    """
    return list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))


def resolve_project(name: str, projects: list[str], root: Path, noun: str = "project") -> Path:
    """The checkout directory for `name`, validated against the registry.

    Validating against the registry (not just `is_dir()`) is what makes a typo'd or
    stale picker entry fail with the list of real names instead of running in a
    directory that happens to exist.

    `noun` is what the failure calls the thing that was not found. It exists because
    `git-merge-default.py` resolves against the RAW registry -- reference checkouts
    included, since merging a trunk needs git and no harness — and telling someone
    `unknown project 'VanillaLand'` about a folder this file deliberately excludes from
    the word "project" would send them to fix the wrong list.
    """
    if not name:
        raise ProjectError(f"no {noun} given; expected one of: {', '.join(projects)}")
    if name not in projects:
        raise ProjectError(f"unknown {noun} {name!r}; the workspace lists: {', '.join(projects)}")
    path = root / name
    if not path.is_dir():
        raise ProjectError(f"{name} is registered in the workspace but {path} does not exist")
    return path


def in_scope(action: Action, project: str) -> bool:
    """Whether `action` applies to `project`. An action with no `projects` applies to all."""
    return not action.projects or project in action.projects


def check_scope(action: Action, project: str) -> None:
    """Refuse an action aimed at a checkout it was never defined for.

    The workspace gives each scoped task a picker listing only its own checkouts, so this
    is a backstop rather than the first line of defence — but the CLI is public and the
    picker is only a list of strings. Without it, `--project devkit backtest` would fall
    through to the missing-script error and read as "devkit has not implemented backtesting
    yet", which invites someone to go and implement it.
    """
    if not in_scope(action, project):
        raise ProjectError(
            f"{project} is out of scope for this action; it is defined for: "
            f"{', '.join(action.projects)}"
        )


def plan_command(
    action: Action, project_dir: Path, extra: list[str], devkit_root: Path = REPO_ROOT
) -> list[str]:
    """The argv to run for `action`: the script, wrapped for logging and notification.

    Raises `ProjectError` when the script is missing — for a PROJECT action that is
    the conformance failure, reported by name rather than as a missing-file traceback
    from an unexpected cwd. A DEVKIT action is referenced by absolute path because it
    runs with cwd set to the *checkout*, not to devkit.

    **Where every dispatched task gets its failure artifact.** Twenty of the workspace's
    tasks are one of these, and this is the only point that can write the artifact to
    the right place: the task names a *picker*, so until `resolve_project` has run
    nothing knows which checkout's `logs/` a failure belongs in. Wrapping here covers
    every action, including ones added later, without touching the workspace file.

    Nesting is `notify-wrap → log-wrap → the script`, each doing one thing: the toast
    needs only an exit code, the artifact needs the output, and the script needs
    neither to know about it.
    """
    if action.owner == DEVKIT:
        script = devkit_root / action.script
        if not script.is_file():
            raise ProjectError(f"devkit is missing {action.script} — its checkout is incomplete")
        target = str(script)
    else:
        script = project_dir / action.script
        if not script.is_file():
            raise ProjectError(
                f"{project_dir.name} does not implement this action: it has no {action.script}"
            )
        target = action.script
    inner = ["python", target, *action.args, *[a for a in extra if a]]
    logged = ["python", str(devkit_root / LOG_WRAP), action.label, "--", *inner]
    if (project_dir / NOTIFY_WRAP).is_file():
        return ["python", NOTIFY_WRAP, action.label, "--", *logged]
    return logged


# --- autofix that would otherwise strand ------------------------------------
#
# `lint-all.py` rewrites the tree before it reports -- `ruff check --fix
# --unsafe-fixes`, `ruff format`, and in the projects that ship it a detect-secrets
# baseline that auto-acknowledges its own new findings. Nobody authored those edits,
# so nobody feels responsible for committing them, and the checkout they land in is
# the static one parked on its home branch: exactly the write `worktree-guard.py`
# routes into a box when an *agent* makes it. The lint task is the hole in that
# guarantee. It writes the same files with no task branch underneath, and the churn
# surfaces days later as a `needs-branch` verdict that reads as though a human left
# it there. `sync-codex-context.py` has the same shape: its entire purpose is to rewrite
# committed generated artifacts, and the scheduled checkout sync correctly refuses to
# move a dirty home branch. Waiting for reconcile therefore strands those outputs by
# design; the producer has to package them while it can still attribute them.
#
# So the dispatcher finishes what the task started. `sweep.py` already takes stranded
# work from a home branch to an open PR -- `--branch`, then `--ship` -- and the only
# new judgement needed is whether that is the honest thing to do with what this run
# left behind. Each action's distinct branch slug names the producer. Labels are also
# per-action: Codex's deterministic mirror carries `autofix` provenance and `automerge`
# authorization, while lint keeps its established unlabelled review path.
AUTOFIX_SLUG = "lint-autofix"


@dataclass(frozen=True)
class AutofixOutcome:
    """What to do with the files an autofix action rewrote: commands, or a reason not to.

    Never both. A note is the mode's way of declining out loud -- the churn is real
    either way, and a silent decline is how it goes unnoticed until the sweep finds it.
    """

    commands: tuple[tuple[str, ...], ...] = ()
    note: str = ""


def autofix_ship_plan(
    project: str,
    branch: str,
    before: tuple[str, ...] | list[str],
    after: tuple[str, ...] | list[str],
    *,
    lint_ok: bool,
    workspace: Path,
    devkit_root: Path = REPO_ROOT,
    slug: str = AUTOFIX_SLUG,
    labels: tuple[str, ...] = (),
) -> AutofixOutcome:
    """Whether to ship what an autofix run rewrote, given the tree before and after.

    Pure: it decides from two `git status --porcelain` snapshots and a branch name,
    so every refusal below is asserted without a repository on disk.

    Ships only the case it can honestly attribute -- a checkout that was **clean**,
    on a **home branch**, whose lint run came back **green**. Each of the other three
    is a decline with a reason, and each reason is a different next action:

    - **Dirty before the run.** The diff is now part autofix and part whatever was
      already there, and nothing here read either. Sweeping both into a PR titled
      after the sweep would bury someone's work under a mechanical subject; that work
      deserves `/ship` and a real message.
    - **On a task branch.** The fixes belong to the task in progress and travel with
      its next commit. `worktree-guard.py` put agent work here precisely so this
      needs no rescue.
    - **Lint still failed.** The autofix pass fixed what it could and the report is
      non-empty, so shipping now opens a PR whose gate is already red -- and pushes a
      commit the project's own pre-commit hook would likely refuse first. The
      remaining findings get fixed, then the whole diff ships together.
    """
    fixed = sorted(set(after) - set(before))
    if not fixed:
        return AutofixOutcome()
    churn = f"autofix rewrote {len(fixed)} file(s) ({', '.join(fixed[:4])}"
    churn += ", ...)" if len(fixed) > 4 else ")"
    if not branch:
        return AutofixOutcome(
            note=f"{churn} on a detached HEAD -- check out a branch, then sweep.py --branch --yes"
        )
    if sweep.is_task_branch(branch):
        return AutofixOutcome(
            note=f"{churn} on the task branch {branch} -- they ship with the task, nothing to do"
        )
    if before:
        return AutofixOutcome(
            note=(
                f"{churn} on top of {len(before)} change(s) that were already there -- "
                f"not shipping a diff nothing has read. Review it, then /ship it"
            )
        )
    if not lint_ok:
        return AutofixOutcome(
            note=(
                f"{churn} but the run still reported findings -- fix those first, then the "
                f"whole diff ships together (a PR opened now starts with a red gate)"
            )
        )
    sweep_py = str(devkit_root / "scripts" / "sweep.py")
    common = ("python", sweep_py, "--workspace", str(workspace), "--only", project)
    label_args = tuple(arg for label in labels for arg in ("--label", label))
    return AutofixOutcome(
        commands=(
            (*common, "--branch", "--slug", slug, "--yes"),
            (*common, *label_args, "--ship", "--yes"),
        )
    )


def autofix_state(directory: Path) -> tuple[str, tuple[str, ...]]:
    """(branch, changed paths) for one checkout -- the snapshot the plan compares.

    Through `sweep.git_for` so the dispatcher and the sweep read a checkout the same
    way, including the no-console-window flag a scheduled run depends on.
    """
    git = sweep.git_for(directory)
    head = git("branch", "--show-current")
    branch = head.stdout.strip() if head.returncode == 0 else ""
    return branch, sweep.parse_porcelain(git("status", "--porcelain").stdout or "")


# --- registering a project, and retiring one --------------------------------
#
# The write side of the same registry. These edit the workspace file as TEXT rather
# than round-tripping it through `json.dumps`: the file is heavily commented, and the
# comments are the only place the folder list explains itself. A load/dump cycle would
# silently delete every one of them.
#
# Offsets are found in a comment-blanked copy and applied to the original — safe
# because `blank_comments` preserves length, and necessary because a `]` inside a
# comment would otherwise end an array early.


class RegistryEditError(ValueError):
    """The workspace file is not shaped the way registration expects."""


def _array_span(scan: str, start: int) -> tuple[int, int]:
    """Offsets of the `[` at/after `start` and its matching `]`."""
    try:
        open_at = scan.index("[", start)
    except ValueError as exc:
        raise RegistryEditError("no array found where one was expected") from exc
    depth = 0
    i = open_at
    in_string = False
    while i < len(scan):
        ch = scan[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return open_at, i
        i += 1
    raise RegistryEditError("unterminated array in the workspace file")


def _entry_spans(scan: str, open_at: int, close_at: int) -> list[tuple[int, int]]:
    """Offsets of each top-level `{...}` object directly inside an array."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    i = open_at + 1
    in_string = False
    while i < close_at:
        ch = scan[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                spans.append((start, i + 1))
        i += 1
    return spans


def _indent_of(text: str, offset: int) -> str:
    """The whitespace beginning the line `offset` sits on — so inserts line up."""
    line_start = text.rfind("\n", 0, offset) + 1
    return text[line_start:offset] if not text[line_start:offset].strip() else "\t\t"


def insert_folder(text: str, name: str) -> str:
    """Add `{"path": name}` to `folders`, before any reference (non-project) checkout.

    Inserting before the references rather than appending keeps VanillaLand last,
    which is how the list reads in the folder tree and in a sweep report.
    """
    scan = devkit_jsonc.blank_comments(text)
    key = scan.find('"folders"')
    if key < 0:
        raise RegistryEditError('the workspace file has no "folders" array')
    open_at, close_at = _array_span(scan, key)
    spans = _entry_spans(scan, open_at, close_at)
    if not spans:
        raise RegistryEditError("the workspace file lists no folders to insert beside")

    for start, end in spans:
        if any(f'"{ref}"' in scan[start:end] for ref in NOT_PROJECTS):
            indent = _indent_of(text, start)
            entry = f'{{\n{indent}\t"path": "{name}"\n{indent}}},\n{indent}'
            return text[:start] + entry + text[start:]

    start, end = spans[-1]
    indent = _indent_of(text, start)
    entry = f',\n{indent}{{\n{indent}\t"path": "{name}"\n{indent}}}'
    return text[:end] + entry + text[end:]


def insert_picker_option(text: str, name: str) -> str:
    """Add `name` to the maintained single- and multi-project picker options.

    VS Code cannot build these at runtime (microsoft/vscode#81370), so the list is
    written here instead. It is a convenience only — `resolve_project` validates the
    answer against `folders`, so a missed update costs a picker entry, not correctness.
    """
    updated = text
    for picker_id in (
        "project",
        "daemonProject",
        "worktreeProject",
        # Lists MORE than the registry -- the reference checkouts too -- so a new
        # project still has to be added to it, and this is the only place that can.
        "mergeCheckout",
    ):
        scan = devkit_jsonc.blank_comments(updated)
        marker = scan.find(f'"id": "{picker_id}"')
        if marker < 0:
            if picker_id == "project":
                raise RegistryEditError('the workspace file has no "project" input to extend')
            continue  # Older/minimal registries do not yet carry the optional multi-pick.
        options_at = scan.find('"options"', marker)
        if options_at < 0:
            raise RegistryEditError(f'the "{picker_id}" input has no options array')
        open_at, close_at = _array_span(scan, options_at)

        last_quote = scan.rfind('"', open_at, close_at)
        if last_quote < 0:
            raise RegistryEditError(f"the {picker_id} picker lists no options to insert beside")
        line_start = updated.rfind("\n", 0, last_quote) + 1
        indent = updated[
            line_start : len(updated[line_start:]) - len(updated[line_start:].lstrip()) + line_start
        ]
        updated = updated[: last_quote + 1] + f',\n{indent}"{name}"' + updated[last_quote + 1 :]
    return updated


def register(text: str, names: list[str]) -> str:
    """Register each of `names` as a project: a folder entry plus a picker option.

    Verifies the result before returning it. A half-applied edit would leave the file
    unparseable, and `sweep.parse_workspace` swallows that as "no checkouts" — the
    exact silent failure this whole change was made to remove.
    """
    updated = text
    for name in names:
        if name in known_projects(updated):
            continue  # re-running the generator must not double-register
        updated = insert_folder(updated, name)
        updated = insert_picker_option(updated, name)

    try:
        devkit_jsonc.loads(updated)
    except json.JSONDecodeError as exc:
        raise RegistryEditError(f"registration produced invalid JSONC: {exc}") from exc
    missing = [n for n in names if n not in known_projects(updated)]
    if missing:
        raise RegistryEditError(f"registration did not take effect for: {', '.join(missing)}")
    return updated


def _drop_element(text: str, scan: str, start: int, end: int) -> str:
    """Remove the array element at `text[start:end]`, taking one comma with it.

    Two shapes, because a JSON array's separator belongs to whichever neighbour
    survives: an element with a comma after it takes that comma and the rest of its
    line, while the last element takes the comma *before* it instead. Getting this
    wrong is not a formatting complaint — a stray comma is a trailing comma the
    workspace file's own parser rejects, which is why `unregister` reparses.

    Offsets are read from `scan`, the comment-blanked copy, so a `//` line sitting
    between two entries cannot contribute a comma the parser never saw.
    """
    after = end
    while after < len(scan) and scan[after] in " \t\r\n":
        after += 1
    if after < len(scan) and scan[after] == ",":
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start].strip():
            line_start = start  # something else shares the line; keep it
        cut = after + 1
        while cut < len(scan) and scan[cut] in " \t":
            cut += 1  # a trailing comment belonged to the element being removed
        if scan.startswith("\r\n", cut):
            cut += 2
        elif cut < len(scan) and scan[cut] == "\n":
            cut += 1
        return text[:line_start] + text[cut:]

    before = start
    while before > 0 and scan[before - 1] in " \t\r\n":
        before -= 1
    if before > 0 and scan[before - 1] == ",":
        return text[: before - 1] + text[end:]
    raise RegistryEditError("cannot remove the only element of an array")


def remove_folder(text: str, name: str) -> str:
    """Drop `name`'s entry from `folders`. The inverse of `insert_folder`.

    Matched on `path`, whitespace-insensitively, because VS Code rewrites this file
    itself whenever a workspace setting is changed through its UI and its spacing is
    not ours to predict. `name` is deliberately not consulted: a folder may carry a
    display label (`VanillaLand (reference)`) and the path is what every reader keys on.
    """
    scan = devkit_jsonc.blank_comments(text)
    key = scan.find('"folders"')
    if key < 0:
        raise RegistryEditError('the workspace file has no "folders" array')
    open_at, close_at = _array_span(scan, key)
    for start, end in _entry_spans(scan, open_at, close_at):
        if f'"path":"{name}"' in "".join(scan[start:end].split()):
            return _drop_element(text, scan, start, end)
    raise RegistryEditError(f'"{name}" is not in the workspace folders list')


def _retarget_default(text: str, scan: str, close_at: int, replacement: str) -> str:
    """Repoint an input's `default` when the option it named has just been removed.

    A `pickString` whose default is not among its options renders that dead value as
    the pre-filled answer, so retiring `carameli` would leave three pickers offering it
    as the one checkout that no longer exists. Bounded by the next `"id":` because
    these inputs are a flat list and `default` always follows `options` within one.
    """
    limit = scan.find('"id":', close_at)
    limit = len(scan) if limit < 0 else limit
    at = scan.find('"default"', close_at, limit)
    if at < 0:
        return text
    value_open = scan.find('"', scan.index(":", at) + 1)
    value_close = scan.find('"', value_open + 1)
    if value_open < 0 or value_close < 0 or value_close > limit:
        return text
    return text[: value_open + 1] + replacement + text[value_close:]


def remove_picker_option(text: str, name: str) -> str:
    """Drop `name` from every maintained picker. The inverse of `insert_picker_option`.

    A picker that never listed it is left alone rather than failed on: `mergeCheckout`
    lists more than the registry and an older workspace file may carry fewer pickers,
    so "not there" is the same outcome as "removed" and neither is an error.
    """
    updated = text
    for picker_id in ("project", "daemonProject", "worktreeProject", "mergeCheckout"):
        scan = devkit_jsonc.blank_comments(updated)
        marker = scan.find(f'"id": "{picker_id}"')
        if marker < 0:
            if picker_id == "project":
                raise RegistryEditError('the workspace file has no "project" input to trim')
            continue
        options_at = scan.find('"options"', marker)
        if options_at < 0:
            raise RegistryEditError(f'the "{picker_id}" input has no options array')
        open_at, close_at = _array_span(scan, options_at)

        token = f'"{name}"'
        at = scan.find(token, open_at, close_at)
        if at < 0:
            continue
        # An element, not the value half of a `{"label": …, "value": …}` option: those
        # would need the whole object removed, and silently deleting half of one is how
        # a picker starts offering an entry that resolves to nothing.
        preceding = scan[open_at + 1 : at].rstrip()
        if preceding and preceding[-1] not in ",":
            raise RegistryEditError(f'the "{picker_id}" options are not a plain string list')

        remaining = [o for o in devkit_jsonc.loads(scan[open_at : close_at + 1]) if o != name]
        updated = _drop_element(updated, scan, at, at + len(token))
        if not remaining:
            continue
        # Re-derive the span from the UPDATED text rather than reusing `close_at`: the
        # last-element branch of `_drop_element` cuts backwards, so every offset past
        # the removal has moved and a stale one lands mid-token.
        after = devkit_jsonc.blank_comments(updated)
        options_at = after.find('"options"', after.find(f'"id": "{picker_id}"'))
        updated = _retarget_default(updated, after, _array_span(after, options_at)[1], remaining[0])
    return updated


def unregister(text: str, names: list[str]) -> str:
    """Retire each of `names`: drop its folder entry and every picker option.

    The inverse of `register`, and verified the same way for the same reason — a
    half-applied removal leaves the file unparseable, and `sweep.parse_workspace`
    swallows that as "no checkouts", which is the silent failure the registration side
    was written to avoid. Nothing on disk is touched: unplugging a project is a
    registry edit, so it stays reversible by plugging it back in.
    """
    updated = text
    for name in names:
        if name not in known_projects(updated):
            continue  # already retired; re-running the picker must not fail on it
        updated = remove_folder(updated, name)
        updated = remove_picker_option(updated, name)

    try:
        devkit_jsonc.loads(updated)
    except json.JSONDecodeError as exc:
        raise RegistryEditError(f"retirement produced invalid JSONC: {exc}") from exc
    still = [n for n in names if n in known_projects(updated)]
    if still:
        raise RegistryEditError(f"retirement did not take effect for: {', '.join(still)}")
    return updated


# --- devkit owns the workspace file -------------------------------------------
#
# The workspace file is not inside any repo, so it cannot be vendored the way
# `sync-devkit.py`'s MANIFEST files are. devkit keeps the canonical copy here instead.
#
# **The direction used to run the other way**, and that is the failure this section
# exists to stop repeating. The live file was the source and `workspace-tasks.jsonc`
# was a mirror adopted from it, which made the authoritative copy of a 2,000-line file
# the one with no branch, no history and no review. Three consequences, all of which
# happened:
#
#   - Two sessions editing it raced, last writer winning silently, with no way to
#     recover the loser's edit -- there was nothing to recover it *from*.
#   - `--adopt-tasks` mirrored the WHOLE block, so one session's adopt swallowed
#     whatever another session had left in the live file and carried it into an
#     unrelated PR.
#   - An agent's in-flight edit was live for every window on the machine the moment it
#     was written. On 2026-08-21 the live file was found running the task block of PR
#     #177, still open: 38 tasks reformatted and five deleted, in main's name.
#
# So the canonical copy is now the WHOLE file and the live one is rendered from it. An
# agent edits `workspace.jsonc` on a task branch; git carries the conflict, where a
# conflict is a visible thing with two sides rather than a silent overwrite.
#
# The comparison is SEMANTIC, not byte-for-byte. Vendored files are compared byte-wise
# because they are copied verbatim and a stray reformat downstream shows up as drift
# the consumer did not cause; this file is hand-edited *and* rewritten by VS Code
# itself whenever a workspace setting is changed through its UI, so indentation and
# key order are not meaningful and flagging them would train everyone to ignore it.

CANONICAL_WORKSPACE = REPO_ROOT / "workspace.jsonc"

# Spelled once so every remedy line in this module names the same command. It is
# printed, not run: the caller may be in a box, in devkit, or in neither.
RENDER_HINT = "python scripts/devkit_project.py"

# Compared whole. `tasks` is compared entry-by-entry instead (see `tasks_drift`),
# because "the tasks block differs" on a 2,000-line object names nothing actionable.
PLAIN_KEYS = ("folders", "extensions", "settings", "launch", "remoteAuthority")


def stamp_path(workspace: Path) -> Path:
    """Where the render stamp for `workspace` lives.

    Beside the live file, NOT under devkit's `logs/`. The stamp describes a
    machine-global file, and an ephemeral box has its own empty `logs/` -- putting it
    there would make every box's first `--render-workspace` believe the live file had
    been hand-edited, which is the one case it must not get wrong.
    """
    return workspace.with_name(".devkit-workspace-render.json")


def semantic_digest(text: str) -> str:
    """A digest of what a workspace file MEANS, ignoring layout and comments.

    A byte digest would report a hand edit every time VS Code rewrote the file after a
    settings change of its own -- an alarm nobody caused and nobody can act on.
    """
    payload = devkit_jsonc.loads(text)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def read_stamp(workspace: Path) -> str | None:
    """The digest devkit last rendered to `workspace`, or None if it never has."""
    try:
        recorded = json.loads(stamp_path(workspace).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = recorded.get("digest")
    return value if isinstance(value, str) else None


def write_stamp(workspace: Path, digest: str) -> None:
    stamp_path(workspace).write_text(
        json.dumps({"digest": digest, "workspace": workspace.name}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def workspace_tasks(text: str) -> dict:
    """The `tasks` block of a workspace file, parsed."""
    payload = devkit_jsonc.loads(text)
    block = payload.get("tasks") if isinstance(payload, dict) else None
    return block if isinstance(block, dict) else {}


def extract_tasks_text(text: str) -> str:
    """The `tasks` block as SOURCE, comments intact, dedented by one level.

    Used to regenerate the canonical copy after an intentional edit to the workspace
    file. Parsing and re-dumping would work for the comparison but would throw away
    the comments, which are most of what the block is worth reading for.
    """
    scan = devkit_jsonc.blank_comments(text)
    key = scan.find('"tasks"')
    if key < 0:
        raise RegistryEditError('the workspace file has no "tasks" block')
    try:
        open_at = scan.index("{", key)
    except ValueError as exc:
        raise RegistryEditError('"tasks" is not an object') from exc

    depth = 0
    i = open_at
    in_string = False
    while i < len(scan):
        ch = scan[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    else:
        raise RegistryEditError('unterminated "tasks" block')

    block = text[open_at : i + 1]
    return "\n".join(line[1:] if line.startswith("\t") else line for line in block.splitlines())


def _by_label(block: dict) -> dict[str, dict]:
    return {t.get("label", "<unlabelled>"): t for t in block.get("tasks", [])}


def tasks_drift(live: dict, canonical: dict) -> list[str]:
    """Human-readable differences between two task blocks; empty when they agree."""
    problems: list[str] = []
    live_tasks, canon_tasks = _by_label(live), _by_label(canonical)
    for label in sorted(canon_tasks.keys() - live_tasks.keys()):
        problems.append(f"missing from the workspace: {label}")
    for label in sorted(live_tasks.keys() - canon_tasks.keys()):
        problems.append(f"in the workspace but not in devkit: {label}")
    for label in sorted(canon_tasks.keys() & live_tasks.keys()):
        if live_tasks[label] != canon_tasks[label]:
            problems.append(f"definition differs: {label}")

    live_inputs = {i.get("id"): i for i in live.get("inputs", [])}
    canon_inputs = {i.get("id"): i for i in canonical.get("inputs", [])}
    for missing in sorted(canon_inputs.keys() - live_inputs.keys()):
        problems.append(f"missing input: {missing}")
    for extra in sorted(live_inputs.keys() - canon_inputs.keys()):
        problems.append(f"input not in devkit: {extra}")
    # Bodies too, not just ids. Comparing only the id set made every picker's OPTIONS
    # invisible to this gate: `new-project.py` added data-lake to the live `project`
    # picker, the id was already present on both sides, and devkit's copy sat a project
    # behind with nothing red. The task half was compared this way from the start —
    # an input is no less part of the block for being referenced rather than clicked.
    for label in sorted(canon_inputs.keys() & live_inputs.keys()):
        if live_inputs[label] != canon_inputs[label]:
            problems.append(f"input definition differs: {label}")
    return problems


def canonical_text() -> str:
    """devkit's copy of the workspace file, as source."""
    return CANONICAL_WORKSPACE.read_text(encoding="utf-8")


def canonical_tasks() -> dict:
    """The canonical `tasks` block, parsed.

    A named accessor rather than a second file. The task block used to live in its own
    `workspace-tasks.jsonc`, and seven call sites read that path directly; folding it
    into the whole-file canonical without this would have replaced one duplicate
    source of truth with seven hard-coded ways to find the new one.
    """
    return workspace_tasks(canonical_text())


def canonical_tasks_text() -> str:
    """The canonical `tasks` block as SOURCE, comments intact."""
    return extract_tasks_text(canonical_text())


def _folder_names(block: object) -> set[str]:
    """The checkouts a `folders` list names, by `path` (falling back to `name`)."""
    if not isinstance(block, list):
        return set()
    names: set[str] = set()
    for entry in block:
        if isinstance(entry, dict):
            names.add(str(entry.get("path") or entry.get("name") or "?"))
    return names


def workspace_drift(live: dict, canonical: dict) -> list[str]:
    """Human-readable differences across the WHOLE workspace file; empty when it agrees.

    `folders` is the one that used to have no gate at all: `new-project.py` registers a
    project by editing the live file, so a newly generated checkout existed only in the
    copy with no history -- and every sweep, every status line and every `--project`
    picker reads that list.
    """
    problems: list[str] = []
    for key in PLAIN_KEYS:
        before, after = canonical.get(key), live.get(key)
        if before == after:
            continue
        if key == "folders":
            # Named, not diffed. "folders differs" on the project registry is the one
            # place a reader most needs to know WHICH checkout appeared or vanished.
            canon_names, live_names = _folder_names(before), _folder_names(after)
            for gone in sorted(canon_names - live_names):
                problems.append(f"folder missing from the workspace: {gone}")
            for new in sorted(live_names - canon_names):
                problems.append(f"folder in the workspace but not in devkit: {new}")
            if canon_names == live_names:
                problems.append("folders: same checkouts, different entries")
        else:
            problems.append(f"{key} differs")
    live_tasks, canon_tasks = live.get("tasks"), canonical.get("tasks")
    return problems + tasks_drift(
        live_tasks if isinstance(live_tasks, dict) else {},
        canon_tasks if isinstance(canon_tasks, dict) else {},
    )


RENDER_PUBLISHED = "published"
RENDER_CURRENT = "current"
RENDER_REFUSED = "refused"


def publish_workspace(live: Path, *, force: bool = False) -> tuple[str, list[str]]:
    """Render the canonical copy over `live`, unless that would discard someone's edit.

    Extracted from `main()` so the session-start hook can publish on the same terms the
    CLI does. That is the whole point of the extraction: a second implementation of
    "is it safe to overwrite the file every window on the machine reads" is the last
    thing this should grow.

    Returns `(outcome, differences)`. `RENDER_CURRENT` stamps and writes nothing --
    an unstamped-but-identical live file is the normal state right after an adopt, and
    leaving it unstamped would make the NEXT render refuse for a hand edit that never
    happened. `RENDER_REFUSED` writes nothing at all.
    """
    text = live.read_text(encoding="utf-8")
    canonical = canonical_text()
    problems = workspace_drift(devkit_jsonc.loads(text), devkit_jsonc.loads(canonical))
    if not problems:
        write_stamp(live, semantic_digest(text))
        return RENDER_CURRENT, []
    if not force and semantic_digest(text) != read_stamp(live):
        return RENDER_REFUSED, problems
    live.write_text(canonical, encoding="utf-8", newline="\n")
    write_stamp(live, semantic_digest(canonical))
    return RENDER_PUBLISHED, problems


def expected_actions(project: str) -> set[str]:
    """The PROJECT-owned actions `project` is on the hook for.

    Scoped actions are excluded from every checkout but their own. Without that, hoisting
    carameli's Playwright task would have reported ibkr_trader and devkit as missing
    `scripts/run-e2e.py` — a "gap" neither should ever close, and the kind of noise that
    teaches everyone to stop reading `--check`.
    """
    return {key for key, a in ACTIONS.items() if a.owner == PROJECT and in_scope(a, project)}


def conformance(projects: list[str], root: Path) -> dict[str, list[str]]:
    """Which PROJECT-owned actions each checkout implements.

    DEVKIT-owned actions are deliberately excluded: they work in any checkout, so
    listing them would make every project look conformant and hide the real gap.
    """
    return {
        name: [
            key
            for key in sorted(expected_actions(name))
            if (root / name / ACTIONS[key].script).is_file()
        ]
        for name in projects
    }


# --- entrypoint -------------------------------------------------------------


def _load_workspace(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"cannot read the workspace registry at {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    # Task labels carry en-dashes and arrows; a Windows console is cp1252 and would
    # raise UnicodeEncodeError mid-report rather than printing the drift it found.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Run a generic project action in a chosen checkout.",
        epilog="Actions: " + ", ".join(sorted(ACTIONS)),
    )
    parser.add_argument(
        "--project",
        default="",
        help="one checkout name, or a comma-delimited list, as listed in the workspace",
    )
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--no-ship-fixes",
        action="store_true",
        help=(
            "leave an autofix action's rewrites in the working tree instead of branching "
            "and shipping them (see autofix_ship_plan) -- for inspecting the fixes locally"
        ),
    )
    parser.add_argument(
        "--list", action="store_true", help="print the known projects, one per line"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print which projects implement which actions, then exit",
    )
    parser.add_argument(
        "--check-workspace",
        "--check-tasks",
        dest="check_workspace",
        action="store_true",
        help="report how the live workspace file differs from devkit's canonical copy",
    )
    parser.add_argument(
        "--render-workspace",
        dest="render_workspace",
        action="store_true",
        help="publish workspace.jsonc to the live workspace file (the normal direction)",
    )
    parser.add_argument(
        "--adopt-workspace",
        "--adopt-tasks",
        dest="adopt_workspace",
        action="store_true",
        help="record a live hand edit back into workspace.jsonc, for committing on a branch",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --render-workspace: overwrite live edits devkit did not write",
    )
    parser.add_argument("action", nargs="?", default="", choices=["", *sorted(ACTIONS)])
    parser.add_argument("extra", nargs=argparse.REMAINDER, help="extra args for the script")
    args = parser.parse_args(argv)

    try:
        text = _load_workspace(args.workspace)
    except ProjectError as exc:
        print(f"devkit_project: {exc}", file=sys.stderr)
        return 2

    try:
        payload_ok = bool(devkit_jsonc.loads(text))
    except json.JSONDecodeError as exc:
        print(f"devkit_project: {args.workspace} is not valid JSONC: {exc}", file=sys.stderr)
        return 2
    if not payload_ok:
        print(f"devkit_project: {args.workspace} is empty", file=sys.stderr)
        return 2

    projects = known_projects(text)
    root = args.workspace.parent

    if args.list:
        print("\n".join(projects))
        return 0

    if args.adopt_workspace:
        # Written directly rather than printed for redirection: the file carries
        # en-dashes and arrows, and a redirected stdout on Windows is cp1252.
        CANONICAL_WORKSPACE.write_text(text, encoding="utf-8", newline="\n")
        write_stamp(args.workspace, semantic_digest(text))
        print(f"adopted {args.workspace.name} into {CANONICAL_WORKSPACE.name}")
        print("commit it on a task branch -- that is what gives the edit a reviewer")
        return 0

    if args.render_workspace or args.check_workspace:
        if not CANONICAL_WORKSPACE.is_file():
            print(f"devkit_project: no canonical copy at {CANONICAL_WORKSPACE}", file=sys.stderr)
            return 2
        problems = workspace_drift(devkit_jsonc.loads(text), devkit_jsonc.loads(canonical_text()))

        if args.check_workspace:
            if not problems:
                print(f"{args.workspace.name}: matches {CANONICAL_WORKSPACE.name}")
                return 0
            print(f"{args.workspace.name} has drifted from {CANONICAL_WORKSPACE.name}:")
            for problem in problems:
                print(f"  {problem}")
            print(f"  -> keep the live edits:  {RENDER_HINT} --adopt-workspace")
            print(f"  -> publish the canonical: {RENDER_HINT} --render-workspace")
            return 1

        rendered, problems = publish_workspace(args.workspace, force=args.force)
        if rendered == RENDER_CURRENT:
            print(f"{args.workspace.name}: already current")
            return 0

        if rendered == RENDER_REFUSED:
            print(
                f"devkit_project: {args.workspace.name} carries edits devkit did not"
                " write -- refusing to overwrite them:",
                file=sys.stderr,
            )
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print(
                f"  -> keep them:    {RENDER_HINT} --adopt-workspace\n"
                f"  -> discard them: {RENDER_HINT} --render-workspace --force",
                file=sys.stderr,
            )
            return 1

        print(f"rendered {CANONICAL_WORKSPACE.name} -> {args.workspace.name}")
        for problem in problems:
            print(f"  {problem}")
        return 0

    if args.check:
        for name, actions in conformance(projects, root).items():
            missing = sorted(expected_actions(name) - set(actions))
            status = "all" if not missing else f"missing: {', '.join(missing)}"
            print(f"  {name:<16} {status}")
        return 0

    try:
        if not args.action:
            raise ProjectError(f"no action given; expected one of: {', '.join(sorted(ACTIONS))}")
        # argparse.REMAINDER keeps a leading "--" when one is passed; drop it.
        extra = [a for a in args.extra if a != "--"]
        selected = project_selection(args.project)
        if not selected:
            raise ProjectError(f"no project given; expected one of: {', '.join(projects)}")

        # Plan every selection before starting the first one. A stale picker entry or
        # missing project script must not leave a multi-project request half-executed.
        planned: list[tuple[Path, list[str]]] = []
        for name in selected:
            directory = resolve_project(name, projects, root)
            check_scope(ACTIONS[args.action], name)
            planned.append((directory, plan_command(ACTIONS[args.action], directory, extra)))
    except ProjectError as exc:
        print(f"devkit_project: {exc}", file=sys.stderr)
        return 2

    action = ACTIONS[args.action]
    ship_fixes = action.autofix and not args.no_ship_fixes
    result = 0
    for directory, command in planned:
        print(f"[{directory.name}] {' '.join(command)}\n", flush=True)
        branch, before = autofix_state(directory) if ship_fixes else ("", ())
        returncode = subprocess.run(command, cwd=directory, check=False).returncode
        if returncode and not result:
            result = returncode
        if not ship_fixes:
            continue
        _, after = autofix_state(directory)
        outcome = autofix_ship_plan(
            directory.name,
            branch,
            before,
            after,
            lint_ok=returncode == 0,
            workspace=args.workspace,
            slug=action.autofix_slug,
            labels=action.autofix_labels,
        )
        if outcome.note:
            print(f"\n[{directory.name}] {outcome.note}", flush=True)
        for step in outcome.commands:
            print(f"\n[{directory.name}] {' '.join(step)}\n", flush=True)
            code = subprocess.run(step, cwd=root, check=False).returncode
            if code:
                # Stop at the first failure rather than shipping from a branch that was
                # never cut: the second command would then read the *home* branch and
                # sweep.ship_plan would refuse it, reporting a confusing second error
                # for the same cause.
                print(
                    f"[{directory.name}] shipping the autofix churn failed (exit {code}) -- "
                    f"the fixes are still in the working tree",
                    file=sys.stderr,
                    flush=True,
                )
                result = result or code
                break
    return result


if __name__ == "__main__":
    raise SystemExit(main())
