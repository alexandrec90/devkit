---
description: Baseline engineering policy shared by every devkit project — test coverage, script conventions, the vendored harness seam, and the instruction-feedback loop
---

# Rule: Baseline engineering policy

Deliberately **unscoped** (no `paths:`) — this is the small set of rules that hold
everywhere, so there is no glob that should exempt a file from them.

**This file is vendored from devkit and is byte-identical in every project.** It is in
`sync-devkit.py`'s `MANIFEST`, so a local edit is reported as drift by the PR gate
rather than quietly becoming this project's private opinion. That is the point: this
policy used to live inline in each repo, was copied forward by hand, and drifted. To
change it, change it here and let projects `--pull`.

A project's `CLAUDE.md` should **point at this file, not restate it.** A restatement is
a fork: it looks authoritative, it is not gated, and the two copies disagree the first
time either is edited.

## Testing

Every code change must include tests in the same commit. Every endpoint and every
testable unit of logic must have test coverage — gaps are not acceptable. If you add
or touch something that has no test, write the test in the same commit even if the
logic itself didn't change.

- **New unit of logic:** cover the happy path, the error cases, and the edge cases.
- **Bug fix:** write the regression test first, and watch it fail before you fix it. A
  regression test that has never failed is asserting the wrong thing.
- **Reversion check:** before declaring a change complete, identify which test would
  fail if the changed behavior were reverted. If no test would fail, the behavior is
  not covered yet.
- **Coverage floors are ratchets:** when a project enforces a minimum coverage floor,
  never lower it merely to make a change pass.
- **Run targeted tests** to verify a change — the module you touched — plus the
  linter. Leave full-suite runs to CI: they are slow, and a fresh-venv full run
  surfaces version-skew failures that have nothing to do with your change.
- **Fix failures in the code, not in the assertion.** Relaxing an assertion to get
  green deletes the only evidence that something is wrong.
- A skipped or `xfail` test carries a linked issue or a one-line reason in the marker.

> If the local toolchain or stack isn't available, still write the required tests in
> the same change and leave execution to CI. "I couldn't run it" is a reason to defer
> the run, never a reason to skip writing it.

Instruction files — `CLAUDE.md`, `.claude/rules/*`, `.claude/skills/*` — are covered by
this same mandate. See `.claude/rules/authoring.md`.

## Claude Code's Bash calls: a short blocklist, not a proof obligation

`scripts/hooks/enforce-capped-bash.py` is a PreToolUse gate, and it blocks exactly one
thing: a statement whose output grows with the **repository** rather than with the
command you wrote. That list is closed -- `ls`, `cat`, `find`, `tree`, `du`, `env`,
`git status`, an uncounted `git log`, and a raw `git diff`/`git show`.

**Everything else runs uncapped, and wrapping it is a mistake.** A `grep`, a `python -c`,
a test run, a `curl`, a heredoc: issue them bare. A session that routes every call through
the wrapper by reflex pays visible indirection for no second bound, and that has happened
here at scale.

Any of three spellings takes a named command off the list: a pipe into `head -c N`,
`tail -c N`, `wc -l` or `grep -c <pat>`, which **masks the exit code, including a
background task's completion status**; `<cmd> > <file>`, the strongest bound, whose
output never enters context; or `python3 scripts/hooks/invoke-capped.py --command
"<cmd>"`, which keeps a head *and* a tail window and preserves the exit code. The
wrapper runs through the platform shell -- **`cmd.exe` on Windows** -- so heredocs,
single-quoted paths and escaped alternation do not survive it; pipe into `head`/`tail`
for those, and prefer the wrapper for test and lint runs, where the summary at the end
is the part worth keeping.

For `ls`, `cat` and `find` the better answer is usually not a cap at all: the Glob, Read
and Grep tools cost no subprocess, no cap, and page rather than dump.

**The unconditional bound is `BASH_MAX_OUTPUT_LENGTH`**, set in `.claude/settings.json`.
It truncates bytes that already exist rather than predicting bytes that might, so it
cannot false-positive and needs no grammar. A project generated before this was added
should add the `env` entry to its own `settings.json`; that file is not vendored, so
`sync-devkit.py --pull` will not do it for you.

**Codex never sees this gate.** `scripts/sync-codex-hooks.py` omits it from
`.codex/hooks.json`, and Codex's shell tool caps captured output before it reaches model
context. Issue commands there directly -- including the nine.

