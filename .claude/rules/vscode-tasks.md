---
description: Where VS Code tasks live, how a task is defined and dispatched, and how devkit's canonical copy is rendered to the live workspace file
paths:
  - workspace.jsonc
  - scripts/devkit_project.py
  - scripts/git-merge-default.py
  - scripts/vanillaland-e2e.py
  - tests/test_devkit_project.py
  - tests/test_vanillaland_e2e.py
  - tests/test_self_hosting.py
---

# Rule: VS Code tasks

**Tasks live in the workspace file, never in a repo's `.vscode/tasks.json`.** A task
defined in a repo is invisible from the workspace root, cannot be scoped with
`Action.projects`, and drifts from its siblings; the workspace file is the one place that
sees every checkout. devkit and both live repos ship zero project-level tasks, and each
one's suite fails if that changes.

**Project-specific is not a reason to keep a task local.** `Action.projects` in
`scripts/devkit_project.py` scopes an action to the checkouts that can run it, which is
how a browser suite or a backtest run is defined once without pretending every checkout
can run it. The scope restricts both directions — the dispatcher refuses an out-of-scope
checkout by name, and `--check` stops demanding the script from projects it was never
meant for.

What a repo owes instead is the **CLI contract**: a `scripts/<name>.py` at the path
`ACTIONS` names, accepting the documented arguments. A task that cannot be expressed that
way is not blocked from hoisting — write the seam. `scripts/backtest-task.py` in
ibkr_trader exists for exactly that reason: its two tasks invoked a console-script
executable directly, which the dispatcher cannot call.

## A task that rewrites the tree ships what it wrote

The lint actions do not only report: `lint-all.py` runs `ruff check --fix`, `ruff format`
and — where a project ships it — a detect-secrets baseline that acknowledges its own new
findings. Those writes land in the static checkout, on its home branch, which is the one
write `worktree-guard.py` would have routed into a box had an agent made it. Nobody
authored them, so nobody committed them, and they surfaced days later as a `needs-branch`
verdict that read like abandoned human work.

So an `Action` marked `autofix=True` is followed by `sweep.py --branch` and `--ship` for
that checkout, and `autofix_ship_plan` decides whether that is honest. It ships one case
only — clean tree, home branch, green run — and declines the rest out loud; its docstring
owns each reason. `--no-ship-fixes` is the escape hatch for inspecting fixes locally.

Two consequences worth knowing before adding an action. **Marking one `autofix` is a
claim that its writes belong in a PR of their own**, so a task that edits the tree as its
*purpose* (a migration, a devkit `--pull`) is not one of these — that diff wants a real
commit message. And the plan is pure, so a new decline is a test in
`tests/test_devkit_project.py`, not a manual trial against a live checkout.

## The tasks that must not be a dispatch

Two of them, for the same reason twice. The dispatcher subtracts `NOT_PROJECTS` because
every action it runs needs a harness a reference checkout does not have — so an operation
needing *only* what such a checkout already carries cannot become an `ACTIONS` entry
without turning that exclusion into an exception. Each resolves against the **raw**
registry instead, and neither moves the dispatcher's contract:

- `scripts/git-merge-default.py` merges a trunk in and needs git alone. Its picker,
  `mergeCheckout`, is the only one listing *more* than the registry —
  `insert_picker_option` maintains it, and a test pins the equality both ways.
- `scripts/vanillaland-e2e.py` runs the start script VanillaLand itself ships and needs
  PowerShell alone. It carries **no** picker: one checkout owns that stack, so the name
  is a constant and `--checkout` stays a flag only so the tests can aim it elsewhere.

Both docstrings carry the rest.

## Changing a task: edit devkit's copy, on a branch, then render

`workspace.jsonc` is devkit's canonical copy of the **whole** workspace file, and the
live `.code-workspace` — which lives outside every repo and so cannot be vendored — is
what VS Code actually runs. **Edit the canonical one.** It is the only copy with a
branch, a diff and a reviewer:

```bash
python scripts/devkit_project.py --check-workspace    # do they agree?
# ...edit workspace.jsonc on a task branch, ship it, let the PR merge...
python scripts/devkit_project.py --render-workspace   # workspace.jsonc -> the live file
```

