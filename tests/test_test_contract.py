"""The gate on test *existence* — the assumption every other module in this tree makes.

Every other test here asserts something about code that someone remembered to write a
test for. Nothing asserted that the remembering happened, so a script could land with
no test module at all and the suite stayed green. Three had: `notify.py`,
`git-sync-keep.py` and `run-tests.py` each ran in this harness with no test module of
their own, while `.claude/rules/engineering.md` said in as many words that "every new
script ships with its tests in the same change". A policy with no gate under it is a
preference.

Two gates, deliberately different in kind.

**Module level, and absolute.** Every script under the source directories needs a
`test_<stem>.py` in one of the two test trees. There is no plain exemption list: where
a script really is covered by a differently-named module, `COVERED_BY` names that
module *and the reason*, and `test_the_indirection_is_real` checks the claim — the
named module has to exist and to reference the script — so a rename cannot leave a
dangling excuse behind.

**Symbol level, and a ratchet.** Every public top-level callable has to be *named* by a
test that references its module. Naming is a proxy for testing and a weak one, but it
is a proxy with no false negatives: a symbol that no relevant test mentions is
certainly untested. Two decisions make it usable rather than merely strict:

- The search is scoped to the test modules that reference the script under test, not to
  the whole corpus. Scoping wider would pass `main`, `run`, `check` and `cap` in every
  module in the repo — dozens of scripts define those names, so a corpus-wide hit says
  nothing about which one was exercised. Scoping narrower (the matching `test_<stem>.py`
  alone) would fail work that is genuinely covered from a sibling: `worktree.py`'s
  routing is exercised from `test_worktree_guard.py`, and that is the right place for it.
- A reference is a call, an attribute access, or an import — never a bare substring,
  which `cap` would satisfy inside `capsys`.

The symbols that fail today are listed in `untested_symbols.txt`. It may only shrink:
adding a line is a visible act in review, and a line that stops being true fails
`test_the_baseline_names_only_real_gaps`, so covering a symbol forces its line out.

This module is devkit-only. The same gate belongs in the vendored tier eventually, but
turning it on in every consumer is a decision with a red PR gate attached, not a rider
on the change that writes it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from support import REPO_ROOT

# Where the code that must be tested lives, and where the tests that cover it may live.
# Both tiers are searched for both, because the two trees cover each other's subjects:
# a vendored hook's test is in `scripts/hooks/tests/`, and `sync-devkit.py` — a
# devkit-owned script — is covered from there too, since a consumer has to verify it.
SOURCE_DIRS = ("scripts", "scripts/hooks", "scripts/precommit")
TEST_DIRS = ("tests", "scripts/hooks/tests")

BASELINE = Path(__file__).with_name("untested_symbols.txt")

# `main` is not exempt — it is where a script's argv handling and exit codes live, and
# those are the parts a consumer's harness depends on. Nothing here is exempt by kind.
# Private names are, because a leading underscore is the author saying "not the API",
# and the public function that calls it is on the hook for its behaviour.

# Scripts whose tests live in a differently-named module, with the reason. Every entry
# is a claim, and `test_the_indirection_is_real` checks it: the module must exist and
# must reference the script, so a rename cannot leave a dangling excuse behind. This is
# not an exemption list — a script with no coverage anywhere does not belong here, it
# needs a test.
COVERED_BY: dict[str, tuple[str, str]] = {
    "check_harness_drift.py": (
        "tests/test_precommit_hooks.py",
        "the three published pre-commit hooks are covered together because what breaks "
        "is what they share: the loader, and the consumer-cwd contract",
    ),
    "check_harness_manifest.py": (
        "tests/test_precommit_hooks.py",
        "same module as its two siblings, for the same reason",
    ),
    "check_stdlib_only.py": (
        "tests/test_precommit_hooks.py",
        "same module as its two siblings, for the same reason",
    ),
    "notify-wrap.py": (
        "tests/test_self_hosting.py",
        "the wrapper's one behaviour is that it propagates the wrapped exit code, and "
        "that is asserted beside the check that it stays byte-identical to the template",
    ),
}


def source_modules() -> list[Path]:
    """Every script the contract covers, as paths relative to the repo root.

    Private modules are skipped: `_loader.py` is an implementation detail of the three
    pre-commit hooks and is exercised through them.
    """
    found: list[Path] = []
    for directory in SOURCE_DIRS:
        for path in sorted((REPO_ROOT / directory).glob("*.py")):
            if path.name.startswith("_"):
                continue
            found.append(path.relative_to(REPO_ROOT))
    return found


def discover_test_modules() -> dict[str, str]:
    """`test_*.py` file name -> its source text, across both test trees.

    This module is excluded from its own corpus. The examples below name real symbols
    (`worktree.recovered_slot`, `worktree.reapable`) to prove the matcher recognises the
    forms a test actually uses — and counting those would have the gate vouch for
    coverage out of its own fixtures, silently marking two untested functions tested.
    """
    return {
        path.name: path.read_text(encoding="utf-8")
        for directory in TEST_DIRS
        for path in sorted((REPO_ROOT / directory).glob("test_*.py"))
        if path.name != Path(__file__).name
    }


def expected_test_module(script: Path) -> str:
    """The test module name a script is expected to have.

    Script names are hyphenated and module names cannot be, so the mapping has to
    normalise: `sync-devkit.py` is covered by `test_sync_devkit.py`.
    """
    return f"test_{script.stem.replace('-', '_')}.py"


def public_symbols(script: Path) -> list[str]:
    """Public top-level functions and classes defined by `script`.

    Read with `ast` rather than by importing: these modules run installers, resolve the
    workspace registry and spawn git at import time is exactly what they must *not* do,
    but proving that is `tests/test_scheduled_jobs.py`'s job, not this one's, and a
    contract test that imports 50 scripts to enumerate them would be the slowest module
    in the suite for no gain.
    """
    tree = ast.parse((REPO_ROOT / script).read_text(encoding="utf-8"))
    definition = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return [n.name for n in tree.body if isinstance(n, definition) and not n.name.startswith("_")]


def reference_pattern(symbol: str) -> re.Pattern[str]:
    """Matches a real reference to `symbol`: an attribute, a call, or an import.

    Deliberately not a substring or even a word match. `cap` appears inside `capsys` in
    half the suite, and `run` inside `run_mode`; either would make the gate report
    coverage that does not exist, which is worse than no gate at all.
    """
    name = re.escape(symbol)
    return re.compile(
        rf"\.{name}\b"  # module.symbol
        rf"|(?<![\w.]){name}\s*\("  # symbol(...) after a from-import
        rf"|^\s*from\s+.*\bimport\b.*\b{name}\b",  # from mod import symbol
        re.MULTILINE,
    )


def relevant_tests(script: Path, modules: dict[str, str]) -> str:
    """The text of every test module that references `script`, concatenated.

    "References" is the module's own name in either spelling — `merge-dependabot-prs.py`
    or the `merge_dependabot_prs` a test imports it as. A test that never names the
    script cannot be the one covering it, and including it would let an unrelated
    module's `main()` vouch for this one's.
    """
    snake = script.stem.replace("-", "_")
    names = re.compile(rf"\b{re.escape(snake)}\b|{re.escape(script.name)}")
    return "\n".join(text for text in modules.values() if names.search(text))


def unexercised(script: Path, modules: dict[str, str]) -> list[str]:
    """Baseline keys (`path::symbol`) for every public symbol no relevant test names."""
    corpus = relevant_tests(script, modules)
    return [
        f"{script.as_posix()}::{symbol}"
        for symbol in public_symbols(script)
        if not reference_pattern(symbol).search(corpus)
    ]


def all_gaps() -> list[str]:
    modules = discover_test_modules()
    return [key for script in source_modules() for key in unexercised(script, modules)]


def baseline_entries() -> list[str]:
    """The recorded gaps, in file order, comments and blank lines dropped."""
    lines = BASELINE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


# --- the module-level gate ----------------------------------------------------


def test_every_script_has_a_test_module():
    """The gate the rule always claimed to have. No exemption list — see COVERED_BY."""
    modules = discover_test_modules()
    missing = [
        script.as_posix()
        for script in source_modules()
        if expected_test_module(script) not in modules and script.name not in COVERED_BY
    ]
    assert not missing, (
        f"{missing} have no test module. Write tests/{{name}} or, if the coverage "
        "genuinely belongs in an existing module, add an entry to COVERED_BY saying "
        "which one and why."
    )


def test_the_indirection_is_real():
    """Every COVERED_BY claim still holds: the script exists, so does the module named,
    and that module actually references the script.

    Without this the dict is a list of excuses that outlive the thing they excuse — a
    renamed script leaves an entry matching nothing, and the gate above silently stops
    covering the file that replaced it.
    """
    modules = discover_test_modules()
    scripts = {script.name for script in source_modules()}
    for script_name, (module_path, reason) in COVERED_BY.items():
        assert script_name in scripts, f"COVERED_BY names {script_name}, which is gone"
        assert reason.strip(), f"{script_name} has an empty reason"
        assert (REPO_ROOT / module_path).exists(), f"{module_path} does not exist"
        text = modules[Path(module_path).name]
        stem = script_name.removesuffix(".py")
        assert stem in text or stem.replace("-", "_") in text, (
            f"{module_path} never mentions {script_name}; the claim that it covers it is stale."
        )


def test_no_script_is_both_expected_and_excused():
    """A script with its own `test_<stem>.py` must not also carry a COVERED_BY entry:
    the entry would be unfalsifiable, since the gate above never consults it."""
    modules = discover_test_modules()
    both = [name for name in COVERED_BY if expected_test_module(Path(name)) in modules]
    assert not both, f"{both} now have their own test module; drop the COVERED_BY entry"


# --- the symbol-level ratchet -------------------------------------------------


def test_every_public_symbol_is_named_by_a_test():
    """New public code arrives tested, or it does not arrive.

    This is the half that bites during review. The baseline is the debt that existed
    when the gate was written; anything not in it is code someone is adding now.
    """
    new = sorted(set(all_gaps()) - set(baseline_entries()))
    assert not new, (
        f"{len(new)} public symbol(s) no test names:\n  " + "\n  ".join(new) + "\n"
        f"Write the tests. {BASELINE.name} records pre-existing debt only and is not "
        "the place to send new code — a symbol added there has never run under test."
    )


def test_the_baseline_names_only_real_gaps():
    """The other half of the ratchet: a line that stops being true has to come out.

    Without this the file is write-only. A symbol gets a test, the line stays, and the
    next person to delete that test finds the gate already looking the other way.
    """
    stale = sorted(set(baseline_entries()) - set(all_gaps()))
    assert not stale, (
        f"{len(stale)} baseline entries are no longer gaps — delete these lines from "
        f"{BASELINE.name}:\n  " + "\n  ".join(stale)
    )


def test_the_baseline_is_sorted_and_unique():
    """So two branches adding a line conflict in git rather than merging into a
    duplicate, and so the diff of a burn-down is readable."""
    entries = baseline_entries()
    assert entries == sorted(entries), f"{BASELINE.name} is not sorted"
    assert len(entries) == len(set(entries)), f"{BASELINE.name} has duplicate lines"


def test_the_baseline_is_shrinking_ground_not_a_config():
    """A guard on the guard: if the baseline ever covers everything, the gate above is
    asserting nothing and should be deleted rather than left looking green."""
    assert len(baseline_entries()) < sum(
        len(public_symbols(script)) for script in source_modules()
    ), "the baseline excuses every public symbol in the repo; the gate is inert"


# --- the helpers, which are themselves the thing that can go quietly wrong ----


def test_public_symbols_reads_the_api_and_nothing_else(tmp_path, monkeypatch):
    source = tmp_path / "scripts" / "sample.py"
    source.parent.mkdir()
    source.write_text(
        "CONSTANT = 1\n"
        "def public():\n    pass\n"
        "def _private():\n    pass\n"
        "async def public_async():\n    pass\n"
        "class Public:\n    def method(self):\n        pass\n"
        "if True:\n    def nested():\n        pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("test_test_contract.REPO_ROOT", tmp_path)
    assert public_symbols(Path("scripts/sample.py")) == ["public", "public_async", "Public"]


@pytest.mark.parametrize(
    "text",
    [
        "assert worktree.recovered_slot(box) is None",
        "from worktree import recovered_slot",
        "    recovered_slot(box)",
    ],
)
def test_reference_pattern_accepts_the_three_real_forms(text):
    assert reference_pattern("recovered_slot").search(text)


@pytest.mark.parametrize(
    "text",
    [
        "def test_recovered_slot_is_none():",  # naming a test after it is not using it
        "recovered_slotted = 1",
        "'recovered_slot'",
    ],
)
def test_reference_pattern_rejects_mere_mentions(text):
    assert not reference_pattern("recovered_slot").search(text)


def test_reference_pattern_does_not_match_inside_a_longer_name():
    """`cap` inside `capsys` is the case that made a substring check unusable."""
    assert not reference_pattern("cap").search("def test_x(capsys):\n    cap_thing = 1\n")


def test_relevant_tests_excludes_modules_that_never_name_the_script():
    modules = {
        "test_worktree.py": "import worktree\nworktree.reapable(box)\n",
        "test_unrelated.py": "def test_x():\n    thing.reapable()\n",
    }
    corpus = relevant_tests(Path("scripts/worktree.py"), modules)
    assert "import worktree" in corpus
    assert "thing.reapable" not in corpus


def test_relevant_tests_matches_the_hyphenated_spelling_too():
    modules = {"test_a.py": "load_script('scripts/merge-dependabot-prs.py')\n"}
    corpus = relevant_tests(Path("scripts/merge-dependabot-prs.py"), modules)
    assert corpus


def test_expected_test_module_normalises_hyphens():
    assert expected_test_module(Path("scripts/sync-devkit.py")) == "test_sync_devkit.py"


def test_source_modules_skips_private_modules():
    names = {script.name for script in source_modules()}
    assert "_loader.py" not in names
    assert "sync-devkit.py" in names


def test_the_gate_never_counts_its_own_examples():
    """The examples above name real symbols on purpose. If this module were in its own
    corpus, `worktree.recovered_slot` would read as covered by the regex fixture that
    merely quotes it — the gate certifying coverage it invented."""
    assert Path(__file__).name not in discover_test_modules()
