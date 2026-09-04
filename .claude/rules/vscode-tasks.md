---
description: Where VS Code tasks live, how a task is defined and dispatched, and how devkit's canonical copy is rendered to the live workspace file
paths:
  - workspace.jsonc
  - scripts/devkit_project.py
  - scripts/git-merge-default.py
  - tests/test_devkit_project.py
  - tests/test_git_merge_default.py
  - tests/test_self_hosting.py
---

# Rule: VS Code tasks

**Tasks live in the workspace file, never in a repo's `.vscode/tasks.json`.** A task
defined in a repo is invisible from the workspace root, cannot be scoped with
`Action.projects`, and drifts from its siblings. Each project's suite fails if that
changes.

**Project-specific is not a reason to keep a task local.** `Action.projects` in
`scripts/devkit_project.py` scopes an action to the checkouts that can run it, in both
directions — the dispatcher refuses an out-of-scope checkout by name, and `--check` stops
demanding the script from projects it was never meant for. What a repo owes instead is the
**CLI contract**: a `scripts/<name>.py` at the path `ACTIONS` names, accepting the
documented arguments. A task that cannot be expressed that way is not blocked from
hoisting — write the seam.

## A task that rewrites the tree ships what it wrote

The lint actions do not only report: `lint-all.py` runs `ruff check --fix`, `ruff format`
and, where a project ships it, a detect-secrets baseline. Those writes land in the static
checkout on its home branch — the one write `worktree-guard.py` would have routed into a
box had an agent made it — so nobody authored them, nobody committed them, and they
surface days later as a `needs-branch` verdict that reads like abandoned human work.

So an `Action` marked `autofix=True` is followed by `sweep.py --branch` and `--ship` for
that checkout, and `autofix_ship_plan` decides whether that is honest: it ships one case
only — clean tree, home branch, green run — and declines the rest out loud, its docstring
owning each reason. `--no-ship-fixes` inspects fixes locally instead.

Two consequences before adding an action. **Marking one `autofix` claims its writes belong
in a PR of their own**, so a task that edits the tree as its *purpose* (a migration, a
devkit `--pull`) is not one of these. And the plan is pure, so a new decline is a test in
`tests/test_devkit_project.py`, not a manual trial against a live checkout.

## The tasks that must not be a dispatch

The dispatcher subtracts `NOT_PROJECTS` because every action it runs needs a harness a
reference checkout does not have — so an operation needing *only* what such a checkout
already carries cannot become an `ACTIONS` entry without turning that exclusion into an
exception. `scripts/git-merge-default.py` merges a trunk in and needs git alone; its
picker, `mergeCheckout`, lists the **raw** registry where the dispatcher's `project`
picker lists `known_projects`. The two coincide while `NOT_PROJECTS` is empty and must not
be collapsed because of it: `insert_picker_option` maintains both, and a test pins each to
its own source. Its docstring carries the rest.

## Changing a task: edit devkit's copy, on a branch, then render

`workspace.jsonc` is devkit's canonical copy of the **whole** workspace file, and the live
`.code-workspace` — which lives outside every repo and so cannot be vendored — is what VS
Code actually runs. **Edit the canonical one.** It is the only copy with a branch, a diff
and a reviewer:

```bash
python scripts/devkit_project.py --check-workspace    # do they agree?
# ...edit workspace.jsonc on a task branch, ship it, let the PR merge...
python scripts/devkit_project.py --render-workspace   # workspace.jsonc -> the live file
```

**The last step runs itself.** `workspace_sync_line` in `scripts/workspace-status.py`
publishes at session start through `publish_workspace`, the same function the CLI calls,
adding two conditions in `publish_verdict`: devkit's checkout is on its default branch and
its `workspace.jsonc` is committed. A task branch's copy is a proposal and an uncommitted
one is not even that, so from a box, or mid-edit, the line reports the drift and publishes
nothing. When it does publish it asks for a window reload — VS Code reads the file once,
at open. That is a wire-up rather than a convenience: the remembered step had already
failed, with three tasks merged, the checkout synced, and nothing on the machine going to
render them until someone typed the command.

**Never hand-edit the live file to make a change.** It has no branch dimension: one copy
serves every window on the machine, so an in-flight edit is globally live before anyone
reviews it, and two agents editing it race with last-writer-wins and nothing to recover
the loser's edit *from*. Editing `workspace.jsonc` on a branch puts the conflict in git,
where a conflict has two visible sides.