**The last step runs itself.** `workspace_sync_line` in `scripts/workspace-status.py`
publishes at session start, on exactly the terms `--render-workspace` does — it calls
`publish_workspace`, the same function the CLI does, so there is one answer to "is it
safe to overwrite the file every window on this machine reads" rather than two. It adds
two conditions of its own, in `publish_verdict`: devkit's checkout is on its default
branch and its `workspace.jsonc` is committed. A task branch's copy is a proposal and an
uncommitted one is not even that, so from a box, or mid-edit, the line reports the drift
and publishes nothing. When it does publish it says so and asks for a window reload —
VS Code reads the file once, at open.

That is a wire-up rather than a convenience. The step it removes had already failed the
way a remembered step does: three tasks merged, the checkout was synced, and the tasks
were still not in anyone's window, because nothing on the machine was going to render
them until someone typed the command.

All three are one click as well — the *Workspace:* tasks, which are deliberately not
dispatches: there is one workspace file, so there is no checkout to pick, and they carry
their own `notify-wrap`/`log-wrap` because `plan_command` never sees them.

**Never hand-edit the live file to make a change.** It has no branch dimension: one
copy serves every window on the machine, so an in-flight edit is globally live before
anyone reviews it, and two agents editing it race with last-writer-wins and nothing to
recover the loser's edit *from*. That is not hypothetical — on 2026-08-21 the live file
was found running PR #177's task block, still open, with five tasks deleted and 38
reformatted, in `main`'s name. Editing `workspace.jsonc` on a branch instead puts the
conflict in git, where a conflict has two visible sides.

**The other direction still exists, for the edits that are not yours to route.** VS
Code rewrites the file itself when a workspace setting is changed through its UI, and a
hand edit in the editor is legitimate. `--adopt-workspace` records the live file back
into `workspace.jsonc` for committing on a branch, and `--render-workspace` *refuses*
rather than overwriting an unadopted edit — it renders only when the live file is
byte-for-meaning what devkit last wrote, recorded in `.devkit-workspace-render.json`
beside it. `--force` overrides that, and it discards.

The pairing is held by `test_the_live_workspace_matches_the_canonical_copy`, which is
`@needs_live_workspace`: skipped in CI, so drift is caught locally or not at all. That
is what the session-start publish exists to backstop — and why it reports rather than
publishes whenever it is not certain, since the only alternative to a session start
noticing is nothing noticing.

**Red there is not evidence of drift.** The live file is rendered from a *merged*
canonical copy, and the order above has you editing `workspace.jsonc` before that merge
— so from a box on an open task branch the test reports your own unlanded edit, every
time: an added task as `missing from the workspace`, a changed one as
`definition differs`. Another branch's edit reads the same way once its PR lands and
this box has not merged main yet. With several boxes open at once that is the normal
state, not a defect. What the live file should match is `workspace.jsonc` **as it stands
on `origin/main`** — `git show` that revision of it, and the difference against your own
copy is what your branch adds.

**And the remedy the failure names is the wrong direction here.** `--adopt-workspace`
takes the *live* file — which is still main's render — over your canonical copy, so
running it in a box to get green deletes the edit the branch exists for. Render only
after the PR merges; a box never renders.

## Conventions for the tasks themselves

- **The settings a task carries are a table in the tests, not a habit.**
  `TASK_CONTRACT` in `tests/test_devkit_project.py` holds `type`, `presentation.panel`,
  `presentation.close` and `presentation.reveal` with what each is for, and
  `CONTRACT_EXCEPTIONS` holds the two deviations and why. Copy the nearest neighbour
  when you add a task and the test tells you what you missed — which is the failure it
  was written for: the block had 33 tasks pinning `close: false` and 8 leaving it to the
  default, and a `panel` split between `shared` and `dedicated` that nobody had decided.
  A run's terminal is the only place a *passing* run's output exists, because
  `log-wrap.py` empties the log when the task passes, so `panel: "new"` is the one
  setting there is no artifact to fall back on.
- **Wrap with `notify-wrap.py`** for the completion toast; never call `notify.py` from
  inside a script. Notifications are a task-layer concern only. A task that ends too
  fast to notify about, or whose own window is the notification, goes in
  `UNTOASTED_TASKS` with its reason.
- **The toast is stdlib, and it must stay that way.** `notify.py` shells out to
  *Windows PowerShell 5.1* — `pwsh` cannot load a WinRT type — because devkit's runtime
  dependency list is empty by contract. It previously imported `win11toast`, which no
  project has ever installed, behind a bare `except Exception: pass`; every call was a
  silent no-op for the wrapper's whole life, and nothing was red anywhere. That is why
  a failed toast now prints one line to stderr: it must never break the task, and it
  must never again be invisible.
