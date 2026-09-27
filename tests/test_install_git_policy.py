"""Tests for installing Devkit's global Git hook dispatcher."""

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from support import REPO_ROOT, load_script

installer = load_script("scripts/install-git-policy.py")
# The layout tier moved to its own module; the names it owns are read from there
# rather than re-exported through the installer purely to keep a test import alive.
layout = load_script("scripts/install_policy_layout.py")
# Same again for the receipt-and-drift tier, cut out when the installer's structural
# ceiling was raised a third consecutive time. `installer` still resolves the names its
# own `run_check` calls, because it imports them; the ones only a test reaches for are
# read from the module that owns them. Its own unit tests are in
# `tests/test_policy_drift.py`.
drift = load_script("scripts/policy_drift.py")


def test_run_command_captures_both_streams_without_raising():
    """The installer's default runner. It must not `check=True`: every caller reads the
    return code itself -- `ensure_compatible_hooks_path` treats a non-zero
    `--get core.hooksPath` as "unset", which is the ordinary case on a fresh machine."""
    ok = installer.run_command([sys.executable, "-c", "print('out')"])
    assert ok.returncode == 0
    assert ok.stdout.strip() == "out"

    bad = installer.run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    )
    assert bad.returncode == 3
    assert bad.stderr.strip() == "boom"


class FakeRunner:
    def __init__(self, hooks_path=""):
        self.hooks_path = hooks_path
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if tuple(argv) == ("git", "config", "--global", "--get", "core.hooksPath"):
            return subprocess.CompletedProcess(
                argv, 0 if self.hooks_path else 1, self.hooks_path, ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_install_copies_all_runtime_files_and_marks_hooks_executable(tmp_path):
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)

    assert (target / "devkit_git_policy" / "__init__.py").is_file()
    for name in ("pre-commit", "pre-push"):
        hook = target / name
        assert hook.is_file()
        if os.name != "nt":
            assert os.access(hook, os.X_OK)