**The other direction still exists, for the edits that are not yours to route.** VS Code
rewrites the file itself when a workspace setting is changed through its UI.
`--adopt-workspace` records the live file back into `workspace.jsonc` for committing on a
branch, and `--render-workspace` *refuses* rather than overwriting an unadopted edit — it
renders only when the live file is byte-for-meaning what devkit last wrote, recorded in
`.devkit-workspace-render.json` beside it. `--force` overrides that, and it discards.

`test_the_live_workspace_matches_the_canonical_copy` holds the pairing and is
`@needs_live_workspace`: skipped in CI, so drift is caught locally or not at all.

**Red there is not evidence of drift.** The live file is rendered from a *merged* canonical
copy, so from a box on an open task branch the test reports your own unlanded edit every
time — an added task as `missing from the workspace`, a changed one as `definition
differs`. With several boxes open that is the normal state. What the live file should match
is `workspace.jsonc` **as it stands on `origin/main`**. **And the remedy the failure names
is the wrong direction here:** `--adopt-workspace` takes the live file — still main's
render — over your canonical copy, so running it in a box to get green deletes the edit the
branch exists for. Render only after the PR merges; a box never renders.

## Conventions for the tasks themselves

- **The settings a task carries are a table in the tests, not a habit.** `TASK_CONTRACT`
  in `tests/test_devkit_project.py` holds `type`, `presentation.panel`,
  `presentation.close` and `presentation.reveal` with what each is for, and
  `CONTRACT_EXCEPTIONS` holds the two deviations and why. Copy the nearest neighbour and
  the test tells you what you missed. A run's terminal is the only place a *passing* run's
  output exists, because `log-wrap.py` empties the log when the task passes, so
  `panel: "new"` is the one setting with no artifact to fall back on.
- **Wrap with `notify-wrap.py`** for the completion toast; never call `notify.py` from
  inside a script. A task that ends too fast to notify about, or whose own window is the
  notification, goes in `UNTOASTED_TASKS` with its reason.
- **The toast is stdlib, and must stay that way.** `notify.py` shells out to *Windows
  PowerShell 5.1* — `pwsh` cannot load a WinRT type — because devkit's runtime dependency
  list is empty by contract. A failed toast prints one line to stderr: it must never break
  the task, and it must never again be invisible.
- **And with `log-wrap.py`, inside it**, so the run's output survives the terminal as a log
  under `logs/` named for the task — emptied when the task passes. The nesting is
  `notify-wrap → log-wrap → the script`. A **dispatched** task gets this for free from
  `plan_command`, which is the only place that can wrap it: nothing knows which checkout's
  `logs/` a failure belongs in until `resolve_project` has run. A task that deliberately
  writes no artifact goes in `UNLOGGED_TASKS` with its reason.
- **A task may ask a question, and the prompt has to be a whole flushed line.**
  `log-wrap.py` pipes the child's stdout and reads it a line at a time, so a bare
  `input("> ")` sits in the pipe buffer and the operator watches an empty terminal hang.
  Print with `flush=True` and read with a bare `input()`. stdin is inherited through both
  wrappers, so an interactive task is *not* a reason to drop the artifact or the toast.
- **The quick-pick is the human's surface, so a task needs a human caller.** An agent
  reaches every script through the CLI and never through VS Code, so a row whose only
  plausible clicker is an agent — or one that duplicates a scheduled pass — is clutter in
  the one menu a person reads. `test_the_box_tier_keeps_one_task_and_it_is_read_only`
  holds the line and its docstring carries the case per task; `reconcile` is not a row.
- Label convention: `"Domain: Title Case Action"`, and **every task carries a `detail`** —
  the second line in the quick-pick, and the only place a one-click action can state its
  cost or blast radius.
- **No label, detail or option states a version.** Nothing renders this file from the tag
  list, so a number written into a quick-pick is a claim with no owner that no bump will
  move. `test_no_task_or_picker_states_a_release_version` is the ratchet, and it reads the
  parsed block — a version in a **comment** is reasoning, not a claim, and stays allowed.
- **Every task carries an `icon`, and no two share the same id+colour pair.** The
  `terminal.ansiBright*` variants mark the project-scoped tasks, so you can see before
  clicking that a task will ask which checkout to use.
- A `${input:...}` picker must supply **one real token in every branch**. An empty string
  reaches argparse as a stray positional and is rejected, which is why
  `scripts/new-project.py` carries the redundant-looking `--dry-run` and `--remote` flags
  alongside their negations. The exception is a picker feeding `scripts/devkit_project.py`,
  which strips empties before exec; `lintScope` relies on that and says so where it is
  defined.