- **And with `log-wrap.py`, inside it**, so the run's output survives the terminal as a
  log under `logs/` named for the task — emptied when the task passes, so it never
  describes a failure that is already fixed. The nesting is `notify-wrap → log-wrap →
  the script`: the toast needs only an exit code, the artifact needs the output, and the
  script needs to know about neither. A **dispatched** task gets this for free —
  `plan_command` in `scripts/devkit_project.py` wraps every action, and it is the only
  place that can, because the task names a picker and nothing knows which checkout's
  `logs/` the failure belongs in until `resolve_project` has run. A task that
  deliberately writes no artifact goes in `UNLOGGED_TASKS` in
  `tests/test_devkit_project.py` with its reason; the two launcher tasks are there
  because the window they open *is* the output.
- Label convention: `"Domain: Title Case Action"`, and **every task carries a `detail`**
  — that is the second line in the quick-pick, and the only place a one-click action can
  state its cost or blast radius.
- **Every task carries an `icon`, and no two share the same id+colour pair.** With one
  consolidated list, colour is what makes it scannable; the `terminal.ansiBright*`
  variants mark the project-scoped tasks, so you can see before clicking that a task
  will ask which checkout to use.
- A `${input:...}` picker must supply **one real token in every branch**. An empty
  string reaches argparse as a stray positional and is rejected, which is why
  `scripts/new-project.py` carries the redundant-looking `--dry-run` and `--remote`
  flags alongside their negations. The exception is a picker feeding
  `scripts/devkit_project.py`, which strips empties before exec — `testScope` and
  `e2eMode` rely on that, and say so.
- **A picker whose options are not knowable in advance reads a file, and the script that
  answers it writes that file.** `rioj7.command-variable` can read and template a JSON
  file, and cannot run a command — so a list of live branches or boxes has to be *cached*
  by the previous run rather than gathered by this one. Three things that costs, all of
  which `previewRow` pays and a new one has to: the list is stale by construction, so it
  needs a visible timestamp and a `Rescan` entry that falls through to a terminal prompt;
  a pick that no longer matches anything must still resolve to something servable, since
  the world moved on after the file was written; and **every row must carry every
  templated field, as a string.** The extension appends options until an expression
  *throws*, and `undefined` does not throw — a row missing one field draws ten thousand
  blank entries instead of ending the list.
- **Two dependent pickers are one input, not two.** VS Code resolves sibling
  `${input:...}` in no defined order and gives neither sight of the other, so a
  "which project, then which of its branches" pair is a `pickStringRemember` nested
  inside the outer input's `args`, read back as `${pickStringRemember:<id>}` — and the
  answer travels to the script as one token, because an input resolves to one string.
- **An action scoped to exactly one checkout writes the name, not a picker.** A
  `${input:...}` with a single option asks a question that has no second answer, and the
  extension still shows it. Spell the checkout in the task's `--project` argument
  instead — `test-hooks-live` does. The usual picker/scope gate skips a literal, so
  `test_the_live_smoke_task_names_the_only_checkout_that_can_run_it` asserts the same
  agreement against `Action.projects` directly; a literal that outgrows its scope fails
  there rather than in a terminal.
- **A new project has to reach more than the `project` picker.** `register()` extends the
  `folders` list and that one picker; the workspace-scoped pickers — `sweepScope`,
  `upgradeScope` — are hand-maintained and were silently skipped, so a newly generated
  project could run every generic task while `--all` was the only way to sweep or upgrade
  it. `SCOPE_PICKERS` in `tests/test_devkit_project.py` now requires each of them to
  cover every checkout the `project` picker lists, and a deliberate omission (devkit is
  not a target of a devkit upgrade) to carry its reason in writing. Pickers scoped by
  `Action.projects` are a separate case and are gated separately.

- **A task meant to run twice at once says so with `runOptions.instanceLimit`.** The
  default is **1**, and re-running a task that is already active offers to terminate it
  instead — which is what the preview tasks did for months, so comparing two branches
  side by side looked impossible from the outside while `worktree.py` underneath had
  been concurrency-safe all along (a lease lock, a port slot and a
  `COMPOSE_PROJECT_NAME` per box). `presentation.panel` is *not* this setting: `new`
  gives each run its own terminal and still refuses the second run. Raising it also
  gives that task's `log-wrap.py` artifact two writers, which is what `write_artifact`'s
  `since` argument handles.
