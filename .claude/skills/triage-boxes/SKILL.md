---
name: triage-boxes
description: Clear every stranded ephemeral box -- a HOLD older than a day with no PR, or one cut for a project on hold -- by reading what each holds and either shipping it (`worktree.py rescue --ship`) or deleting it (`worktree.py reap --force`). Ends with zero stranded boxes, not a list for the user.
disable-model-invocation: true
argument-hint: 'Optional: a box name to triage first, or `--all` (the default)'
---

# Clear the stranded boxes

> Depends on git, `gh` (authenticated), and the workspace file the boxes are registered
> under. Run it from the devkit checkout; every command takes `--workspace` from there.

`worktree.py reconcile` runs every fifteen minutes and destroys a box once its PR merges.
It cannot decide what to do with a box that never got a PR: reaping it may destroy the
only copy of real work, and shipping it may open a PR for an editor autosave. So for
months it printed those boxes under *holding work -- ship it*, addressed to a user who
does not ship by hand and had set the schedule up precisely so they would never have to.
The 2026-08-30 audit found thirteen of them, the oldest ten days old.

The tier now calls that state **stranded** (`worktree.stranded`: a `HOLD` older than
`STRANDED_AGE_DAYS` with no PR, or any `HOLD` for a project in `workspace.jsonc`'s
`devkit.onHold` list) and names this skill as the remedy. The judgement the scheduled
pass cannot make -- *is this work or is it debris* -- is one an agent can, from the diff.
That is the whole job here: read each stranded box, decide, act, and end with none left.

Devkit-only, deliberately: the boxes belong to the workspace, not to any one project, and
this is where `worktree.py` lives. It is not in `sync-devkit.py`'s `MANIFEST`.

> Every command below is issued bare. A wrapper here buys no second bound.

## 1. List them

```bash
python scripts/worktree.py reconcile --dry-run --json --workspace <workspace file>
```

Every row with `"stranded": true` is yours. The dry run asks GitHub, so `pr`, `pr_state`
and `age_days` are in each row; `python scripts/worktree.py list` is the cheaper tree-only
view and can over-report a box that is still being edited after its PR opened -- when the
two disagree, the reconcile row wins.

Also glance at `.worktrees/` itself: a directory there that no row names is a clone
nothing registered (`git -C <dir> worktree list` says whether it is even a worktree), and
nothing will ever reap it. Treat it as stranded with no lease.

### The session tier, which no schedule reaps either

```bash
python scripts/agent-worktree.py list
```

`.claude/worktrees/<name>` — what `claude --worktree` and a remote session cut — is a
second stranding surface, and the reason it needs naming here is that **every signal this
skill runs on is absent there**: no lease, so `reconcile` never sees one; no registration,
so `worktree.py list` does not either; not under `.worktrees/`, so the paragraph above
misses it too. The only thing that ever removed one was Claude Code's own offer on the way
out, which applies only to a worktree that is **UNCHANGED** — and a repo whose commit
hooks rewrite a generated file (devkit's `detect-secrets` rewrites `.secrets.baseline`'s
`generated_at` on every commit) has no such worktree, so the offer never fires and they
accumulate without limit. 78 across devkit, carameli and roguelike when this was reported,
three of them holding unshipped work with tests.

Steps 2 and 3 judge one of these exactly as they judge a box — the diff is the diff —
and only step 4 differs, because there is no lease to rescue or reap. Confirm the
directory is a worktree with `worktree list` inside it, then remove it from the project
checkout with `worktree remove <name>`, which the `remove` verb of
[`scripts/agent-worktree.py`](../../../scripts/agent-worktree.py) also drives.

Removing without `--force` is the right default here and not the timidity it looks like:
it refuses a dirty tree, which is the one case step 3 may not have read yet, and **the
branch survives either way** — so a removal never destroys committed work, only the
checkout of it. Ship the ones holding work first, as an ordinary push and `gh pr create`;
`worktree.py rescue` is the box tier's verb and does not apply.

## 2. Read each one

Per box, in this order, and stop as soon as the decision is clear:

```bash
git -C <box path> status --short
git -C <box path> diff HEAD --stat
git -C <box path> log --oneline origin/<default>..HEAD
gh pr list --repo <owner/repo> --head <branch> --state all
```

Then the one comparison that settles most of them -- whether the uncommitted edits are
already on the default branch in some form:

```bash
git -C <box path> fetch origin <default>
git -C <box path> diff origin/<default> -- <each dirty path>
```

An empty diff against `origin/<default>` for every dirty path means a later PR carried
the same change; a diff that is only a rearrangement of the same lines usually means a
later PR redesigned the file and this box's version lost. Read the box's `session` and
`created` from the row too: a box cut by `upgrade-project.py --all` for an on-hold project
holds a vendored-tier bump nobody asked for, and its work is *supposed* to be discarded.

## 3. Decide

| Delete when | Ship when |
| --- | --- |
| the project is on hold and the box was cut by automation (`devkit-upgrade-…`) | the diff is a coherent change a later PR did not carry |
| every dirty path diffs empty against `origin/<default>` | the box holds commits `origin/<default>..HEAD` that never reached a PR |
| the only dirt is an editor autosave later PRs rewrote (carameli's `layoutConfig.ts`) | the dirt adds a test, a fixture, or a script -- something no autosave produces |
| commits landed under another PR and the tree is otherwise clean | you cannot tell -- a PR nobody merges costs less than work nobody can recover |

Never resolve a doubt by leaving the box: that is the state this skill exists to end.

## 4. Act

**Ship**, in one command -- it cuts a fresh `agent/…` branch, rebases the work onto
`origin/<default>` (autostashing the dirt), records the new branch in the box's lease,
commits, pushes and opens the PR:

```bash
python scripts/worktree.py provision <box> --yes
python scripts/worktree.py rescue <box> --ship --message "<subject>" --yes --workspace <workspace file>
```

Provision first: the commit runs the project's pre-commit gate, which needs the box's
venv. Prepend the box's `.venv/Scripts` to `PATH` if `detect-secrets` hangs -- the
machine-wide copy does. Give `--message` a real subject once you have read the diff; the
default is a mechanical `rescue(<topic>): …` line and the PR body says nothing reviewed
it. A rescue that stops on a conflict rolls the box back to its old branch and says so;
resolve it by hand in the box and re-run.

**Delete**:

```bash
python scripts/worktree.py reap <box> --force --yes --workspace <workspace file>
```

`--force` is what a stranded box needs -- `reap` refuses a dirty box on purpose, and the
refusal is right for every box except the ones you have just read. An unregistered clone
has no lease to reap: `git -C <project> worktree remove --force <dir>` if it is a
worktree of the checkout, else delete the directory.

## 5. Report

One table, one row per box you touched:

| Box | Age | What it held | Decision | Result |
| --- | --- | --- | --- | --- |

*Result* is the PR URL or `reaped`. Then re-run step 1: the skill is finished when it
prints no stranded row, and a box you decided to leave is a row in the report with the
reason, not an omission.

## Not evaluated headless

This skill reads live boxes, live diffs and live PRs on one workstation; there is no
fixture that reproduces a stranded worktree with its lease, and a headless run would need
a GitHub remote to open PRs against. Its mechanics are tested where they live --
`tests/test_worktree.py` covers `stranded`, `rescue_plan`, `apply_rescue` and the
`rescue --ship` path -- and the exclusion is recorded here per `.claude/rules/authoring.md`.