- **A scope that fits in a picker is a picker, not a second task.** Two tasks whose labels
  differ only in how much they cover sit adjacent in the quick-pick and cost an icon, a
  `detail` and — where dispatched — a second `ACTIONS` entry for one flag. What earns a
  task of its own is a difference the picker cannot carry back: a cost nobody consented to
  (`test-hooks-live` is separate so the free one's flag cannot reach it by a mistyped
  argument), or a different script.
- **Escape cancels the task only for VS Code's own inputs; a `command` input has to be
  cancelled by the script.** `promptString` and `pickString` abort the run when dismissed.
  A `command` input — every multi-select picker, since `multiPick` exists only in
  `rioj7.command-variable` — returns nothing, VS Code records no substitution, and the task
  **starts anyway** with the literal `${input:<id>}` in its argument list. The extension's
  `checkEscapedUI` is not the fix: it started a task anyway on a dismissed picker, and the
  flag is a sticky bit no path clears, so one Escape retires the dropdown for the life of
  the window. The receiving script recognises the literal itself via `scripts/task_input.py`
  and exits 0 having run nothing — **ahead of `argparse`**, because a cancel reported as a
  usage error is a red icon, a toast and a `logs/` artifact for a run the user called off.
  `test_every_task_with_a_command_picker_can_be_cancelled` checks the **innermost** command,
  since a wrapper passes its tail through.
- **A picker whose options are not knowable in advance reads a file, and the script that
  answers it writes that file.** `rioj7.command-variable` can read and template JSON and
  cannot run a command, so a list of live branches or boxes has to be *cached* by the
  previous run. Three things that costs: the list is stale by construction, so it needs a
  visible timestamp **and a writer that is not a task run** — `previewRow`'s file is
  rewritten by `worktree.py reconcile` on its schedule, so the menu tracks open PRs without
  anyone asking; a pick that no longer matches anything must still resolve to something
  servable; and **every row must carry every templated field, as a string**, because the
  extension appends options until an expression *throws*, and `undefined` does not throw —
  a row missing one field draws ten thousand blank entries instead of ending the list. Ride
  on an existing scheduled pass rather than adding a daemon, and make the rider unable to
  fail it: any exception leaves `reconcile`'s own verdict untouched and prints one warning.
- **Two dependent pickers are one input, not two.** VS Code resolves sibling `${input:...}`
  in no defined order and gives neither sight of the other, so a "which project, then which
  of its branches" pair is a `pickStringRemember` nested inside the outer input's `args`,
  read back as `${pickStringRemember:<id>}` — one token, because an input resolves to one
  string.
- **An action scoped to exactly one checkout writes the name, not a picker.** A
  `${input:...}` with a single option asks a question that has no second answer, and the
  extension still shows it. Spell the checkout in the task's `--project` argument instead.
  The usual picker/scope gate skips a literal, so
  `test_a_literal_checkout_dispatch_agrees_with_its_actions_scope` asserts the agreement
  against `Action.projects` directly.
- **A scope picker is only allowed if `register()` maintains it.** Two that did not —
  `sweepScope`, then `upgradeScope` — were each a hand-maintained second copy of the
  project registry, silently skipped when `register()` added a project. **The defect was
  the second copy, not the asking**, so the rule is the maintenance: a batch task with no
  reason to narrow still takes `--all`, and one that does gets a picker
  `insert_picker_option` names. That list is `project`, `daemonProject`, `mergeCheckout`,
  `adoptProjects`, held to the workspace file by
  `test_registering_against_the_real_workspace_file`. Pickers scoped by `Action.projects`
  are gated separately.
- **`adoptProjects` is the exception, and its cost is worth reading before adding
  another.** It lists the registry **minus `devkit`** — a release is pulled *from* that
  checkout, and `upgrade-project.py` treats a named checkout it cannot upgrade as an
  operator error and stops the whole run, where `--all` skips it silently. So
  `insert_picker_option` inserts into it anyway (`register()` never names `devkit`), and
  `tests/test_devkit_project.py` pins it against *registry minus devkit* both ways.
  Escaping means *never mind*, and the two scripts read that differently on purpose — see
  `upgrade-project.picked_nothing` and its caller in `release-pipeline.main`. That reading
  is downstream of `argparse`, so `upgrade-project.main` also runs the `task_input` check
  ahead of the parser, per the Escape bullet above.
- **A task meant to run twice at once says so with `runOptions.instanceLimit`.** The
  default is **1**, and re-running an active task offers to terminate it instead — which
  made comparing two preview branches look impossible while `worktree.py` underneath had
  been concurrency-safe all along. `presentation.panel` is *not* this setting: `new` gives
  each run its own terminal and still refuses the second run. Raising it gives that task's
  `log-wrap.py` artifact two writers, which is what `write_artifact`'s `since` argument
  handles.
