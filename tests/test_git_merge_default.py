"""Tests for the trunk-merge task's script.

Two behaviours carry the whole thing and neither is about the happy path.

**A conflict must leave the merge in progress and name every unmerged file.** The task
exists so a long-lived branch can take the trunk, and the expected outcome of that on a
big repo is conflicts someone -- usually an agent -- resolves afterwards. A script that
aborted, or that reported "merge failed" and nothing else, would destroy exactly the
state the next step needs.

**A reference checkout must be reachable.** `devkit_project.known_projects` subtracts
`NOT_PROJECTS`, and every other workspace tool subtracts something similar, because the
actions they dispatch need a harness. This one needs git and nothing else, so it
resolves against the raw registry -- and that is the seam the task was written for.
"""

import json
import subprocess
from pathlib import Path

import pytest
from support import devkit_project, load_script

merge_default = load_script("scripts/git-merge-default.py")

conflicted_paths = merge_default.conflicted_paths
every_checkout = merge_default.every_checkout
main = merge_default.main
merge = merge_default.merge
remediation = merge_default.remediation
resolve_base = merge_default.resolve_base
target_repo = merge_default.target_repo
MergeError = merge_default.MergeError

REPO = Path("C:/checkouts/VanillaLand")

# The fixture registries below are deliberately NOT named `alex-projects.code-workspace`:
# `test_self_hosting.py` treats that literal in an unmarked test as a read of the live
# workstation file, which CI does not have. These tests pass their registry in as an
# argument and never touch the real one.

# The calls a clean run makes, keyed by the git arguments. A test overrides only the one
# it is about; anything unlisted answers success with no output, which keeps each test
# to the single response that makes its point.
CLEAN = {
    ("rev-parse", "--show-toplevel"): (0, str(REPO)),
    ("branch", "--show-current"): (0, "feature/alex-testing"),
    ("fetch", "--prune", "origin"): (0, ""),
    ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"): (0, "origin/develop"),
    ("rev-parse", "--verify", "--quiet", "refs/remotes/origin/develop"): (0, "deadbeef"),
    ("rev-list", "--count", "HEAD..origin/develop"): (0, "7"),
    ("status", "--porcelain"): (0, ""),
    ("merge", "--no-edit", "origin/develop"): (0, "Merge made by the 'ort' strategy."),
}


class FakeGit:
    """A `Runner` answering from a table, recording what it was asked."""

    def __init__(self, **overrides):
        self.responses = dict(CLEAN)
        for key, value in overrides.items():
            self.responses[tuple(key.split("|"))] = value
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv):
        args = tuple(argv[1:])
        self.calls.append(args)
        code, output = self.responses.get(args, (0, ""))
        return subprocess.CompletedProcess(list(argv), code, output, "")

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == prefix for call in self.calls)


def run(fake, base=merge_default.AUTO, remote="origin"):
    return merge(fake, REPO, remote, base)


# --- the registry seam ------------------------------------------------------

WORKSPACE = json.dumps(
    {"folders": [{"path": "carameli"}, {"path": "devkit"}, {"path": "VanillaLand"}]}
)


def test_the_reference_checkout_is_reachable_from_here():
    """The point of the whole script. Every other workspace tool drops this checkout
    because its actions need a harness; a merge needs git, so dropping it here would
    remove the one the task was written for."""
    assert "VanillaLand" in every_checkout(WORKSPACE)
    assert "VanillaLand" not in devkit_project.known_projects(WORKSPACE)


def test_a_named_checkout_resolves_to_its_directory(tmp_path):
    workspace = tmp_path / "registry.code-workspace"
    workspace.write_text(WORKSPACE, encoding="utf-8")
    (tmp_path / "VanillaLand").mkdir()
    assert target_repo("VanillaLand", workspace) == tmp_path / "VanillaLand"


def test_an_unknown_checkout_names_the_real_ones(tmp_path):
    workspace = tmp_path / "registry.code-workspace"
    workspace.write_text(WORKSPACE, encoding="utf-8")
    with pytest.raises(devkit_project.ProjectError, match=r"unknown checkout 'nope'.*VanillaLand"):
        target_repo("nope", workspace)


