"""Tests for installing Devkit's global Git hook dispatcher."""

import os
import subprocess
import sys
from dataclasses import replace

from support import REPO_ROOT, load_script

installer = load_script("scripts/install-git-policy.py")


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

    assert (target / "devkit_git_policy.py").is_file()
    for name in ("pre-commit", "pre-push"):
        hook = target / name
        assert hook.is_file()
        if os.name != "nt":
            assert os.access(hook, os.X_OK)


def test_installed_wrappers_delegate_to_the_policy_module(tmp_path):
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    (target / "devkit_git_policy.py").write_text(
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


def test_the_default_source_is_a_released_tag_not_the_working_tree():
    """The prevention, in one assertion. A working-tree install must be something
    you ask for by name, never what you get by not thinking about it."""
    ref = installer.resolve_ref(REPO_ROOT)
    assert ref != installer.WORKTREE_REF
    assert ref.startswith("v"), ref


def test_installing_from_a_ref_writes_that_refs_bytes(tmp_path):
    target = tmp_path / "hooks"
    ref = installer.resolve_ref(REPO_ROOT)
    installer.install(REPO_ROOT, target, ref)

    expected = installer.read_blob(REPO_ROOT, ref, "scripts/git_policy.py")
    assert (target / "devkit_git_policy.py").read_bytes() == expected


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
    assert set(receipt.files) == set(installer.RUNTIME_FILES.values())
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
    """It is as current as it can be described; nagging would punish the escape
    hatch rather than the accident."""
    target = tmp_path / "hooks"
    receipt = installer.install(REPO_ROOT, target, installer.WORKTREE_REF)
    assert installer.behind_ref(receipt, "v0.5.3") == ""


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
        tmp_path, [installer.Drift("pre-commit", "modified since it was installed")]
    )
    assert "pre-commit" in report
    assert "--yes" in report
