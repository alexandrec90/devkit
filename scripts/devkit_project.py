#!/usr/bin/env python3
"""Run a generic project action in a chosen checkout: `--project X <action>`.

Every action that is *generic* (test, lint, sync the agent context) is defined once here
and the checkout is an argument, rather than a copy per project that drifts. The
checkout names are the registry's (`project_registry`, `projects.toml`).

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
applies to (`projects=`), so one definition here serves a Playwright run or an IBKR
backtest without pretending every project can do it.
`projects` restricts both halves: the dispatcher refuses an out-of-scope checkout, and
`--check` stops demanding the script from projects the action was never meant for.

Pure helpers (`resolve_project`, `in_scope`, `plan_command`) are unit-tested in
`tests/test_devkit_project.py`; `main` is the thin subprocess shell around them.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_registry
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Checkouts registered so they can be read but shipping no harness, so nothing in ACTIONS
# applies. Empty since 2026-09-02 and kept as a mechanism: `known_projects` subtracts it
# from the raw registry, so re-introducing one is a name here rather than a refactor.
NOT_PROJECTS: frozenset[str] = frozenset()


class ProjectError(ValueError):
    """The project name is unknown, or does not implement the requested action."""


@dataclass(frozen=True)
class Action:
    """One generic action: which script implements it, and how it is announced."""

    script: str  # relative to the owner's root, e.g. "scripts/lint-all.py"
    label: str  # names the run's `logs/` artifact and its ledger entry
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
    # checkout (`("carameli",)`) is how a genuinely project-specific action lives here.
    # Names rather than a capability probe because the registry lists checkouts by name,
    # and a stale entry fails loudly here.
    projects: tuple[str, ...] = ()


PROJECT = "project"
DEVKIT = "devkit"

# The `gh repo create` visibility for every repo devkit creates (`new-project.py`,
# `plug-projects.py`). Public, because every project it creates runs its PR gate in
# Actions, and only a public repo's minutes are free and unlimited: productivity-tracker
# and social-scraper were created private, spent the free plan's monthly allowance, and
# GitHub refused every job after (2026-11-01). Spelled once so the two cannot differ.
CREATE_VISIBILITY = "--public"

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
SOCIAL_SCRAPER = ("social-scraper",)
PHOTOLAB = ("photolab",)

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
# A NEWLY GENERATED project with alembic is not in here until someone adds it.
# `new-project.py` cannot extend this tuple — it is devkit source, not the registry the
# generator already maintains.
DB_PROJECTS = CARAMELI + IBKR

# The shared contract. Adding an entry here is the *only* place a new generic action
# needs defining.
ACTIONS: dict[str, Action] = {
    # --- implemented by each checkout ---
    "test": Action("scripts/run-tests.py", "Test: Run Suite"),
    # Scope is an argument, not a second action: `--changed` or nothing at all. Empty
    # tokens are dropped in `plan_command`, so the wide case may pass an empty one.
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
    # `sync-devkit` is deliberately not an action: `--pull` in a STATIC checkout writes an
    # upgrade with no branch and no PR under it, which is `upgrade-project.py`'s whole job
    # and the reason it works in a box.
    # --- implemented once, here ---
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
    # The person's step the scheduled jobs name for a stuck WSL service
    # (`collector_tasks.WSL_REMEDY`); their unelevated runs may not end it themselves.
    "docker-recover-wsl": Action(
        "scripts/docker-maint.py", "Docker: Recover Stuck WSL", ("recover-wsl",), owner=DEVKIT
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
    # `--yes` is baked in because asking for the action is asking it to clean up; the
    # script's own default is a dry run, which is what an agent gets when it runs it.
    #
    # DEVKIT_ONLY rather than DEVKIT-owned-and-unscoped, and the difference is not
    # cosmetic here: an unscoped action is run once per *selected checkout*, and this one
    # has no project dimension at all -- it sweeps `%TEMP%`, stops every container on the
    # machine and reconciles the box registry. Naming three checkouts would sweep the
    # same machine three times, the second and third finding nothing and reading as a
    # no-op. Scoped, the only checkout it takes is `--project devkit`.
    "reclaim": Action(
        "scripts/reclaim.py", "Machine: Reclaim Resources", ("--yes",), projects=DEVKIT_ONLY
    ),
    # The scheduled jobs themselves, as a verb a human can reach. Scoped for `reclaim`'s
    # reason: the jobs are machine state with no project dimension, so an unscoped action
    # would run the same pass once per named checkout.
    #
    # Deliberately *not* how the jobs are kept current -- `devkit-installers` does that,
    # daily and at logon. What a human needs the verb for is the two things a schedule
    # cannot decide: seeing the set at all, and taking it back off again. `--yes` rides
    # in as a trailing argument, so the uninstall is read before it runs.
    "installers": Action("scripts/installers.py", "Machine: Scheduled Jobs", projects=DEVKIT_ONLY),
    # Which ingestion collectors this PC runs, stops or leaves alone. Scoped for
    # `reclaim`'s reason -- the assignment is this machine's, not a checkout's.
    "collectors": Action(
        "scripts/collectors.py", "Machine: Ingestion Collectors", projects=DEVKIT_ONLY
    ),
    # The read-only half of `reclaim`'s memory question -- who holds the commit charge,
    # which `claude` processes are yours, what `reap-stale.py` would stop -- and scoped
    # for `reclaim`'s reason: one machine, one inventory, no checkout to pick.
    "memory-inventory": Action(
        "scripts/memory-inventory.py", "Machine: What Is Eating Memory", projects=DEVKIT_ONLY
    ),
    # Reviewing a UI change before its PR merges: host Vite on the picked branches'
    # frontends -- `npm run dev`, one port each, no Docker anywhere in it.
    #
    # Looking at a UI change needs node, the branch's files and a free port; the compose
    # stack `worktree.py preview` builds is the CLI verb for the full-stack kind.
    #
    # DEVKIT_ONLY for `reclaim`'s reason: what it serves is assembled from the box
    # registry and the port registry, and there is exactly ONE of each on this machine.
    # Run once per selected checkout it would do the same machine-wide work two or three
    # times over, so it takes `--project devkit` and names its branches itself.
    "preview-ui-host": Action(
        "scripts/preview-ui-host.py", "Preview: Open a UI Branch", projects=DEVKIT_ONLY
    ),
    # The teardown half of the pair above: a Vite server outlives the run that started it
    # whenever the terminal is closed instead of interrupted, and the `finally` clause
    # that would have stopped it never runs. Three nets catch that inside
    # `preview-ui-host.py`; this is the fourth, for when every server on the machine
    # should be gone now, regardless of which terminal owns it.
    #
    # DEVKIT_ONLY for the same reason as its siblings -- there is one registry of running
    # servers on this machine, so there is no checkout to name.
    "preview-ui-stop": Action(
        "scripts/preview-ui-host.py",
        "Preview: Stop Host UI Servers",
        ("--stop",),
        projects=DEVKIT_ONLY,
    ),
    # Sending an agent at a PR that is already red -- a merge conflict, a failed gate, or
    # both. Nothing else on this machine does it: `worktree.py reconcile` merges only what
    # is green and carries the merge label, so a broken PR is exactly the state its
    # fifteen-minute pass steps over forever, and this is the human's way of saying
    # "yes, go and deal with that one".
    #
    # DEVKIT_ONLY and `--project devkit`, for `preview-ui-host`'s reason: the pass is one
    # scan across every checkout in the registry, so run once per selected checkout it
    # would do the same cross-checkout work two or three times over.
    "fix-pass": Action("scripts/fix-pass.py", "Agent: Fix What Is Red", projects=DEVKIT_ONLY),
    # The worktree pair. Both runtimes cut their own now -- `claude --worktree` inside the
    # checkout, `codex --worktree` under `~/.codex/` behind a repo digest -- so these are
    # not the only way to get one. They stay because `remove` is the only verb that reads
    # every tier at once.
    #
    # DEVKIT_ONLY and `--project devkit` for `fix-pass`'s reason exactly: the checkout is
    # an argument of the script rather than a scope for the action.
    "worktree-new": Action(
        "scripts/agent-worktree.py", "Agent: New Worktree", ("new",), projects=DEVKIT_ONLY
    ),
    "worktree-remove": Action(
        "scripts/agent-worktree.py", "Agent: Delete Worktrees", ("remove",), projects=DEVKIT_ONLY
    ),
    # Cutting a devkit release. DEVKIT_ONLY because a release is devkit's own act and
    # has no project dimension at all -- run per selected checkout it would try to tag
    # this repo two or three times, and the second attempt would refuse a tag that now
    # exists. The consumers appear at the END of the run, as the adoption pass this hands
    # off to, which is a different thing from the action being scoped to them: this
    # `--project devkit`, fixed, decides where the release is cut, and a trailing
    # `--projects=` decides whose adoption PR opens afterwards.
    #
    # It is an ACTIONS entry because it needs the wrapping: the run spans a PR gate and a
    # workflow dispatch, so its useful output arrives minutes apart and the reader is not
    # watching. `plan_command` gives it the `logs/` artifact for free, and the level rides
    # through as a trailing argument the same way `db-revision`'s `-m` does.
    "release": Action("scripts/release-pipeline.py", "Devkit: Cut Release", projects=DEVKIT_ONLY),
    # --- scoped to one repo ---
    #
    # Nothing about these is generic — a Playwright run and an IBKR backtest are not things
    # devkit or a `bare` preset can do — but they are defined once here and the checkout
    # is an argument.
    #
    # `projects=` is what keeps `--check` honest about it: without the scope, every one of
    # these would be demanded of every checkout and each project would report five or six
    # phantom gaps.
    "test-target": Action("scripts/run-tests.py", "Test: Run Carameli Target", projects=CARAMELI),
    "e2e": Action("scripts/run-e2e.py", "Test: Run Browser E2E", projects=CARAMELI),
    # A `local-e2e` action stood here until 2026-09-02. It drove carameli's integration
    # suite against the reference checkout that was retired from this registry on that
    # date, and the other half of the pair is what made it runnable: the orchestrator
    # boots a counterpart stack named by `VS_REPO_DIR`, so with no such checkout on the
    # machine it can only fail at the first step. carameli still ships the script; what
    # is gone is the dispatch that implied the machine could serve it.
    "ngrok": Action("scripts/start-ngrok.py", "Start: ngrok + Sync URLs", projects=CARAMELI),
    # Takes no argument on purpose. The script encodes every master in
    # `frontend/assets-src/comic-book/` that has no export yet and skips the rest, so
    # the action is "I dropped pictures in, pick them up". Naming one is a direct call of
    # the script, where `--label` and `--max-edge` live.
    "encode-art": Action(
        "scripts/encode-comic-art.py", "Assets: Encode Comic-Book Art", projects=CARAMELI
    ),
    "vnc": Action("scripts/vnc-viewer.py", "IBKR: Open Gateway VNC Viewer", projects=IBKR),
    "ingest": Action("scripts/ingest-task.py", "Ingest: Run Source", projects=IBKR),
    "snapshot-monthly": Action(
        "scripts/snapshot-monthly.py", "Snapshot: Run Monthly", projects=IBKR
    ),
    # One script, two subcommands — the OOS run fixes its own warm-up and simulation
    # starts, so it cannot just be `backtest` with different arguments.
    "backtest": Action("scripts/backtest-task.py", "Backtest: Run", ("run",), projects=IBKR),
    "backtest-oos": Action(
        "scripts/backtest-task.py", "Backtest: OOS (Honest Per-Fold)", ("oos",), projects=IBKR
    ),
    # One script again, split the other way: the four read-only views are one action,
    # the view riding in as a trailing argument, while the strategy lab is its own
    # action because it re-simulates for minutes and asks two more questions.
    "report": Action(
        "scripts/report-task.py", "Report: Open Model & Strategy Views", projects=IBKR
    ),
    "strategy-lab": Action(
        "scripts/report-task.py", "Report: Run Strategy Lab", ("lab",), projects=IBKR
    ),
    # Advisory only: the Questrade client is GET-only, so the plan is a CSV and nothing is
    # sent. Its sibling `check` subcommand is no action -- `plan` already reads every
    # account, and keeping the token alive is a schedule's job.
    "questrade-plan": Action(
        "scripts/questrade-task.py", "Questrade: Recommend Orders", ("plan",), projects=IBKR
    ),
    # The local web UI, in the foreground until its terminal closes -- the MLflow view's
    # shape. The script builds the frontend first when it has to, so the action is the
    # whole setup. One server per port, which is why the action names the checkout.
    "trader-ui": Action("scripts/ui-task.py", "IBKR: Open Trader UI", projects=IBKR),
    # --- scoped to social-scraper ---
    #
    # Signing in once in a visible browser, so the platform's persistent profile under
    # the checkout's `data/browser-profiles/<platform>/` carries the session every later
    # scrape reuses. The platform rides in as a trailing argument, so Reddit is an
    # argument rather than a second action.
    "scraper-login": Action("scripts/login.py", "Scraper: Log In", projects=SOCIAL_SCRAPER),
    # --- scoped to photolab ---
    #
    # The trader UI's shape: in the foreground until its terminal closes. On the host rather
    # than in Docker because the `codex` and `claude` commands and their logins are out
    # here. Fixed ports, which is why the action names the checkout.
    "album": Action("scripts/album.py", "Photolab: Open Album", projects=PHOTOLAB),
    # --- scoped to the checkouts that have a database ---
    #
    # PROJECT-owned because the two
    # implementations genuinely differ and neither is a wrapper for the other: carameli
    # runs alembic inside its app container (PgBouncer sits in front of Postgres, so the
    # DDL connection has to bypass the pooler, and the container's env is the single
    # source of that URL), while ibkr_trader runs it on the host through uv against
    # Postgres on 5433, where there is no pooler and the `app` profile is a scheduler
    # rather than a dev shell. Same CLI — `-m "<message>"` — different bodies, which is
    # exactly the split `run-tests.py` and `lint-all.py` already use.
    "db-revision": Action("scripts/db-revision.py", "DB: New Migration", projects=DB_PROJECTS),
}


# --- the test menu ----------------------------------------------------------
#
# `--project a,b tests <kind>,<kind>` asks which checkouts and which kinds of test, and
# this table is the second question's answer set.
#
# A kind is an (action, argument) pair rather than a new action, so nothing on the menu
# can reach a script `ACTIONS` does not already name, and `Action.projects` still decides
# which checkout may run it. `plan_test_runs` skips an out-of-scope pair BY NAME rather
# than dropping it: one menu serves every checkout, so naming `devkit` beside a
# Playwright kind is the ordinary way to use this, and a pair that vanishes silently is
# indistinguishable from a suite that ran and passed.
#
# `test-hooks-live` is here as two kinds, one per CLI; naming both runs claude then
# codex, since `plan_test_runs` orders by this table. They launch a paid CLI session, and
# `hook-tests-live.py` prints what it is about to spend before it launches anything.
@dataclass(frozen=True)
class SuiteKind:
    """One row of the test menu: the action it dispatches, and the argument shaping it.

    Not spelled `TestKind` — pytest collects a `Test`-prefixed class out of any test
    module that imports it, and the tests for this table import it by name.
    """

    action: str
    args: tuple[str, ...] = ()


TEST_KINDS: dict[str, SuiteKind] = {
    "suite": SuiteKind("test"),
    "suite-changed": SuiteKind("test", ("--changed",)),
    "hooks": SuiteKind("test-hooks"),
    "all-targets": SuiteKind("test-target", ("--all",)),
    "frontend": SuiteKind("test-target", ("--target=frontend-tests",)),
    "bundle-budgets": SuiteKind("test-target", ("--target=bundle-budgets",)),
    "webhook": SuiteKind("test-target", ("--target=webhook-e2e",)),
    "e2e": SuiteKind("e2e"),
    "e2e-headed": SuiteKind("e2e", ("--headed",)),
    "e2e-cross": SuiteKind("e2e", ("--cross-browser",)),
    # Last, so the distance from `suite` is as large as the table can make it. The
    # argument is the suite name `hook-tests-live.py` already takes, so nothing new is
    # reachable here that its CLI did not already offer.
    "hooks-live-claude": SuiteKind("test-hooks-live", ("claude",)),
    "hooks-live-codex": SuiteKind("test-hooks-live", ("codex",)),
}

# The verb that spends that table. Not an `ACTIONS` key, because an action is one script
# and this fans out to several — so it is a choice `main` handles beside them.
TESTS_VERB = "tests"

# The failure artifact, taken from **devkit's** checkout rather than the target's. Two
# reasons, and the second is the load-bearing one: a checkout that has not yet pulled
# the release adding `scripts/log-wrap.py` still gets an artifact, and the wrapper that
# runs is the one this dispatcher was tested against rather than whatever vintage the
# target vendored. Unconditional -- it is a MANIFEST entry of devkit's own, so its
# absence is a broken checkout and should say so loudly instead of quietly dropping every
# run's artifact.
LOG_WRAP = "scripts/log-wrap.py"

# Where a dispatch that could not start keeps its reason: devkit's own `logs/`, since no
# checkout was resolved to own it. See `record_task_failure`.
FAILURE_ROOT = REPO_ROOT


# --- pure helpers -----------------------------------------------------------


def known_projects() -> list[str]:
    """Registered project names, minus the non-projects.

    Read through `project_registry.all_projects`, so every caller asks the one registry
    and none can disagree with another about which checkouts exist.
    """
    return [name for name in project_registry.all_projects() if name not in NOT_PROJECTS]


def project_selection(value: str) -> list[str]:
    """Checkout names from a comma-delimited `--project`, preserving order, duplicates
    dropped. A single name is a one-item instance of the same format.
    """
    return list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))


def kind_selection(value: str) -> list[str]:
    """Test-menu kinds from a comma-delimited argument, returned in MENU order.

    Menu order rather than the order they were typed in: a run whose sequence depends on
    how the arguments were spelled is a run nobody can compare against the last one.

    An unknown kind is refused here, before any checkout is resolved, because the pairs
    are a cross-product: the fourth of eight runs failing on a typo would leave three
    suites already run and five never attempted.
    """
    picked = {name.strip() for name in value.split(",") if name.strip()}
    unknown = sorted(picked - set(TEST_KINDS))
    if unknown:
        raise ProjectError(
            f"unknown test kind(s) {', '.join(unknown)}; the menu offers: {', '.join(TEST_KINDS)}"
        )
    return [kind for kind in TEST_KINDS if kind in picked]


def resolve_project(name: str, projects: list[str], root: Path, noun: str = "project") -> Path:
    """The checkout directory for `name`, validated against the registry.

    Validating against the registry (not just `is_dir()`) is what makes a typo'd or
    stale name fail with the list of real names instead of running in a
    directory that happens to exist.

    `noun` is what the failure calls the thing that was not found. It exists because
    `git-merge-default.py` resolves against the RAW registry -- reference checkouts
    included, since merging a trunk needs git and no harness — and calling such a folder
    an unknown *project*, when `NOT_PROJECTS` is exactly what excludes it from that word,
    would send the reader to fix the wrong list.

    `projects` is the registry as the caller read it -- `known_projects()`, or
    `project_registry.all_projects()` for the raw one -- and `root` is
    `project_registry.projects_root()`, or the directory of a deprecated `--workspace`.
    """
    if not name:
        raise ProjectError(f"no {noun} given; expected one of: {', '.join(projects)}")
    if name not in projects:
        raise ProjectError(f"unknown {noun} {name!r}; the registry lists: {', '.join(projects)}")
    path = root / name
    if not path.is_dir():
        raise ProjectError(f"{name} is registered but {path} does not exist")
    return path


def in_scope(action: Action, project: str) -> bool:
    """Whether `action` applies to `project`. An action with no `projects` applies to all."""
    return not action.projects or project in action.projects


def check_scope(action: Action, project: str) -> None:
    """Refuse an action aimed at a checkout it was never defined for.

    Without it, `--project devkit backtest` would fall
    through to the missing-script error and read as "devkit has not implemented backtesting
    yet", which invites someone to go and implement it.
    """
    if not in_scope(action, project):
        raise ProjectError(
            f"{project} is out of scope for this action; it is defined for: "
            f"{', '.join(action.projects)}"
        )


def plan_test_runs(
    kinds: list[str], selected: list[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Every (checkout, kind) pair the menu asks for that is in scope, and the rest by name.

    Project-major, so one checkout's runs happen in one stretch and its `logs/` artifacts
    are not interleaved with another's.

    The skips are RETURNED rather than swallowed, and that is the whole reason this is a
    function. A single menu serves every checkout, so `devkit` beside a Playwright kind
    is the ordinary case, and the honest answer is a printed line naming the pair and the scope that excluded it.

    `check_scope` stays the single-action path's refusal: asking for one out-of-scope
    action is a mistake with nothing else to run, while asking for one of eight is not.
    """
    runs: list[tuple[str, str]] = []
    skipped: list[str] = []
    for project in selected:
        for kind in kinds:
            action = ACTIONS[TEST_KINDS[kind].action]
            if in_scope(action, project):
                runs.append((project, kind))
            else:
                skipped.append(f"{project}: {kind} is defined for {', '.join(action.projects)}")
    return runs, skipped


