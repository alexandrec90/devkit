# scripts/

What a change to a script here has to preserve. The root [`CLAUDE.md`](../CLAUDE.md)
carries the repo-wide decisions; `.claude/rules/engineering.md` carries the script
conventions every project shares (stdlib-only hooks, importable functions, failure
artifacts under `logs/`).

## Vendoring rules

- `MANIFEST` in `scripts/sync-devkit.py` is the shared set. Every entry ships with its
  test; keep both listed so a vendored copy is verifiable in isolation.
- **`.devkit.toml` is never vendored** — it is the per-project seam the shared code
  reads. Same for `.claude/settings.json`, `scripts/lint-all.py` and
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

The move that has actually gone wrong is the other one — vendoring something whose
*content* varies. [`.github/CLAUDE.md`](../.github/CLAUDE.md) has that case in full.

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

## A path a vendored script hard-codes is a promise

`stop.py` resolves its dispatch targets by path, spawns them with both streams on
`DEVNULL`, and never reads the exit code. A target that is not there is therefore the
quietest failure in the harness: state finalization and session archiving simply stop
happening, in every consumer, with nothing red anywhere. devkit shipped exactly that
for several releases while its own vendored contract test asserted one of the missing
files existed.

So: either the file is **in the `MANIFEST`**, or the dispatcher treats its absence as
an explicit, documented skip. `tests/test_dispatch_coherence.py` enforces the choice
and requires a written reason for each exception.

## Loading a module by path

Three places do it (`tests/support.py`, `scripts/new-project.py`,
`scripts/precommit/_loader.py`) and the order is load-bearing every time: **register the
module in `sys.modules` before calling `exec_module`.** `@dataclass` resolves its string
annotations by looking the defining module up by name, so exec-first dies inside
`dataclasses` with `AttributeError: 'NoneType' object has no attribute '__dict__'` — a
traceback that points at CPython internals and not at your loader. `harness_config.py` is
nothing but frozen dataclasses, so anything that loads it by path hits this immediately.
Use `scripts/precommit/_loader.py`'s `load_by_path` rather than writing a fourth one.

## Ephemeral boxes

The workspace holds two kinds of checkout: one static checkout per repo, for a human
browsing the stack or for long-lived work, and as many ephemeral boxes under
`<workspace>/.worktrees/` as there are agent tasks in flight. The workspace `CLAUDE.md`
covers working in one; `README.md` covers the commands. What follows is what a change to
`scripts/worktree.py` or `scripts/worktree-guard.py` has to preserve.

The static tier's whole problem is that a checkout **outlives its task**. That is where
`sweep.py`'s workload comes from: `needs-branch`, `needs-rebranch`, `spent-branch`, the
anchor marker, `home_ref`, `dedupe_reaps` are every one of them a state a checkout can
only reach by surviving the work done in it. A box cut fresh off `origin/<default>` and
destroyed at the end cannot reach any of them. So the tiers differ in *when* the
guarantee is enforced, not in what it is: the sweep **searches for** work left behind,
whenever someone remembers to run it, while `reap` **will not free the box** until the
work has left it. Nothing can be stranded because being stranded is what stops the
cleanup.

Each of these has already been violated by something:

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

### What the guard declines, and why each case exists

- **A path that belongs to no project**, which is what keeps a reference checkout out of
  the box tier entirely. It builds its project list with `devkit_project.known_projects`,
  so a folder in `NOT_PROJECTS` is registered in the workspace — visible, readable — and
  yet owns no path the guard will route: an edit there is allowed silently, on whatever
  branch it is parked on. Reading the registry raw instead would cut `VanillaLand` a box
  on an `agent/...` branch, for a checkout that ships nothing and whose Azure DevOps
  remote has no PR for that branch to become.
  `test_an_edit_into_a_reference_checkout_is_allowed` is the ratchet, and it is a
  `main()` test on purpose: `redirect_decision` takes the project list as an argument,
  so only the shell can be wrong about it.
- **Among paths it does own, exactly two cases**: the edit is already inside a box, or
  the checkout is on a managed task branch **that carries commits of its own** — the
  "fix PR #42" case, where something deliberately checked that branch out and a fresh
  box would put the fix somewhere the PR never sees. Anything else that would land on a
  home branch gets a box, because landing there with no task branch under it is the
  agent manufacturing the exact `needs-branch` backlog the sweep exists to clear.
- **"Is this a task branch" is not the question; "is there work here a box would
  strand" is.** Being a managed task branch used to be the whole test, and the effect
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

## A scheduled task is registered from XML, never from `schtasks` flags

Both unattended jobs — `install-reconcile-task.py` and `install-upgrade-schedule.py` —
go through `scripts/devkit_schtasks.py`, which builds a task document and registers it
with `/XML`. That is not a style preference. **The three settings that decide whether a
scheduled job on a laptop runs at all have no `schtasks.exe` flags**, so every task
that tool creates silently inherits server defaults: it skips every fire while on
battery, kills a run in progress when you unplug, and never catches up a fire it slept
through.

This workspace runs on a laptop, and the cost was measured rather than imagined: the
reconcile task was found stopped for five days with every box it manages leaking its
port slot and volume set, and the nightly upgrade loses a whole day for any night the
lid is closed at 03:00. None of that reports anything — a job that does not run writes
no log, so its silence is identical to a healthy pass with nothing to do.

Two things a change here has to keep:

- **`<Settings>` is a schema sequence, not a set.** Reordering it is rejected at
  registration time, on the installing machine, where no unit test can reach it. The
  order in `task_xml` was verified by registering the document and reading it back;
  `test_the_settings_block_is_in_schema_order` pins it, and scopes its search to the
  `<Settings>` element because `<Enabled>` is also a legal trigger child.
- **A repetition carries no `<Duration>`.** Absent means indefinitely; any value
  present is a stopping point, so a plausible-looking `P1D` turns the job off after a
  day.

The other half of keeping these alive is noticing when one has stopped anyway, which
is `workspace-status.py`'s `scheduler_line` — it reads when a pass last *finished*
rather than whether a task is registered, because `schtasks` answers yes for a task
that is disabled, wedged, or pointed at a checkout that has moved.

### The scheduled pass carries the static tier too

`reconcile` is the only thing in the workspace on a schedule, so it is also the only
place the *static* checkouts can be brought up to their remotes without someone
remembering. It runs `sweep.py --sync` over them after the box pass — `sync_checkouts`
— and the tiers stay disjoint in what they decide: boxes by `reconcile_action`,
checkouts by `sweep.classify`, neither tool learning about the other's.

What made this worth doing is not tidiness. Every checkout in the workspace was found
stale at once: four parked on task branches whose PRs had merged days earlier, and a
session opened in one of them reads a tree that predates the work it was asked to
continue — with nothing red anywhere, because a local branch that never advanced is
not a failure of anything.

Two things a change here must not do. **The checkout half never gains authority the
hand-run sweep does not have** — it acts only on `sweep.SYNCABLE`, so a checkout
holding uncommitted work, unpushed commits or an open PR is named and stepped over;
its steps stay `merge --ff-only` and `branch -d`, both of which refuse rather than
destroy. And **it must not redden a healthy pass**: `sweep.run_mode` returns 1 from a
dry run that merely found something to do, so `checkout_sync_summary` reinterprets the
code and only a failed git step under `--yes` counts. A scheduled runner whose alerts
fire on the normal case is a runner whose alerts nobody reads — and this one has
already been found *disabled*, with every box and checkout it manages left to rot,
which is the failure mode that costs the most and shows the least.