def test_installed_wrappers_delegate_to_the_policy_module(tmp_path):
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    # Stub the package's `__init__`, not a flat module: a flat `devkit_git_policy.py`
    # beside the package would be *shadowed* by it and this would assert nothing.
    (target / "devkit_git_policy" / "__init__.py").write_text(
        "def main(name):\n    print(f'delegated:{name}')\n    return 0\n",
        encoding="utf-8",
    )

    for hook_name in ("pre-commit", "pre-push"):
        result = subprocess.run(
            [sys.executable, str(target / hook_name)],
            input="" if hook_name == "pre-push" else None,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"delegated:{hook_name}"


def test_configuration_sets_global_dispatcher_and_pruning(tmp_path):
    runner = FakeRunner()
    installer.configure_git(tmp_path / "hooks", runner)
    assert (
        "git",
        "config",
        "--global",
        "core.hooksPath",
        (tmp_path / "hooks").resolve().as_posix(),
    ) in runner.calls
    assert ("git", "config", "--global", "fetch.prune", "true") in runner.calls
    assert (
        "git",
        "config",
        "--global",
        "devkit.branchPolicy.failClosed",
        "true",
    ) in runner.calls


def test_install_refuses_to_overwrite_an_unrelated_global_hooks_path(tmp_path):
    runner = FakeRunner(hooks_path="C:/someone-elses-hooks\n")
    try:
        installer.ensure_compatible_hooks_path(tmp_path / "hooks", runner)
    except installer.InstallRefusedError as error:
        assert "someone-elses-hooks" in str(error)
    else:
        raise AssertionError("an unrelated core.hooksPath must be preserved")


def test_reinstall_accepts_the_same_global_hooks_path(tmp_path):
    target = (tmp_path / "hooks").resolve()
    runner = FakeRunner(hooks_path=f"{target.as_posix()}\n")
    installer.ensure_compatible_hooks_path(target, runner)


# --- installing from a committed ref -----------------------------------------
# The runtime installed here is what every repository on this machine enforces.
# Copying the working tree into that position is how a policy came to be enforced
# that no commit contained: installed from a work-in-progress file ~18 hours
# before that change was committed, so `DEVKIT_SKIP_BRANCH_POLICY` did not exist
# in the running code while the source and the README both described it.


def test_the_policy_is_listed_in_both_layouts_so_an_older_tag_still_installs():
    """The entry that stops a merge from bricking every machine's git hooks.

    `scripts/git_policy.py` became the `scripts/git_policy/` package, and
    `install_files` skips a `RUNTIME_FILES` source the ref does not hold. Listing only
    the package would therefore make `--yes` from the newest TAG -- what `main()`
    defaults to and what `installers.py` re-runs nightly -- install no policy at all
    until the next release, leaving the hooks importing a module that is not there.
    """
    assert "scripts/git_policy.py" in installer.RUNTIME_FILES
    assert "scripts/git_policy/__init__.py" in installer.RUNTIME_FILES
    assert layout.POLICY_ENTRYPOINTS == {
        "devkit_git_policy.py",
        "devkit_git_policy/__init__.py",
    }


def test_an_install_that_found_no_policy_at_all_refuses():
    """The backstop, so "no policy installed" is unrepresentable rather than unlikely.

    A per-file skip is right for a runtime that gained a file and catastrophic for the
    policy module, because the hooks run on every commit in every repository on the
    machine -- so the failure is total and arrives with no warning.
    """
    assert installer.install_refusal({"pre-commit": "x"}, "v0.0.1") != ""
    assert "nothing to import" in installer.install_refusal({}, "v0.0.1")
    # Either layout on its own satisfies it; neither is required in particular.
    assert installer.install_refusal({"devkit_git_policy.py": "x"}, "v0.5.3") == ""
    assert installer.install_refusal({"devkit_git_policy/__init__.py": "x"}, "HEAD") == ""


def test_installing_from_a_ref_without_either_layout_raises(tmp_path):
    """End to end through `install_files`, not just the predicate: the refusal has to
    be wired in, or the check exists and the brick still ships."""
    empty = tmp_path / "checkout"
    (empty / "scripts").mkdir(parents=True)
    try:
        installer.install_files(empty, tmp_path / "hooks", installer.WORKTREE_REF)
    except installer.InstallRefusedError as error:
        assert "nothing to import" in str(error)
    else:
        raise AssertionError("an install with no policy module must refuse")


def test_a_worktree_install_skips_the_layout_the_checkout_does_not_have(tmp_path, capsys):
    """And says which, in words that do not send the reader looking for a release.

    The skip message used to read "is newer than <ref>" for every skip. Printed over
    `scripts/git_policy.py` -- a file no future ref will hold, because it became the
    package -- that is a claim about a release that is never coming.
    """
    installer.install_files(REPO_ROOT, tmp_path / "hooks", installer.WORKTREE_REF)
    out = capsys.readouterr().out
    assert "scripts/git_policy.py not in the working tree" in out
    assert "newer than" not in out


def _imports_from(target: Path) -> str:
    """`__file__` of `devkit_git_policy` imported the way the hook wrapper imports it.

    In a child process on purpose: the name must be resolved from `target` by a fresh
    interpreter, exactly as `pre-commit` does, not found in this one's `sys.modules`.
    """
    probe = (
        f"import sys; sys.path.insert(0, r'{target}');"
        "import devkit_git_policy as p; print(p.__file__)"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_a_pre_package_tag_installs_a_policy_the_hooks_can_still_import(tmp_path):
    """v0.11.16 is a real tag that predates the package, and `main()` installs from the
    newest tag by default -- so this is the ordinary path on every machine until the
    release after the split, not a hypothetical."""
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, "v0.11.16")
    assert "devkit_git_policy.py" in receipt.files
    assert not (target / "devkit_git_policy").exists()
    assert _imports_from(target).endswith("devkit_git_policy.py")


def test_an_upgrade_takes_the_layout_it_replaced_with_it(tmp_path):
    """This used to assert the opposite, and the reasoning it recorded was half a case.

    It said the leftover `devkit_git_policy.py` was "harmless only because Python
    prefers a package to a same-named module on `sys.path`" -- correct, and it only
    ever covered the upgrade direction. Going back the other way, which is what
    `main()` does by default and `installers.py` re-runs nightly, writes the flat
    module underneath the package and the package keeps winning: every hook on the
    machine imports a release nobody chose while the receipt names the one just
    installed. "Harmless only because" turned out to be load-bearing on which way you
    were walking, so the install no longer leaves the pair to resolution order.
    """
    target = tmp_path / "hooks"
    installer.install(REPO_ROOT, target, "v0.11.16")
    assert (target / "devkit_git_policy.py").is_file(), "v0.11.16 is the flat-module layout"

    installer.install(REPO_ROOT, target, installer.WORKTREE_REF)

    assert not (target / "devkit_git_policy.py").exists(), "the replaced layout must go"
    assert _imports_from(target).endswith(str(Path("devkit_git_policy") / "__init__.py"))


def test_downgrading_to_a_pre_package_tag_leaves_the_module_actually_importable(tmp_path):
    """The direction the old test did not have, and the one that bit a real machine.

    Installing v0.11.16 over a package install must leave `devkit_git_policy.py` as
    what imports -- not a flat module sitting unreachable under a stale package.
    """
    target = tmp_path / "hooks"
    installer.install(REPO_ROOT, target, installer.WORKTREE_REF)
    assert (target / "devkit_git_policy" / "__init__.py").is_file()

    installer.install(REPO_ROOT, target, "v0.11.16")

    assert not (target / "devkit_git_policy").exists(), "the stale package must go"
    assert _imports_from(target).endswith("devkit_git_policy.py")


def test_the_default_source_is_a_released_tag_not_the_working_tree():
    """The prevention, in one assertion. A working-tree install must be something
    you ask for by name, never what you get by not thinking about it."""
    ref = installer.resolve_ref(REPO_ROOT)
    assert ref != installer.WORKTREE_REF
    assert ref.startswith("v"), ref


def test_installing_from_a_ref_writes_that_refs_bytes(tmp_path):
    """Whichever layout the ref has -- the flat module or the package.

    This pinned `scripts/git_policy.py` by name, and that is not a detail of the
    assertion: it is the one thing `install_files` is written NOT to assume, because a
    ref cut before the package split has the module and a ref cut after has the package.
    The test only sees the ref `resolve_ref` returns, which is the newest TAG -- so it
    stayed green on every branch and went red for the first time inside the release
    pipeline's `phase=tag`, where the staged tag is the first ref with no flat module.
    A red suite there is a refusal to publish, so v0.11.18 was prepared, merged, and
    never tagged; `FALLBACK_DEVKIT_REF` then named a tag that did not exist, which is
    what reddened main's nightly. A test that hard-codes one side of a supported fork
    fails at the worst possible moment.
    """
    target = tmp_path / "hooks"
    ref = installer.resolve_ref(REPO_ROOT)
    installer.install(REPO_ROOT, target, ref)

    written = {
        source: destination
        for source, destination in installer.RUNTIME_FILES.items()
        if destination in layout.POLICY_ENTRYPOINTS and installer.in_ref(REPO_ROOT, ref, source)
    }
    assert written, f"{ref} carries neither policy layout"
    for source, destination in written.items():
        expected = installer.read_blob(REPO_ROOT, ref, source)
        assert (target / destination).read_bytes() == expected


def test_an_unresolvable_ref_refuses_rather_than_installing_nothing():
    try:
        installer.read_blob(REPO_ROOT, "v0.0.0-does-not-exist", "scripts/git_policy.py")
    except installer.InstallRefusedError as error:
        assert "cannot read" in str(error)
    else:
        raise AssertionError("a bad ref must refuse, not yield empty bytes")


def test_the_plan_names_the_ref_it_would_install_from(tmp_path):
    plan = installer.render_plan(tmp_path / "hooks", "v0.5.3")
    assert "v0.5.3" in plan
    assert "installed.json" in plan


def test_the_plan_warns_when_the_source_is_uncommitted(tmp_path):
    plan = installer.render_plan(tmp_path / "hooks", installer.WORKTREE_REF)
    assert "WARNING" in plan
    assert "uncommitted" in plan


# --- the receipt --------------------------------------------------------------


def test_installing_records_what_was_installed(tmp_path):
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, installer.WORKTREE_REF)

    assert (target / installer.RECEIPT_NAME).is_file()
    assert receipt.ref == installer.WORKTREE_REF
    # Every destination *except* the layout this ref does not have. `RUNTIME_FILES`
    # carries the policy in both spellings so an install from a tag cut before the
    # package still works, which means one of the two is always legitimately skipped.
    expected = set(installer.RUNTIME_FILES.values()) - {"devkit_git_policy.py"}
    assert set(receipt.files) == expected
    assert installer.read_receipt(target) == receipt