def plan_command(
    action: Action, project_dir: Path, extra: list[str], devkit_root: Path = REPO_ROOT
) -> list[str]:
    """The argv to run for `action`: the script, wrapped by `log-wrap.py`.

    Raises `ProjectError` when the script is missing — for a PROJECT action that is
    the conformance failure, reported by name rather than as a missing-file traceback
    from an unexpected cwd. A DEVKIT action is referenced by absolute path because it
    runs with cwd set to the *checkout*, not to devkit.

    **Where every dispatched action gets its failure artifact.** This is the only point
    that can write the artifact to the right place: until `resolve_project` has run
    nothing knows which checkout's `logs/` a failure belongs in. Wrapping here covers
    every action, including ones added later.
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
    return ["python", str(devkit_root / LOG_WRAP), action.label, "--", *inner]


def task_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every dispatched command runs in: `base` (this one's), in UTF-8 mode.

    Dozens of the scripts behind the tasks read a child's output with `text=True` and no
    encoding, which decodes with the console's code page. On cp1252 a child's `”` (0x9d
    in UTF-8) killed the reader thread and printed its traceback into the task's
    terminal -- and the call returned exit 0 with `stdout=None`, so a `gh` listing it
    could not read was a listing of nothing. The scheduled fix pass had UTF-8 mode from
    its watchdog (`fix-pass-watchdog.run_pass`), and dispatching the same pass did not,
    because a dispatch comes through here. The variable is set rather than `-X utf8` because
    it reaches every Python down the chain, the wrappers' children and their children
    included.
    """
    return {**(os.environ if base is None else base), "PYTHONUTF8": "1"}


