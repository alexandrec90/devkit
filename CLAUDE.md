# devkit

The portable agent-coding harness for Claude Code / Codex, and the project generator
that ships it. This repo is the **source of truth**: consuming projects commit a
vendored copy of `scripts/sync-devkit.py`'s `MANIFEST` and pull changes from here.

## Baseline policy

`.claude/rules/engineering.md` (testing, script conventions, failure artifacts, the
harness seam, the instruction-feedback loop) and `.claude/rules/authoring.md` (writing
rules and skills) apply here too — devkit vendors them *out*, so it is also the first
place they have to hold. Everything below is what is true about devkit specifically.

## Tech Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| Runtime dependencies | **none** — stdlib only, by contract (see below) |
| Tests | pytest |
| Lint | ruff + mypy |

There is no Docker stack, no database, and no frontend. That is what lets CI run with
no service containers, and it is why `.devkit.toml` declares `[db] enabled =
false` and `[frontend] enabled = false`.

## devkit runs its own harness

Everything devkit ships to other projects is wired up **here**, on itself:

| Utility | Wired by |
| --- | --- |
| SessionStart provisioning | `.claude/settings.json` → `.claude/hooks/session-start.sh` |
| Task naming | `.claude/settings.json` → `scripts/task_slug.py` |
| Home-branch edit guard | `.claude/settings.json` → `scripts/worktree-guard.py` |
| Auto-lint on edit | `.claude/settings.json` → `scripts/hooks/lint-fix.py` |
| Pre-stop verification | `.claude/settings.json` → `scripts/hooks/stop.py` |
| Lint / test wrappers | `scripts/lint-all.py`, `scripts/run-tests.py` |
| Failure artifacts | `logs/lint-errors.log`, `logs/test-failures.log` (gitignored) |
| VS Code tasks | `.vscode/tasks.json` |
| Pre-commit gate | `.pre-commit-config.yaml` → `scripts/precommit/*.py` |
| Dependency updates | `.github/dependabot.yml` |
| Dependabot auto-merge | `.github/workflows/dependabot-automerge.yml` (vendored) |
| PR gate | `.github/workflows/pr-gate.yml`, titled `PR Gate` like every consumer's |

This is not decoration. A hook that only runs downstream is a hook nobody tests: devkit
shipped a `lint-fix.py` that formats on every edit and then needed a dedicated commit
(`4fbda17`) to clean up the format drift that had accumulated in the one repo where the
hook was not wired.

**When you change a hook script, you are changing the thing that is running you.** A
syntax error in `stop.py` breaks the current session's Stop; a bad `lint-fix.py` blocks
every subsequent edit. Both fail loudly and immediately, which is the point — but run
`python scripts/run-tests.py` and `python -m pytest scripts/hooks/tests/ -q` before
assuming a change is good.

## Scripts

All scripts under `scripts/` are Python, for cross-environment compatibility (local
Windows desktop and GitHub Actions).

- **Expose pure importable functions** guarded by `if __name__ == '__main__'` so pytest
  can test the logic without spawning a subprocess.
- Every new script ships with tests in the same change.
- **The hook scripts are stdlib only** — no third-party packages, ever. Hooks run
  *before* the virtualenv is active; a third-party import there breaks provisioning on
  exactly the sessions the harness exists to set up.

## The two test trees

They are deliberately separate, and the distinction is load-bearing.

- **`scripts/hooks/tests/`** — the vendored tier. It ships into every consuming project
  via `MANIFEST` and must stay **project-agnostic**: every value that varies per project
  comes from `hook.CFG` (read from that project's `.devkit.toml`), never from a
  literal. A hardcoded path once made 12 of these fail on every generated project's
  first CI run; `scripts/` being devkit's own `app_dir` broke another. Excluded from
  `pyproject.toml`'s `testpaths`, so it runs as its own step.
- **`tests/`** — devkit-only (generator, port registry, renderer, sweep). Never
  vendored, which is what lets the generator grow without forcing a `--pull` in every
  consumer. There is **no `conftest.py` here** on purpose — see `tests/support.py` for
  why a second one would collide with the vendored tree's.

## Vendoring rules

- `MANIFEST` in `scripts/sync-devkit.py` is the shared set. Every entry ships with its
  test; keep both listed so a vendored copy is verifiable in isolation.
- **`.devkit.toml` is never vendored** — it is the per-project seam the shared
  code reads. Same for `.claude/settings.json`, `scripts/lint-all.py` and
  `scripts/run-tests.py`: each project's copy differs (lint scope, mypy scope, OTEL
  ports), so they live in `templates/`, not `MANIFEST`.