**If this gate blocks something that is not one of the nine, that is a defect in it:**
report it with the exact command, per the feedback-loop guardrail at the foot of this
file. Never rewrite a correct command to satisfy it. Why the gate is a blocklist rather
than a proof obligation, and what preemptive wrapping has cost, are in
[`.claude/engineering-evidence.md`](../engineering-evidence.md).

## Waiting on a CI gate: one blocking call, not a poll loop

**Spell the wait as a single call that blocks**, backgrounded so the harness re-invokes
you when it exits instead of holding a turn open:

```bash
gh pr checks <N> --watch --fail-fast      # with run_in_background: true
```

`--watch` returns only once every check has settled, so N polls collapse into 1 call plus
the completion notification. Backgrounding is the half that is easy to drop: a gate
routinely outruns the Bash tool's ten-minute ceiling, and a foreground `--watch` that
times out has become a poll loop again with the timeout as its interval.

Two things this does **not** condemn: **diagnosing a failure** (`gh run view
--log-failed` and the greps after it are the work itself, not waiting -- send them to a
file where the volume warrants it), and **asking once** (a single `gh pr checks` is
often the right answer; the waste begins at the *second* identical poll). What polling
costs when it does, and the two PRs behind the paragraph below, are in
[`.claude/engineering-evidence.md`](../engineering-evidence.md).

**"No checks reported" has two causes and they need opposite responses**: a gate that
has not started *yet*, and one that will **never** start because a `CONFLICTING` PR has
no merge ref for GitHub to build a run against. Ask the one question that separates
them, once, after a push:

```bash
gh pr view <N> --json mergeStateStatus,statusCheckRollup
```

`CONFLICTING` means merge `origin/<default>` and push — the gate starts on the next
commit. `BLOCKED`/`UNSTABLE`/`CLEAN` mean the run exists and `--watch` is the right call.
`UNKNOWN` means GitHub has not finished computing mergeability yet, which is the ordinary
answer in the seconds after a push and says nothing either way — `--watch` covers it.

When you get the message anyway, tell the two apart by **how long the call took, not by
what it said**: a `--watch` back in about a second never waited for anything, so re-issue
it once; a `CONFLICTING` PR gives the same message and does not improve on a retry.

When the gate will outlast anything useful you could do meanwhile, the cheapest correct
move is to stop: report that the branch is pushed and the gate is running, and let the
result arrive in a fresh session. The same report costs the session floor there, against
six times as much at the tail of a long one.

## Scripts

All scripts under `scripts/` are Python, for cross-environment compatibility (a local
desktop and a CI runner are rarely the same OS).

- **Expose pure importable functions** guarded by `if __name__ == '__main__'`, so the
  logic can be tested without spawning a subprocess.
- **Every new script ships with its tests in the same change.**
- **Hook scripts (`scripts/hooks/`) are stdlib only** — no third-party imports. Hooks
  run before the virtualenv is active, so an import of anything installed is a crash
  in the one context that cannot report it well.
- **Side effects live behind `main()`**, never at import time: the test suite imports
  these modules.

### Failure artifacts — fix from a file, not from the terminal

Any task or script whose failures an agent is expected to act on must persist those
failures to a **parseable artifact file** under `logs/`. Never rely on streamed
terminal output — it scrolls away and buries the signal. Keep the terminal to a status
line plus the artifact path, and put everything needed to diagnose in the file. Write
the artifact on failure too, not only on success, and overwrite it per run.

## Lint policy

### What is on, and why

Lint exists to catch **correctness and security** problems — the ones a human reviewer
reads past. Style and formatting are not judgement calls worth an agent's turn: a
formatter settles them, in place, with no discussion — `ruff format` runs on every edit
via the `lint-fix.py` PostToolUse hook and again in CI, so line length, quote style and
import order never reach a review.

So: **on** for correctness, security and resource-handling; **off** for anything a
formatter can decide. Which selectors that puts on each side is in
[`.claude/engineering-evidence.md`](../engineering-evidence.md). The split has a
practical consequence worth stating: a lint rule that fires on something a formatter
would fix is misconfigured, not useful. Turn it off rather than teaching everyone to
ignore it.

### Rule families are how cosmetic rules get in

**Adding a family prefix to `select` enables every member, including the cosmetic
ones**, so when adding a family, **read its members and ignore the cosmetic ones in the
same change**. A rule already exempted in two or three directories is not a rule anyone
wants: turn it off globally rather than exempt it a fourth time.

