# scripts/

What a change to a script here has to preserve. The root [`CLAUDE.md`](../CLAUDE.md)
carries the repo-wide decisions;
[`.claude/rules/engineering.md`](../.claude/rules/engineering.md) carries the script
conventions every project shares.

## The two channels

| | Vendored tier | Pre-commit channel |
| --- | --- | --- |
| Delivered by | `sync-devkit.py --pull` copies files in | pre-commit clones devkit at a pinned `rev` |
| Lives in | `scripts/hooks/`, listed in `MANIFEST` | `scripts/precommit/`, listed in `.pre-commit-hooks.yaml` |
| Versioned by | `DEVKIT_VERSION` + a CI drift job | the `rev` in the consumer's config |
| Use it when | the code must run with no network and no install | the check runs at commit time and a pinned version is better than a copy |

Pre-commit specifics: **`language: script`, stdlib only, executable bit set** — there is
nothing to install from a virtual project, so pre-commit execs the file directly. The
hooks run with the *consumer's* repo as the cwd while the scripts live in pre-commit's
clone, so resolve devkit files through `Path(__file__)` and read layout from
`.devkit.toml`, never from the cwd. devkit wires its own as `repo: local` rather than by
rev, or a hook fix could never be validated by the hook it fixes. A new hook needs an id
in both `.pre-commit-hooks.yaml` and `.pre-commit-config.yaml`; a test asserts the sets
match, with `devkit-drift` as the one documented exception.

## Vendoring rules

- `MANIFEST` in `scripts/sync-devkit.py` is the shared set, and every entry ships with its
  test. Vendored files are compared **byte-for-byte**, so formatting counts — CI runs
  `ruff format --check .` because an unformatted MANIFEST file gets reformatted downstream
  on first edit, and the consumer's `--check` then reports drift it did not cause.
- **Never vendored**, because each project's copy differs: `.devkit.toml`,
  `.claude/settings.json`, `scripts/lint-all.py`, `scripts/run-tests.py`. They live in
  `templates/`.
- **Never hard-code project specifics in a hook script.** A new behaviour gets a manifest
  field and a neutral default in `harness_config.py`, not an `if project ==` branch.
- **`templates/` is a one-shot copy.** `--pull` never looks at a template again, so every
  fix made here after a project was generated stays here, and nothing can report the gap.
  When a file stops having a per-project value, move it into `MANIFEST` rather than
  leaving it rendered.
- **Vendoring a generator does not vendor its output.** `.codex/hooks.json` is written by
  `sync-codex-hooks.py` from the project's own `.claude/settings.json`, so the script is
  in `MANIFEST` and the file it produces cannot be. **Anything generated from a vendored
  script needs a check that regenerates it and compares**, running in the consumer's own
  gate where no `$DEVKIT_DIR` exists — `sync_devkit.codex_hooks_stale` is that check.
  Regenerating on `--pull` is not enough alone: a project only pulls when asked to.

## `templates/` is content, not source

`.tmpl` files are not valid Python until rendered, and the plain `.py` files under
`templates/` are linted by the `ruff.toml` that ships *alongside* them into each generated
project — which carries `scripts/**` allowances devkit's own config does not apply at
those paths. So `templates/` is excluded from ruff (`force-exclude = true`, so the
exclusion holds for the explicitly-named paths that `lint-fix.py` and `lint-all.py
--changed` pass), from mypy, and from `lint-all.py`'s `--changed` scope.
`scripts/notify.py` and `scripts/notify-wrap.py` are **byte-identical copies** of the
files under `templates/core/scripts/`, and a test enforces that.

## Loading a module by path

**Register the module in `sys.modules` before calling `exec_module`.** `@dataclass`
resolves its string annotations by looking the defining module up by name, so exec-first
dies inside `dataclasses` with `AttributeError: 'NoneType' object has no attribute
'__dict__'` — a traceback that points at CPython internals and not at your loader.
`harness_config.py` is nothing but frozen dataclasses, so anything that loads it by path
hits this immediately. Use `scripts/precommit/_loader.py`'s `load_by_path` rather than
writing a fourth one.

## A path a vendored script hard-codes is a promise

`stop.py` resolves its dispatch targets by path, spawns them with both streams on
`DEVNULL`, and never reads the exit code, so a target that is not there is the quietest
failure in the harness — state finalization simply stops happening, in every consumer,
with nothing red anywhere. Either the file is in the `MANIFEST`, or the dispatcher treats
its absence as a documented skip; `tests/test_dispatch_coherence.py` enforces the choice
and requires a written reason for each exception.

## Ephemeral boxes

One static checkout per repo, for a human browsing the stack, plus as many ephemeral boxes
under `<workspace>/.worktrees/` as there are agent tasks in flight. `README.md` covers the
commands; [`worktree-guard.md`](worktree-guard.md) covers what the guard judges, what it
declines, and what `agent-box.py` couples to when `harness-switch.py` has stood the tier
down.

