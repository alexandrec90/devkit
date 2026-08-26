# The two test trees

Here rather than in the root `CLAUDE.md` because a session needs it only once it is
writing a test, and the root file is re-sent on **every** API call of **every** session —
`tests/test_instruction_budget.py` is the ratchet that keeps it that way. A `CLAUDE.md`
is the right home rather than a `.claude/rules/` file because Codex reads every
`CLAUDE.md` and reads straight past `.claude/rules/`, and where a test goes is not
guidance a Codex session can afford to miss.

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

## Nothing gated whether a test existed at all

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
