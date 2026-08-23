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

## Vendoring a generator does not vendor its output

`.codex/hooks.json` is written by `sync-codex-hooks.py` from the project's own
`.claude/settings.json`, so the script is in the `MANIFEST` and the file it produces
cannot be — its content is per-project. That asymmetry is a **third** delivery path,
and it was the one with no gate on it: a `--pull` adopts a new generator and changes
nothing about what Codex actually runs, because the file Codex reads was written by the
generator before it.

It cost months. `REDUNDANT_HANDLERS` stopped porting the Claude-only Bash cap into Codex
the day it landed, and Codex sessions in every already-generated project went on being
blocked by it — with the block's own suggested remedy, `invoke-capped.py`, being a
wrapper the session then applied to every command after it. Half the shell calls in one
project's Codex sessions were the wrapper. Nothing was red anywhere, because both halves
were individually correct.

So: **anything generated from a vendored script needs a check that regenerates it and
compares.** `sync_devkit.codex_hooks_stale` is that check, `regenerate_codex_hooks` runs
it on `--pull`, and the vendored
`test_the_committed_codex_artifact_matches_the_generator` runs it in a consumer's PR
gate where no `$DEVKIT_DIR` exists. Regenerating on pull is not enough on its own —
a project only pulls when someone asks it to.

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
- **"Only the checkout is lost" was a claim with nothing behind it.** That is the
  sentence the pressure branch reaps an open PR's box on, and for a year there was no
  verb that could get the checkout back: every route into the tier ran through
  `spawn_plan`, which cuts a *new* branch off `origin/<default>` by construction. So the
  reap was invisible and its consequence was not — the session's next edit continued one
  task on a second branch under a second PR, and nothing in either said so. `resume_plan`
  is the missing half, and `plan_respawn` is what makes it automatic: the guard asks for
  a resume before it cuts anything, because an agent cannot know its box was destroyed
  and by the time the divergence shows it is two PRs old. The branch comes from the reap
  ledger, which is why `ReapPlan.session` exists — the lease file is the live set, and
  the entry naming the box's owner was deleted by the reap that wrote the ledger line.
  Resumability is decided by `branch_is_merged` against local refs rather than by the
  PR's state: "has this work landed" is the actual question, and a merged branch lingers
  in `refs/remotes/` until someone prunes.
- **"Has the work left the box" is not a question the verdict can answer alone.** A
  squash merge rewrites the commits and `--delete-branch` takes the upstream with it, so
  the box `sweep.classify` sees is one with unmerged commits on a retired branch —
  `needs-rebranch`, forever, however completely the work landed. That verdict is not
  reapable, so **every squash-merged box was a permanent `HOLD`**: a leaked checkout,
  port lease and volume set apiece, and `--force` — the flag that also discards
  uncommitted work — as the only way out. `worktree.reapable` is the single predicate
  both `reap` and `reconcile` now ask, and it consults the merged PR the way
  `branch_delete_flag` already did to choose `-D`. It is narrow in both directions:
  `MERGE_CAN_BE_STALE_ABOUT` scopes the escape to that one verdict, and the box must
  also be clean, because a merge says where the *commits* are and nothing about the
  edits on top of them.
- **A PR can end without merging, and the tier knew only one ending.** A closed PR
  left its box `needs-pr` forever — *wait for the merge*, about a merge nobody was
  going to make — so the leak above arrived a second time from the other end, with
  `--force` again the only way out. What a close clears is the **policy** refusal in
  `reap_decision`, never `reapable`: `needs-rebranch` plus a *closed* PR is an
  abandoned branch holding the only copy of its commits, which is precisely what the
  merge escape above exists to keep holding. `reap` reads the state from the same
  `pr_for` call `reconcile` makes, so the two can no longer describe one box in two
  ways — the disagreement `AWAITS_A_MERGE` was added to end.
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
- **The guard re-aims the call rather than refusing it, and the refusal is the
  fallback.** For a year this hook could only exit 2, because a PreToolUse hook could
  only allow or deny — so every guarded session paid a failed tool call per routed edit,
  the agent re-sent arguments it had already sent (for a `Write`, a whole file), and the
  transcript filled with hook errors that described correct behaviour. Claude Code's
  `hookSpecificOutput.updatedInput` rewrites the arguments the tool is called with, so
  the edit now lands in the box on the first attempt with the prose arriving as
  `additionalContext`. Two properties keep it honest, both of them silent when broken:
  the rewrite is applied **only when the same object sets no `permissionDecision`**, so
  an `"allow"` added for symmetry drops it and lands the edit on the home branch; and
  an unrecognised path key is logged as `permission_updated_input_invalid` and the
  **original** arguments are used, which is why the path is written back under the key
  it was read from rather than under `file_path`. `redirect_blocker` is the single
  predicate for falling back to the old block, and every case in it is a way the rewrite
  would fail *quietly* rather than loudly — a hook adapter that would drop the member
  (`DEVKIT_HOOK_ADAPTER`), a tool whose argument shape the guard does not model, or an
  `Edit` whose `old_string` the box's copy of the file does not contain.
