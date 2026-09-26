---
name: supervise-fix-pass
description: Run the fix pass for a few iterations and audit it against its contract -- nothing left for a person, no friction or wasted turn that is not fixed or filed -- fixing what it finds in devkit as it goes.
disable-model-invocation: true
argument-hint: 'Optional: how many iterations (default 3), or "plan" to rehearse only'
---

# Supervise the fix pass

> Depends on `gh` being authenticated and `claude` on PATH: the pass dispatches real
> sessions and pushes real branches.

Devkit-only, like `/triage-harness`. The fix pass (`scripts/fix-pass.py`) holds one
contract: **every observation ends green, in flight, or filed** on the harness-defect
ledger -- nothing ends at a person, and no friction or wasted agent turn goes unrecorded.
This skill is how that contract is checked against the real machine, and how the pass is
repaired when it breaks it. You are the harness's own supervisor: **every defect you find
is yours to fix in this worktree, now**, with a regression test. Filing is only for what
needs something outside the repository.

`scripts/fix-pass-supervise.py` does the mechanical half -- runs the iterations, waits
for dispatched sessions, checks the record and the ledgers, measures each session -- so
this session spends its turns on judgement, not on waiting or on commands a script runs.

## 1. Rehearse before anything is sent

```bash
python scripts/fix-pass-supervise.py --mode plan --iterations 1
```

Read `logs/fix-pass-supervise.log`, then the `record` in `logs/fix-pass-supervise.json`.
A `plan` pass does nothing, so this is where a dispatch that would do harm is caught.
Before step 2, answer each of these from the record, and fix the pass for any "yes":

- Would it **ship an intent whose work already landed** (its PR merged, its branch gone
  from origin)? Shipping that recreates a deleted branch or opens an empty PR.
- Would it **send a session at something already fixed**, superseded, or in flight?
- Is anything **held or skipped without a reason a script is tracking**?
- Is any **filed** line noise -- a detector that fired on text rather than on an event?
  Fix the detector in `scripts/session_friction.py`; a noisy finding costs the devkit
  session a verification every time it recurs.

## 2. Run the iterations, without watching them

```bash
python scripts/fix-pass-supervise.py --iterations 3      # run_in_background: true
```

Start it in the background and wait for its completion notice -- expect hours, since
each iteration waits for its sessions. **Do not poll it, tail it or sleep on it**: each
check you make while it runs is a turn spent on a verdict the report gives in one read.

It runs from this worktree, so your fixes are live on the next iteration -- and the
sessions it sends cut their branches from `main`, which has none of them yet. Until this
branch merges, expect them to report that `main`'s tools disagree with the evidence the
pass gave them (a finding kind `main`'s `harness_triage.py` does not know, say). That is
skew, not a defect to fix twice; getting the branch merged is the fix.

The pass ships this worktree's own intent like any other, and sends fixers at its PR.
A live session in a branch's tree holds fixers for that branch, so while you work here
its fixes wait for you -- that is the pass keeping two sessions out of one tree.

## 3. Audit what came back

The script exits 1 when any iteration broke the contract. For every `VIOLATION`:

1. **Reproduce it from the report** -- the record line, the transcript path, the finding.
2. **Fix the cause in devkit** -- the pass, a prompt, a detector, a vendored script --
   with a test that fails without the fix. A violation is a pass defect by definition.
3. Only when the fix needs something outside the repository (a credential, admin
   rights, a paid service), file it: `python scripts/hooks/report-harness-defect.py`.

Then read what the script cannot judge. For **each dispatched session** in the report,
read its transcript end to end -- the `readable` field is it rendered as audit lines
under `logs/fix-pass-supervise/`; hand batches of them to parallel subagents when they
run past a few hundred KB, each told the iteration's `filed` lines so it reports only
what was missed -- and list every turn it lost:
a wrong path, missing evidence, an instruction that misled it, a check it could not
run, a question it had to answer that the prompt should have. Compare that list with
the session's `friction` and with the iteration's `filed` lines. **Every loss the
detectors missed is a detector gap**: add the pattern to `session_friction.py` with a
test built from the transcript's own lines, or -- when no pattern could see it -- fix
the prompt or the evidence that caused it.

Finally, read each `filed` line once more: a finding the devkit session cannot act on
(vague, duplicated, mis-grouped) is itself a defect in how it was filed. And read what
each session *fixed*: a repair to one tree, box or run whose cause is left standing (a
`uv sync` by hand where the provisioner should have run) is a fix that will recur --
fix the cause yourself. A `RECURRED` group in `logs/harness-triage.log` is one that
already did.

## 4. Repeat until an iteration is clean

Before re-running, run what the PR gate will run on what you changed: ruff and mypy,
`scripts/hooks/structure_check.py`, `scripts/hooks/untested_symbols.py`, the tests of
every module you touched, the contract tests that read every module
(`tests/test_test_contract.py`, `tests/test_doc_claims.py`), and
`scripts/posix-rehearsal.py` -- the gate runs on Linux, where a Windows path in a test
reads differently. A red gate on this branch
costs the next iteration a fixer and a round trip -- the first supervision paid that
twice. Then run step 2 again. Stop when an iteration comes back with **no violation,
no unfiled friction and no noisy finding** -- or after three rounds, in which case the
report's headline is what is still breaking and why.

## Merging, and watching spend

**You may merge.** A fixer's PR merges itself once green (`automerge`, from a tree the
pass cut). Your own supervision branch, and a fixer PR the loop is waiting on, you merge
yourself once its gate is green: `gh pr merge <n> --squash --delete-branch`. Resolve its
conflicts with `origin/main` yourself when it has any -- you are the one who knows what
it changes. A PR from a person's own session is theirs: never merge it.

**The pass has no daily cap on sessions; you are the cap.** The report's `spend:`
violations are the signal -- an iteration sending more than `SPEND_SESSIONS`, a session
past `SPEND_CALLS` calls or `SPEND_TOKENS` output tokens -- and the run brakes itself
past `--brake-tokens` in total. On any of those, stop and find out what is being paid
for before running again: the same problem re-sent at a changing signature, a fixer
looping on a check it cannot pass, a backlog refilling as fast as it drains. A spend
problem is fixed in the pass like any other defect -- not by adding a cap back.

Memory is the one limit the machine sets regardless: `fix_send` holds a session the
free memory cannot take (`held for memory` in the record), and the next pass sends it.
A background fixer loads no MCP server for the same reason. Holds on every iteration
mean something else is eating the memory -- find it (`claude agents` for finished
sessions still alive, `docker stats` for stacks) rather than lowering the floor.

## 5. Your own turns

This session is held to the same contract. Any turn *you* lost to the harness -- a
refusal, a missing tool, a wrong instruction in this file -- goes in
`logs/friction.md` before you ship, and a wrong instruction here is fixed here.

**Decide, as a fixer must.** A finding that comes back as a choice -- a stuck session's
question, a design fork in a project -- is yours to settle: the option you would
recommend is the decision, so carry it out and say why in the intent. Ending a report on
"your call" is the same lost turn the `handed-back` detector files against a fixer; a
supervisor once relayed a fixer's three-option question and recommended one, and the
answer was to have done it.

## Reporting

Ship with the ship skill. In the reply, give per iteration: what the pass shipped,
sent and filed; each violation and what fixed it; each session's cost (calls, failed
calls) and the friction found in it; and what, if anything, is still open.