Which selectors are currently off by this policy, the `E501` incident behind the rule,
why a selector never spans linters, and the generated-project test that stops them
drifting back are in
[`.claude/engineering-evidence.md`](../engineering-evidence.md) — read it before editing
any `select` or `ignore` list.

### Never silence a finding without naming the reason

`# noqa`, `# type: ignore`, `# nosec`, `eslint-disable` — each one is a claim that the
tool is wrong *here*. Write the claim down:

```python
result = subprocess.run(cmd, shell=True)  # noqa: S602 - agent-supplied tooling, not input
```

A bare `# noqa` is indistinguishable from a bare "I gave up", and the next agent
cannot tell which it was. Prefer the rule-specific form (`# noqa: S602`, not `# noqa`)
so the suppression stops applying the moment a *different* problem appears on that
line.

### When a linter is wrong: fix the producer, or escalate

There is no third option, and in particular **skipping is not one**.

1. **Fix the producer.** The finding is usually right about something even when it is
   wrong about the fix. Change the code so the rule has nothing to say.
2. **Suppress narrowly, with the reason**, per the section above — when the rule is
   genuinely inapplicable to this line.
3. **Report to the user with concrete options** — when neither of the above is honest.
   Say what the rule wants, why it does not fit, and what the alternatives cost.

**Never skip a failing check, and never describe an error as "cosmetic", "harmless",
or "pre-existing" to justify leaving it.** An error message is either actionable or it
is noise that must be removed at the source; deciding it is ignorable is the one move
that is always wrong, because it trains everyone downstream to ignore the next one too.
The same applies to tests: a failing test gets fixed or reported, never `skip`ped,
`xfail`ed, or deleted to make a run green.

If a check is genuinely obsolete, delete the check — deliberately, in its own change,
with the reason in the commit message. That is a different act from ignoring it.

## The vendored agent harness

The hook scripts, this rule, and the shared skills are **vendored from devkit, which is
the source of truth**. Each project commits its own copy, so a fresh clone gets
everything with no submodule and no install step.

- Everything project-specific lives in `.devkit.toml`, read by
  `scripts/hooks/harness_config.py`. **Never hard-code project specifics in a vendored
  file**: a new behaviour gets a manifest field and a default, not an `if project ==`
  branch, and not a paragraph that names one repo's paths.
- `python scripts/sync-devkit.py --check` fails on drift, `--pull` adopts upstream,
  `--push` sends a change authored here back up. `DEVKIT_VERSION` records which
  upstream commit the vendored copy corresponds to.
- **`$DEVKIT_DIR` unset means there is nothing to compare against, and the stamp
  decides what that is worth** — clean before adoption, a failure once `DEVKIT_VERSION`
  exists.
- **An operator may switch the harness off** — `DEVKIT_HOOKS_OFF`, which cannot reach
  the tier that puts an agent edit on a task branch.
- A vendored script may depend on a file the project owns (`lint-all.py`,
  `run-tests.py`), and a missing one is a silent skip by design. That, the stamp rule,
  the switch's values, and the drift check for a machine with no devkit clone are all in
  [`.claude/engineering-evidence.md`](../engineering-evidence.md).

## Guardrail: the instruction-file feedback loop

If an instruction in a skill, a rule, or a `CLAUDE.md` sent you into a dead end or a
wasted operation — or a mistake you made would have been prevented by one that isn't
there — flag it in your report with the file, the line, and a proposed edit.

**Never silently work around a bad instruction.** Working around it fixes your current
turn and leaves the next agent to hit the same wall; the instruction files only improve
if the failures they cause are reported as defects in them.

Give the flag a durable copy too:

```bash
python scripts/hooks/report-harness-defect.py --message "<what went wrong>" --command "<the exact command, when one triggered it>"
```

appends it to this machine's central harness-events ledger, where a devkit-scoped
session reads it without being handed your chat. It complements the flag in your reply
rather than replacing it — the user still has to see it — and on a machine with no
`$DEVKIT_DIR` it says so and exits 0.

When the defect is in the **vendored harness** rather than in prose, run
`python scripts/sync-devkit.py --check` first and put its answer, with `DEVKIT_VERSION`,
in the report: this copy is routinely weeks of fixes behind devkit, and why that decides
whether a report can be triaged at all is in
[`.claude/engineering-evidence.md`](../engineering-evidence.md). An old copy is still
worth reporting once you know that is what it is — never a reason to route around a hook.
