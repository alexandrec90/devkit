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
| [`.claude/rules/authoring.md`](.claude/rules/authoring.md) | writing rules, skills and instruction files |
| [`.claude/rules/vscode-tasks.md`](.claude/rules/vscode-tasks.md) | the workspace task block and its dispatcher |
| [`scripts/CLAUDE.md`](scripts/CLAUDE.md) | vendoring, ephemeral boxes, scheduled jobs, loading a module by path |
| [`.github/CLAUDE.md`](.github/CLAUDE.md) | the CI surface every project has |

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
   the repo. It cannot tell you a rationale went stale; it does stop the *silent* rot,
   which is the kind that accumulates. Its docstring owns the rest, including where a
   deliberate absence goes and what each one has to keep proving.

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

This is not decoration. A hook that only runs downstream is a hook nobody tests: devkit
shipped a `lint-fix.py` that formats on every edit and then needed a dedicated commit
(`4fbda17`) to clean up the format drift that had accumulated in the one repo where the
hook was not wired.

**When you change a hook script, you are changing the thing that is running you.** A
syntax error in `stop.py` breaks the current session's Stop; a bad `lint-fix.py` blocks
every subsequent edit. Both fail loudly and immediately, which is the point — but run
`python scripts/run-tests.py` and the vendored tree's suite
(`python -m pytest scripts/hooks/tests/ -q`) before assuming a change is good.

## The two test trees

They are deliberately separate, and the distinction is load-bearing.