def test_a_receipt_survives_a_round_trip(tmp_path):
    target = tmp_path / "hooks"
    written = installer.install(REPO_ROOT, target, "v0.5.3")
    assert installer.read_receipt(target) == written


def test_a_corrupt_receipt_reads_as_absent_rather_than_raising(tmp_path):
    """A session start may not be taken down by a malformed file."""
    target = tmp_path / "hooks"
    installer.install(REPO_ROOT, target, "v0.5.3")
    (target / installer.RECEIPT_NAME).write_text("{not json", encoding="utf-8")
    assert installer.read_receipt(target) is None


def test_a_receipt_missing_its_ref_is_not_a_receipt():
    assert installer.Receipt.parse('{"files": {}}') is None
    assert installer.Receipt.parse('{"ref": "v1", "files": "nope"}') is None


def test_a_runtime_file_newer_than_the_ref_is_skipped_rather_than_refusing(tmp_path):
    """THE REF DECIDES THE RUNTIME. `RUNTIME_FILES` grows and releases do not move, so
    the moment the list gained an entry, every install from the newest TAG -- which is
    what `main()` defaults to and what `installers.py` re-runs nightly -- would have
    refused outright, on every machine, until the next release. v0.5.3 predates most of
    the current list, which is exactly the shape of that failure."""
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, "v0.5.3")

    assert receipt.files, "an old ref still installs what it does have"
    assert set(receipt.files) <= set(installer.RUNTIME_FILES.values())
    for name in receipt.files:
        assert (target / name).is_file()