- **Never hard-code project specifics in a hook script.** A new behaviour gets a
  manifest field and a neutral default in `harness_config.py`, not an `if project ==`
  branch.
- Vendored files are compared **byte-for-byte**, so formatting counts. CI runs
  `ruff format --check .` because an unformatted MANIFEST file gets reformatted
  downstream on first edit, and the consumer's `sync-devkit.py --check` then reports
  drift it did not cause.
- **`templates/` is a one-shot copy.** This is the whole reason the line between the
  two tiers matters: `--pull` never looks at a template again, so every fix made here
  after a project was generated stays here. carameli's `dependabot-automerge.yml` is
  three such fixes behind (`issues: write`, `--force` on `gh label create`, `GH_REPO`)
  and nothing could report it. So when a file stops having a per-project value, move
  it into `MANIFEST` rather than leaving it rendered.
- The GitHub Actions split follows from that. `.github/workflows/pr-gate.yml` stays a
  template — its jobs are the project's (services, migrations, a frontend tier), and
  carameli's five-job gate is what a shared one would have to delete or exempt.
  `.github/workflows/dependabot-automerge.yml` is vendored, because it has no
  per-project value left: it carries no `branches:` filter and waits on a gate titled
  **`PR Gate` in every project, devkit included**.

  `.github/actions/setup-python-env/action.yml` was vendored alongside it in v0.7.0, on
  the argument that its one variable — the Python version — had moved to the caller.
  **That was wrong, and it is a template again.** Two consumers disproved it on the
  first pull that reached them, and both failures were invisible until CI:

  - apt-finder's copy opened with a step cloning its private sibling `data-lake` into
    `../data-lake`, because `[tool.uv.sources]` declares an editable path dependency
    there. The vendored copy deleted the step and every job died on `Distribution not
    found` before running a check.
  - carameli does not use `uv sync` at all. It installs pip-tools compiled locks with
    `uv pip install --system -r requirements.txt -r requirements-dev.txt`, pins uv
    itself to the version in that lock, and takes an `extra-packages` input its weekly
    mutation job passes. There is no `uv.lock` for `uv sync` to read.

  The lesson is worth more than the file: what varies between projects is not *which
  interpreter* they install, it is **how they install**, and that is never shared.
  `test_setup_action_template_matches_devkits` holds devkit's own copy and the template
  together the way `notify.py` is held, and `test_the_setup_action_is_not_vendored` is
  the ratchet against re-adding it.

## The CI surface every project has

Four files, and which tier each belongs to is decided by whether its *content* varies:

| File | Tier | Why |
| --- | --- | --- |
| `.github/workflows/dependabot-automerge.yml` | vendored | nothing in it varies |
| `.github/actions/setup-python-env/action.yml` | template | how a project installs is the project's |
| `.github/workflows/pr-gate.yml` | template | its jobs are the project's |
| `.github/workflows/nightly.yml` | template | same, plus the tiers too slow to gate on |
| `.github/dependabot.yml` | template | names the ecosystems this project ships |

`scripts/hooks/tests/test_ci_workflow_contract.py` is vendored alongside them and
requires **all four to exist**, plus the settings that make an unattended run safe: a
top-level `permissions:` block, a `concurrency:` group, `cancel-in-progress: false` on
anything scheduled, and no action pinned to a mutable ref.

That test exists because **`templates/` cannot notice an absence.** A one-shot copy has
no way to report that a project never received a file or later deleted one, and the
result was measurable: of six repos in this workspace, one had a nightly, five had a
`dependabot.yml`, and two had nothing that could merge a Dependabot PR. None of those
gaps is visible from inside the repo that has it — a missing nightly does not fail, it
just never reports that the world moved under a branch nobody pushed to. The contract
test does not supply the workflow; it refuses to let a project go without one, and the
failure message carries the minimal file to add.

Adding a required file therefore has a cost the vendored tier does not: an existing
project's next `--pull` gets the *requirement* and not the render, and goes red until
someone writes the file. That is intended, and it is the reason the required set is
small and every entry has to earn its place.