# --- failures the wrapper never sees ----------------------------------------
#
# `log-wrap.py` files every failure it wraps on the harness-events ledger, which is how a
# failed task reaches the fix pass's devkit session. Two of this dispatcher's failures
# happen where no wrapper is running -- a refusal before `plan_command` has wrapped
# anything, and the autofix ship steps after the wrapped run has exited -- so they are
# filed here, in the same event and the same fields.


def _log_wrap():
    """devkit's `log-wrap.py`, loaded by path because the name is hyphenated. Its
    helpers decide the event's shape here too, so the two writers cannot drift apart.
    A private module name: the hook tests register their own copy as `log_wrap`."""
    name = "_devkit_project_log_wrap"
    if name in sys.modules:
        return sys.modules[name]
    precommit = str(REPO_ROOT / "scripts" / "precommit")
    if precommit not in sys.path:
        sys.path.append(precommit)
    from _loader import load_by_path

    return load_by_path(name, REPO_ROOT / LOG_WRAP)


def task_title(action: str) -> str:
    """The label a failure of `action` is filed under; `""` when there is no task.

    `Action.label`, because that is the title `plan_command` hands `log-wrap.py` -- so
    one task's failures carry one name whichever side of the wrapper they happened on.
    The test menu is the `test` action's label, which is the menu task's own.
    """
    if action == TESTS_VERB:
        action = TEST_KINDS["suite"].action
    return ACTIONS[action].label if action in ACTIONS else ""