def test_a_skipped_file_is_not_reported_as_drift_forever(tmp_path):
    """The other half, and the reason the check reads the receipt rather than
    `RUNTIME_FILES`: a file the ref never had would otherwise report as "not installed"
    on every nightly check, and every re-install would skip it again."""
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, "v0.5.3")
    assert installer.compare_install(target, receipt) == []


def test_in_ref_separates_a_missing_path_from_an_unreadable_one(tmp_path):
    """`read_blob` must stay loud -- a ref that cannot be read is a refusal. Only "this
    ref predates the file" is the benign case, and it is asked for separately."""
    # The path is asserted against the ref that actually holds it, in both directions:
    # `scripts/git_policy.py` is in the tags before the package and gone from HEAD,
    # which is the whole reason `RUNTIME_FILES` lists both layouts. Naming it against
    # HEAD is what made this test pass right up until the split was *committed* -- the
    # working tree had already lost the file while HEAD still had it.
    assert installer.in_ref(REPO_ROOT, "HEAD", "scripts/git_policy/__init__.py") is True
    assert installer.in_ref(REPO_ROOT, "v0.11.16", "scripts/git_policy.py") is True
    assert installer.in_ref(REPO_ROOT, "HEAD", "scripts/git_policy.py") is False
    assert installer.in_ref(REPO_ROOT, "HEAD", "scripts/not-a-file.py") is False
    assert installer.in_ref(REPO_ROOT, "v0.0.0-does-not-exist", "scripts/git_policy.py") is False


def test_every_hook_in_the_map_gets_the_executable_bit(tmp_path):
    """git skips a hook it cannot execute WITHOUT SAYING SO, so the set that gets chmod
    is derived from the map rather than listed beside it."""
    assert installer.HOOK_NAMES == {"pre-commit", "pre-push", "post-checkout", "post-index-change"}
    for source, destination in installer.RUNTIME_FILES.items():
        assert (destination in installer.HOOK_NAMES) == source.startswith("scripts/git-hooks/")


# --- what the check actually compares -----------------------------------------


