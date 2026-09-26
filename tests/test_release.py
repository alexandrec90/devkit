"""Tests for scripts/release.py.

The ordering assertion at the bottom is the one that matters: it is the machine-
checkable form of RELEASING.md steps 3-5, and getting it wrong breaks projects
generated afterwards rather than anything here.
"""

import subprocess

import pytest
from support import REPO_ROOT, load_script

release = load_script("scripts/release.py")

SOURCE = 'DEVKIT_ROOT = Path("x")\nFALLBACK_DEVKIT_REF = "v0.5.2"\n\n\ndef f():\n    pass\n'


@pytest.mark.parametrize(
    ("version", "ok"),
    [
        ("v0.5.3", True),
        ("v1.0.0", True),
        ("0.5.3", False),  # no leading v -- pins nothing
        ("v0.5", False),
        ("v0.5.3-rc1", False),
        ("main", False),
        ("", False),
    ],
)
def test_only_a_full_v_prefixed_version_is_accepted(version, ok):
    """The tag is what consumers pin; a malformed one fails in their repo, later."""
    assert release.valid_version(version) is ok


def test_the_fallback_constant_is_retargeted():
    updated, previous = release.bump_fallback(SOURCE, "v0.5.3")
    assert previous == "v0.5.2"
    assert 'FALLBACK_DEVKIT_REF = "v0.5.3"' in updated


def test_bumping_leaves_the_rest_of_the_file_alone():
    updated, _ = release.bump_fallback(SOURCE, "v0.5.3")
    assert 'DEVKIT_ROOT = Path("x")' in updated
    assert updated.count("v0.5.3") == 1


def test_a_missing_constant_is_reported_not_silently_skipped():
    updated, previous = release.bump_fallback("x = 1\n", "v0.5.3")
    assert previous is None
    assert updated == "x = 1\n"


def test_the_real_new_project_still_carries_the_constant():
    """Guards the coupling: release.py rewrites a constant it does not own."""
    text = (REPO_ROOT / "scripts" / "new-project.py").read_text(encoding="utf-8")
    _, previous = release.bump_fallback(text, "v9.9.9")
    assert previous, "FALLBACK_DEVKIT_REF is gone or renamed; release.py cannot bump it"


def test_an_existing_tag_is_refused():
    """Releases are immutable. Re-tagging silently changes what every consumer
    pinned to that version resolves to."""
    steps, refusal = release.prepare_plan("v0.5.3", "v0.5.2", {"v0.5.2", "v0.5.3"})
    assert not steps
    assert "already exists" in refusal


def test_a_malformed_version_is_refused():
    steps, refusal = release.prepare_plan("0.5.3", "v0.5.2", set())
    assert not steps
    assert "vMAJOR.MINOR.PATCH" in refusal


def test_re_releasing_the_current_fallback_is_refused():
    steps, refusal = release.prepare_plan("v0.5.2", "v0.5.2", set())
    assert not steps
    assert "nothing to bump" in refusal


def test_a_clean_release_is_planned():
    steps, refusal = release.prepare_plan("v0.5.3", "v0.5.2", {"v0.5.2"})
    assert refusal == ""
    assert steps


def test_phase_one_never_tags():
    """`main` is protected, so the bump reaches it through a PR. A tag cut before
    that PR merges names a commit a squash merge may never put on `main` -- and
    cutting v0.5.3 hit exactly that, leaving main with a newer tag than its own
    constant until the PR landed."""
    steps, _ = release.prepare_plan("v0.5.3", "v0.5.2", set())
    assert not any(s.startswith("git tag") for s in steps), steps
    assert any(s.startswith("git push -u origin release/") for s in steps), steps


def test_phase_one_bumps_before_it_commits():
    steps, _ = release.prepare_plan("v0.5.3", "v0.5.2", set())
    bump = next(i for i, s in enumerate(steps) if "FALLBACK_DEVKIT_REF" in s)
    commit = next(i for i, s in enumerate(steps) if s.startswith("git commit"))
    push = next(i for i, s in enumerate(steps) if s.startswith("git push"))
    assert bump < commit < push, steps


def test_phase_two_refuses_until_the_bump_is_on_main():
    """The mirror image: tagging a main whose constant still names the previous
    release is the same red state, reached from the other direction."""
    steps, refusal = release.tag_plan("v0.5.3", "v0.5.2", set())
    assert not steps
    assert "merge the phase-1 PR" in refusal


def test_phase_two_tags_main_once_the_bump_landed():
    steps, refusal = release.tag_plan("v0.5.3", "v0.5.3", set())
    assert refusal == ""
    # By name, not HEAD: the merge happened on the remote and a stale local
    # checkout would otherwise tag the wrong commit.
    assert steps[0] == "git tag v0.5.3 origin/main"


def test_an_unreadable_tag_list_is_refused_not_read_as_empty(capsys, monkeypatch):
    """An empty set passes both plans' "that tag already exists" refusal, so a git
    failure read as one would re-cut an immutable release. It stops at the read."""
    failed = subprocess.CompletedProcess(
        [], 128, stdout="", stderr="fatal: detected dubious ownership"
    )
    monkeypatch.setattr(release, "_git", lambda *_args: failed)
    with pytest.raises(RuntimeError, match="dubious ownership"):
        release.existing_tags()
    assert release.main(["v0.9.1", "--tag"]) == 2
    assert (
        "could not read devkit's tags: fatal: detected dubious ownership" in capsys.readouterr().err
    )


def test_phase_two_still_refuses_an_existing_tag():
    steps, refusal = release.tag_plan("v0.5.3", "v0.5.3", {"v0.5.3"})
    assert not steps
    assert "immutable" in refusal


def test_the_release_branch_is_named_after_the_version():
    assert release.branch_for("v0.5.3") == "release/v0.5.3"
