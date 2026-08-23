# devkit

A portable agent-coding harness for **Claude Code / Codex**: the project-agnostic
hook scripts (auto-lint-on-edit, capped Bash, pre-stop PR-gate verification), the
session lifecycle, and the Codex skill/hook compatibility tooling — **vendored into
each project** and configured per-project through `.devkit.toml`.

One source of truth, tested in isolation, pulled into every repo. No submodule: each
project commits its own copy, so cloning a single project still gets everything.

> **Renamed from `agent-harness` on 2026-07-25.** The repo is being widened into a
> five-channel upstream. Two exist today — the **vendored tier** (everything described
> below) and the **[pre-commit hooks](#pre-commit-hooks-a-second-channel)**. The agent
> plugin, pip package, and reusable CI workflows are still planned.
>
> The **internal** names were migrated to match on 2026-07-30: `.devkit.toml`,
> `$DEVKIT_DIR`, `DEVKIT_VERSION`, `scripts/sync-devkit.py`, and the published hook ids
> `devkit-manifest` / `devkit-hooks-stdlib-only` / `devkit-drift`. It had to be one
> atomic change across devkit and every consumer, because `sync-devkit.py` is itself in
> the `MANIFEST` and the drift check compares by path. Any surviving `agent-harness`
> spelling is a miss, not a holdout.

## How it works

- **This repo is the source of truth.** Each consuming project commits a *vendored
  copy* of the files in [`scripts/sync-devkit.py`](scripts/sync-devkit.py)'s
  `MANIFEST`.
- **Everything project-specific lives in `.devkit.toml`** at the consuming
  repo's root, read by `scripts/hooks/harness_config.py` (stdlib `tomllib`; a
  missing/bad manifest falls back to neutral defaults). The scripts stay
  shape-agnostic — a new project drops in a manifest instead of forking the code.
- The **canonical example** manifest is
  [`templates/core/dot-devkit.toml.tmpl`](templates/core/dot-devkit.toml.tmpl),
  which is what a new project is rendered with. The `.devkit.toml` in *this*
  repo used to serve that role by holding a copy of carameli's; it now describes
  **devkit**, because devkit runs these hooks on itself and a hook reading another
  project's shape acts on directories that are not here.

## devkit runs its own harness

Everything devkit ships is wired up here, on itself — `.claude/settings.json` fires
the same hook set the generator emits, against devkit's own scripts.

| Utility | Wired by |
| --- | --- |
| SessionStart provisioning | `.claude/hooks/session-start.sh` (uv-native: `pyproject.toml` + `uv.lock`) |
| Task naming | `scripts/task_slug.py` records the prompt's slug; `scripts/worktree-guard.py` names the box it cuts after it |
| Work isolation | `scripts/worktree-guard.py` routes an edit that would land on a home branch into an ephemeral box |
| Auto-lint on edit | `scripts/hooks/lint-fix.py` |
| Pre-stop verification | `scripts/hooks/stop.py` → `scripts/lint-all.py`, `scripts/run-tests.py`, both test trees |
| Failure artifacts | `logs/lint-errors.log`, `logs/test-failures.log`, `logs/stop-verify.log` |
| Scheduled-failure reporting | `.github/workflows/scheduled-failure-issue.yml` → `scripts/report-workflow-failure.py` opens one assigned issue when `Nightly` fails, and closes it when it passes |
| VS Code tasks | the multi-root workspace file — devkit owns no `.vscode/tasks.json`, which is the rule it prescribes |

Not decoration — a hook that only runs downstream is a hook nobody tests. Wiring
these up surfaced four bugs that had shipped to every consumer: the Stop hook passed
`--no-secrets` to a lint runner that rejected it (argparse exit 2, so Tier 1 failed on
*every* stop in *every* generated project), it invoked a `check-lock-markers.py` no
generated project has, it treated pytest's "no tests collected" as a failure, and with
`[db] enabled = false` it never ran the project's own test suite at all.
`tests/test_self_hosting.py` is what keeps devkit from drifting back into shipping a
utility it does not use.

## Consuming it in a project

```bash
# One-time bootstrap: grab the sync tool, then pull everything it lists.
# NB: raw.githubusercontent.com does NOT follow the rename redirect — this URL
# must say devkit, even though the file it fetches is still sync-devkit.py.
curl -sSfL https://raw.githubusercontent.com/alexandrec90/devkit/main/scripts/sync-devkit.py \
  -o scripts/sync-devkit.py
DEVKIT_DIR=/path/to/devkit python scripts/sync-devkit.py --pull

# Add a .devkit.toml (see this repo's as the template), then commit.
```

- `--check` (default): fail on drift — wire into CI. With no `$DEVKIT_DIR`/`--src` it
  **no-ops until the project has pulled** (CI is green before adoption) and **fails
  once `DEVKIT_VERSION` is stamped**, because there is then vendored code it did not
  compare.
- `--pull`: adopt this repo's version (stamps `DEVKIT_VERSION` with the commit).
- `--push`: copy a project's version back here (author a change / seed a fresh repo).
- `--list`: print the manifest + the project's vendored version.

