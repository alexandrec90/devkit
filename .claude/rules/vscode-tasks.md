---
description: Where VS Code tasks live, how a task is defined and dispatched, and the one-way adoption flow that records them
paths:
  - workspace-tasks.jsonc
  - scripts/devkit_project.py
  - scripts/git-merge-default.py
  - tests/test_devkit_project.py
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

## The one task that must not be a dispatch

`scripts/git-merge-default.py` is a workspace task, not an `ACTIONS` entry: the dispatcher
subtracts `NOT_PROJECTS` because its actions need a harness, and a merge needs git alone,
so it resolves against the **raw** registry rather than making the exclusion an exception.
That makes `mergeCheckout` the only picker listing *more* than the registry —
`insert_picker_option` maintains it, and a test pins the equality both ways. Its docstring
carries the rest.

## Changing a task: the live file first, then adopt

`workspace-tasks.jsonc` is devkit's copy of the block, and the workspace file — which
lives outside every repo and so cannot be vendored — is the one VS Code actually runs.
**Edit the workspace's `.code-workspace` file, then record it:**

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
- **A new project has to reach more than the `project` picker.** `register()` extends the
  `folders` list and that one picker; the workspace-scoped pickers — `sweepScope`,
  `upgradeScope` — are hand-maintained and were silently skipped, so a newly generated
  project could run every generic task while `--all` was the only way to sweep or upgrade
  it. `SCOPE_PICKERS` in `tests/test_devkit_project.py` now requires each of them to
  cover every checkout the `project` picker lists, and a deliberate omission (devkit is
  not a target of a devkit upgrade) to carry its reason in writing. Pickers scoped by
  `Action.projects` are a separate case and are gated separately.