def test_a_fresh_install_reports_nothing(tmp_path):
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, "v0.5.3")
    assert installer.compare_install(target, receipt) == []


def test_a_runtime_modified_after_install_is_reported(tmp_path):
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, "v0.5.3")
    (target / "devkit_git_policy.py").write_text("# tampered\n", encoding="utf-8")

    drifted = installer.compare_install(target, receipt)
    assert [d.name for d in drifted] == ["devkit_git_policy.py"]
    assert "modified" in drifted[0].reason


def test_a_missing_installed_file_is_reported(tmp_path):
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, "v0.5.3")
    (target / "pre-push").unlink()
    assert [d.name for d in installer.compare_install(target, receipt)] == ["pre-push"]


def test_a_runtime_with_no_receipt_is_unidentifiable_and_says_so(tmp_path):
    """The state this machine was actually in: installed before receipts existed,
    so not provably stale -- just impossible to identify without a byte-diff."""
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    drifted = installer.compare_install(target, None)
    assert [d.name for d in drifted] == [installer.RECEIPT_NAME]


def test_a_checkout_ahead_of_the_installed_release_is_not_drift(tmp_path):
    """The false positive this design exists to avoid. The runtime is pinned to a
    release, so a checkout sitting ahead of it is the normal state -- warning about
    it would make the line fire continuously while the policy is being edited.

    The recorded ref is deliberately older than what is installed here: bytes are
    what `compare_install` answers about, and the ref is not its business.
    """
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, "v0.5.3")
    assert installer.compare_install(target, replace(receipt, ref="v0.5.2")) == []


def test_being_behind_a_release_is_reported_separately():
    receipt = installer.Receipt(ref="v0.5.2", installed_at="", files={})
    assert installer.behind_ref(receipt, "v0.5.3") == "v0.5.2"
    assert installer.behind_ref(receipt, "v0.5.2") == ""


def test_a_ref_that_predates_the_policy_refuses_with_a_readable_reason():
    """Real, not hypothetical: `scripts/git_policy.py` exists only from v0.5.3, so
    any machine resolving an older tag must be told why rather than handed a
    runtime that is missing files."""
    try:
        installer.read_blob(REPO_ROOT, "v0.5.2", "scripts/git_policy.py")
    except installer.InstallRefusedError as error:
        assert "v0.5.2" in str(error)
    else:
        raise AssertionError("a tag without the policy file must refuse")


def test_a_working_tree_install_is_never_reported_behind(tmp_path):
    """A worktree install has no ref to fall behind, so a *tag* comparison says
    nothing true about it either way. `worktree_drift` is what answers the question
    for this ref, by bytes; the tests below it are the ones that hold that."""
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, installer.WORKTREE_REF)
    assert installer.behind_ref(receipt, "v0.5.3") == ""


def _worktree_source(root: Path) -> Path:
    """A checkout holding just the runtime files, so one can be edited under an install."""
    for source_name in installer.RUNTIME_FILES:
        path = root / source_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {source_name}\n", encoding="utf-8")
    return root


def test_a_worktree_install_is_reported_stale_once_the_checkout_moves_on(tmp_path):
    """The reported defect, in the state the machine was actually in.

    `--check` said "up to date" over a dispatcher installed from a checkout that had
    since gained the whole pre-push wiring, so `devkit-push-gate` had not run on any
    push for five days and nothing said so. Neither existing question could catch it:
    `compare_install` asks only whether the install still matches its own receipt, and
    `behind_ref` returns "" for this ref.
    """
    source = _worktree_source(tmp_path / "checkout")
    target = tmp_path / "hooks"
    receipt = installer.install(source, target, installer.WORKTREE_REF)
    assert installer.worktree_drift(source, target, receipt) == []

    (source / "scripts" / "git_policy.py").write_text("# the pre-push wiring\n", encoding="utf-8")
    drifted = installer.worktree_drift(source, target, receipt)
    assert [d.name for d in drifted] == ["devkit_git_policy.py"]
    assert "working tree" in drifted[0].reason


