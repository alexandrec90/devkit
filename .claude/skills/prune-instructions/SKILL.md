---
name: prune-instructions
description: Cut the recurring token cost of the instruction tier — every CLAUDE.md, rule, skill and stored memory — by moving what a session rarely needs out of the always-loaded tier and deleting what has gone inert. Measurement-first; never edits a vendored file.
---

# Prune the instruction tier

> Depends on `scripts/instruction-budget.py` in this repo and, for the memory pass, on
> `~/.claude/projects/` existing on this machine.

An instruction file is billed on **every API call of every session**, because each call
re-sends the whole conversation. A paragraph written once is paid for thousands of
times. Nothing bills it at the point of writing, so the tier only ever grows, and a
paragraph nobody reads costs exactly what one that saves an hour costs.

This skill spends that budget deliberately. It is **not** a style audit: line counts,
tone and header structure are not what makes an instruction expensive.

## The move is re-tiering, and deletion is the exception

Four tiers, and which one a paragraph sits in is nearly the whole cost:

| Tier | Loaded | What belongs |
| --- | --- | --- |
| **hot** | every session, in full | root `CLAUDE.md`, rules with no `paths:` |
| **lazy** | when a file under it is touched | subtree `CLAUDE.md`, rules with `paths:` |
| **on-demand** | when named or invoked | `SKILL.md` bodies, sibling reference files |
| **gone** | never | deleted |

Moving a section from hot to lazy drops its recurring cost to near zero **and loses no
information**. That is why it is the default remedy and deletion is the exception: it
gets the saving without the risk. Most of the win in a typical repo is here, not in
cutting.

devkit has already made this move once and left the reasoning in place —
`scripts/windowless-jobs.md` was split out of `scripts/CLAUDE.md` because it was "105
lines of measurement and mechanism that a session needs only when it is in that code,
carried in an instruction file every session pays for." Generalise that.

## The one thing that cannot move down

**A lazy tier loads on touch. So any instruction that must fire _before_ the first file
is touched cannot be moved down** — by the time the load triggers, the action it governs
has already happened.

Ask it directly: _if this loaded only after the agent touched a file in that subtree,
would it still have worked?_

- Which checkout an edit may land in → **no**. The write is the thing being governed.
- How to invoke this subtree's test runner → **yes**. You are already there.

This is the test that keeps a re-tiering pass from being a regression. Everything else
about the workspace-routing tier is negotiable; its position is not.

## Keep, move, cut

### Cut — aggressively, no hesitation

- **Restates what a config file already says.** A version, a port, a dependency list, a
  lint selector set. The file moves, the sentence does not. Name the file instead.
- **A fact about another repo's current state.** It has no owner here and no gate.
- **Generic best practice.** "Write clean code", "follow conventions", "be helpful."
- **Describes what the code does** where one Read would show it.
- **Duplicates a parent `CLAUDE.md`** rather than adding to it.
- **Names a path, flag or command that no longer exists.** Verify before cutting —
  `tests/test_doc_claims.py` gates _cited_ paths, not prose that describes behaviour.
- **Instructs what a hook or test now enforces.** Once a gate exists, the gate is the
  instruction; the paragraph is a second, ungated copy of it. Leave a one-line pointer
  to the gate, not the argument for it.

### Move — the default

- **Reference, measurement, mechanism** a session needs only while inside one area →
  sibling `.md` beside the file, linked, **one level deep** (`.claude/rules/authoring.md`).
- **Policy that applies to one subtree** → that subtree's `CLAUDE.md`, or a rule with
  `paths:` scoped to it. Scope to the variant's own directory, never the domain tree.
- **A workflow with a trigger** → a skill.

### Keep hot — however verbose it reads

**A paragraph that names a failure that actually happened, and the invariant it
produced.** This is the class where "missing beats irrelevant" inverts, and the reason
is specific rather than sentimental: this prose is the only record of a bug that has
already been paid for once. Cutting it re-buys the bug.