- **`scripts/hooks/tests/`** — the vendored tier. It ships into every consuming project
  via `MANIFEST` and must stay **project-agnostic**: every value that varies per project
  comes from `hook.CFG` (read from that project's `.devkit.toml`), never from a
  literal. A hardcoded path once made a dozen of these fail on every generated project's
  first CI run; `scripts/` being devkit's own `app_dir` broke another. Excluded from
  `pyproject.toml`'s `testpaths`, so it runs as its own step.
- **`tests/`** — devkit-only (generator, port registry, renderer, sweep). Never
  vendored, which is what lets the generator grow without forcing a `--pull` in every
  consumer. There is **no `conftest.py` here** on purpose — see `tests/support.py` for
  why a second one would collide with the vendored tree's.

Two consequences for where a test goes. A change to a hook script needs its test in the
*vendored* tree, written against `hook.CFG` rather than devkit's literal values, because
it has to pass in every consumer too. And the generator is verified by **rendering, not
by reading**: `tests/` builds a project of each preset and parses every file it emits.

### Nothing gated whether a test existed at all

Both trees are full of contract tests, and every one of them asserts about code someone
remembered to cover. Nothing asserted the remembering, so "every new script ships with
its tests in the same change" — `.claude/rules/engineering.md`, in those words — was a
preference with a green suite behind it. Three scripts had no test module at all.

Two gates now, and the tier each lives in is the decision worth knowing.

**Module level, devkit-only** — `tests/test_test_contract.py`. Every script under
`scripts/`, `scripts/hooks/` and `scripts/precommit/` needs a `test_<stem>.py`, and
where the coverage genuinely lives elsewhere `COVERED_BY` names the module *and* the
reason — a claim the same file checks, so a rename cannot leave a dangling excuse. It
stays here because `COVERED_BY` is project data and the naming convention is devkit's;
vendoring either would be devkit holding an opinion about somebody else's layout.

**Symbol level, vendored** — `scripts/hooks/untested_symbols.py` and its test. Every
public top-level callable must be *named* by a test that references its module. Naming
is a weak proxy for testing, but one with no false negatives, which is what makes it
gateable. Scope comes from `[test_contract]` in `.devkit.toml`, so the scanner names no
path; the gaps live in `.devkit-untested.txt`, which is **never vendored** because its
content is a fact about one repo.

Three properties make that file debt rather than configuration, and a change here has to
keep all three. A line that stops being true **fails**, so covering a symbol forces its
line out and the file can only shrink. `--seed` **refuses to overwrite**, so new
untested code cannot be laundered into the list. And `sync-devkit.py --pull` seeds it on
adoption, so switching the gate on is not a red PR gate in every consumer — which is the
objection that kept this devkit-only when it was first written.

Scoping is the part that took measuring. Searching the whole test corpus passes `main`,
`run`, `check` and `cap` everywhere, since dozens of scripts define those names;
searching only the matching `test_<stem>.py` fails work that is correctly covered from a
sibling. So the corpus is the test modules that reference the script under test — and
`test_untested_symbols.py` uses **invented symbol names** in its fixtures, because a
fixture quoting a real one would have the gate certify coverage it invented. The
devkit-only ancestor shipped exactly that bug.

## The two channels

devkit ships the same discipline through two mechanisms, and which one a thing belongs to
is a real decision, not a preference:

| | Vendored tier | Pre-commit channel |
| --- | --- | --- |
| Delivered by | `sync-devkit.py --pull` copies files in | pre-commit clones devkit at a pinned `rev` |
| Lives in | `scripts/hooks/`, listed in `MANIFEST` | `scripts/precommit/`, listed in `.pre-commit-hooks.yaml` |
| Versioned by | `DEVKIT_VERSION` + a CI drift job | the `rev` in the consumer's config |
| Use it when | the code must run with no network and no install (agent hooks) | the check runs at commit time and a pinned version is better than a copy |

Rules specific to the pre-commit channel:

- **`language: script`, stdlib only, executable bit set.** There is nothing to install
  from a virtual project, so pre-commit execs the file directly. A missing `chmod +x` or a
  broken shebang fails only on a consumer's machine, after the rev is tagged — a test
  guards both.
- **The hooks run with the *consumer's* repo as the cwd**, while the scripts themselves
  live in pre-commit's clone. Never resolve a devkit file relative to the cwd; go through
  `Path(__file__)`. Never assume the consumer's layout — read it from `.devkit.toml`.
- **devkit wires its own hooks as `repo: local`, not by rev.** Pinning a rev here would
  check a released tag's hooks against the working tree trying to change them, so a hook
  fix could never be validated by the hook it fixes.
- **A new hook needs an id in both files** — `.pre-commit-hooks.yaml` (published) and
  `.pre-commit-config.yaml` (run here). A test asserts the sets match, with `devkit-drift`
  as the one documented exception (in devkit it would compare against itself).

## Guardrails

The instruction-file feedback loop lives in `.claude/rules/engineering.md` — report a
rule that sent you into a dead end instead of routing around it.

### One bad commit here reddens every consumer

Generated PR gates pin a devkit **tag**, never `@main`, for this reason. When a change
alters vendored behaviour, say so in the commit message: adopters find out by running
`sync-devkit.py --pull`, and the message is the only changelog they get.

A missing tag is the mirror-image failure and is easier to miss: `new-project.py`
resolves `latest_devkit_tag() or FALLBACK_DEVKIT_REF`, so **an untagged feature does
not exist as far as a generated project is concerned**, however green `main` is. That
is how a rendered `.pre-commit-config.yaml` came to request hook ids its pinned tag
could not serve, aborting the new owner's first commit. The release checklist —
including why the fallback test is deliberately red for one commit — is
[`RELEASING.md`](RELEASING.md).

### The internal names are `devkit`

`.devkit.toml`, `$DEVKIT_DIR`, `DEVKIT_VERSION`, `scripts/sync-devkit.py`, and the
published hook ids `devkit-manifest` / `devkit-hooks-stdlib-only` / `devkit-drift`. The
rename from the `agent-harness` spelling had to be one atomic change across devkit and
every consumer, because `sync-devkit.py` is **itself in the `MANIFEST`** — renaming it
changes the very path list the drift check compares by, and a half-applied rename fails
`--check` in whichever repo lands second. Treat these names as fixed; if you find the
old spelling anywhere, it is a miss from that migration, not a deliberate holdout.