def test_a_release_install_is_not_judged_against_the_working_tree(tmp_path):
    """The false positive `compare_install` exists to avoid, and this must not
    reintroduce it: a checkout ahead of the pinned release is the normal state, so
    only `WORKTREE_REF` opts into the byte comparison."""
    source = _worktree_source(tmp_path / "checkout")
    target = tmp_path / "hooks"
    receipt = installer.install(source, target, installer.WORKTREE_REF)
    (source / "scripts" / "git_policy.py").write_text("# moved on\n", encoding="utf-8")

    assert installer.worktree_drift(source, target, replace(receipt, ref="v0.5.3")) == []
    assert installer.worktree_drift(source, target, None) == []


def test_a_source_file_that_has_gone_missing_is_not_reported_as_stale(tmp_path):
    """`install_files` skips what a source does not hold, so reporting one would be a
    failure that re-installing could not clear."""
    source = _worktree_source(tmp_path / "checkout")
    target = tmp_path / "hooks"
    receipt = installer.install(source, target, installer.WORKTREE_REF)
    (source / "scripts" / "git_policy.py").unlink()
    assert installer.worktree_drift(source, target, receipt) == []


def test_check_exits_one_for_a_worktree_install_the_checkout_has_moved_past(tmp_path, monkeypatch):
    """End to end through `run_check`, because the defect was that this exit code was
    0 -- the finding existing but not being wired in is the same silence."""
    source = _worktree_source(tmp_path / "checkout")
    target = (tmp_path / "hooks").resolve()
    installer.install(source, target, installer.WORKTREE_REF)
    monkeypatch.setattr(installer, "resolve_ref", lambda _root=None: "v0.5.3")
    runner = FakeRunner(hooks_path=f"{target.as_posix()}\n")
    assert installer.run_check(source, target, runner) == 0

    (source / "scripts" / "git_policy.py").write_text("# the pre-push wiring\n", encoding="utf-8")
    assert installer.run_check(source, target, runner) == 1


def test_behind_is_silent_when_either_side_is_unknown():
    assert installer.behind_ref(None, "v0.5.3") == ""


# --- --check exit codes -------------------------------------------------------


def test_check_exits_zero_for_a_current_install(tmp_path, monkeypatch):
    target = (tmp_path / "hooks").resolve()
    installer.install(REPO_ROOT, target, "v0.5.3")
    monkeypatch.setattr(installer, "resolve_ref", lambda _root=None: "v0.5.3")
    runner = FakeRunner(hooks_path=f"{target.as_posix()}\n")
    assert installer.run_check(REPO_ROOT, target, runner) == 0


def test_check_exits_one_for_a_modified_runtime(tmp_path, monkeypatch):
    target = (tmp_path / "hooks").resolve()
    installer.install(REPO_ROOT, target, "v0.5.3")
    (target / "pre-commit").write_text("# tampered\n", encoding="utf-8")
    monkeypatch.setattr(installer, "resolve_ref", lambda _root=None: "v0.5.3")
    runner = FakeRunner(hooks_path=f"{target.as_posix()}\n")
    assert installer.run_check(REPO_ROOT, target, runner) == 1


def test_check_exits_one_when_a_newer_release_exists(tmp_path, monkeypatch):
    target = (tmp_path / "hooks").resolve()
    installer.install(REPO_ROOT, target, "v0.5.3")
    monkeypatch.setattr(installer, "resolve_ref", lambda _root=None: "v0.6.0")
    runner = FakeRunner(hooks_path=f"{target.as_posix()}\n")
    assert installer.run_check(REPO_ROOT, target, runner) == 1


def test_check_exits_two_where_the_policy_is_not_installed(tmp_path):
    """A fresh clone, CI, anyone else's machine. Reporting that as drift would make
    the check meaningless everywhere it is not the point."""
    assert installer.run_check(REPO_ROOT, tmp_path / "hooks", FakeRunner()) == 2


def test_check_exits_two_when_the_hooks_path_belongs_to_someone_else(tmp_path):
    runner = FakeRunner(hooks_path="C:/someone-elses-hooks\n")
    assert installer.run_check(REPO_ROOT, tmp_path / "hooks", runner) == 2


