"""The gate on test *existence* — the assumption every other module in this tree makes.

Every other test here asserts something about code that someone remembered to write a
test for. Nothing asserted that the remembering happened, so a script could land with
no test module at all and the suite stayed green. Three had: `notify.py`,
`git-sync-keep.py` and `run-tests.py` each ran in this harness with no test module of
their own, while `.claude/rules/engineering.md` said in as many words that "every new
script ships with its tests in the same change". A policy with no gate under it is a
preference.

This module is the **module-level** half, and it is devkit-only: every script under the
source directories needs a `test_<stem>.py` in one of the two test trees. There is no
plain exemption list — where a script really is covered by a differently-named module,
`COVERED_BY` names that module *and the reason*, and `test_the_indirection_is_real`
checks the claim, so a rename cannot leave a dangling excuse behind.

It stays here rather than moving to the vendored tier because `COVERED_BY` is *project
data*: one repo's list of which module covers which script. A vendored file carrying it
would be devkit's opinion about somebody else's layout, which is the thing
`.claude/rules/engineering.md` forbids in as many words. The naming convention it
enforces is also devkit's, not a fact about every project.

The **symbol-level** half moved out. It is `scripts/hooks/untested_symbols.py` plus
`scripts/hooks/tests/test_untested_symbols.py`, it reads its scope from `.devkit.toml`,
it records this repo's debt in `.devkit-untested.txt`, and it ships to every consumer —
where it gates that project's own code, never devkit's. `sync-devkit.py --pull` seeds
the baseline on adoption, so turning it on is not a red gate.
"""

from __future__ import annotations

from pathlib import Path

from support import REPO_ROOT

# Where the code that must be tested lives, and where the tests that cover it may live.
# Both tiers are searched for both, because the two trees cover each other's subjects:
# a vendored hook's test is in `scripts/hooks/tests/`, and `sync-devkit.py` — a
# devkit-owned script — is covered from there too, since a consumer has to verify it.
SOURCE_DIRS = ("scripts", "scripts/hooks", "scripts/precommit")
TEST_DIRS = ("tests", "scripts/hooks/tests")

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
    """`test_*.py` file name -> its source text, across both test trees."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for directory in TEST_DIRS
        for path in sorted((REPO_ROOT / directory).glob("test_*.py"))
    }


def expected_test_module(script: Path) -> str:
    """The test module name a script is expected to have.

    Script names are hyphenated and module names cannot be, so the mapping has to
    normalise: `sync-devkit.py` is covered by `test_sync_devkit.py`.
    """
    return f"test_{script.stem.replace('-', '_')}.py"


# --- the gate -----------------------------------------------------------------


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


# --- the helpers, which are themselves the thing that can go quietly wrong ----


def test_expected_test_module_normalises_hyphens():
    assert expected_test_module(Path("scripts/sync-devkit.py")) == "test_sync_devkit.py"


def test_source_modules_skips_private_modules():
    names = {script.name for script in source_modules()}
    assert "_loader.py" not in names
    assert "sync-devkit.py" in names


def test_discover_test_modules_spans_both_trees():
    """The vendored tier's tests must count: a hook covered from `scripts/hooks/tests/`
    is covered, and a gate reading only `tests/` would demand a second module for it."""
    modules = discover_test_modules()
    assert "test_test_contract.py" in modules
    assert "test_untested_symbols.py" in modules
