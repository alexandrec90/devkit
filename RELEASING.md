# Releasing devkit

One bad tag here reddens every consumer, and one *missing* tag quietly breaks every
project generated after the feature it was supposed to carry. Both have happened. This
is the checklist that prevents them.

## Why a tag is not optional

Three things pin a devkit **tag**, never a branch:

| Pin | Where | Consequence of a stale tag |
| --- | --- | --- |
| `rev:` | a consumer's `.pre-commit-config.yaml` | pre-commit resolves hook ids **strictly** — against a tag that predates a hook, the consumer's next commit aborts with "hook not found" |
| `ref:` | a consumer's PR gate drift job | the drift check compares the vendored tree against the wrong revision |
| `FALLBACK_DEVKIT_REF` | `scripts/new-project.py` | every newly generated project pins it into both files above |

`new-project.py` resolves the ref as `latest_devkit_tag() or FALLBACK_DEVKIT_REF`. Both
paths normally return the same thing, so **a feature that is not tagged does not exist
as far as a generated project is concerned**, no matter that it is on `main`.

The same holds for a project that already exists: `upgrade-project.py` pulls from a
worktree at the newest tag, so an untagged MANIFEST change is invisible to a consumer
that has just upgraded — and to everyone reading its green gate. That run now names the
vendored files main carries and the tag does not (`unreleased_vendored_changes`); when it
does, the fix is a release, not a re-run.

## Use the workflow

`.github/workflows/release.yml` does all of this. It is `workflow_dispatch` with a
`version` and a `phase`, and it runs in two passes because the ordering below is not
optional — the fallback bump has to be **committed before the tag exists**.

1. **Land the work on `main`** and confirm CI is green, `generated-project` job
   included. That job renders a project of every preset and runs its suites — it is
   the only check that catches vendored-tier coupling.

2. **Decide the version.** Bump the minor for new vendored files, published
   pre-commit hooks, or a manifest field; bump the patch for fixes to existing ones.

3. **Run the workflow with `phase=prepare`.** It bumps `FALLBACK_DEVKIT_REF`, pushes
   `release/vX.Y.Z`, and opens its PR.

   ```bash
   gh workflow run release.yml -f version=vX.Y.Z -f phase=prepare
   ```

   > **Opening the PR is always yours.** The workflow pushes the branch and puts the
   > exact `gh pr create` command in its **step summary**; paste it. It deliberately
   > never opens the PR itself: a PR opened with `GITHUB_TOKEN` triggers no workflow
   > run, so it would arrive with no PR Gate — and step 4 below is *reading that
   > gate's* `test-failures.log`. The v0.9.0 release proved this the hard way: with
   > *Allow GitHub Actions to create and approve pull requests* on, the workflow's own
   > `gh pr create` succeeded and the release PR sat with `no checks reported` until a
   > human closed and reopened it to get a real-actor `pull_request` event.

4. **Merge that PR — expecting exactly one red test.**

   > `test_fallback_devkit_ref_tracks_the_newest_tag` **will fail on the release PR**.
   > That is the check working, not a problem to route around: it compares the constant
   > against `git describe --tags`, and the tag does not exist yet. Do not "fix" it by
   > reverting the bump.
   >
   > Nothing else may be red. Read the uploaded `test-failures.log` and confirm that
   > this is the only failure before merging — the terminal shows a status line, not
   > the failures.

5. **Run the workflow with `phase=tag`.**

   ```bash
   gh workflow run release.yml -f version=vX.Y.Z -f phase=tag
   ```

   It stages the tag **locally**, runs the full suite and lint against that exact
   commit, and only then pushes it. That order is the point: a tag is what every
   consumer pins, so it must never name a commit whose suite was red. It then proves
   the tag carries the published pre-commit hooks.

The workflow refuses a version that already exists, so a double-run is a safe no-op
failure rather than a moved tag.

### The pre-push policy refuses a hand-pushed tag

