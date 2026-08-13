# devkit

The portable agent-coding harness for Claude Code / Codex, and the project generator
that ships it. This repo is the **source of truth**: consuming projects commit a
vendored copy of `scripts/sync-devkit.py`'s `MANIFEST` and pull changes from here.

[`README.md`](README.md) says what each tool is and how to run it, and the code says
what it does today. This file carries only what neither of those can: the decisions,
and the failures that produced them.

## Baseline policy

`.claude/rules/engineering.md` (testing, script conventions, failure artifacts, the
harness seam, the instruction-feedback loop) and `.claude/rules/authoring.md` (writing
rules and skills) apply here too — devkit vendors them *out*, so it is also the first
place they have to hold. Everything below is what is true about devkit specifically.

## The docs are vibe-coded too

Every file here was written by an agent, this one included. Prose is the only artifact
in the repo with no compiler and no test, so a wrong sentence survives in a way wrong
code cannot — and an instruction file is read as *authority*, which is what turns a
stale paragraph into one agent talking the next one out of a correct change. That has
already happened here. Assume it is happening now.

1. **The code decides.** Where a document and the repo disagree, the document is the
   defect. Fix it in the same change as the work that found it; never route around it,
   and never let it veto a change you can otherwise see is right.
2. **Write only what stays true.** A version, a count, a fact about another repo, or a
   restatement of something a config file already states is a claim with no owner — the
   thing moves, the sentence does not. Name the file that owns it instead of copying
   its current value.
3. **`tests/test_doc_claims.py` gates the checkable half.** Every path cited in an
   inline code span or a Markdown link has to exist, and instruction prose may pin no
   version. It cannot tell you a rationale went stale; it does stop the *silent* rot,
   which is the kind that accumulates. Its two exemption lists are where a deliberate
   absence goes, with the reason, and each entry has to stay both absent and cited.

## Nothing but the standard library, by contract

Read the interpreter version and the dev tools off `pyproject.toml`; they are not
worth pinning in prose. What is not readable there is that the empty runtime
dependency list is a **constraint, not a state**: the vendored hooks run before a
virtualenv exists, in a repo devkit does not control, so an import of anything
installed breaks provisioning on exactly the sessions the harness exists to set up.

The same contract is why there is no stack here — no database, no frontend, no
compose file — which is what lets CI run with no service containers and why
`.devkit.toml` turns both of those tiers off.

## devkit runs its own harness

Everything devkit ships to other projects is wired up **here**, on itself: the hooks
in `.claude/settings.json`, the lint and test wrappers they call, the pre-commit gate,
the failure artifacts under `logs/`, and a PR gate titled `PR Gate` like every
consumer's. The wiring is readable from those files; what matters is that it exists.

This is not decoration. A hook that only runs downstream is a hook nobody tests: devkit
shipped a `lint-fix.py` that formats on every edit and then needed a dedicated commit
(`4fbda17`) to clean up the format drift that had accumulated in the one repo where the
hook was not wired.

**When you change a hook script, you are changing the thing that is running you.** A
syntax error in `stop.py` breaks the current session's Stop; a bad `lint-fix.py` blocks
every subsequent edit. Both fail loudly and immediately, which is the point — but run
`python scripts/run-tests.py` and the vendored tree's suite
(`python -m pytest scripts/hooks/tests/ -q`) before assuming a change is good.

## The two test trees

They are deliberately separate, and the distinction is load-bearing.