The nightly is the one worth arguing for explicitly, since a gate already runs
everything. A gate fires on a change, so it cannot see the failures that arrive without
one: a dependency published inside the project's version bounds, a runner image bump, an
expired credential, a test that is flaky rather than broken. devkit's own nightly adds a
second job — `unlocked-toolchain`, which resolves the `dev` group off-lock — because
devkit's dev group *is* its product surface: a ruff release that breaks `lint-all.py`
breaks it in every consumer, and the lock hides that until Dependabot's weekly PR.

Deliberately **not** normalized, and each for the same reason (it encodes one project's
economics, not a shared practice): carameli's `weekly.yml` (mutation testing, migration
round-trip, a reliability issue comment), `sandbox-tests.yml` (a paid provider tier),
`dependabot-lock-repair.yml` (pip-tools universal locks, which no other project uses),
and `on-demand.yml` (an agent-fixer loop).

## The two channels

devkit ships the same discipline through two mechanisms, and which one a thing belongs to
is a real decision, not a preference:

| | Vendored tier | Pre-commit channel |
| --- | --- | --- |
| Delivered by | `sync-devkit.py --pull` copies files in | pre-commit clones devkit at a pinned `rev` |
| Lives in | `scripts/hooks/`, listed in `MANIFEST` | `scripts/precommit/`, listed in `.pre-commit-hooks.yaml` |
| Versioned by | `DEVKIT_VERSION` + a CI drift job | the `rev` in the consumer's config |
| Use it when | the code must run with no network and no install (agent hooks) | the check runs at commit time and a pinned version is better than a copy |

Rules specific to the pre-commit channel:

- **`language: script`, stdlib only, executable bit set.** There is nothing to install
  from a virtual project, so pre-commit execs the file directly. A missing `chmod +x` or a
  broken shebang fails only on a consumer's machine, after the rev is tagged — a test
  guards both.
- **The hooks run with the *consumer's* repo as the cwd**, while the scripts themselves
  live in pre-commit's clone. Never resolve a devkit file relative to the cwd; go through
  `Path(__file__)`. Never assume the consumer's layout — read it from
  `.devkit.toml`.
- **devkit wires its own hooks as `repo: local`, not by rev.** Pinning a rev here would
  check a released tag's hooks against the working tree trying to change them, so a hook
  fix could never be validated by the hook it fixes.
- **A new hook needs an id in both files** — `.pre-commit-hooks.yaml` (published) and
  `.pre-commit-config.yaml` (run here). A test asserts the sets match, with `devkit-drift`
  as the one documented exception (in devkit it would compare against itself).

## Loading a module by path

Three places do it (`tests/support.py`, `scripts/new-project.py`,
`scripts/precommit/_loader.py`) and the order is load-bearing every time: **register the
module in `sys.modules` before calling `exec_module`.** `@dataclass` resolves its string
annotations by looking the defining module up by name, so exec-first dies inside
`dataclasses` with `AttributeError: 'NoneType' object has no attribute '__dict__'` — a
traceback that points at CPython internals and not at your loader. `harness_config.py` is
nothing but frozen dataclasses, so anything that loads it by path hits this immediately.
Use `scripts/precommit/_loader.load_by_path` rather than writing a fourth one.

## `templates/` is content, not source

- `.tmpl` files are not valid Python until rendered, and the plain `.py` files under
  `templates/` are linted by the `ruff.toml` that ships *alongside* them into each
  generated project — which carries `scripts/**` allowances devkit's own config does
  not apply at those paths.
- So `templates/` is excluded from ruff (`force-exclude = true`, so the exclusion holds
  for the explicitly-named paths that `lint-fix.py` and `lint-all.py --changed` pass),
  from mypy, and from `lint-all.py`'s `--changed` scope.
- `scripts/notify.py` and `scripts/notify-wrap.py` are **byte-identical copies** of the
  files under `templates/core/scripts/`, and a test enforces that. Fix either one and
  copy it across.

## The two checkout tiers

The workspace holds two kinds of checkout, and which one a piece of work belongs in is
a real decision:

| | Static checkout | Ephemeral box |
| --- | --- | --- |
| Lives in | `<workspace>/<project>` | `<workspace>/.worktrees/<box>` |
| How many per repo | exactly one | as many as there are tasks in flight |
| Listed in `alex-projects.code-workspace` | yes | **no** — `sweep.py` never sees it |
| Port slot | pinned in `ports.toml` `[slots]` | leased on spawn, released on reap |
| Managed by | `sweep.py` (`--branch` → `--ship` → `--sync`) | `worktree.py` (`new` → `provision` → `/ship` → `reconcile`) |
| Toolchain | installed once, by hand, years ago | installed per box by `new` |
| Use it when | a human browses the stack, or the work is long-lived | an agent has one task |