`git push origin vX.Y.Z` from a workstation is **blocked** by
`git_policy.release_tag_decision`, installed globally by `install-git-policy.py`. Use
`phase=tag`; it is the only thing that runs lint and the suite against the commit *as
tagged* before publishing.

This paragraph used to be the whole guard, and it did not hold. It read "never push the
tag before the commit that bumps the fallback", and on 2026-08-16 `v0.9.0` was pushed by
hand six minutes after its prepare run, before #107 had merged — so the published tag
named a commit whose `FALLBACK_DEVKIT_REF` still said `v0.8.0` and whose vendored tree
already differed from `main` by one MANIFEST file. Nothing was red: the cost was a
drift-red gate waiting in every consumer that adopted it, plus three open PRs failing
`test_fallback_devkit_ref_tracks_the_newest_tag` until the bump landed. `v0.9.1` exists
because moving the published tag would have bypassed the gate a second time.

To push one by hand anyway — recovering a half-finished release, or a repo with no
workflow — the escape hatch is `DEVKIT_SKIP_BRANCH_POLICY=1`, and the ordering it leaves
you responsible for is: bump `FALLBACK_DEVKIT_REF` in `scripts/new-project.py`, land that
commit, and only then tag. Re-run the suite afterwards; the fallback test is green once
`git describe --tags` can see the tag. Deleting a tag is not blocked — that is the
recovery path for one already published.

## Verify the tag serves what it claims

A tag is only useful if it carries the channel a consumer will ask it for. The
`phase=tag` run already asserts the first half of this (`The tag carries the published
hook channel`); the end-to-end half is still worth doing by hand after a release that
touched the pre-commit channel:

```bash
# The published pre-commit hooks must be IN the tagged tree.
git ls-tree -r --name-only vX.Y.Z | grep -E 'pre-commit-hooks|precommit/'

# End to end: a fresh project's commit gate must actually run.
python scripts/new-project.py probe_tag --preset bare --parent /tmp/gen \
  --no-remote --no-worktree --no-register --yes
cd /tmp/gen/probe_tag && pre-commit run --all-files
```

> **`--no-register` is not optional here.** Without it the probe is written into
> `alex-projects.code-workspace` — `folders` plus every scope picker — and stays
> there after you delete the directory, leaving `sweep.py` with a registered checkout
> under a temp path. The flag exists for exactly this command.

That last command is the acceptance test. It must not print "hook not found", and
`new-project.py` must not print the unpublished-channel warning
(`_warn_if_pre_commit_channel_is_unpublished`).

> The **executable bit** only fails here. The published hooks are `language: script`,
> so pre-commit execs them directly; a missing `chmod +x` fails on a consumer's
> machine, at commit time, after the tag is cut. A test guards it, but this run is the
> end-to-end confirmation.

## Tell adopters what changed

Adopters find out by running `sync-devkit.py --pull`, and **the commit message is the
only changelog they get.** When a change alters vendored behaviour, say so there.

## After the tag: the consumers

Each consuming repo needs, ideally in the same commit as its `--pull`:

- `.github/workflows/pr-gate.yml` — bump the drift job's `ref:`
- `.pre-commit-config.yaml` — bump the devkit `rev:` (or `pre-commit autoupdate`)

Bumping the `ref:` alone, without pulling, turns a green gate red by design: the drift
check compares the vendored tree against the checked-out ref.

### When a release moves a file from `templates/` into the MANIFEST

That is the one change adopters cannot review as a diff of *devkit*: the file already
exists in their repo, project-owned, and the first `--pull` after the release replaces
it wholesale. Say so in the commit message, and check the two things that make the
replacement safe before tagging:

- **Every consumer already satisfies whatever the vendored copy assumes.**
  `dependabot-automerge.yml` waits on a workflow titled `PR Gate`; a consumer whose
  gate is titled anything else gets a merge job that is inert rather than red.
- **Whatever local content it overwrites is genuinely disposable.** carameli's copy
  carried a note about `dependabot-lock-repair.yml` bypassing the merge job — true, and
  worth re-adding to that repo's own workflow rather than to the shared one.