- **`scripts/hooks/tests/`** — the vendored tier. It ships into every consuming project
  via `MANIFEST` and must stay **project-agnostic**: every value that varies per project
  comes from `hook.CFG` (read from that project's `.devkit.toml`), never from a
  literal. A hardcoded path once made a dozen of these fail on every generated project's
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
  after a project was generated stays here, and nothing can report the gap. A live
  consumer is currently several fixes behind on a template it was rendered with, and
  only a human diffing the two files would ever know. So when a file stops having a
  per-project value, move it into `MANIFEST` rather than leaving it rendered.

### The move that has actually gone wrong is the other one

`.github/actions/setup-python-env/action.yml` was vendored once, on the argument that
its one variable — the interpreter version — had moved to the caller. **That was wrong,
and it is a template again.** Two consumers disproved it on the first pull that reached
them, and both failures were invisible until CI:

- One opens with a step cloning a private sibling repo that its editable path
  dependency points at. The vendored copy deleted the step, and every job died on a
  missing distribution before running a single check.
- The other does not use the same installer at all. It installs compiled locks, pins
  the installer itself to the version in that lock, and takes an input its mutation job
  passes — none of which the vendored copy had.

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

The automerge workflow is vendored because it has no per-project value left: it carries
no `branches:` filter and waits on a gate titled `PR Gate` in every project, devkit
included. The PR gate is not, and cannot be — its jobs are the project's own services,
migrations and frontend tier, and the largest consumer's five-job gate is what a shared
one would have to delete or exempt.

`scripts/hooks/tests/test_ci_workflow_contract.py` is vendored alongside them and
requires **all four to exist**, plus the settings that make an unattended run safe: a
top-level `permissions:` block, a `concurrency:` group, `cancel-in-progress: false` on
anything scheduled, and no action pinned to a mutable ref.

That test exists because **`templates/` cannot notice an absence.** A one-shot copy has
no way to report that a project never received a file or later deleted one, and the
result was measurable: when the contract was written, most repos in this workspace were
missing at least one of the four, and two had nothing that could merge a dependency-bump
PR at all. None of those gaps is visible from inside the repo that has it — a missing
nightly does not fail, it just never reports that the world moved under a branch nobody
pushed to. The contract test does not supply the workflow; it refuses to let a project
go without one, and the failure message carries the minimal file to add.

Adding a required file therefore has a cost the vendored tier does not: an existing
project's next `--pull` gets the *requirement* and not the render, and goes red until
someone writes the file. That is intended, and it is the reason the required set is
small and every entry has to earn its place.

The nightly is the one worth arguing for explicitly, since a gate already runs
everything. A gate fires on a change, so it cannot see the failures that arrive without
one: a dependency published inside the project's version bounds, a runner image bump, an
expired credential, a test that is flaky rather than broken. devkit's own nightly adds a
second job — `unlocked-toolchain`, which resolves the `dev` group off-lock — because
devkit's dev group *is* its product surface: a linter release that breaks `lint-all.py`
breaks it in every consumer, and the lock hides that until the weekly dependency PR.

Deliberately **not** normalized, and each for the same reason (it encodes one project's
economics, not a shared practice): mutation testing and migration round-trips, a paid
provider tier's smoke suite, lock repair for a locking scheme no other project uses, and
an agent-fixer loop.

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

## Ephemeral boxes

The workspace holds two kinds of checkout: one static checkout per repo, for a human
browsing the stack or for long-lived work, and as many ephemeral boxes under
`<workspace>/.worktrees/` as there are agent tasks in flight. The workspace `CLAUDE.md`
covers working in one; `README.md` covers the commands. What follows is what a change to
`worktree.py` or `worktree-guard.py` has to preserve.

The static tier's whole problem is that a checkout **outlives its task**. That is where
`sweep.py`'s workload comes from: `needs-branch`, `needs-rebranch`, `spent-branch`, the
anchor marker, `home_ref`, `dedupe_reaps` are every one of them a state a checkout can
only reach by surviving the work done in it. A box cut fresh off `origin/<default>` and
destroyed at the end cannot reach any of them. So the tiers differ in *when* the
guarantee is enforced, not in what it is: the sweep **searches for** work left behind,
whenever someone remembers to run it, while `reap` **will not free the box** until the
work has left it. Nothing can be stranded because being stranded is what stops the
cleanup.

Five invariants, each of which something has already violated:

- **`HOLD` is tested before anything that destroys.** `reconcile` is the unattended pass
  meant for a schedule — merged PR → reap, green PR under `--merge` → squash and reap —
  and under disk pressure it also reaps boxes whose PR is merely *open*, since every
  commit is on the remote and what is lost is the checkout rather than the work. Work
  that exists only in a box has to survive a merged PR, disk pressure and any age. The
  ordering is the whole safety property, and four tests fail if it moves.
- **`reap` is the one place in the workspace that passes `-v` to `compose down`.**
  `docker-maint.py` must never do it — its target is a static checkout whose named
  volumes hold a dev database costing hours to re-ingest. A box's volumes were created
  minutes ago by the box and are namespaced to its own `COMPOSE_PROJECT_NAME`, and
  leaking a set per task is how the WSL2 VHDX becomes the next bottleneck. `-p <box>` is
  passed explicitly so the scope cannot widen to the source project.
- **A box is never registered in the workspace file.** Registering one would put it in
  `sweep.py`'s scope, and then both tools would own its lifecycle. The cost is that
  nothing else can see boxes, which is why `workspace-status.py` reports them at session
  start, split by whether each holds work or is a pure leaked slot.
- **The guard is the one caller that skips provisioning.** A linked worktree checks out
  tracked files only, so a fresh box has no installed toolchain and nothing else was
  going to create one — `session-start.sh` returns early on a local machine. `worktree.py
  new` therefore installs it, walking the same ladder in the same order. But an install
  takes minutes and a PreToolUse hook that takes minutes is one the agent experiences as
  a hang, so the guard passes `provision=False` and puts the `provision` command in its
  block message instead.
- **The guard declines for a path that belongs to no project**, which is what keeps a
  reference checkout out of the box tier entirely. It builds its project list with
  `devkit_project.known_projects`, so a folder in `NOT_PROJECTS` is registered in the
  workspace — visible, readable — and yet owns no path the guard will route: an edit
  there is allowed silently, on whatever branch it is parked on. Reading the registry
  raw instead would cut `VanillaLand` a box on a `claude/...` branch, for a checkout
  that ships nothing and whose Azure DevOps remote has no PR for that branch to become.
  `test_an_edit_into_a_reference_checkout_is_allowed` is the ratchet, and it is a
  `main()` test on purpose: `redirect_decision` takes the project list as an argument,
  so only the shell can be wrong about it.
- **Among paths it does own, the guard declines in exactly two cases**: the edit is
  already inside a box, or the checkout is on a `claude/...` branch **that carries
  commits of its own** — the "fix PR #42" case, where something deliberately checked
  that branch out and a fresh box would put the fix somewhere the PR never sees.
  Anything else that would land on a home branch gets a box, because landing there with
  no task branch under it is the agent manufacturing the exact `needs-branch` backlog
  the sweep exists to clear.
- **"Is this a task branch" is not the question; "is there work here a box would
  strand" is.** Being a `claude/...` branch used to be the whole test, and the effect
  was that the first session to leave one checked out turned the guard off for every
  session afterwards — the checkout became shared, unguarded space until someone parked
  it back on a home branch. Two sessions landed in one checkout that way, one of them
  on a branch whose PR had already merged. `branch_has_own_commits` is the distinction,
  and it is deliberately local (`git rev-list` against an already-fetched
  `origin/<default>`, not a PR lookup) because this runs on every edit and a network
  round trip in a PreToolUse hook is a hang. It **fails closed**: any error declines.

The prompt's slug reaches the box through `scripts/task_slug.py`, keyed by **session id**
rather than by worktree. That is the only key the two events share: the prompt arrives on
UserPromptSubmit, the box is cut on PreToolUse, and the two run in different processes
with different working directories. Without it every guard-cut box was named
`ws-<8 hex of session id>` and no PR title said what it did.

## VS Code tasks

**Tasks live in the workspace file, never in a repo's `.vscode/tasks.json`.** A task
defined in a repo is invisible from the workspace root, cannot be scoped with
`Action.projects`, and drifts from its siblings; the workspace file is the one place that
sees every checkout. devkit and both live repos ship zero project-level tasks, and each
one's suite fails if that changes.

**Project-specific is not a reason to keep a task local.** `Action.projects` in
`devkit_project.py` scopes an action to the checkouts that can run it, which is how a
browser suite or a backtest run is defined once without pretending every checkout can run
it. The scope restricts both directions — the dispatcher refuses an out-of-scope checkout
by name, and `--check` stops demanding the script from projects it was never meant for.

What a repo owes instead is the **CLI contract**: a `scripts/<name>.py` at the path
`ACTIONS` names, accepting the documented arguments. A task that cannot be expressed that
way is not blocked from hoisting — write the seam. `scripts/backtest-task.py` in
ibkr_trader exists for exactly that reason: its two tasks invoked a console-script
executable directly, which the dispatcher cannot call.

### Changing a task: the live file first, then adopt

`workspace-tasks.jsonc` is devkit's copy of the block, and the workspace file — which
lives outside every repo and so cannot be vendored — is the one VS Code actually runs.
**Edit `<workspace>/alex-projects.code-workspace`, then record it:**

```bash
python scripts/devkit_project.py --adopt-tasks   # live file -> workspace-tasks.jsonc
python scripts/devkit_project.py --check-tasks   # verify they agree
```

**One-way, with no flag for the other direction.** Editing `workspace-tasks.jsonc`
directly looks right — it is the file in the repo, the diff is clean, and the drift test
even names `--adopt-tasks` as the remedy — and running that *deletes the edit*, because
it regenerates the canonical copy from the live file. One test holds the pair together
(`test_the_live_workspace_matches_the_canonical_block`) and it is
`@needs_live_workspace`: skipped in CI, so drift is caught locally or not at all.

### Conventions for the tasks themselves

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
  `upgradeScope` — are hand-maintained and were silently skipped, so a newly generated
  project could run every generic task while `--all` was the only way to sweep or upgrade
  it. `SCOPE_PICKERS` in `tests/test_devkit_project.py` now requires each of them to
  cover every checkout the `project` picker lists, and a deliberate omission (devkit is
  not a target of a devkit upgrade) to carry its reason in writing. Pickers scoped by
  `Action.projects` are a separate case and are gated separately.

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

### The internal names are `devkit`

`.devkit.toml`, `$DEVKIT_DIR`, `DEVKIT_VERSION`, `scripts/sync-devkit.py`, and the
published hook ids `devkit-manifest` / `devkit-hooks-stdlib-only` / `devkit-drift`. The
rename from the `agent-harness` spelling had to be one atomic change across devkit and
every consumer, because `sync-devkit.py` is **itself in the `MANIFEST`** — renaming it
changes the very path list the drift check compares by, and a half-applied rename fails
`--check` in whichever repo lands second. Treat these names as fixed; if you find the
old spelling anywhere, it is a miss from that migration, not a deliberate holdout.
