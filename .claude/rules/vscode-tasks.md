---
description: Where VS Code tasks live, how a task is defined and dispatched, and the one-way adoption flow that records them
paths:
  - workspace-tasks.jsonc
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

## Changing a task: check the live file, edit it, then adopt

`workspace-tasks.jsonc` is devkit's copy of the block, and the workspace file — which
lives outside every repo and so cannot be vendored — is the one VS Code actually runs.
**Check for existing drift before editing**, so adoption cannot mistake stale or unrelated
workspace changes for part of yours. Resolve that drift or preserve its reported list,
then edit the workspace's `.code-workspace` file and record the intentional result:

```bash
python scripts/devkit_project.py --check-tasks   # preflight: run before editing
python scripts/devkit_project.py --adopt-tasks   # live file -> workspace-tasks.jsonc
python scripts/devkit_project.py --check-tasks   # verify they agree
```

**One-way, with no flag for the other direction.** Editing `workspace-tasks.jsonc`
directly looks right — it is the file in the repo, the diff is clean, and the drift test
even names `--adopt-tasks` as the remedy — and running that *deletes the edit*, because
it regenerates the canonical copy from the live file. One test holds the pair together
(`test_the_live_workspace_matches_the_canonical_block`) and it is
`@needs_live_workspace`: skipped in CI, so drift is caught locally or not at all.

## Conventions for the tasks themselves

- Use `"type": "process"` so VS Code monitors the process directly — that is what makes
  the spinner stop and the exit-code icon appear reliably.
- Set `"close": false` in `presentation` so the terminal stays open for review.
- **Wrap with `notify-wrap.py`** for the completion toast; never call `notify.py` from
  inside a script. Notifications are a task-layer concern only.
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
