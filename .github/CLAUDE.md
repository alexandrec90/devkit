# .github/

The CI surface every project has, and which tier each file belongs to. The root
[`CLAUDE.md`](../CLAUDE.md) carries the repo-wide decisions;
[`scripts/CLAUDE.md`](../scripts/CLAUDE.md) carries the vendoring rules these files are
an application of.

## Five files, and the tier is decided by whether the *content* varies

| File | Tier | Why |
| --- | --- | --- |
| `.github/workflows/dependabot-automerge.yml` | vendored | nothing in it varies |
| `.github/workflows/scheduled-failure-issue.yml` | vendored | same; the assignee is read at run time |
| `.github/actions/setup-python-env/action.yml` | template | how a project installs is the project's |
| `.github/workflows/pr-gate.yml` | template | its jobs are the project's |
| `.github/workflows/nightly.yml` | template | same, plus the tiers too slow to gate on |
| `.github/dependabot.yml` | template | names the ecosystems this project ships |

The two vendored ones have no per-project value left: each waits on a workflow by a title
every project shares (`PR Gate`, `Nightly`) and names nothing else about the repo it runs
in. That is also the reporter's ceiling, and it is why it has a second job — see the last
section. The gate cannot be — its jobs are the project's own services, migrations and frontend
tier, and the largest consumer's five-job gate is what a shared one would have to delete
or exempt. `scripts/hooks/tests/test_ci_workflow_contract.py` is vendored alongside them
and requires **all five to exist**, plus the settings that make an unattended run safe:
a top-level `permissions:` block, a `concurrency:` group, `cancel-in-progress: false` on
anything scheduled, and no action pinned to a mutable ref.

That test exists because **`templates/` cannot notice an absence** — a one-shot copy
cannot report that a project never received a file, and no such gap is visible from
inside the repo that has it; its module docstring carries what was missing where when the
contract was written. Adding a required file therefore has a cost the vendored tier does
not: an existing project's next `--pull` gets the *requirement* and not the render, and
goes red until someone writes the file. That is intended, and it is why the required set
is small and every entry has to earn its place.

## The move that has actually gone wrong is vendoring the setup action

`.github/actions/setup-python-env/action.yml` was vendored once, on the argument that
its one variable — the interpreter version — had moved to the caller. **That was wrong,
and it is a template again.** Two consumers disproved it on the first pull that reached
them, and both failures were invisible until CI:

- One opens with a step cloning a private sibling repo that its editable path
  dependency points at. The vendored copy deleted the step, and every job died on a
  missing distribution before running a single check.
- The other does not use the same installer at all. It installs compiled locks, pins
  the installer itself to the version in that lock, and takes an input its mutation job
  passes — none of which the vendored copy had.

The lesson is worth more than the file: what varies between projects is not *which
interpreter* they install, it is **how they install**, and that is never shared.
`test_setup_action_template_matches_devkits` holds devkit's own copy and the template
together the way `notify.py` is held, and `test_the_setup_action_is_not_vendored` is
the ratchet against re-adding it.

## Why a nightly, when a gate already runs everything

A gate fires on a change, so it cannot see the failures that arrive without one: a
dependency published inside the project's version bounds, a runner image bump, an
expired credential, a test that is flaky rather than broken. devkit's own nightly adds a
second job — `unlocked-toolchain`, which resolves the `dev` group off-lock — because
devkit's dev group *is* its product surface: a linter release that breaks `lint-all.py`
breaks it in every consumer, and the lock hides that until the weekly dependency PR.

Deliberately **not** normalized, each because it encodes one project's economics rather
than a shared practice: mutation testing, migration round-trips, a paid provider tier's
smoke suite, lock repair for a scheme no other project uses, and an agent-fixer loop.

## A workflow run is the least visible artifact GitHub has

`scheduled-failure-issue.yml` fixes that: the dashboards aggregate issues and PRs and
**nothing else**, so a failing nightly and one that silently stopped being scheduled read
the same. Its docstring holds the three properties a change must keep; `assignees` in
`.github/dependabot.yml` is the same argument applied to a bot PR.

### Being vendored is what made it watch one workflow, and why it now sweeps

`on.workflow_run` selects the workflows it watches **by title**, and a title list is
exactly the per-project value this file may not carry — so it shipped watching `Nightly`
and nothing else. That is not a small gap. carameli had grown two more scheduled
workflows by the time anyone looked, and one of them had failed three consecutive Sundays
with no issue filed, while its nightly tracker sat correctly closed. Nothing was red
anywhere; the reporter was working exactly as written.

The second job, `sweep`, runs on the file's own cron and **enumerates instead of
subscribing**: every workflow in `.github/workflows/` declaring a `schedule:`, reconciled
against its latest run on the default branch. There is no list to keep current, which is
the only form this can take in a file that may not name a project.

It also reaches what the event half cannot see at all — a workflow GitHub **disabled**
after 60 days of repository inactivity. That one emits no completion event, so the
event-driven reporter goes quiet precisely when there is something to report. The
workflows API states it in `state`, so the sweep reads it rather than guessing from run
timestamps, and `disabled_manually` is deliberately excluded: that switch was flipped on
purpose.

Two consequences worth keeping. Both jobs write the **same** trackers — same titles, same
dedup — so running both is idempotent, not duplicative; and the sweep cannot judge
itself, because its own latest run is the one executing. That last one is inherent rather
than fixable: nothing inside a repository can report that its own reporter is broken.
