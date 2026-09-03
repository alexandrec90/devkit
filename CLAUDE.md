# devkit

The portable agent-coding harness for Claude Code / Codex, and the project generator
that ships it. This repo is the **source of truth**: consuming projects commit a
vendored copy of `scripts/sync-devkit.py`'s `MANIFEST` and pull changes from here.

[`README.md`](README.md) says what each tool is and how to run it, and the code says
what it does today. The instruction tier carries only what neither of those can: the
decisions, and the failures that produced them.

## Where the instruction tier lives

| File | What it governs |
| --- | --- |
| [`.claude/rules/engineering.md`](.claude/rules/engineering.md) | baseline policy: testing, scripts, lint, capped Bash, the vendored harness |
| [`.claude/engineering-evidence.md`](.claude/engineering-evidence.md) | the measurements and incidents behind that policy — loaded only when a pointer sends you |
| [`.claude/rules/authoring.md`](.claude/rules/authoring.md) | writing rules, skills and instruction files |
| [`.claude/rules/vscode-tasks.md`](.claude/rules/vscode-tasks.md) | the workspace task block and its dispatcher |
| [`scripts/CLAUDE.md`](scripts/CLAUDE.md) | the two delivery channels, vendoring, ephemeral boxes, scheduled jobs, loading a module by path |
| [`scripts/hooks/CLAUDE.md`](scripts/hooks/CLAUDE.md) | editing a hook here; the Codex translation tier: ported wiring, the response contract, the adapter |
| [`.github/CLAUDE.md`](.github/CLAUDE.md) | the CI surface every project has |
| [`tests/CLAUDE.md`](tests/CLAUDE.md) | the two test trees, and which one a test belongs in |

The first two are vendored *out* of here, so devkit is also the first place they have to
hold.

**Codex reads every `CLAUDE.md` and reads straight past `.claude/rules/`.** A rule is
therefore only the right home for guidance a Codex session can afford to miss;
`vscode-tasks.md` qualifies because VS Code tasks are a Claude-side workflow, and the
vendored pair does not — moving that policy inline is what `BLOCK_MANIFEST` in
`scripts/sync-devkit.py` exists for. Until it moves, **point at a rule, never restate
it**: a second copy is not drift-checked, and `test_repo_contract.py` fails a
`CLAUDE.md` that paraphrases the vendored clauses.

## The docs are vibe-coded too

Every file here was written by an agent, this one included. Prose is the only artifact
in the repo with no compiler and no test, so a wrong sentence survives in a way wrong
code cannot — and an instruction file is read as *authority*, which is what turns a
stale paragraph into one agent talking the next one out of a correct change. That has
already happened here. Assume it is happening now.

1. **The code decides.** Where a document and the repo disagree, the document is the
   defect. Fix it in the same change as the work that found it; never route around it,
   and never let it veto a change you can otherwise see is right.
2. **Write only what stays true.** A version, a count, a fact about another repo, or a
   restatement of something a config file already states is a claim with no owner — the
   thing moves, the sentence does not. Name the file that owns it instead of copying
   its current value.
3. **`tests/test_doc_claims.py` gates the checkable half** — every cited path exists,
   and instruction prose pins no version — across every `CLAUDE.md`, rule and skill in
   the repo. It cannot tell you a rationale went stale; it does stop the *silent* rot.
   Its docstring owns the rest.

## Nothing but the standard library, by contract

Read the interpreter version and the dev tools off `pyproject.toml`; they are not
worth pinning in prose. What is not readable there is that the empty runtime
dependency list is a **constraint, not a state**: the vendored hooks run before a
virtualenv exists, in a repo devkit does not control, so an import of anything
installed breaks provisioning on exactly the sessions the harness exists to set up.

The same contract is why there is no stack here — no database, no frontend, no
compose file — which is what lets CI run with no service containers and why
`.devkit.toml` turns both of those tiers off.

## devkit runs its own harness

Everything devkit ships to other projects is wired up **here**, on itself: the hooks in
`.claude/settings.json`, the lint and test wrappers they call, the pre-commit gate, the
failure artifacts under `logs/`, and a PR gate titled `PR Gate` like every consumer's.

This is not decoration — a hook that only runs downstream is a hook nobody tests. What
that costs when it lapses, and what it means for a session editing a hook (you are
changing the thing that is running you), is in
[`scripts/hooks/CLAUDE.md`](scripts/hooks/CLAUDE.md).

## The two test trees

`scripts/hooks/tests/` is vendored and must stay project-agnostic; `tests/` is
devkit-only. Which one a test belongs in, and the two gates that assert a test exists
at all, are in [`tests/CLAUDE.md`](tests/CLAUDE.md) — a subtree file, so it loads when
you are writing a test rather than on every call of every session.

## Guardrails

The instruction-file feedback loop lives in `.claude/rules/engineering.md` — report a
rule that sent you into a dead end instead of routing around it.

### One bad commit here reddens every consumer

Generated PR gates pin a devkit **tag**, never `@main`, for this reason. When a change
alters vendored behaviour, say so in the commit message: adopters find out by running
`sync-devkit.py --pull`, and the message is the only changelog they get.

A missing tag is the mirror-image failure and is easier to miss: **an untagged feature
does not exist as far as a generated project is concerned**, however green `main` is.
Which refs a consumer pins, what a late tag broke, and the release checklist are in
[`RELEASING.md`](RELEASING.md).

### The internal names are `devkit`

`.devkit.toml`, `$DEVKIT_DIR`, `DEVKIT_VERSION`, `scripts/sync-devkit.py`, and the
published hook ids `devkit-manifest` / `devkit-hooks-stdlib-only` / `devkit-drift`.
Treat them as fixed: the rename from the `agent-harness` spelling had to be one atomic
change across devkit and every consumer, because `sync-devkit.py` is **itself in the
`MANIFEST`** — it is the very path list the drift check compares by, so a half-applied
rename fails `--check` in whichever repo lands second. The old spelling anywhere is a
miss from that migration, not a deliberate holdout.