def test_the_drift_report_names_the_fix(tmp_path):
    report = installer.render_drift(
        tmp_path, [drift.Drift("pre-commit", "modified since it was installed")]
    )
    assert "pre-commit" in report
    assert "--yes" in report


# --- uninstall ----------------------------------------------------------------


def test_render_uninstall_plan_names_every_step_and_the_one_it_declines(tmp_path):
    """The plan is read before anything is removed, so it has to say what stays too --
    `fetch.prune` is left set, and a reader who is not told that will assume otherwise."""
    plan = installer.render_uninstall_plan(tmp_path / "hooks")
    assert "core.hooksPath" in plan
    assert "devkit.branchPolicy.failClosed" in plan
    assert str(tmp_path / "hooks") in plan
    assert "fetch.prune" in plan and "left set" in plan


def test_uninstall_clears_the_hooks_path_before_deleting_what_it_points_at(tmp_path):
    """The ordering **is** the safety property. A global `core.hooksPath` naming a
    directory that does not exist fails every git command in every repository on the
    machine, so between the two steps the path must either still resolve or be unset --
    never name a deleted directory.
    """
    target = tmp_path / "hooks"
    target.mkdir()
    (target / "pre-commit").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    runner = FakeRunner(hooks_path=target.resolve().as_posix())

    order: list[str] = []
    real = installer.shutil.rmtree

    def watched(path, *args, **kwargs):
        order.append("rmtree")
        return real(path, *args, **kwargs)

    def watching(argv):
        if "--unset" in argv and "core.hooksPath" in argv:
            order.append("unset")
        return runner(argv)

    installer.shutil.rmtree = watched
    try:
        installer.uninstall(target, watching)
    finally:
        installer.shutil.rmtree = real

    assert order == ["unset", "rmtree"], "the files went before the config pointing at them"
    assert not target.exists()


def test_uninstall_leaves_someone_elses_hooks_path_alone(tmp_path):
    """The same test `ensure_compatible_hooks_path` applies on the way in: a path this
    installer did not set is not this installer's to clear."""
    target = tmp_path / "hooks"
    target.mkdir()
    runner = FakeRunner(hooks_path="C:/someone-elses-hooks")
    undone = installer.unconfigure_git(target, runner)
    assert "core.hooksPath" not in undone
    assert not any("--unset" in call and "core.hooksPath" in call for call in runner.calls)


def test_uninstall_keeps_fetch_prune(tmp_path):
    """A general git preference this installer happened to turn on, not devkit's own
    setting -- removing a behaviour the operator may now rely on is the worse error."""
    target = tmp_path / "hooks"
    target.mkdir()
    runner = FakeRunner(hooks_path=target.resolve().as_posix())
    installer.unconfigure_git(target, runner)
    assert not any("fetch.prune" in call for call in runner.calls)


def test_uninstall_survives_a_machine_where_nothing_was_installed(tmp_path):
    """`--unset` on an absent key exits 5, which is 'nothing to do' rather than a fault;
    an uninstall whose goal is a state has to read that as success."""

    class Absent(FakeRunner):
        def __call__(self, argv):
            self.calls.append(tuple(argv))
            if "--unset" in argv:
                return subprocess.CompletedProcess(argv, 5, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "")

    done = installer.uninstall(tmp_path / "never-installed", Absent())
    assert done == []