def record_task_failure(
    title: str, why: str, command: list[str], output: str, checkout: Path, code: int
) -> str | None:
    """File one failed task on the ledger, its reason kept under `checkout/logs/`.

    Returns the artifact the event names, or None when nothing could be filed.

    The message is `task <title> <why>` and nothing else: `harness_triage` groups by it,
    so a refusal recurring nightly reads as one defect recurring. What varies rides in
    `cause` (`log-wrap.failure_cause`), which splits a new reason into a new group.

    The kept copy is named for the title *and* `why`, never `<slug>.failed.log` alone --
    that path is the one `log-wrap.py` keeps for the run it wrapped, and an earlier
    ledger row points at it. Best-effort throughout: a ledger that cannot be reached
    must never change the exit code the task reports.
    """
    try:
        wrap = _log_wrap()
        hooks = str(REPO_ROOT / "scripts" / "hooks")
        if hooks not in sys.path:
            sys.path.append(hooks)
        import harness_events
    except ImportError:
        return None
    name = wrap.slug(f"{title} {why}")
    body = wrap.artifact_body(title, command, code, output, kept=True)
    kept = wrap.write_artifact(checkout, name + wrap.FAILED_SUFFIX, body)
    artifact = wrap.artifact_ref(name, kept=kept is not None)
    cause, said = wrap.failure_cause(output), wrap.cause_said(output)
    harness_events.record(
        wrap.FAILED_EVENT,
        (
            ("project", harness_events.project_name(checkout)),
            ("command", " ".join(command)),
            ("artifact", artifact),
            ("exit", code),
            ("message", f"task {title!r} {why}"),
            ("cause", cause or "-"),
            *((("said", said),) if said and said != cause else ()),
        ),
    )
    return artifact