There used to be a second static checkout per repo (`carameli-b`, `ibkr_trader-b`, …),
which existed purely to let two tasks run at once. Boxes do that without a ceiling of
two, so the `-b` tier was retired: it was spending four permanent `ports.toml` slots and
a container set per repo on concurrency the ephemeral tier provides for free, and it was
the sole reason the workspace needed a "which checkout owns this work" convention.
Freeing those slots raised the concurrent-box ceiling from 8 to 12.

The static tier's whole problem is that a checkout **outlives its task**. That is where
`sweep.py`'s workload comes from: `needs-branch`, `needs-rebranch`, `spent-branch`, the
anchor marker, `home_ref`, `dedupe_reaps` are every one of them a state a checkout can
only reach by surviving the work done in it. A box cut fresh off `origin/<default>` and
destroyed at the end cannot reach any of them.

So the tiers differ in *when* the guarantee is enforced, not in what it is:

- `sweep.py` **searches for** work that got left behind, out of band, when someone
  remembers to run it.
- `worktree.py reap` **will not free the box** until the work has left it. Nothing can
  be stranded because being stranded is what stops the cleanup.

Two consequences worth keeping straight:

- **`reap` is the one place in the workspace that passes `-v` to `compose down`.**
  `docker-maint.py` must never do it — its target is a static checkout whose named
  volumes hold a dev database costing hours to re-ingest. A box's volumes were created
  minutes ago by the box and are namespaced to its own `COMPOSE_PROJECT_NAME`, and
  leaking a set per task is how the WSL2 VHDX becomes the next bottleneck. `-p <box>`
  is passed explicitly so the scope cannot widen to the source project.
- **A box is never registered in the workspace file.** Registering one would put it in
  `sweep.py`'s scope, and then both tools would own its lifecycle.

`worktree-guard.py` is what routes work into a box automatically: an Edit or Write
that **would land on a home branch** gets a box spawned for it and the path handed
back, rather than landing there with no task branch under it — which is the agent
manufacturing the exact `needs-branch` backlog the sweep exists to clear. One box per
(session, project), so the fortieth edit reuses the first's.

"Would land on a home branch" covers both session shapes, and that is what let
`branch-per-task.py` and `branch-on-write.py` be retired. Those cut a branch *inside*
the checkout the session was in, which is the one act that makes a checkout outlive
its task. The guard declines in exactly two cases: the edit is already inside a box,
or the checkout is already on a `claude/...` branch — the "fix PR #42" case, where
something deliberately checked that branch out and a fresh box would put the fix
somewhere the PR never sees.

The prompt's slug reaches the box through `scripts/task_slug.py`, keyed by **session
id** rather than by worktree. That is the only key the two events share: the prompt
arrives on UserPromptSubmit, the box is cut on PreToolUse, and the two run in
different processes with different working directories. Without it every guard-cut
box was named `ws-<8 hex of session id>` and no PR title said what it did.

### `reconcile` is what makes the tier cheaper than the sweep, not merely faster

`reap --all` always skipped boxes holding work — but something had to *run* it, and
that something was a human reading the session-start line. So a merged PR left its
box, its branch, its port slot and its volume set in place indefinitely.

`worktree.py reconcile` is the unattended pass, meant for a schedule. Per box: PR
merged → reap; PR green and `--merge` is on → squash-merge, then reap in the same
pass; holding work → report and never touch. **`HOLD` is tested before anything that
destroys**, so work that exists only in a box survives a merged PR, disk pressure and
any age — the ordering is the whole safety property, and four tests fail if it moves.

Disk is the second half of it. A box costs its project's whole toolchain plus, with a
stack, a volume set, and this workstation runs short of disk. At or under
`--min-free-gb` reconcile also reaps boxes whose PR is merely *open*: every commit is
on the remote, so what is lost is the checkout and not the work. `free_gb` is one
syscall; per-box sizes are behind `list --sizes` because measuring them means walking
a `.venv` and a `node_modules` per box.

### A box is not usable until it is provisioned

A linked worktree checks out **tracked files only**, so a fresh box has no `.venv` and
no `node_modules`, and nothing else was going to create them: `session-start.sh` returns
early on a local machine, because a static checkout is provisioned once by hand and then
never again. A box that skips this can be edited and then fails its own `/ship` — the
lint gate runs with no ruff in the box to run it.