## Pre-commit hooks: a second channel

devkit publishes pre-commit hooks in
[`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml). Unlike the vendored tier there is
nothing to copy in — a consumer pins a rev, and pre-commit clones it:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/alexandrec90/devkit
    rev: v0.5.0 # a tag, never a branch — see below
    hooks:
      - id: devkit-manifest
      - id: devkit-hooks-stdlib-only
      - id: devkit-drift
```

`scripts/new-project.py` renders this into every new project already, pinned to the same
devkit ref as the PR gate.

| Hook | Catches |
| --- | --- |
| `devkit-manifest` | A `.devkit.toml` the harness would silently ignore: unparseable TOML, a path prefix missing its trailing slash, a declared directory that does not exist in the repo, a `[db]`/`[frontend]` block switched on and left half-filled. |
| `devkit-hooks-stdlib-only` | A third-party import in `scripts/hooks/`. Those scripts run *before* the virtualenv exists, so this cannot be caught by a test suite — which runs inside it. |
| `devkit-drift` | A vendored file that differs from the pinned devkit rev. |

**Why `devkit-drift` exists next to `sync-devkit.py --check`.** The sync tool needs a
**local devkit clone** for `$DEVKIT_DIR` to point at. Where there is none it can only
refuse — which is at least loud now, rather than the exit 0 it used to report — and a
refusal is still not a check. A second workstation, a fresh clone and a CI job whose
`env:` block was dropped are all that shape. Run through pre-commit there is nothing to
configure and nothing to clone by hand: pre-commit has already fetched devkit at the
pinned rev, so the version being compared against is written down in the consumer's
config and moved by `pre-commit autoupdate`.

Two consequences worth knowing:

- **The hooks are `language: script`, not `language: python`.** devkit is a virtual
  project with nothing to install, and these scripts are stdlib-only, so the clone is
  already everything they need. That also means the executable bit matters — a test
  enforces it, because a missing one fails only on a consumer's machine, at commit time,
  after the rev is tagged.
- **A rev that predates the channel fails hard.** pre-commit resolves hook ids strictly:
  against an older tag the consumer's first commit aborts with "hook not found" rather
  than skipping. `new-project.py` checks the ref it is about to pin and warns when it
  cannot serve the hooks.

devkit runs these on itself via [`.pre-commit-config.yaml`](.pre-commit-config.yaml),
wired as `repo: local` — pinning a rev there would validate a released tag's hooks against
the working tree trying to change them, so a hook fix could never be tested by the hook it
fixes. `.claude/hooks/session-start.sh` runs `pre-commit install` when a config is
present, unless the global dispatcher below is installed and already owns that job.
Either way, a fresh clone or sandbox gets the gate without anyone remembering to.

## Global branch-lifecycle policy

GitHub Free cannot enforce protected branches in private repositories, so Devkit can
install a local policy dispatcher for every Git repository on this machine:

```bash
# Read-only plan first.
python scripts/install-git-policy.py

# Install the runtime to ~/.devkit/git-hooks and configure Git globally.
python scripts/install-git-policy.py --yes
```

The installer preserves an unrelated existing `core.hooksPath` and refuses rather
than overwriting it. It also enables `fetch.prune` and makes GitHub lookup failures
fail closed.

### The runtime is installed from a tag, never the working tree

The installed copy is what **every repository on this machine** enforces, so it is
taken from devkit's newest release tag (`latest_devkit_tag() or
FALLBACK_DEVKIT_REF`, the same pin generated projects use) rather than from
whatever happens to be in the checkout.

That is not a stylistic preference. A runtime once installed from a
work-in-progress file about eighteen hours before that change was committed, so
`DEVKIT_SKIP_BRANCH_POLICY` did not exist in the executing code while both the
source and this README described it. The only symptom was an environment variable
that appeared to do nothing.

```bash
python scripts/install-git-policy.py --ref v0.5.3 --yes   # a specific release
python scripts/install-git-policy.py --from-worktree --yes  # uncommitted; asks for it
```

`--from-worktree` is the deliberate escape hatch for developing the policy itself.
It prints a warning in the plan and records `worktree` as the provenance, so a
receipt never claims a commit it did not come from.

### Knowing what is installed

Each install writes `~/.devkit/git-hooks/installed.json` — the ref it came from,
when, and a SHA-256 per file. Without it, "which policy is actually running?" can
only be answered by diffing against a checkout, which is a question about *this*
machine that nothing on this machine could answer.

```bash
python scripts/install-git-policy.py --check
```

Exits 0 when current, 1 when a file was modified after install or a newer release
exists, and 2 where nothing is installed — a fresh clone, CI, or anyone else's
machine, none of which should read as a failure.

`workspace-status.py` runs the same comparison at session start, so this is
normally noticed without anyone asking. It answers two separate questions, because
they deserve different reactions: **modified** means the installed bytes no longer
match the receipt and should never happen, while **behind** means a newer release
shipped and just wants a re-run.

Neither compares against the working tree, so editing `scripts/git_policy.py` stays
silent — a check that fires continuously while the policy is being worked on is one
nobody reads. It is also deliberately *not* a test: a test asserting
"installed == source" could only be made green by installing work-in-progress code
globally, which is precisely the mistake described above.

The global `pre-commit` hook:

- rejects detached-HEAD commits and commits on `main`, `master`, or the detected
  remote default branch;
- asks GitHub whether the current branch name has ever had a merged PR, permanently
  retiring that name when it has;
- runs the repository's `.pre-commit-config.yaml`, then an optional
  `.githooks/pre-commit`.

The global `pre-push` hook inspects the destination refs rather than just the current
branch, so `git push origin HEAD:main` is blocked too. It rejects protected
destinations and any branch name with an already-merged PR, while still allowing
remote branch deletion. Non-GitHub remotes skip only the PR lookup; protected branch
destinations remain blocked.

It also refuses a push that creates or moves a **release tag** — a `vX.Y.Z` ref, the one
thing consumers pin — because only a release workflow runs the suite against the commit
*as tagged* before publishing it. Tag deletion stays allowed, since that is the recovery
path for one already published, and any other tag shape is ignored. `RELEASING.md` has
the release this was written for.

GitHub verification fails closed by default. Temporarily degrade it to a warning when
offline with:

```bash
git config --global devkit.branchPolicy.failClosed false
```

A repository with no remote is exempt: there is no PR to route a commit through, so
the policy would refuse the only commit possible rather than redirect it. That is what
lets `new-project.py` seed its initial commit and lets test fixtures build scratch
repos, both of which land on the default branch by design.

For scripted setup that *does* have a remote, `DEVKIT_SKIP_BRANCH_POLICY=1` waives the
branch checks for a single command:

```bash
DEVKIT_SKIP_BRANCH_POLICY=1 git commit -m "seed"
```

Unlike `--no-verify`, this skips only Devkit's branch policy — the repository's own
`.pre-commit-config.yaml` and `.githooks/` still run. Values that read as "off"
(`0`, `false`, `no`, `off`) leave the policy enforcing, so setting the variable to `0`
does not disable it. Every run under the opt-out prints a warning and records it in
`.git/devkit-branch-policy.json`, so a variable exported into a shell profile is
visible on each commit instead of silently retiring the gate.

Project-specific hooks can live at `.githooks/pre-commit` and
`.githooks/pre-push`; Devkit runs them only after its policy passes. Do not set a
repository-local `core.hooksPath`, because local Git configuration overrides the
global dispatcher. Like every client-side hook, this remains bypassable with
`--no-verify`; hard server-side enforcement still requires GitHub's paid private-repo
branch protection.

### Persistent Docker-backed worktrees

A linked worktree can park at the same commit as `origin/main` without checking out
the `main` branch:

```bash
git fetch --prune origin
git switch --detach origin/main
```

The one-worktree restriction applies to a checked-out **local branch**, not to a
detached commit. The directory and its Docker wiring stay intact. Start the next task
in that same directory with:

```bash
git switch --no-track -c agent/new-task origin/main
```

The global pre-commit hook intentionally rejects commits while the slot is parked,
making branch creation mandatory before new work is committed.

**This is the human flow, and an agent editing here now gets a box instead.** A branch
cut this way carries no commits yet, and `worktree-guard.py` reads exactly that — a
managed task branch with nothing on it protects no PR, so the edit is routed rather
than allowed to land. Once the branch has a commit of its own the guard declines again,
which is what keeps "check out PR #42 and fix it" working. If you want an agent to keep
working *in* this checkout on a fresh branch, commit something first; if you do not
care which directory it happens in, that is what the ephemeral tier below is for.

### Ephemeral worktrees (boxes)

The section above is the *static* tier: a checkout that outlives the task, parked and
reused. `scripts/worktree.py` is the other one — a worktree cut per task and destroyed
when the work leaves it:

```bash
python scripts/worktree.py new carameli --slug voicemail --yes  # cut, lease, install
python scripts/worktree.py list                                 # what exists, and its verdict
python scripts/worktree.py reap --all --yes                     # everything already shipped
python scripts/worktree.py claim <box> --session <id> --yes     # hand a box to another session
python scripts/worktree.py resume carameli --pr 163 --yes       # a box back on a branch it lost
```

`new` branches off `origin/<default>`, leases a port slot from `ports.toml` (released on
reap, so it does not need a pinned entry), seeds the box's own
`COMPOSE_PROJECT_NAME`, and installs the project's toolchain — a fresh worktree has
tracked files only, so it starts with no `.venv`. `reap` **refuses while the box still
holds unshipped work**, which is the difference that matters: the static tier's
stranded work is found afterwards by `sweep.py`, and a box's cannot be stranded at all,
because being stranded is what stops the cleanup.

`resume` is the way back in. `reconcile` destroys a box whose PR is still *open* when
the disk is tight, on the grounds that the remote has every commit — a trade that is
only honest if the checkout can be got back, and `new` cannot do it: it mints a branch,
so continuing the task through it would open a second PR for the same work. `resume`
takes the branch instead (`--branch`, `--pr N`, or, with `--session`, the one the reap
ledger records for that session), checks it out with `origin/<branch>` as its upstream
so a bare push still lands where the PR is watching, and refuses a branch origin no
longer has. `worktree-guard.py` asks for it before it cuts anything, so a session whose
box was reaped mid-task lands back on its own branch without knowing it had left.

A port lease is not the whole story, because a setting *derived* from a port is not a
port. Seeding copies the source checkout's `.env` verbatim, so a value naming the
primary's frontend goes on naming it in every box — which nothing notices until a
browser does. carameli's `CORS_ORIGINS` named `http://localhost:5173`, the box served
on its own port, and its app then refused every request its own frontend made, as a
CORS error that reads like an application bug rather than like a half-configured box.
A project declares those values in its own `.devkit.toml`, as templates over the env
devkit already writes:

```toml
[worktree.env]
CORS_ORIGINS = "http://localhost:${FRONTEND_HOST_PORT}"
```

`${...}` resolves against `COMPOSE_PROJECT_NAME` and one `<SERVICE>_HOST_PORT` per
service in `ports.toml`. A template naming anything else is **dropped rather than
written half-expanded**: compose's dotenv parser does no substitution of its own, so a
surviving `${...}` would reach the application as those literal characters, and leaving
the seeded line in force is at least a value somebody chose.

Boxes live in `<workspace>/.worktrees/` and are deliberately absent from the multi-root
workspace file — registering one would hand `sweep.py` a second owner for its lifecycle.
`scripts/worktree-guard.py`, wired as a PreToolUse hook at the workspace root, is what
puts work in one automatically: an agent editing a checkout its session is not inside
gets a box spawned and the path handed back, instead of a commit on that repo's home
branch. The same hook holds the boundary between boxes: each one is leased to the
session it was cut for, an edit aimed into another session's box is blocked toward the
editor's own, and `claim` is the deliberate handover for when the user moves a task
between sessions.

### Running someone else's branch before it merges

`preview` is the reviewer's half of the same tier. Testing an agent's change used to mean
merging the PR first and pulling the result into a checkout — the change had to land
before anyone could see whether it should:

```bash
python scripts/worktree.py preview carameli --pr 163 --yes   # a PR, wherever it was authored
python scripts/worktree.py preview carameli --branch agent/ui-editor-0817 --yes
python scripts/worktree.py preview carameli--ui-editor-0817 --yes   # a live box, served as-is
```

It cuts a box like `new` does — leased slot, own `COMPOSE_PROJECT_NAME`, seeded `.env` —
then brings the stack up and prints the URLs that slot publishes, so the frontend of two
different branches can run side by side without either one noticing the other.

What it does **not** do is check out the branch it is showing. A preview gets its own
`preview/<slug>` copy of `origin/<ref>`, for two reasons that are the whole design: a
local branch cannot be checked out in two worktrees, so the agent's own box would refuse
to be previewed at all; and if that box had already been reaped, the reviewer would be
sitting on the real branch with `origin/...` as its upstream, one reflexive `git push`
from writing to work that is not theirs. `--force` refreshes a preview onto the ref's
current tip; there is no spelling of it that resets a task box.

`reconcile` treats previews as their own kind: reaped once the work they show has merged
or closed, reclaimed under slot pressure or at `--max-age-days`, and held — like any box
— the moment there are uncommitted edits inside. Reviewing by editing is allowed; the
box just stops being disposable when you do.

#### The menu in front of it

Each of those commands needs the ref and the flag that takes it, which is a lot to know
before you can look at something. `scripts/preview-task.py` asks instead: it enumerates
every standing preview, live agent box, open PR and recent `agent/…` branch on the
machine, prints them newest-first as one numbered menu, and previews the row that gets
picked.

```bash
python scripts/preview-task.py            # menu, then bring the pick up and open it
python scripts/preview-task.py --list     # print the menu and exit (agents: --json)
python scripts/preview-task.py --pick 3   # take row 3 without asking
python scripts/preview-task.py --all      # re-serve every standing preview
```

`--all` is the post-reboot case: Docker stops every container on a restart while the
boxes, their branches and their port leases all survive, so each preview lands back on
the port it had before. Two VS Code tasks — *Preview: Open a UI Branch* and *Preview:
Restart Standing Previews* — are those two invocations, one click each.

A checkout with no compose stack contributes no rows, which keeps devkit out of a menu it
would publish nothing for.

### The scheduled pass

Both tiers need something to run afterwards, and "afterwards" is exactly when nobody
is looking. `worktree.py reconcile` is that pass, and
`scripts/install-reconcile-task.py` puts it on the Windows scheduler — the only
runner here that outlives a session, a reboot and a closed editor:

```bash
python scripts/install-reconcile-task.py --yes      # every 15 minutes
python scripts/install-reconcile-task.py --status   # what is installed, and whether it runs
python scripts/install-reconcile-task.py --uninstall --yes
```

The task is registered from an XML document rather than `schtasks` flags, because the
settings that decide whether it runs on a laptop — battery, and catching up a fire it
slept through — have no flags. `scripts/devkit_schtasks.py` owns that document and its
sibling `install-upgrade-schedule.py` uses the same one.

One pass, both tiers. It reaps every box whose PR has settled — merged, or closed
without merging, which ends the wait just as finally — and reclaims disk when the
volume is low; then it runs `sweep.py --sync` over the **static** checkouts, so a
merged PR advances each one's default branch instead of leaving it parked on a spent
task branch that the next session then opens on. Merging stays a human decision unless
the task was installed with `--merge`, which squash-merges a green box PR only when it
carries the `automerge` label — applying the label is the review decision, and
`upgrade-project.py` labels its PRs at creation (`--merge-label ""` at install time
drops the gate and merges anything green). The checkout half destroys nothing: it
refuses any checkout holding uncommitted work, unpushed commits or an open PR, names
it, and moves on.

The run is windowless, so its only record is `logs/reconcile.log`, overwritten per
pass and written on success too — a log that appears only on failure cannot be told
apart from a task that has stopped running. That log's timestamp is also what
`workspace-status.py` reads at session start: a scheduled task that has been disabled
looks exactly like one that is working, so the status line says when a pass last
finished rather than leaving you to notice the drift it causes.

Overwritten per pass is right for a health check and wrong for a destruction, so those
go somewhere else: every box that is destroyed, by any path, appends one line to
`logs/worktree-reaped.log` — box, branch, verdict, whether it was forced, and how many
uncommitted files it held at the time. Nothing else outlives a box: the lease registry
is the live set and the reap deletes the entry, `reconcile.log` is gone in fifteen
minutes, and `reap` used to write nothing at all — so a box that vanished could not be
attributed to a mechanism even with every log on the machine in hand. This file is
append-only and never rotated; it costs a line per box.

### Every scheduled job, and where each one reports

One installer apiece, and the same contract on all of them: registered from XML so a
laptop actually runs them, and leaving a file to read when one fails.

| Job | Installer | Cadence | Its record |
| --- | --- | --- | --- |
| `devkit-worktree-reconcile` | `scripts/install-reconcile-task.py` | every 15 min | `logs/reconcile.log` |
| `devkit-upgrade-projects` | `scripts/install-upgrade-schedule.py` | daily 03:00 | `logs/upgrade.log` |
| `devkit-docker-stop-idle` | `scripts/install-docker-stop-idle.py` | daily 03:30 | `logs/scheduled-docker-stop-idle.log` |
| `devkit-docker-prune` | `scripts/install-docker-prune.py` | daily 04:00 | `logs/scheduled-docker-prune.log` |
| `devkit-global-tools` | `scripts/install-global-tools.py` | daily 04:30 | `logs/global-tools.log` |
| `devkit-vanillaland-merge` | `scripts/install-vanillaland-merge.py` | daily 05:00 | `logs/scheduled-vanillaland-merge-develop.log` |

`scripts/schedule_health.py` answers the question no artifact can — *did it run at all*
— and names the file above when one exits non-zero, so the session-start line is a
pointer rather than a bare exit code. `tests/test_scheduled_jobs.py` holds the contract:
a job registered by hand, or one that leaves nothing behind, fails the suite.

The global-tools pass is the machine-wide counterpart to the project upgrade. Four of
this workspace's MCP servers — chrome-devtools, postgres, redis, azure-devops — are
launched from a **globally installed** npm bin rather than through `npx`, so the global
install is the pin and nothing was moving it; the same is true of every linter reachable
without a project venv. `scripts/global-tools.py` reads the outdated set from npm itself
rather than from a list that would go stale, updates each package to the exact version it
reported, and skips `npm` and `@anthropic-ai/claude-code` — the two things that would have
to still work in order to undo a bad update.

It is the one devkit artifact that keeps a **history** rather than overwriting per run,
because an unpinned auto-update is noticed days after the bump that caused it: each pass
records `npm install -g <name>@<old>` for everything it moved. A registry it cannot reach
is recorded and exits 0 — a laptop offline at 04:30 is the system working, and a job whose
alerts fire on the normal case is a job whose alerts nobody reads.

```bash
python scripts/install-global-tools.py            # what it would register
python scripts/install-global-tools.py --yes      # daily 04:30
python scripts/global-tools.py                    # what is behind, installing nothing
```

The prune runs `--idle-only`, so it declines whenever containers are up; reclaiming the
VHDX needs `wsl --shutdown`, and stopping a running stack at 04:00 for disk is not a
trade to make unattended.

The stop-idle pass is the other half of that trade. `restart: unless-stopped`
resurrects on every boot whatever was left running, so a stack someone brought up once
runs around the clock; nightly, any stack that has **opted in** (`[docker]
auto_stop = true` in the project's own `.devkit.toml`) and shows no established
connection to a published port -- with a grace window for anything recently started --
is stopped. `docker stop`, never `down`: containers and named volumes survive, and a
stopped stack stays stopped across reboots until something wants it again
(`docker-maint.py up`, or the stop hook's `*_STOP_TESTS_AUTOSTART` tier). Opt-in is
the safety property: a collector-style stack doing scheduled work with no client
connected looks exactly like an idle one, so it is safe by default rather than by
being remembered.

The VanillaLand merge is the odd one out and the only job that touches a working tree a
human is going to open: it runs `git-merge-default.py` against the reference checkout, so
`develop` arrives daily instead of as one unreviewable merge weeks later. It commits
locally and never pushes. A conflict is left **in progress** with every unmerged file
named in the log, and uncommitted work it had to set aside stays in a named stash — the
installer's docstring is the place that explains why, and the log repeats the recovery.

## Authoring changes

The harness repo is the source of truth. Edit here, open a PR, let CI test it, merge.
Projects then `--pull`. Only `--push` from the one project actively authoring a change.

## Creating a new project

`scripts/new-project.py` renders a whole project from `templates/` — the harness
seam, a Docker stack on registry-allocated ports, a parallel worktree, VS Code
tasks, and a PR gate whose drift check actually gates — instead of copying whichever
existing repo was nearest.

```bash
# Dry run is the DEFAULT: prints every file and command, writes nothing.
python scripts/new-project.py sports_betting --preset data --description "..."

# Apply. --no-remote stops before the GitHub repo is created.
python scripts/new-project.py sports_betting --preset data --yes
```

There is also a VS Code task, **"Project: New from devkit"**, in the shared workspace
block (`workspace.jsonc` here, the multi-root workspace file live). A user-level
copy in `%APPDATA%/Code/User/tasks.json` is callable from any window, which matters
because a window opened on the project it creates sees no task list at all.

| Preset | Features | Shaped like |
| --- | --- | --- |
| `bare` | none — harness + CI only | — |
| `service` | docker, app, postgres, alembic | — |
| `service-redis` | + redis | — |
| `data` | docker, postgres, archive seam | `ibkr_trader` |
| `fullstack` | + redis, frontend | `carameli` |

Individual `--with-*` flags add to a preset; they never subtract.

Everything local and reversible happens before the two outward-facing steps
(creating the GitHub repo, pushing). A failure before that leaves a directory you
can delete.

### Host ports: `ports.toml`

Each checkout — a project or one of its worktrees — owns one integer **slot**, and
every published port is `conventional_base + slot`. Slot 0 gets the familiar
defaults (Postgres 5432, Vite 5173); every other checkout is a uniform offset.

This replaces prose in per-repo READMEs, which had already drifted: carameli's README
prescribes `DB_HOST_PORT=5433` for its `-b` worktree while the real `.env` uses
`5434`, and following the README to the letter would have collided with
`ibkr_trader`'s hardcoded `5433`. `validate()` rejects duplicate slots and
insufficiently-spaced service bases rather than letting either reach
`docker compose up`, where it surfaces only as "port is already allocated".

```bash
python scripts/devkit_ports.py                 # the whole registry
python scripts/devkit_ports.py carameli-b      # one checkout's *_HOST_PORT block
```

The generator **does not** edit `ports.toml` itself — it prints the lines to add.
devkit is a git repo with its own gate, and a tool that silently commits to its own
source of truth is how two sessions hand out one slot twice.

### The two test trees

| Tree | Vendored? | Must be project-agnostic? |
| --- | --- | --- |
| `scripts/hooks/tests/` | Yes — in the `MANIFEST` | **Yes.** It runs inside every consuming repo, against that repo's `.devkit.toml`. |
| `tests/` | No | No. Generator, port registry, renderer — devkit-only. |

That distinction was violated for a while and it mattered: the vendored tests pinned
carameli's literal credentials, paths, env prefix, and skill list, so **every
generated project failed 12 of them on its first CI run**, and no other repo could
have adopted the harness. They now derive those values from `CFG` and skip tiers a
project does not have. CI's `generated-project` job renders a project of each preset
and runs its suites, because devkit's own suite passes precisely when devkit's
manifest is the one being hard-coded against.

devkit's own `.devkit.toml` describes **devkit** — it held a copy of a consumer's for a
while, which was harmless as an example and not as configuration, since these hooks now
run here and a hook reading another project's shape acts on directories that are not
here. So the tiers devkit does not have are off in it, and the vendored suite skips them
locally; the `generated-project` job above is what exercises them.

### The repo contract

`scripts/hooks/tests/test_repo_contract.py` (vendored) closes the gap the drift check
cannot see. `sync-devkit.py --check` guarantees the `MANIFEST` files are *identical*
everywhere; it says nothing about the files they depend on. `stop.py` dispatches to
project-owned sibling scripts, and at runtime a missing one
is a skip — deliberately, since a local tooling gap must never block the agent. That
is also why it is invisible: a project whose `lint-all.py` was never rendered has a
Stop gate that reports green having run nothing. The same shape shipped here once
already — `_REQ_RE` did not match `uv.lock`, so the lock-marker tier was inert in
every uv-native project and nothing looked broken.

The split is the point: **the runtime degrades quietly, CI is where that gets
noticed.** The contract asserts only what a repo's own config decides —

- the scripts a reachable tier needs exist (`lint-all.py` always);
- `[paths]` and `[frontend]` name directories that are actually there, since every
  tier selects by `startswith` and a stale prefix matches nothing, silently;
- the manifest has no unknown keys — `from_dict` is all `raw.get(name, default)`, so
  `db_servce` reads as "unset", and the tier quietly falls back to a default that
  does not match the compose file.

Everything gated on the repo actually wiring `stop.py` as a Stop hook, which is what
keeps devkit's fixture manifest from being held to devkit's files. Tiers whose script
is project-owned (`check-lock-markers.py`, whose sentinels name that project's own
lockfiles) stay optional and skip explicitly.

### The shared instruction tier

The same argument applies to prose. `.claude/rules/engineering.md`,
`.claude/rules/authoring.md`, and the `/ship` workflow are in the `MANIFEST` and
vendored byte-identical. `/ship` is the only shared skill: it has a concrete lifecycle
job and delegates its mechanical checks to the tested `scripts/ship.py` driver.

Generic audits, compatibility smoke commands, stateful refactor sweeps, and model-to-model
handoff prompts do not belong in every project. Mechanical constraints such as the
500-line instruction ceiling and local script reachability are enforced by
`test_repo_contract.py`; contextual review remains the coding agent's normal job.

**A project's `CLAUDE.md` cites these files; it does not restate them.** A restatement
is a fork — it reads as authoritative, it is not in the `MANIFEST`, and so it is the one
copy nothing drift-checks. `test_repo_contract.py` fails on a `CLAUDE.md` that reproduces
a vendored clause, matching on the distinctive middle of each rather than the whole
sentence, since a verbatim-only check passes the moment someone paraphrases — which is
how the drift happened the first time.

Only genuinely portable prose belongs here. A rule naming one project's services, paths,
or default branch is that project's own; vendoring it repeats the mistake that made every
generated project fail 12 tests on its first CI run. Carameli's security, skin, VoIP, and
webhook rules stay where they are.

> **Adopting this in an existing project takes two `--pull` runs.** The tool iterates the
> `MANIFEST` it was imported with, so the first pull installs the new `sync-devkit.py`
> and the second copies new entries and removes reviewed retired paths. Retirement never
> deletes project-owned siblings such as `state.json` or `known-fixes.md`.

Each pull also writes `DEVKIT_FILES.json`, a path-to-hash receipt for the files it
managed. Later pulls remove paths dropped from the manifest only when their bytes still
match that receipt; a locally edited retired file is preserved and reported. The explicit
retirement list exists only to migrate consumers created before receipts were introduced.

### Codex skills and hooks are generated, never hand-edited

Codex reads `CLAUDE.md` through its project-document fallback, so project instructions
need no duplicate. Configure that once in `~/.codex/config.toml` (or in a trusted
project's `.codex/config.toml`):

```toml
project_doc_fallback_filenames = ["CLAUDE.md"]
```

Repository skills still live at `.agents/skills/`, while Codex hooks live at
`.codex/hooks.json`. Because Codex does not discover `.claude/rules/`, the global
`~/.codex/AGENTS.md` carries one generic bridge: inspect rule frontmatter and read the
unscoped rules plus rules whose `paths` match the files being edited. The repository's
`CLAUDE.md` remains the rule index; copying rule bodies into `AGENTS.md` would create a
second instruction tree that can drift.

`sync-codex-context.py` mirrors only `.claude/skills/` to `.agents/skills/` and invokes
`sync-codex-hooks.py` to regenerate `.codex/hooks.json` from the `settings.json` hooks
block when a repository has opted into `.codex/`. Both scripts are in the `MANIFEST`,
and `new-project.py` runs the compatibility sync at creation.

The conversion is semantic, not a byte-for-byte copy. Generated Codex hooks carry
`commandWindows` overrides, while a too-short authored SessionStart timeout is raised
to 60 seconds. Explicit `commandWindows` values remain authoritative. Claude's Bash
output-cap handler is deliberately omitted because Codex already bounds shell-tool
output before returning it to the model.

`.codex/hooks.json` is generated and **committed**, so it can fall behind the generator
that writes it. `sync-devkit.py --pull` regenerates it after adopting upstream, and
`--check` reports a stale one as `STALE` — as does the vendored
`test_the_committed_codex_artifact_matches_the_generator`, which needs no `$DEVKIT_DIR`.
`sync-codex-hooks.py --check` is the same comparison on its own.

The generated commands run through `scripts/hooks/codex-hook-adapter.py`; Codex's
cross-platform SessionStart path uses `scripts/hooks/codex-session-start.py`. Both
runtime files and their unit tests are vendored with the converter. The generic repo
contract parses an opted-in `.codex/hooks.json` and fails when any git-root handler
path is absent, so successful conversion cannot mask an incomplete pull.

Carameli's `test_codex_hooks_contract.py` stays in carameli: it pins that repo's exact
hook topology and every semantic drop, which is the coupling this whole tier exists to
avoid.

`tests/test_codex_hooks_live.py` contains the paid, explicit release smokes. They create
isolated repositories with project-local `.codex/hooks.json` and launch the real Codex
CLI. One proves discovery plus a real `PreToolUse` denial; the other supplies Claude's
capped-Bash handler beside a recorder and proves one ordinary shell command executes
once without a denial, wrapper retry, or repeated call. Normal test runs exclude the
`paid` marker; opt in deliberately with
`python -m pytest tests/test_codex_hooks_live.py -m "codex_live and paid" -s`. The smokes
default to `gpt-5.6-luna` with low reasoning, no reasoning summary, and low verbosity;
`CODEX_LIVE_HOOK_MODEL` and `CODEX_LIVE_HOOK_REASONING_EFFORT` override those defaults.
Those settings are written only to the smoke's throwaway `CODEX_HOME`; human sessions
continue to take their model and reasoning effort from `~/.codex/config.toml`.
Keep these tests manual, nightly, or release-only. The converter and adapter tests are the
zero-model-cost gate for every hook change.

`tests/test_claude_hooks_live.py` is the Claude-side counterpart. It loads the real
vendored engineering rule and capped-Bash hook in an isolated project, exposes only the
Bash tool, and proves Claude runs an **unlisted** command bare on its first and only
shell call — no `invoke-capped.py` wrapper, and no denial or retry — which is the reflex
the rule's "do not wrap commands it did not name" paragraph exists to prevent. It is
also paid and opt-in:
`python -m pytest tests/test_claude_hooks_live.py -m "claude_live and paid" -s`.
`CLAUDE_LIVE_HOOK_MODEL`, `CLAUDE_LIVE_HOOK_EFFORT`, and
`CLAUDE_LIVE_HOOK_BUDGET_USD` override its low-cost defaults.

Because it is paid it is deselected by default, so nothing turned red when the Bash gate
was inverted underneath it and its expectation became the opposite of shipped policy.
`tests/test_live_smoke_matches_the_gate.py` is the free half added for that: it asks
`enforce-capped-bash.decide` about the exact command the smoke prompts for and fails if
the two ever disagree again, at no cost, in every PR gate.

Both are reachable from one workspace task, **Test: Harness Hook Tests — paid, live
CLI**, backed by `scripts/hook-tests-live.py`; it picks a suite, prints what it is about
to spend before launching anything, and writes failures to
`logs/hook-test-live-failures.log`. It exists because the raw `pytest` invocations above
have two ways to cost nothing and prove nothing while exiting 0 — the CLI is not on
`PATH`, so the suite skips, or the `-m` selector is dropped, so `addopts`' `-m "not
paid"` deselects everything. The runner reads the count of tests that actually ran and
fails when it is zero. The task is scoped to devkit alone: these suites live in `tests/`,
which is never vendored, so no consuming project has the file.

For a local diagnostic, run `codex doctor --summary` first; it checks installation,
configuration, authentication, and connectivity, but not whether a hook changed
behavior. Then use the two durable behavior checks:

```bash
python -m pytest scripts/hooks/tests/test_sync_codex_hooks.py \
  scripts/hooks/tests/test_codex_hook_adapter.py -q
python -m pytest tests/test_codex_hooks_live.py -m "codex_live and paid" -s
```

The live test uses a temporary `CODEX_HOME` with authentication but no user hooks, and
trusts only its temporary project. That isolation is load-bearing: a workstation hook
can otherwise block the sentinel and make the project hook look healthy when it never
ran. The test uses `--dangerously-bypass-hook-trust` only inside that vetted temporary
repository; normal sessions review changed hook hashes with `/hooks`.

Instruction discovery has a free, model-less check. `codex debug prompt-input
"instruction probe"` prints the exact model-visible input; search it for the global
rule bridge and the repository's `CLAUDE.md` heading. This catches a missing fallback
setting without spending a model call.

### The shell output cap

For Claude Code, `enforce-capped-bash.py` (PreToolUse) blocks a short, closed list of
commands whose output grows with the repository — `ls`, `cat`, `find`, `tree`, `du`,
`env`, `git status`, an uncounted `git log`, and a raw `git diff`/`git show`. Everything
else runs uncapped. `invoke-capped.py` is one of the three ways out it names, and the
unconditional bound on every call is `BASH_MAX_OUTPUT_LENGTH` in `.claude/settings.json`.
Both scripts are vendored and ship together — the gate names the wrapper's path in its
block message, so vendoring one without the other offers a remedy the repo does not have.

The gate used to require every Bash call to *prove* it was bounded. Its docstring records
why that ended, with the measurement: 46% of every block it ever issued was its own false
positive rather than a command anyone needed to rewrite.

Codex's shell tool already bounds captured output. The hook converter therefore drops
this handler, removes a `PreToolUse` group or event left empty by the drop, and preserves
unrelated handlers that shared the group. This avoids the deny-and-retry cycle without
weakening Claude's Bash policy.

Cap size is `[bash] max_bytes` / `head_bytes` in `.devkit.toml`, read by both Claude-side
scripts, so the number the agent is told to use is the number it actually gets. The
wrapper uses the platform shell and preserves the exit code, while `| head -c N` keeps
POSIX syntax but masks the exit code behind `head`'s.

## Scope note

The current `MANIFEST` is the reviewed, coupling-free core, including the branch
lifecycle and `/ship`. Default branches are resolved from `origin/HEAD` with `main`/
`master` fallbacks; no vendored workflow hard-codes one repository's base branch.