def refuse_dispatch(exc: ProjectError, action: str, argv: list[str]) -> int:
    """Report a dispatch that could not start, and file it when it was a task's. Exit 2.

    No action is no task: `--list` and `--check` only report.
    """
    print(f"devkit_project: {exc}", file=sys.stderr)
    title = task_title(action)
    if title:
        command = ["python", str(Path(__file__).resolve()), *argv]
        record_task_failure(title, "could not start", command, str(exc), FAILURE_ROOT, 2)
    return 2


def report_ship_failure(directory: Path, action: Action, step: tuple[str, ...], code: int) -> None:
    """Say that shipping an autofix run's churn failed, and file it against the checkout.

    The ship steps run after the wrapped lint run has exited -- green, or there would be
    nothing to ship -- so no wrapper sees this one.
    """
    line = (
        f"[{directory.name}] shipping the autofix churn failed (exit {code}) -- "
        f"the fixes are still in the working tree"
    )
    print(line, file=sys.stderr, flush=True)
    record_task_failure(
        action.label, "could not ship its autofix", list(step), line, directory, code
    )


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
    root: Path,
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
    common = ("python", sweep_py, "--root", str(root), "--only", project)
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


# --- conformance ------------------------------------------------------------


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


def _registry_projects() -> list[str]:
    """The projects for `main`, off the registry. A malformed registry is
    a `ProjectError`, so `refuse_dispatch` files it under the task it stopped.
    """
    try:
        return known_projects()
    except project_registry.RegistryError as exc:
        raise ProjectError(f"cannot read the project registry: {exc}") from exc