So `worktree.py new` installs the toolchain, walking the same ladder
`session-start.sh` does, in the same order: `[python] install_command` from
`.devkit.toml`, else `uv.lock` → `uv sync`, else the requirements locks, else an
editable `pyproject.toml`, plus `npm install` for a project with a frontend tier. **The
guard hook is the one caller that passes `provision=False`** — an install is minutes and
a PreToolUse hook that takes minutes is one the agent experiences as a hang — so the
box it cuts carries the `worktree.py provision <box> --yes` command in its block message
instead.

### Where the tier is reachable from

Three places, and they are the answer to "who would ever notice a box":

- **`Worktree: New Box` / `List Boxes` / `Reap Finished Boxes`** in the workspace task
  list. There is deliberately no reap-one-box task: a picker cannot enumerate boxes
  created between clicks, so reaping by name stays CLI-only and the one-click shape is
  the sweep.
- **`workspace-status.py`** reports live boxes at every session start, split by whether
  each is holding work (ship it) or reapable (pure leaked slot). Nothing else can: boxes
  are absent from the workspace file by design, so the sweep cannot see them.
- **The same status line reports a workspace root that does not run the guard.** That
  wiring lives in `<workspace>/.claude/settings.json`, outside every repository, so no
  test in devkit can hold it in place — and an unwired guard has no symptom at all,
  which is why absent is reported here rather than passed over in silence.

## Failure artifacts (fix from a file, not from the terminal)

Any task or script whose failures an agent is expected to act on must persist the
failure to a **parseable artifact file** under `logs/`. Never rely on streamed terminal
output — it scrolls away and buries the signal. Keep the terminal to a status line plus
the artifact path, put everything needed to diagnose in the file, write it on failure
*and* on success (an empty artifact on success, so a stale run cannot mislead the next
agent), and overwrite per run.

## VS Code tasks

**Tasks live in the workspace file, never in a repo's `.vscode/tasks.json`.** The
original reason was the `-b` tier: every repo was checked out twice, both folders were
in one multi-root workspace, and a task defined inside a repo rendered once per folder —
two quick-pick entries under one label, nothing saying which checkout each would run in,
and two copies that disagreed the moment the worktrees sat on different branches.

That tier is gone, and the rule survives it. A task defined in a repo is invisible from
the workspace root, cannot be scoped with `Action.projects`, and drifts from its
siblings; the workspace file is the one place that sees every checkout. devkit and both
live repos ship zero project-level tasks, and each one's suite fails if that changes.

**Project-specific is not a reason to keep a task local.** `Action.projects` in
`devkit_project.py` scopes an action to the checkouts that can run it. That is how
carameli's Playwright run and ibkr_trader's backtests are defined once without
pretending every checkout can run them.
The scope restricts both directions — the dispatcher refuses an out-of-scope checkout by
name, and `--check` stops demanding the script from projects it was never meant for.

What a repo owes instead is the **CLI contract**: a `scripts/<name>.py` at the path
`ACTIONS` names, accepting the documented arguments. A task that cannot be expressed that
way is not blocked from hoisting — write the seam. `scripts/backtest-task.py` in
ibkr_trader exists for exactly that reason: the two Backtest tasks invoked
`.venv\Scripts\ibkr-trader.exe` directly, which the dispatcher cannot call.

Conventions for the tasks themselves:

- Use `"type": "process"` so VS Code monitors the process directly — that is what makes
  the spinner stop and the exit-code icon appear reliably.
- Set `"close": false` in `presentation` so the terminal stays open for review.
- **Wrap with `notify-wrap.py`** for the completion toast; never call `notify.py` from
  inside a script. Notifications are a task-layer concern only.
- **And with `log-wrap.py`, inside it**, so the run's output survives the terminal as
  `logs/<slug of the task>.log` — emptied when the task passes, so it never describes a
  failure that is already fixed. The nesting is `notify-wrap → log-wrap → the script`:
  the toast needs only an exit code, the artifact needs the output, and the script
  needs to know about neither. A **dispatched** task gets this for free — `plan_command`
  in `devkit_project.py` wraps every action, and it is the only place that can, because
  the task names a picker and nothing knows which checkout's `logs/` the failure belongs
  in until `resolve_project` has run. A task that deliberately writes no artifact goes
  in `UNLOGGED_TASKS` in `tests/test_devkit_project.py` with its reason; the two
  launcher tasks are there because the window they open *is* the output.