The workspace `CLAUDE.md` carries the worked example — a bullet documenting that for
months the file asserted the guard could only _block_, so an agent that trusted the
denial "had no way left to read the message it actually got." That paragraph is long,
reads like history, and is load-bearing.

**But scar tissue still moves.** Keeping it is not the same as keeping it hot. If it
survives the fires-before-touch test above, move it to lazy or to a sibling reference —
that satisfies the cost concern with none of the risk. Reserve deletion for the inert
classes above, where there is nothing to re-buy.

## Never edit a vendored file outside devkit

`.claude/rules/engineering.md` and `authoring.md` are byte-compared against upstream. A
cleanup edit to one inside a consuming project lands as **drift in that project's PR
gate** — a red gate someone else has to explain.

`instruction-budget.py` marks these `[vendored: edit in devkit only]`. In a consumer,
report them and stop. In devkit, edit freely: the change reaches consumers through
`sync-devkit.py --pull`, and a change to vendored behaviour belongs in the commit
message, because that message is the only changelog adopters get.

## The memory pass

`~/.claude/projects/<slug>/memory/` — one directory per working directory. Its index
file loads hot; the individual memories are recalled on demand. **Nothing expires any
of it.**

Be aggressive here; the calculus genuinely differs from instruction files. A memory is
cheap to re-learn and is recalled _as authority_, so a stale one does not merely cost
tokens — it actively misleads, and the harness already has to warn that recalled
memories "reflect what was true when written."

- **Delete** when it names a file, flag or command that no longer exists; when it
  records a one-off incident with no forward instruction; or when a later memory
  supersedes it. Verify the referent is gone before cutting — that is one Grep.
- **Keep** machine facts (interpreter quirks, tool behaviour, account limits) and stated
  preferences. These are the ones that are expensive to rediscover and rarely go stale.
- **Merge** near-duplicates into the older file's name, so inbound `[[links]]` survive.
- **Prune the index in the same edit.** It is the hot half of the memory tier; a
  pointer to a deleted memory costs tokens forever and resolves to nothing.
- **Orphaned slugs** — a directory no live checkout's path produces — are pure waste:
  nothing can ever recall them. `--orphans` finds them; delete the whole directory.

## Workflow

1. **Measure first.** Never open a file before the report says it is expensive.

   ```bash
   python scripts/instruction-budget.py --root . --orphans
   ```

   Reads `logs/instruction-budget.log`: totals by tier, hot files broken down by H2
   section, and findings. The section table is the working list — a file's total says
   it is expensive, only the sections say which paragraph to move and where.

2. **Re-tier before you cut.** Work the largest hot sections. For each, apply the
   fires-before-touch test, then move it down a tier. Cut only what falls in the Cut
   list — and where a whole section is inert, say so in the commit message rather than
   letting a large deletion look like an accident.

3. **Leave a pointer where you moved something**, one line, naming the file. A rule
   points at its reference; a root `CLAUDE.md` points at a scoped rule. Never restate:
   a second copy is not drift-checked and the two disagree on the first edit.

4. **Run the memory pass** against the report's stale list.

5. **Re-measure and record the delta.** `hot: N tok/session` before and after belongs in
   the commit message — it is the only evidence the pass did anything.

6. **Update the ratchet.** `tests/test_instruction_budget.py` pins the hot ceiling. Lower
   it to the new figure. It only ever goes down; raising it is how a tier grows back.

## What this skill will not do

- **Rewrite prose for tone.** A formatter settles style; this settles cost.
- **Enforce a line count.** The 500-line cap in `authoring.md` is a separate check, and
  a 400-line hot file can cost more than an 800-line skill nobody invokes.
- **Cut to a quota.** The retired `audit-claude-md` skill told the agent to "highlight at
  least 3 lines that can be pruned" — a quota produces cuts whether or not any are
  warranted, and it is why that skill is in `sync-devkit.py`'s `_RETIRED_CLAUDE_PATHS`.
  If the report is clean, say so and stop.