def test_no_checkout_means_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert target_repo("", tmp_path / "missing.code-workspace") == Path.cwd()


def test_a_missing_registry_is_reported_rather_than_traced(tmp_path):
    with pytest.raises(MergeError, match="cannot read the workspace registry"):
        target_repo("VanillaLand", tmp_path / "missing.code-workspace")


# --- choosing the base ------------------------------------------------------


def test_the_base_is_read_off_the_remote_by_default():
    """VanillaLand's trunk is `develop`; nothing here knows that, and it must not."""
    assert resolve_base(FakeGit(), "origin", merge_default.AUTO) == "develop"


def test_an_explicit_base_wins_over_detection():
    assert resolve_base(FakeGit(), "origin", "release-250312") == "release-250312"


NO_HEAD = {
    "symbolic-ref|--quiet|refs/remotes/origin/HEAD": (1, ""),
    "symbolic-ref|--quiet|--short|refs/remotes/origin/HEAD": (1, ""),
}


def test_an_unpopulated_remote_head_is_repaired_before_falling_back_to_a_guess():
    """The ladder's fallback probes `main` then `master`, which is a guess in any repo
    that has a `main` AND a `develop` -- and it guesses in the direction that merges the
    wrong branch into someone's work. Asking the remote once costs a round trip we can
    afford here, unlike in the pre-commit hook this detector also backs."""
    fake = FakeGit(**NO_HEAD)
    resolve_base(fake, "origin", merge_default.AUTO)
    assert fake.ran("remote", "set-head", "origin", "--auto")


def test_a_populated_remote_head_is_left_alone():
    fake = FakeGit()
    assert resolve_base(fake, "origin", merge_default.AUTO) == "develop"
    assert not fake.ran("remote", "set-head")


def test_an_undetectable_default_asks_for_one_instead_of_guessing():
    fake = FakeGit(
        **NO_HEAD,
        **{
            "rev-parse|--verify|--quiet|refs/remotes/origin/main": (1, ""),
            "rev-parse|--verify|--quiet|refs/remotes/origin/master": (1, ""),
        },
    )
    with pytest.raises(MergeError, match=r"pass --base <branch>"):
        resolve_base(fake, "origin", merge_default.AUTO)


# --- the merge --------------------------------------------------------------


def test_a_clean_run_merges_the_detected_trunk(capsys):
    fake = FakeGit()
    assert run(fake) == 0
    assert fake.ran("merge", "--no-edit", "origin/develop")
    assert "Nothing was pushed" in capsys.readouterr().out


def test_an_explicit_base_is_the_one_merged():
    fake = FakeGit(**{"rev-parse|--verify|--quiet|refs/remotes/origin/main": (0, "cafe")})
    assert run(fake, base="main") == 0
    assert fake.ran("merge", "--no-edit", "origin/main")


def test_nothing_is_ever_pushed():
    """A one-click task that also published would be a very different blast radius from
    the one its `detail` states."""
    fake = FakeGit()
    run(fake)
    assert not fake.ran("push")


def test_an_up_to_date_branch_merges_nothing(capsys):
    fake = FakeGit(**{"rev-list|--count|HEAD..origin/develop": (0, "0")})
    assert run(fake) == 0
    assert not fake.ran("merge")
    assert "Already up to date" in capsys.readouterr().out


def test_a_detached_head_stops_before_fetching(capsys):
    fake = FakeGit(**{"branch|--show-current": (0, "")})
    assert run(fake) == 2
    assert not fake.ran("fetch")
    assert "detached" in capsys.readouterr().err


def test_a_directory_that_is_not_a_repo_is_named(capsys):
    fake = FakeGit(**{"rev-parse|--show-toplevel": (1, "")})
    assert run(fake) == 2
    assert str(REPO) in capsys.readouterr().err


def test_a_failed_fetch_stops_before_merging(capsys):
    fake = FakeGit(**{"fetch|--prune|origin": (128, "fatal: Authentication failed")})
    assert run(fake) == 1
    assert not fake.ran("merge")
    assert "Authentication failed" in capsys.readouterr().err