- Label convention: `"Domain: Title Case Action"`, and **every task carries a `detail`**
  — that is the second line in the quick-pick, and the only place a one-click action can
  state its cost or blast radius.
- **Every task carries an `icon`, and no two share the same id+colour pair.** With one
  consolidated list, colour is what makes it scannable; the `terminal.ansiBright*`
  variants mark the project-scoped tasks, so you can see before clicking that a task
  will ask which checkout to use.
- A `${input:...}` picker must supply **one real token in every branch**. An empty
  string reaches argparse as a stray positional and is rejected, which is why
  `new-project.py` carries the redundant-looking `--dry-run` and `--remote` flags
  alongside their negations. The exception is a picker feeding `devkit_project.py`,
  which strips empties before exec — `testScope` and `e2eMode` rely on that, and say so.
- **A new project has to reach more than the `project` picker.** `register()` extends the
  `folders` list and that one picker; the workspace-scoped pickers — `sweepScope`,
  `upgradeScope` — are hand-maintained and were silently skipped, so data-lake could run
  every generic task while `--all` was the only way to sweep or upgrade it. `SCOPE_PICKERS`
  in `tests/test_devkit_project.py` now requires each of them to cover every checkout the
  `project` picker lists, and a deliberate omission (devkit is not a target of a devkit
  upgrade) to carry its reason in writing. Pickers scoped by `Action.projects` are a
  separate case and are gated separately.

## Testing

The policy is `.claude/rules/engineering.md`; it is vendored and drift-gated, so this
file does not restate it (`test_repo_contract.py` fails a CLAUDE.md that does — a second
copy reads as authoritative and is the one nothing checks). What is specific to devkit:

- A change to a hook script needs a test in the *vendored* tree, written against
  `hook.CFG` rather than devkit's literal values — it has to pass in every consumer too.
- Verify the generator by rendering, not by reading: `tests/` builds a project of each
  preset and parses every file it emits.

## Guardrails

The instruction-file feedback loop lives in `.claude/rules/engineering.md` — report a
rule that sent you into a dead end instead of routing around it.

### One bad commit here reddens every consumer

Generated PR gates pin a devkit **tag**, never `@main`, for this reason. When a change
alters vendored behaviour, say so in the commit message: adopters find out by running
`sync-devkit.py --pull`, and the message is the only changelog they get.

A missing tag is the mirror-image failure and is easier to miss: `new-project.py`
resolves `latest_devkit_tag() or FALLBACK_DEVKIT_REF`, so **an untagged feature does
not exist as far as a generated project is concerned**, however green `main` is. That
is how a rendered `.pre-commit-config.yaml` came to request hook ids its pinned tag
could not serve, aborting the new owner's first commit. The release checklist —
including why the fallback test is deliberately red for one commit — is
[`RELEASING.md`](RELEASING.md).

### A path a vendored script hard-codes is a promise

`stop.py` resolves its dispatch targets by path, spawns them with both streams on
`DEVNULL`, and never reads the exit code. A target that is not there is therefore the
quietest failure in the harness: state finalization and session archiving simply stop
happening, in every consumer, with nothing red anywhere. devkit shipped exactly that
for several releases while its own vendored contract test asserted one of the missing
files existed.

So: either the file is **in the `MANIFEST`**, or the dispatcher treats its absence as
an explicit, documented skip. `tests/test_dispatch_coherence.py` enforces the choice
and requires a written reason for each exception.

## The internal names are `devkit` now

`.devkit.toml`, `$DEVKIT_DIR`, `DEVKIT_VERSION`, `scripts/sync-devkit.py`, and the
published hook ids `devkit-manifest` / `devkit-hooks-stdlib-only` / `devkit-drift`.

They previously used the pre-rename `agent-harness` spelling, deferred because
`sync-devkit.py` is **itself in the `MANIFEST`** — so renaming it changes the very path
list the drift check compares by, and a half-applied rename fails `--check` in whichever
repo lands second. It was done as one atomic change across devkit and every consumer
while there was exactly one consumer and it was already 21 entries behind, which made
its vendored copies due for wholesale replacement anyway. That window is closed now;
treat these names as fixed.

If you find the old spelling anywhere, it is a miss from that migration, not a
deliberate holdout — fix it.
