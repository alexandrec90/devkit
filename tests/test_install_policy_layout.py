"""Tests for the policy's two-layout rules -- `scripts/install_policy_layout.py`.

Separated from the installer's tests because the tier is: these are pure functions of
what an install *wrote*, so they need no filesystem, no git and no target directory, and
they are the half of the layout question that can be answered without performing an
install.

The regression they exist for is on the ledger as `6dae4ff3`. `RUNTIME_FILES` lists the
policy in both layouts so either ref installs the one it has, and the comment there used
to argue the pair could safely coexist: Python prefers a package to a same-named flat
module, so an upgrade needs no cleanup. True, and it covers one direction only. Going
back the other way -- reinstalling from a tag that predates the split, which is
`main()`'s default and what `installers.py` re-runs nightly -- writes the flat module
underneath a package an earlier install left, and the package keeps winning. Every hook
on the machine then imports a release nobody chose, while `--check` reports the runtime
current at the ref it just installed.
"""

from pathlib import Path

from support import load_script

layout = load_script("scripts/install_policy_layout.py")


def test_the_shadow_of_a_flat_module_install_is_the_package():
    assert layout.shadowing_entrypoint({"devkit_git_policy.py": "x"}) == (
        "devkit_git_policy/__init__.py"
    )


def test_the_shadow_of_a_package_install_is_the_flat_module():
    assert layout.shadowing_entrypoint({"devkit_git_policy/__init__.py": "x"}) == (
        "devkit_git_policy.py"
    )


def test_an_install_that_wrote_no_entrypoint_has_no_shadow():
    """`install_refusal` owns that case and must stay the thing that reports it."""
    assert layout.shadowing_entrypoint({"pre-commit": "x"}) == ""
    assert layout.shadowing_entrypoint({}) == ""


def test_the_package_directory_is_the_thing_to_remove_not_its_init():
    """An empty `devkit_git_policy/` is a namespace package and still shadows a flat
    module, so deleting only `__init__.py` would leave the shadow standing."""
    target = Path("hooks")
    assert layout.entrypoint_path(target, "devkit_git_policy/__init__.py") == (
        target / "devkit_git_policy"
    )
    assert layout.entrypoint_path(target, "devkit_git_policy.py") == (
        target / "devkit_git_policy.py"
    )


def test_clearing_removes_the_whole_stale_package(tmp_path):
    target = tmp_path / "hooks"
    (target / "devkit_git_policy").mkdir(parents=True)
    (target / "devkit_git_policy" / "__init__.py").write_text("STALE", encoding="utf-8")
    (target / "devkit_git_policy.py").write_text("fresh", encoding="utf-8")

    cleared = layout.clear_shadowing_entrypoint(target, {"devkit_git_policy.py": "x"})

    assert cleared == "devkit_git_policy/__init__.py"
    assert not (target / "devkit_git_policy").exists()
    assert (target / "devkit_git_policy.py").read_text(encoding="utf-8") == "fresh"


def test_clearing_reports_nothing_when_there_was_nothing_to_clear(tmp_path):
    """The ordinary case, and it must stay silent: the installer prints what this
    returns, and a line printed on every install is one nobody reads."""
    target = tmp_path / "hooks"
    target.mkdir()
    (target / "devkit_git_policy.py").write_text("fresh", encoding="utf-8")
    assert layout.clear_shadowing_entrypoint(target, {"devkit_git_policy.py": "x"}) == ""


def test_an_install_with_no_policy_module_is_refused():
    """The backstop. A skip here leaves every hook on the machine importing a
    `devkit_git_policy` that is not there, on every commit in every repository."""
    assert "nothing to import" in layout.install_refusal({}, "v0.0.1")
    assert layout.install_refusal({"pre-commit": "x"}, "v0.0.1") != ""


def test_either_layout_satisfies_the_refusal():
    """Which is the whole reason `RUNTIME_FILES` may list both."""
    assert layout.install_refusal({"devkit_git_policy.py": "x"}, "v0.5.3") == ""
    assert layout.install_refusal({"devkit_git_policy/__init__.py": "x"}, "HEAD") == ""


def test_the_hook_names_are_derived_from_the_runtime_map_not_listed_again():
    """Listed twice, a hook added to one and forgotten in the other installs without its
    executable bit -- and git skips a hook it cannot execute without saying so."""
    assert layout.HOOK_NAMES == {"pre-commit", "pre-push", "post-checkout", "post-index-change"}
    assert layout.HOOK_NAMES <= set(layout.RUNTIME_FILES.values())


def test_both_policy_layouts_are_listed_as_entrypoints():
    assert layout.POLICY_ENTRYPOINTS <= set(layout.RUNTIME_FILES.values())
    assert len(layout.POLICY_ENTRYPOINTS) == 2