def test_a_base_that_does_not_exist_on_the_remote_is_refused(capsys):
    fake = FakeGit(**{"rev-parse|--verify|--quiet|refs/remotes/origin/nope": (1, "")})
    assert run(fake, base="nope") == 2
    assert not fake.ran("merge")
    assert "origin/nope does not exist" in capsys.readouterr().err


def test_uncommitted_changes_are_flagged_but_do_not_block(capsys):
    """git decides: it merges around edits it does not touch and refuses, changing
    nothing, when it would. Pre-empting that would refuse merges git would allow."""
    fake = FakeGit(**{"status|--porcelain": (0, " M app/Web.config\n?? notes.txt")})
    assert run(fake) == 0
    assert fake.ran("merge", "--no-edit", "origin/develop")
    assert "2 file(s) have uncommitted changes" in capsys.readouterr().out


# --- the conflict path, which is the expected one ---------------------------

CONFLICTED = {
    "merge|--no-edit|origin/develop": (1, "CONFLICT (content): Merge conflict in a.cs"),
    "diff|--name-only|--diff-filter=U": (0, "src/a.cs\nsrc/b.cs\n"),
}


def test_a_conflict_leaves_the_merge_in_progress():
    """Aborting would discard the resolution work the task exists to hand off."""
    fake = FakeGit(**CONFLICTED)
    assert run(fake) == 1
    assert not fake.ran("merge", "--abort")
    assert not fake.ran("reset")


def test_a_conflict_names_every_unmerged_file(capsys):
    assert run(FakeGit(**CONFLICTED)) == 1
    err = capsys.readouterr().err
    assert "src/a.cs" in err
    assert "src/b.cs" in err


def test_a_conflict_reports_the_two_branches_and_the_checkout(capsys):
    """Whoever reads the artifact is usually not whoever clicked the task."""
    assert run(FakeGit(**CONFLICTED)) == 1
    err = capsys.readouterr().err
    assert "origin/develop" in err
    assert "feature/alex-testing" in err
    assert str(REPO) in err


def test_a_refusal_with_nothing_unmerged_reports_gits_own_message(capsys):
    """Uncommitted changes in the way, or unrelated histories: nothing was started, so
    there is nothing to resolve and the remediation block would be a lie."""
    fake = FakeGit(
        **{
            "merge|--no-edit|origin/develop": (1, "error: Your local changes would be overwritten"),
            "diff|--name-only|--diff-filter=U": (0, ""),
        }
    )
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "would be overwritten" in err
    assert "MERGE CONFLICT" not in err


def test_conflicted_paths_ignores_blank_lines():
    assert conflicted_paths("a.cs\n\n  b.cs  \n") == ["a.cs", "b.cs"]


def test_the_remediation_block_carries_a_prompt_that_forbids_aborting():
    text = remediation(REPO, "feature/x", "origin/develop", ["a.cs"])
    assert "do not abort it" in text
    assert "git merge --abort" in text  # still offered, as the deliberate way out


# --- the entrypoint ---------------------------------------------------------


def test_main_runs_against_the_checkout_it_was_given(tmp_path):
    workspace = tmp_path / "registry.code-workspace"
    workspace.write_text(WORKSPACE, encoding="utf-8")
    (tmp_path / "VanillaLand").mkdir()
    seen = {}

    def factory(repo):
        seen["repo"] = repo
        return FakeGit()

    code = main(["--checkout", "VanillaLand", "--workspace", str(workspace)], run_factory=factory)
    assert code == 0
    assert seen["repo"] == tmp_path / "VanillaLand"


def test_main_reports_a_bad_checkout_without_touching_git(tmp_path, capsys):
    workspace = tmp_path / "registry.code-workspace"
    workspace.write_text(WORKSPACE, encoding="utf-8")

    def factory(repo):  # pragma: no cover - reaching this is the failure
        raise AssertionError("git must not be reached for an unresolvable checkout")

    assert main(["--checkout", "ghost", "--workspace", str(workspace)], run_factory=factory) == 2
    assert "ghost" in capsys.readouterr().err
