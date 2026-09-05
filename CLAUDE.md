# devkit

The portable agent-coding harness for Claude Code / Codex, and the project generator that
ships it. This repo is the **source of truth**: consuming projects commit a vendored copy
of `scripts/sync-devkit.py`'s `MANIFEST` and pull changes from here.

[`README.md`](README.md) says what each tool is and how to run it, and the code says what
it does today. This tier carries only what neither of those can.

## Where the rest of the tier lives

| File | What it governs |
| --- | --- |
| [`.claude/rules/engineering.md`](.claude/rules/engineering.md) | baseline policy: testing, scripts, lint, the vendored harness |
| [`.claude/rules/authoring.md`](.claude/rules/authoring.md) | writing rules, skills and instruction files |
| [`.claude/rules/vscode-tasks.md`](.claude/rules/vscode-tasks.md) | the workspace task block and its dispatcher |
| [`scripts/CLAUDE.md`](scripts/CLAUDE.md) | vendoring, the two channels, boxes, scheduled jobs, loading a module by path |

The first two are vendored *out* of here, so **point at them, never restate them**:
`scripts/hooks/tests/test_repo_contract.py` fails a `CLAUDE.md` that paraphrases their
clauses, because a second copy is not drift-checked.

## The docs are vibe-coded too

Every file here was written by an agent, this one included. Prose is the only artifact in
the repo with no compiler and no test, so a wrong sentence survives in a way wrong code
cannot — and an instruction file is read as *authority*, which is what turns a stale
paragraph into one agent talking the next one out of a correct change.

**The code decides.** Where a document and the repo disagree, the document is the defect:
fix it in the same change as the work that found it, and never let it veto a change you
can otherwise see is right. Prefer naming the file that owns a value to copying its
current one. [`tests/test_doc_claims.py`](tests/test_doc_claims.py) gates the checkable
half — every cited path exists, and instruction prose pins no version — across every
`CLAUDE.md`, rule and skill in the repo.

## Nothing but the standard library, by contract

Read the interpreter version and the dev tools off [`pyproject.toml`](pyproject.toml).
What is not readable there is that the empty runtime dependency list is a **constraint,
not a state**: the vendored hooks run before a virtualenv exists, in a repo devkit does
not control, so an import of anything installed breaks provisioning on exactly the
sessions the harness exists to set up. The same contract is why there is no stack here —
no database, no frontend, no compose file — which is what lets CI run with no service
containers and why `.devkit.toml` turns both of those tiers off.

## One bad commit here reddens every consumer

Generated PR gates pin a devkit **tag**, never `@main`. When a change alters vendored
behaviour, say so in the commit message: adopters find out by running
`sync-devkit.py --pull`, and the message is the only changelog they get. A missing tag is
the mirror-image failure and is easier to miss — **an untagged feature does not exist as
far as a generated project is concerned**, however green `main` is.
[`RELEASING.md`](RELEASING.md) owns which refs a consumer pins and the release checklist.

## The internal names are `devkit`

`.devkit.toml`, `$DEVKIT_DIR`, `DEVKIT_VERSION`, `scripts/sync-devkit.py`, and the
published hook ids `devkit-manifest` / `devkit-hooks-stdlib-only` / `devkit-drift`. Treat
them as fixed: the rename from the `agent-harness` spelling had to be one atomic change
across devkit and every consumer, because `sync-devkit.py` is **itself in the `MANIFEST`**
— it is the very path list the drift check compares by, so a half-applied rename fails
`--check` in whichever repo lands second. The old spelling anywhere is a miss from that
migration, not a deliberate holdout.