def dispatch_root(workspace: Path | None) -> Path:
    """The projects root the checkouts are resolved under.

    `--workspace` is a deprecated alias kept because scheduled-task XML on a PC may still
    pass it: only its directory is read. Absent, the root is the registry's own.
    """
    return workspace.parent if workspace is not None else project_registry.projects_root()


def main(argv: list[str] | None = None) -> int:
    # Action labels carry en-dashes; a Windows console is cp1252 and would raise
    # UnicodeEncodeError mid-report rather than printing what it found.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    raw_argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(
        description="Run a generic project action in a chosen checkout.",
        epilog=(
            "Actions: "
            + ", ".join(sorted(ACTIONS))
            + f"\nTest kinds, for the '{TESTS_VERB}' verb: "
            + ", ".join(TEST_KINDS)
        ),
    )
    parser.add_argument(
        "--project",
        default="",
        help="one checkout name, or a comma-delimited list, as the registry lists them",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="deprecated: only its directory is read, as the projects root",
    )
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
    parser.add_argument("action", nargs="?", default="", choices=["", TESTS_VERB, *sorted(ACTIONS)])
    parser.add_argument("extra", nargs=argparse.REMAINDER, help="extra args for the script")
    args = parser.parse_args(argv)

    try:
        projects = _registry_projects()
    except ProjectError as exc:
        return refuse_dispatch(exc, args.action, raw_argv)

    root = dispatch_root(args.workspace)

    if args.list:
        print("\n".join(projects))
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

        # Plan every selection before starting the first one. A stale name or
        # missing project script must not leave a multi-project request half-executed.
        directories = {name: resolve_project(name, projects, root) for name in selected}
        planned: list[tuple[Path, Action, list[str]]] = []
        skipped: list[str] = []
        if args.action == TESTS_VERB:
            # The test menu: a cross-product, so the plan carries its own action per
            # entry rather than one for the whole run.
            kinds = kind_selection(",".join(extra))
            if not kinds:
                raise ProjectError(f"no test kind given; the menu offers: {', '.join(TEST_KINDS)}")
            runs, skipped = plan_test_runs(kinds, selected)
            if not runs:
                # Every pair was out of scope. Exiting 0 here would report a green test
                # run to a toast and an empty artifact, having run nothing at all.
                raise ProjectError(
                    "nothing to run — every checkout/kind pair is out of scope:\n  "
                    + "\n  ".join(skipped)
                )
            for name, kind in runs:
                kind_action = ACTIONS[TEST_KINDS[kind].action]
                planned.append(
                    (
                        directories[name],
                        kind_action,
                        plan_command(kind_action, directories[name], list(TEST_KINDS[kind].args)),
                    )
                )
        else:
            for name in selected:
                check_scope(ACTIONS[args.action], name)
                planned.append(
                    (
                        directories[name],
                        ACTIONS[args.action],
                        plan_command(ACTIONS[args.action], directories[name], extra),
                    )
                )
    except ProjectError as exc:
        return refuse_dispatch(exc, args.action, raw_argv)

    for note in skipped:
        print(f"[skipped] {note}", flush=True)

    result = 0
    for directory, action, command in planned:
        ship_fixes = action.autofix and not args.no_ship_fixes
        print(f"[{directory.name}] {' '.join(command)}\n", flush=True)
        branch, before = autofix_state(directory) if ship_fixes else ("", ())
        returncode = subprocess.run(command, cwd=directory, env=task_env(), check=False).returncode
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
            root=root,
            slug=action.autofix_slug,
            labels=action.autofix_labels,
        )
        if outcome.note:
            print(f"\n[{directory.name}] {outcome.note}", flush=True)
        for step in outcome.commands:
            print(f"\n[{directory.name}] {' '.join(step)}\n", flush=True)
            code = subprocess.run(step, cwd=root, env=task_env(), check=False).returncode
            if code:
                # Stop at the first failure rather than shipping from a branch that was
                # never cut: the second command would then read the *home* branch and
                # sweep.ship_plan would refuse it, reporting a confusing second error
                # for the same cause.
                report_ship_failure(directory, action, step, code)
                result = result or code
                break
    return result


if __name__ == "__main__":
    raise SystemExit(main())