- **Two copies of the guard run on every call, and they race for the box.** It is
  registered in the user's `settings.json` and in the project's, and Claude Code fires
  both; each plans a box for the same `(session, project)` and the loser's `git worktree
  add` dies on the branch the winner has just created. Because the two responses are
  merged into one object, the agent was handed a spawn-failure error *beside* an
  `additionalContext` saying the edit had been applied in the box — and nothing had been
  written either way, so believing the context meant building on a change that did not
  exist. `worktree.spawn_lock` brackets plan-and-apply so the second process waits and
  finds the first one's box; `after_failed_spawn` is the fallback for when the wait runs
  out, and it asks whether a box exists rather than matching on git's message, because
  the box is what the decision turns on. Deduplicating the registration would fix the
  race and cost every project without its own copy of the hook, so the hook absorbs it.
- **The guard is the one caller that skips provisioning.** A linked worktree checks out
  tracked files only, so a fresh box has no installed toolchain and nothing else was
  going to create one — `session-start.sh` returns early on a local machine. `worktree.py
  new` therefore installs it, walking the same ladder in the same order. But an install
  takes minutes and a PreToolUse hook that takes minutes is one the agent experiences as
  a hang, so the guard passes `provision=False` and puts the `provision` command in the
  message it hands back instead.
- **The ladder detects the dependency model but cannot detect the interpreter.** No
  marker file names one: a lockfile pins packages, and `requires-python` is a floor that
  a newer release satisfies. So provisioning built every box's `.venv` from
  `sys.executable` — the workstation default — and a project pinned to an older version
  in its `FROM python:` tag, its locks, its type-checker config and CI got a box the
  container does not match, reported as provisioned. `[python] version` in `.devkit.toml` is the seam,
  and `uv venv --python` fetches the version when the machine lacks it, which
  `python -m venv` cannot: it only ever copies the interpreter already running it.

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
- **Among paths it does own, exactly two cases**: the edit is already inside a box
  **this session holds the lease on**, or the checkout is on a managed task branch
  **that carries commits of its own** — the "fix PR #42" case, where something
  deliberately checked that branch out and a fresh box would put the fix somewhere the
  PR never sees. Anything else that would land on a home branch gets a box, because
  landing there with no task branch under it is the agent manufacturing the exact
  `needs-branch` backlog the sweep exists to clear.
- **A box is owned space, not shared space.** The allow for `.worktrees/` paths used to
  be unconditional, and a second session that saw a topically-matching live box in
  `worktree.py list` adopted it wholesale — two sessions' edits interleaved in one
  worktree until one watched files change under it mid-turn. `foreign_box` now blocks an
  edit into a box leased to a different session and routes it to the session's own box,
  with `worktree.py claim <box> --session <id> --yes` named in the message as the
  sanctioned takeover when the user really has handed the work over. The comparison is
  `sessions_match`, which accepts a hand-abbreviated lease id (≥8 characters, prefix in
  either direction) so a `worktree.py new --session <first 8 hex>` box keeps admitting
  the session that cut it. An **unowned** box — an adopted orphan whose lease cannot
  name an owner — stays open to everyone, because blocking there would dead-end every
  box that survived a lost lease file.
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

Every unattended job — `install-reconcile-task.py`, `install-upgrade-schedule.py`,
`install-release-schedule.py`, `install-docker-prune.py`, `install-vanillaland-merge.py`,
`install-global-tools.py` —
goes through
`scripts/devkit_schtasks.py`, which builds a
task document and registers it with `/XML`. That is not a style preference. **The three settings that decide whether a
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
- **The interpreter is part of "which checkout", not part of the environment.**
  `--devkit` exists so an agent in a box can register a job against the static checkout,
  and `install-vanillaland-merge.py` moved every *script* path across while leaving
  `sys.executable` — the box's own `.venv` — as the interpreter. `reconcile` deletes that
  when the PR merges, so the escape hatch registered the exact failure it was the escape
  from, silently and only after the box was reaped. `interpreter(root)` is the fix; when
  the named checkout has no virtualenv, `sys._base_executable` is the only interpreter in
  reach that outlives every box. The test that should have caught it named the *running*
  checkout as the other one, so it asserted nothing until the suite itself ran from a box
  — at which point its two assertions contradicted each other.

The other half of keeping these alive is noticing when one has stopped anyway, which
is `workspace-status.py`'s `scheduler_line` — it reads when a pass last *finished*
rather than whether a task is registered, because `schtasks` answers yes for a task
that is disabled, wedged, or pointed at a checkout that has moved.

### A job with no artifact is a job that fails in private

`pythonw.exe` is what keeps a console window from flashing up on every fire, and its
stdout goes **nowhere** — not to a file, not to the Event Log. So a scheduled job leaves
exactly what it writes itself, and `devkit-docker-prune` was found writing nothing: it
had been exiting 1 for a day, and the entire record on the machine was a `Last Result`
integer. Nobody could say which of `generic_prune`'s two failure exits it took, and the
job was also the only one no installer here had ever registered — it was created by hand
with `schtasks /Create /SC DAILY`, so it had none of the settings above either.

Both halves of that are now enforced rather than remembered. Every installer declares
`TASK_NAME` and `ARTIFACT`, `schedule_health.ARTIFACTS` maps one to the other so the
session-start failure line ends in `see logs/…`, and `tests/test_scheduled_jobs.py`
fails a job that skips either. For a runner with no artifact of its own — `docker-maint.py`
has several callers, most of them interactive — wrap the scheduled call in
`log-wrap.py --always`, which records the passing run too. That flag exists for exactly
this caller: for a task someone clicked, an empty log means "you watched it pass", and
for a job nobody watches it means nothing at all.

Two mechanics that make the wrapper work from a scheduler, both of which fail silently
if forgotten: pass `working_dir` to `task_xml`, because a task's cwd is `system32` and
`log-wrap.py` resolves `logs/` from the cwd; and give the *inner* interpreter — the one
inside the wrapped argv — the **console** spelling, `python.exe`, which is the opposite
of what this paragraph said until 2026-08-20 and the reason the next section exists.

### A job's reach is longer than the script the scheduler names

`pythonw.exe` stops the job opening a console. It does nothing about the console child
that job spawns: a process with no console of its own makes Windows allocate a **brand
new console window** for each console child, so `creationflags=CREATE_NO_WINDOW` has to
be on the spawn as well. That much was known, and `sweep.py`, `worktree.py` and
`worktree-guard.py` were fixed for it — with a check that read exactly those three files.

The flicker came back anyway, from `git` spawned by `sync-devkit.py` on the nightly
upgrade pass, and the shape of the miss is the part worth keeping. **The flag stops at a
process boundary**: `upgrade-project.py` flags its spawn of `sync-devkit.py`, Windows
*ignores* the flag because the interpreter it launches is `pythonw.exe` and the flag
applies only to console-subsystem children, and the fresh console-less process then
spawns `git` per project with nothing set. A check scoped to one job's own scripts could
not have seen it, because the script at fault belongs to no job.

So the rule is about the reachable set, and `tests/test_scheduled_jobs.py` holds both
halves: every module in `UNATTENDED` flags every spawn, and every script an installer
names has to be in `UNATTENDED`. Only the outermost spawn strictly needs the flag — a
window-less console *is* inherited, which is why nothing below `git` needs to know — but
"outermost" is not checkable, so every site carries it.

### The flag is half of it. The interpreter is the other half

Everything above was in place, every spawn carried `NO_WINDOW`, and a console window
still opened every night for about sixteen seconds. The paragraph above even names the
mechanism in passing — *Windows ignores the flag for a GUI-subsystem child* — without
drawing the conclusion, and the wrapper section above it drew the opposite one.

`CREATE_NO_WINDOW` is a **console** flag. Passing it alongside `pythonw.exe` suppresses
nothing, because there is no console to suppress; the child is left console-*less*,
which is the precise condition that makes Windows allocate a fresh visible console for
each of *its* children. The flag protects the hop it is passed to and loses the whole
subtree behind it. Pass it with a console `python.exe` instead and the child gets a real
console that is merely hidden — and **every descendant inherits that**, including the
`ensurepip` that `python -m venv` re-spawns, and every hook `python -m pre_commit` runs.

Measured under `pythonw.exe`, flag set on both spawns:

| Spawn | Result |
| --- | --- |
| `sys.executable -m venv X` | rc 0 in 16.5s, **and a console window** for `ensurepip` |
| `python.exe -m venv Y` | rc 0 in 11.7s, no window |

So the pair is the rule, and neither half works alone:

- **`pythonw.exe` only at the scheduler boundary** — the task's own `<Command>`, and
  nowhere else. That is what `windowless()` is for in each installer.
- **`console_python()` plus `creationflags=NO_WINDOW` for every Python child a job
  spawns**, including the interpreter *inside* a `log-wrap.py --always ... -- <python>`
  argv, which is what `console()` is for in the three wrapped installers.

`sys.executable` is therefore banned outright in `UNATTENDED`, not merely inspected at
spawn sites: `git_policy` builds its argv in one helper and spawns it three functions
away through an injected `runner`, so a site-scoped check reads it as clean. The ban and
its one exemption — the body of `console_python` itself — are
`test_no_scheduled_job_spawns_the_interpreter_that_is_running_it`.

Three further checks in that file exist because each was a way this could come back
unseen: the `creationflags` **value** is now compared against a `NO_WINDOW` spelling
rather than the keyword merely being present; `os.system` and the other spawns that
accept no `creationflags` at all are refused; and the **import closure** of `UNATTENDED`
is walked, so a helper module that gains a spawn joins the check by being imported
rather than by being remembered. `IMPORTED_NOT_ENTERED` is the one exemption to that
last, and it is mechanically narrow: the module's spawns must all be inside its own
`main()`, which nothing imports its way into.

Its cost is the second half, and it is the one that trades a visible bug for an invisible
one: **the flag binds a child that captures nothing to the console it was just given**,
so such a child's output stops reaching the handles it inherited. Every spawn in the
reconcile path captures, which is why this never bit there. `docker-maint.py` streams,
and flagging it alone would have emptied `logs/scheduled-docker-prune.log` of everything
docker said — an artifact reporting an exit code and nothing to diagnose it with, which
is the failure that made artifacts mandatory two sections up. Capture, or name the
streams (`docker-maint.inherited_streams`); never just add the flag.

### A file named `pythonw.exe` need not be an interpreter

Both sections above were in place — the pair was the rule, every spawn carried the flag,
and the checks were in `tests/test_scheduled_jobs.py` — when a console window came back
at boot on 2026-08-21. **Every guard for this bug was a source scan of devkit's own
files, and every one of them was checking a name.**

Inside a virtualenv, `Scripts\pythonw.exe` is not an interpreter. It is a stub deferring
to the base install named in `pyvenv.cfg`, and the two builders differ in the one way
that matters here: CPython's stub loads the base **in-process**, while uv's is a
trampoline that **spawns it as a child**. So `devkit-global-tools` — the only job whose
interpreter comes from a `.venv`, because `install-global-tools.interpreter` prefers the
checkout's — was registered against a GUI-subsystem file correctly named `pythonw.exe`,
opened no console of its own, and handed its console child a brand new visible one. That
is the same mechanism as the section above, arriving through the *scheduler boundary*
rather than through a spawn site.

Two consequences, both now enforced rather than written down:

- **`devkit_schtasks.windowless` owns the resolution, and no installer keeps a copy.**
  Six of them did, identically, on the stated reasoning that six lines are cheaper to
  repeat than to couple — and all six were wrong at once for as long as it took one
  job's interpreter to come from a venv. It resolves through `home`, which also settles
  the hazard `interpreter` warned about from the other end: a box's `.venv` disappears
  when its PR merges, and the base install outlives every venv.
- **A source scan cannot close this class of bug, so one check reads the machine.**
  `schedule_health.virtualenv_interpreter` compares each registered task's `Task To Run`
  against `pyvenv.cfg` and reports it at session start. All three rounds of this bug were
  found by a human watching windows flash; that is the loop this replaces.

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