The static tier's problem is that a checkout **outlives its task** — `needs-branch`,
`spent-branch`, `home_ref` and the rest of `sweep.py`'s workload are every one of them a
state a checkout can only reach by surviving the work done in it. A box cut fresh off
`origin/<default>` and destroyed at the end reaches none of them. What a change to
`worktree.py` must preserve, each having been violated once already:

- **`HOLD` is tested before anything that destroys.** `reconcile` reaps a merged PR's box
  and, under disk pressure, one whose PR is merely *open*. Work that exists only in a box
  has to survive a merged PR, disk pressure and any age; the ordering is the whole safety
  property, and four tests fail if it moves.
- **A `HOLD` with nobody in the box is stranded, not holding work.** `stranded` is the
  predicate; the remedy is the `/triage-boxes` skill and `rescue <box> --ship`, which
  rewrites the lease to a fresh branch — which is why nothing may `git checkout -b` in a
  box by hand.
- **"Has the work left the box" is not a question the verdict can answer alone.** A squash
  rewrites the shas `--cherry-pick` compares by, so `reapable` also consults the merged PR
  (`MERGE_CAN_BE_STALE_ABOUT`), the box's `HEAD^{tree}` against recent
  `origin/<default>` commits (`head_tree_landed`), and the merged PR's `headRefOid`. Every
  unknown reads as *not landed*, and all of it is refused while the box is dirty: a landed
  tree says where the committed work is and nothing about the edits on top of it.
- **`reap` is the one place in the workspace that passes `-v` to `compose down`,** scoped
  with `-p <box>` so it cannot widen to the source project. `docker-maint.py` must never
  do it — its target is a static checkout whose volumes hold a dev database costing hours
  to re-ingest.
- **The teardown has a host half.** A vite server's mapped `.node` binding makes Windows
  refuse the delete, leaving a **husk** — a directory with no `.git` — that no later pass
  can clear. `box_teardown.py` evicts a process whose **executable or a loaded module**
  lives under the box, never one whose command line merely names it: an agent's own shell
  names it too, and killing the session that asked for the reap is worse than the husk.
- **A box is never registered in the workspace file.** That would put it in `sweep.py`'s
  scope and give its lifecycle two owners; `workspace-status.py` reports boxes at session
  start instead.
- **The guard is the one caller that skips provisioning.** A worktree checks out tracked
  files only, and a PreToolUse hook that takes minutes is one the agent experiences as a
  hang — so the guard passes `provision=False` and puts the command in its message
  instead. The ladder detects the dependency model but cannot detect the interpreter, so
  `[python] version` in `.devkit.toml` is the seam; `uv venv --python` fetches a version
  the machine lacks, which `python -m venv` cannot.

## A scheduled task is registered from XML, never from `schtasks` flags

Every unattended job goes through `scripts/devkit_schtasks.py`, which builds a task
document and registers it with `/XML`. **The three settings that decide whether a
scheduled job on a laptop runs at all have no `schtasks.exe` flags**, so a task created
with them silently skips every fire on battery, dies when you unplug, and never catches up
a fire it slept through. None of that reports anything: a job that does not run writes no
log. Three things a change must keep:

- **`<Settings>` is a schema sequence, not a set.** Reordering it is rejected at
  registration time, on the installing machine, where no unit test can reach it.
- **A repetition carries no `<Duration>`.** Absent means indefinitely; a plausible-looking
  `P1D` turns the job off after a day.
- **The interpreter is part of "which checkout".** Use `interpreter(root)`, not
  `sys.executable` — a box's `.venv` is deleted when its PR merges, so an installer run
  from a box would register the exact failure it was the escape from.

Every installer declares `TASK_NAME` and `ARTIFACT`, because `pythonw.exe` sends stdout
nowhere and a job leaves exactly what it writes itself;
[`tests/test_scheduled_jobs.py`](../tests/test_scheduled_jobs.py) fails one that skips
either. For a runner with no artifact of its own, wrap the scheduled call in
`log-wrap.py --always`. [`windowless-jobs.md`](windowless-jobs.md) covers keeping a job
window-less — `CREATE_NO_WINDOW` on every spawn in the job's reachable set, paired with a
**console** `python.exe` as the inner interpreter — and its rules are enforced by that
same test.

## The events ledger: append-only, and that is why resolution is an event

`harness_events.py` is the write half and `harness_triage.py` the read half.

**"Open" is a state, never an age** — an event with no `triage-resolved` naming it, at any
age. Counting a recent window meant a defect fixed within the hour kept being reported for
a week while one nobody looked at vanished silently; both failure directions at once.
**`project=` names the repo, not the directory the writer ran in**, applied on read as
well as on write, because from a box those differ and the log is append-only. **`agent=`
is part of `Item.signature`** so the same hook failing under two runtimes is two items —
a hook that misbehaves under one routinely behaves under the other — while **`host=`
deliberately is not**, since one defect hit on two machines is one defect. The
`/triage-harness` skill works the backlog, and it is devkit-only on purpose: every defect
on this ledger is a defect in devkit, whatever project the session that hit it was scoped
to.
