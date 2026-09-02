# The Codex translation tier

Split out of [`scripts/CLAUDE.md`](../CLAUDE.md) when that file reached the 500-line
ceiling. It is a `CLAUDE.md` rather than a rule on purpose: **Codex reads every
`CLAUDE.md` and reads straight past `.claude/rules/`**, and a Codex session working on
this tier is the one that can least afford to miss it.

Most of it is about one seam — Claude-shaped hook wiring being made to run under
Codex. The scripts are `sync-codex-hooks.py`, `sync-codex-context.py`,
`codex-hook-adapter.py`, `codex-session-start.py`, and devkit-only
`extract-codex-schema.py`. The section below applies to every hook in this tree,
whichever runtime it is being made to run under.

## Changing a hook changes the thing that is running you

devkit wires the hooks it ships on itself, and that is not decoration: a hook that only
runs downstream is a hook nobody tests. devkit shipped a `lint-fix.py` that formats on
every edit and then needed a dedicated commit (`4fbda17`) to clean up the format drift
that had accumulated in the one repo where the hook was not wired.

The consequence for a session editing one here is immediate. A syntax error in
`stop.py` breaks the current session's Stop; a bad `lint-fix.py` blocks every subsequent
edit. Both fail loudly, which is the point — but run `python scripts/run-tests.py` and
this tree's own suite (`python -m pytest scripts/hooks/tests/ -q`) before assuming a
change is good.

## Vendoring a generator does not vendor its output

`.codex/hooks.json` is written by `sync-codex-hooks.py` from the project's own
`.claude/settings.json`, so the script is in the `MANIFEST` and the file it produces
cannot be — its content is per-project. That asymmetry is a **third** delivery path,
and it was the one with no gate on it: a `--pull` adopts a new generator and changes
nothing about what Codex actually runs, because the file Codex reads was written by the
generator before it.

It cost months. `REDUNDANT_HANDLERS` stopped porting the Claude-only Bash cap into Codex
the day it landed, and Codex sessions in every already-generated project went on being
blocked by it — with the block's own suggested remedy, `invoke-capped.py`, being a
wrapper the session then applied to every command after it. Half the shell calls in one
project's Codex sessions were the wrapper. Nothing was red anywhere, because both halves
were individually correct.

So: **anything generated from a vendored script needs a check that regenerates it and
compares.** `sync_devkit.codex_hooks_stale` is that check, `regenerate_codex_hooks` runs
it on `--pull`, and the vendored
`test_the_committed_codex_artifact_matches_the_generator` runs it in a consumer's PR
gate where no `$DEVKIT_DIR` exists. Regenerating on pull is not enough on its own —
a project only pulls when someone asks it to.

## Regenerating proves the wiring, not that the other runtime acts on it

That check compares which handler each event points at. It says nothing about the half
that actually decides an outcome: a ported hook still emits a **Claude-shaped JSON
response**, and Codex validates one against schemas this repo does not own. Every Codex
output schema is `additionalProperties: false`, so a member Codex does not recognise does
not get ignored — it fails validation and takes the hook's *decision* down with it. The
accepted set is per event: `PreToolUse` carries `hookSpecificOutput.permissionDecision`,
`PermissionRequest` carries `hookSpecificOutput.decision` instead, `Stop` carries
neither, and `SessionEnd` publishes no output schema at all.

That contract is not guesswork any more. `codex.exe` embeds its draft-07 hook schemas;
[`extract-codex-schema.py`](../extract-codex-schema.py) — devkit-only, because a consumer
has no reason to re-derive it and most CI runners have no Codex to read — lifts them into
[`codex-hook-schema.json`](codex-hook-schema.json), which **is** vendored, because
[`codex-hook-adapter.py`](codex-hook-adapter.py) reads it at runtime.

The adapter classifies every member of a rc-0 hook's stdout against that snapshot:

| Class | What the adapter does |
| --- | --- |
| portable | passes through untouched |
| **lost** — Codex will not carry it and it is a decision | refuses the call outright, in the hook's own words |
| **dropped** — Codex would reject it and it is decorative | strips it so the rest of the response survives |

Either non-portable class also lands a `codex-translation-gap` on the events ledger, so
a translation that silently stopped meaning what it said becomes an open triage item
instead of a hook that "works". `scripts/hooks/tests/test_codex_translation.py` is the
static half of the same judgement, and it runs in the consumer's own **free** hook-test
task — no CLI, no cost, no Codex installed.

**Re-run `extract-codex-schema.py --check` after a Codex upgrade.** It exits 0 with a
note when no `codex` is on `PATH`, on the same reasoning `sync-devkit.py` applies to an
unset `$DEVKIT_DIR`: with nothing to compare against there is no drift to report.

### Read the extractor's docstring before hand-writing a member list again

The list this replaced was hand-built from observed behaviour, and it was wrong on the
member that matters most: it classified `hookSpecificOutput.updatedInput` as Claude-only.
Codex accepts it on `PreToolUse`. Shipping that guess would have converted
`worktree-guard.py`'s re-aim into a hard **deny** on every Codex edit — a guardrail
turned into a wall, by a fact nobody had checked against a source that was free to read.

`worktree-guard.py` still blocks under `DEVKIT_HOOK_ADAPTER`, and that is now **caution
rather than a known absence**: a schema says what a runtime will accept, not what it will
honour. Nobody has yet watched a live Codex session act on a re-aim, and this hook's
asymmetry says an unverified rewrite is worse than a needless block. The block names the
box, so no work is lost either way. Watch one honour it, and the branch can go.