def test_the_uninstall_is_a_dry_run_until_yes(tmp_path, capsys):
    target = tmp_path / "hooks"
    target.mkdir()
    (target / "pre-commit").write_text("x", encoding="utf-8")
    assert installer.main(["--uninstall", "--target", str(target)]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert target.exists(), "the dry run removed the runtime"


def test_build_parser_accepts_every_verb_and_the_apply_flag_with_them():
    """The CLI, as its own function so `main` holds decisions rather than declarations.

    The assertion that matters is `--uninstall --yes`: while `--yes` sat in the same
    mutually-exclusive group as the verbs, argparse rejected that combination outright, so
    the uninstall had no dry run to offer and the bare verb had to act on the machine.
    """
    parser = installer.build_parser()
    assert parser.parse_args(["--uninstall", "--yes"]).uninstall is True
    assert parser.parse_args(["--check"]).check is True
    parser.parse_args([])


# --- the two layouts must never coexist -------------------------------------------


"""`RUNTIME_FILES` lists both the flat module and the package so that either ref can be
installed from. The cost is that an install directory can end up holding both, and
Python's resolution order decides which one every hook on the machine imports -- a
package beats a same-named flat module. Reinstalling from a tag that predates the split
therefore writes a module that nothing will import, underneath a package nothing
updated, and the receipt records the module. `--check` then reports the runtime current
at the ref it just installed while the hooks run code from some other release."""


def _install_dir_with_both_layouts(tmp_path):
    """An install directory as a real machine had it: a package left by a newer install
    and the flat module an older tag writes back over the top."""
    target = tmp_path / "hooks"
    (target / "devkit_git_policy").mkdir(parents=True)
    (target / "devkit_git_policy" / "__init__.py").write_text("STALE", encoding="utf-8")
    (target / "devkit_git_policy.py").write_text("fresh", encoding="utf-8")
    return target


def test_installing_the_flat_module_removes_a_package_that_would_shadow_it():
    installed = {"devkit_git_policy.py": "abc"}
    assert layout.shadowing_entrypoint(installed) == "devkit_git_policy/__init__.py"


def test_installing_the_package_names_the_flat_module_as_the_stale_one():
    installed = {"devkit_git_policy/__init__.py": "abc"}
    assert layout.shadowing_entrypoint(installed) == "devkit_git_policy.py"


def test_an_install_that_wrote_no_entrypoint_has_no_shadow_to_clear():
    """`install_refusal` owns that case and must stay the thing that reports it."""
    assert layout.shadowing_entrypoint({"pre-commit": "abc"}) == ""


def test_the_package_directory_is_what_gets_removed_not_its_init():
    """An empty `devkit_git_policy/` is a namespace package and still shadows a flat
    module, so deleting only `__init__.py` would leave the shadow in place."""
    assert (
        layout.entrypoint_path(Path("hooks"), "devkit_git_policy/__init__.py")
        == Path("hooks") / "devkit_git_policy"
    )


def test_clearing_removes_the_whole_stale_package(tmp_path):
    target = _install_dir_with_both_layouts(tmp_path)
    cleared = installer.clear_shadowing_entrypoint(target, {"devkit_git_policy.py": "abc"})
    assert cleared == "devkit_git_policy/__init__.py"
    assert not (target / "devkit_git_policy").exists()
    assert (target / "devkit_git_policy.py").read_text(encoding="utf-8") == "fresh"


def test_clearing_is_a_no_op_when_there_is_nothing_to_clear(tmp_path):
    target = tmp_path / "hooks"
    target.mkdir()
    (target / "devkit_git_policy.py").write_text("fresh", encoding="utf-8")
    assert installer.clear_shadowing_entrypoint(target, {"devkit_git_policy.py": "abc"}) == ""


def test_check_reports_a_package_shadowing_the_module_the_receipt_names(tmp_path):
    """The half `compare_install` could not see. It walks the receipt, and the receipt
    can only describe what it wrote -- so the one file that changes which code runs was
    the one file never looked at."""
    target = _install_dir_with_both_layouts(tmp_path)
    receipt = installer.Receipt(
        ref="v0.11.17",
        installed_at="2026-09-16T00:00:00Z",
        files={"devkit_git_policy.py": installer.digest(b"fresh")},
    )
    drifted = installer.compare_install(target, receipt)
    names = [d.name for d in drifted]
    assert "devkit_git_policy/__init__.py" in names, drifted
    assert any("shadows" in d.reason for d in drifted), drifted


def test_check_stays_quiet_when_only_the_receipts_own_layout_is_present(tmp_path):
    """The check has to be silent in the normal case or nobody reads it."""
    target = tmp_path / "hooks"
    target.mkdir()
    (target / "devkit_git_policy.py").write_text("fresh", encoding="utf-8")
    receipt = installer.Receipt(
        ref="v0.11.17",
        installed_at="2026-09-16T00:00:00Z",
        files={"devkit_git_policy.py": installer.digest(b"fresh")},
    )
    assert installer.compare_install(target, receipt) == []
