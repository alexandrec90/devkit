"""Tests for scripts/upgrade-project.py (adopting a devkit release in a consumer).

The refusals are the interesting half. An upgrade that runs when it should not is
how a harness bump ends up mixed into unrelated work — the failure that motivated
this script — so each precondition is pinned individually.
"""

import contextlib
import datetime as dt
import json
import string
import subprocess

import pytest
from support import REPO_ROOT, load_script, sweep

up = load_script("scripts/upgrade-project.py")

DATE = dt.date(2026, 8, 2)


@pytest.fixture(autouse=True)
def artifact_elsewhere(tmp_path, monkeypatch):
    """Keep `logs/upgrade.log` out of the real devkit checkout during a test run.

    Every exit from `main` writes the artifact, so without this the suite would
    overwrite whatever a real upgrade had left there -- with the outcome of a run
    against a fixture workspace, which is the most misleading thing it could say."""
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path / "_artifact_root")


def done(code: int = 0):
    """A stand-in `upgrade_one` result, for the tests that fake the per-project work."""
    return lambda *_a, **_kw: up.Outcome("stub", code)


def clean(**overrides) -> sweep.State:
    """A consumer sitting on its home branch with nothing uncommitted."""
    base = {
        "name": "carameli",
        "default_branch": "master",
        "branch": "master",
        "host": "github",
    }
    return sweep.State(**{**base, **overrides})


# --- naming ------------------------------------------------------------------


def test_the_branch_is_a_dated_task_branch():
    assert up.branch_name(DATE) == "claude/devkit-upgrade-0802"


def test_the_commit_names_the_release():
    """Unlike a swept commit, the version really is the description here."""
    assert up.commit_message("v0.5.3", 38) == "Adopt devkit v0.5.3 (38 vendored file(s))"


def test_the_planned_commit_does_not_claim_a_file_count_it_cannot_know(
    tmp_path, capsys, monkeypatch
):
    """The plan is printed before the pull runs, so the count does not exist yet.
    It printed `0` there, which described every applied run wrongly."""
    monkeypatch.setattr(up.sweep, "inspect", lambda *_a, **_kw: clean())
    (tmp_path / "DEVKIT_VERSION").write_text("1234567\n", encoding="utf-8")
    assert up.upgrade_one("carameli", tmp_path, "v0.5.3").code == 0
    printed = capsys.readouterr().out
    assert "<n> vendored file(s)" in printed
    assert "0 vendored file(s)" not in printed


def test_the_pr_body_lists_all_four_pins_and_the_previous_version():
    body = up.pr_body("v0.5.3", "v0.5.2", ["scripts/hooks/stop.py"])
    assert "v0.5.3" in body and "v0.5.2" in body
    assert "DEVKIT_VERSION" in body
    assert "rev:" in body
    assert "ref:" in body
    assert "- `scripts/hooks/stop.py`" in body


def test_the_pr_body_truncates_a_long_file_list():
    files = [f"scripts/hooks/f{i}.py" for i in range(120)]
    body = up.pr_body("v0.5.3", "v0.5.2", files)
    assert f"and {120 - sweep.PR_BODY_FILE_LIMIT} more" in body


# --- refusals ----------------------------------------------------------------


def test_a_clean_project_on_its_home_branch_is_upgradable():
    assert up.refusal(clean(), "v0.5.3") == ""
    assert up.plan(clean(), "v0.5.3", DATE).steps == (
        ("checkout", "-b", "claude/devkit-upgrade-0802"),
    )


def test_a_devkit_with_no_releases_is_refused():
    """There is nothing to adopt. Note this is about *tags existing*, not about
    where devkit's HEAD happens to sit -- keying off HEAD made this refuse on
    nearly every run, since devkit normally lives on a working branch."""
    reason = up.refusal(clean(), None)
    assert "no release tags" in reason
    assert up.plan(clean(), None, DATE).refusal


# A made-up commit SHA, not a credential — but detect-secrets cannot tell a 40-char hex
# string from a key, so it is allowlisted inline per `.pre-commit-config.yaml`'s note.
RELEASE_COMMIT = "9d95e4471bd60d6f3a2c81e5f7c0a4b8d1e2f3a4"  # pragma: allowlist secret


def test_a_project_stamped_with_the_release_commit_is_current(tmp_path):
    """The scheduled-run case: proving a project is up to date reads one file and
    touches nothing, so it cannot fail on a dirty tree or the wrong branch."""
    (tmp_path / "DEVKIT_VERSION").write_text("9d95e44\n", encoding="utf-8")
    assert up.is_current(tmp_path, RELEASE_COMMIT)
    assert not up.is_current(tmp_path, "a" * 40)


def test_the_stamp_is_never_compared_against_the_tag_name(tmp_path):
    """The regression. DEVKIT_VERSION holds the upstream **SHA** by contract, so a
    predicate that compared it to `v0.5.3` was false for every project forever --
    and each run then cut a branch, built a worktree and ran a full pull on a project
    already on the release, only to abandon once the tree came back clean. That is
    the "already current" line that contradicted the plan printed above it."""
    (tmp_path / "DEVKIT_VERSION").write_text("9d95e44\n", encoding="utf-8")
    assert not up.is_current(tmp_path, "v0.5.3")
    assert up.is_current(tmp_path, RELEASE_COMMIT)


def test_a_provisional_pull_is_never_current(tmp_path):
    """`--allow-dirty` stamps `<rev>-dirty`; those files are at no release at all."""
    (tmp_path / "DEVKIT_VERSION").write_text("9d95e44-dirty\n", encoding="utf-8")
    assert not up.is_current(tmp_path, RELEASE_COMMIT)


def test_a_stamp_that_is_not_a_sha_proves_nothing(tmp_path):
    """`git_head` writes `unknown` when it cannot read the source's HEAD."""
    (tmp_path / "DEVKIT_VERSION").write_text("unknown\n", encoding="utf-8")
    assert not up.is_current(tmp_path, RELEASE_COMMIT)


def test_a_full_length_stamp_matches_too():
    assert up.names_commit(RELEASE_COMMIT, RELEASE_COMMIT)


def test_an_abbreviation_too_short_to_be_a_sha_is_rejected():
    """Guards the prefix test: a 2-char "commit" would match a sixteenth of them."""
    assert not up.names_commit("9d", RELEASE_COMMIT)


def test_currency_cannot_be_proven_without_the_release_commit(tmp_path):
    """A rev git could not resolve means "cannot tell", and cannot tell is not
    current -- the run proceeds and finds out by pulling."""
    (tmp_path / "DEVKIT_VERSION").write_text("9d95e44\n", encoding="utf-8")
    assert not up.is_current(tmp_path, "")


def test_a_project_that_never_vendored_is_not_current(tmp_path):
    assert not up.is_current(tmp_path, RELEASE_COMMIT)


def test_the_release_commit_is_resolved_from_the_devkit_checkout():
    """Against the real repo, because the point of `commit_for` is what git says."""
    head = up.commit_for(REPO_ROOT, "HEAD")
    assert len(head) == 40
    assert all(char in string.hexdigits for char in head)
    assert up.commit_for(REPO_ROOT, "no-such-rev-anywhere") == ""


def test_a_checkout_stamped_with_devkits_head_is_current(tmp_path):
    """End to end, with a real SHA and a real abbreviation of it: this is the shape
    every consumer's DEVKIT_VERSION actually has."""
    head = up.commit_for(REPO_ROOT, "HEAD")
    (tmp_path / "DEVKIT_VERSION").write_text(f"{head[:7]}\n", encoding="utf-8")
    assert up.is_current(tmp_path, head)


def test_a_dirty_project_is_refused():
    """A branch cut under uncommitted work carries it along, and the commit would
    have to guess which files belonged to the upgrade."""
    reason = up.refusal(clean(dirty=364), "v0.5.3")
    assert "uncommitted" in reason
    assert not up.plan(clean(dirty=364), "v0.5.3", DATE).steps


def test_a_project_already_on_a_task_branch_is_refused():
    reason = up.refusal(clean(branch="claude/thing-0801"), "v0.5.3")
    assert "task branch" in reason


def test_a_non_git_directory_is_refused():
    assert up.refusal(clean(is_git=False), "v0.5.3") == "not a git checkout"


def test_every_refusal_state_yields_a_plan_that_says_why():
    """No silent no-ops: a plan with neither steps nor a refusal reads as done."""
    states = [
        clean(),
        clean(dirty=1),
        clean(is_git=False),
        clean(branch="claude/x-0801"),
    ]
    for state in states:
        for tag in ("v0.5.3", None):
            built = up.plan(state, tag, DATE)
            assert built.steps or built.refusal, (state, tag)


def test_the_upgrade_records_the_home_branch_it_came_from():
    # So `sweep --sync` can park the worktree back afterwards.
    assert up.plan(clean(branch="carameli-b"), "v0.5.3", DATE).anchor == "carameli-b"


# --- what gets committed -----------------------------------------------------


class FakeGit:
    def __init__(self, porcelain: str = ""):
        self.porcelain = porcelain

    def __call__(self, *args: str):
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=0, stdout=self.porcelain, stderr=""
        )


def test_changed_paths_reports_everything_the_pull_touched():
    """Safe to take wholesale because the tree was clean beforehand -- the refusal
    above is what makes `add -A` correct rather than reckless."""
    git = FakeGit(" M DEVKIT_VERSION\n M .pre-commit-config.yaml\n?? scripts/hooks/new.py\n")
    assert up.changed_paths(git) == [
        "DEVKIT_VERSION",
        ".pre-commit-config.yaml",
        "scripts/hooks/new.py",
    ]


def test_an_already_current_project_has_nothing_to_commit():
    assert up.changed_paths(FakeGit("")) == []


class RecordingGit:
    """Records calls; `fail_on` makes the first matching call fail."""

    def __init__(self, fail_on: str = ""):
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        failed = bool(self.fail_on) and self.fail_on in " ".join(args)
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1 if failed else 0, stdout="", stderr="nope"
        )


def test_a_no_op_upgrade_leaves_no_branch_behind():
    """This is meant to run on a schedule to prove nothing is stale, so the
    already-current path has to be free: one empty claude/devkit-upgrade branch per
    check would be litter that --sync then has to reap."""
    git = RecordingGit()
    assert up._abandon(git, "carameli", "master", "already current", code=0) == 0
    assert git.calls[0] == ("checkout", "master")
    assert git.calls[1][:2] == ("branch", "-d")


def test_every_abandon_message_names_its_project(capsys):
    """Under `--all` these interleave with other checkouts' lines, and an
    unattributed "already current" is a sentence with no subject."""
    for fail_on, why in (("", "already current"), ("branch -d", "x"), ("checkout", "y")):
        up._abandon(RecordingGit(fail_on=fail_on), "carameli", "master", why, code=0)
        captured = capsys.readouterr()
        assert "carameli" in captured.out + captured.err, fail_on


def test_abandoning_never_force_deletes():
    """`branch -d` refusing means the run did more than it thought -- a state for a
    human, not one to force past."""
    git = RecordingGit(fail_on="branch -d")
    assert up._abandon(git, "carameli", "master", "already current", code=0) == 2
    assert not any(step[:2] == ("branch", "-D") for step in git.calls)


def test_abandoning_reports_when_it_cannot_get_home():
    git = RecordingGit(fail_on="checkout")
    assert up._abandon(git, "carameli", "master", "the pull refused", code=2) == 2
    assert not any(step[:2] == ("branch", "-d") for step in git.calls)


# --- scope: which checkouts `--all` covers -----------------------------------


def test_the_devkit_source_is_never_its_own_upgrade_target():
    """It is where the release comes from; pulling it into itself is meaningless."""
    assert up.select_all([up.Candidate("devkit", is_source=True)]) == [("devkit", up.SKIP_SOURCE)]


def test_the_source_is_reported_as_the_source_even_though_it_never_vendored():
    """devkit has no DEVKIT_VERSION either, and "it never adopted" would be a
    misleading way to describe the repo that publishes the releases."""
    decided = up.select_all([up.Candidate("devkit", adopts=False, is_source=True)])
    assert decided == [("devkit", up.SKIP_SOURCE)]


def test_a_checkout_that_never_vendored_is_skipped_not_refused():
    """A workspace holds checkouts that were never generated from devkit. Passing
    over one is not a failure, and must not colour the exit code of a run that
    upgraded everything it could."""
    assert up.select_all([up.Candidate("reference-repo", adopts=False)]) == [
        ("reference-repo", up.SKIP_UNADOPTED)
    ]


def test_worktree_siblings_are_upgraded_once_and_the_first_listed_wins():
    """Both would cut `claude/devkit-upgrade-<mmdd>` in one ref store, so the second
    would fail on a branch that already exists -- and land a duplicate PR if it did not."""
    decided = up.select_all(
        [
            up.Candidate("carameli", common_dir="/repos/carameli/.git"),
            up.Candidate("carameli-b", common_dir="/repos/carameli/.git"),
        ]
    )
    assert decided[0] == ("carameli", "")
    assert "shares a repo with carameli" in decided[1][1]


def test_the_sibling_skip_names_the_way_around_it():
    """First-listed-wins is arbitrary when that one turns out to be dirty, so the
    message has to point at the escape hatch: name the sibling explicitly."""
    decided = up.select_all(
        [
            up.Candidate("carameli", common_dir="/repos/carameli/.git"),
            up.Candidate("carameli-b", common_dir="/repos/carameli/.git"),
        ]
    )
    assert "name this one explicitly" in decided[1][1]


def test_two_clones_of_one_remote_are_both_upgraded():
    """Separate ref stores, separate vendored copies -- each needs its own PR."""
    decided = up.select_all(
        [
            up.Candidate("carameli", common_dir="/repos/a/.git"),
            up.Candidate("carameli-clone", common_dir="/repos/b/.git"),
        ]
    )
    assert decided == [("carameli", ""), ("carameli-clone", "")]


def test_checkouts_git_would_not_identify_are_not_collapsed_together():
    """An empty ref store is "unknown", not "the same one" -- bucketing them would
    silently skip every project after the first."""
    decided = up.select_all([up.Candidate("one"), up.Candidate("two")])
    assert decided == [("one", ""), ("two", "")]


def test_candidates_read_the_source_and_the_stamp_off_disk(tmp_path):
    devkit = tmp_path / "devkit"
    devkit.mkdir()
    adopter = tmp_path / "carameli"
    adopter.mkdir()
    (adopter / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    (tmp_path / "VanillaLand").mkdir()

    built = up.candidates_for(tmp_path, ["devkit", "carameli", "VanillaLand"], devkit)
    assert [c.is_source for c in built] == [True, False, False]
    assert [c.adopts for c in built] == [True, True, False]


def test_a_missing_checkout_directory_is_an_unadopted_one(tmp_path):
    """`--all` reads names from the workspace file, which can outlive a directory.
    Reporting the skip beats crashing the whole run on one stale entry."""
    built = up.candidates_for(tmp_path, ["gone"], tmp_path / "devkit")
    assert built == [up.Candidate("gone", adopts=False)]


# --- the CLI scope contract ---------------------------------------------------


def workspace(tmp_path, *names):
    """A `.code-workspace` listing `names`, as `parse_workspace` reads them."""
    path = tmp_path / "alex-projects.code-workspace"
    path.write_text(json.dumps({"folders": [{"path": n} for n in names]}), encoding="utf-8")
    return path


def test_a_scope_is_mandatory():
    """Neither branch of the VS Code picker can emit nothing, and a bare run that
    guessed "all" would open PRs nobody asked for."""
    with pytest.raises(SystemExit) as exit_info:
        up.main([])
    assert exit_info.value.code == 2


def test_naming_a_project_and_all_at_once_is_rejected():
    with pytest.raises(SystemExit) as exit_info:
        up.main(["carameli", "--all"])
    assert exit_info.value.code == 2


def test_multi_project_scope_is_comma_delimited():
    assert up.project_selection("carameli, ibkr_trader,carameli") == [
        "carameli",
        "ibkr_trader",
    ]


def test_an_untagged_devkit_stops_the_whole_run_once(tmp_path, capsys):
    """Same fact about devkit for every project; repeating it per project would read
    as four problems rather than one."""
    ws = workspace(tmp_path, "carameli", "carameli-b")
    assert up.main(["--all", "--workspace", str(ws), "--devkit", str(tmp_path / "nope")]) == 1
    assert capsys.readouterr().err.count("no release tags") == 1


def test_naming_the_devkit_source_is_an_error_not_a_skip(tmp_path, capsys, monkeypatch):
    """A skip is what `--all` does with a checkout it was not asked about. Asked
    directly, this cannot do what the operator wants, and must not exit 0."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    (tmp_path / "devkit").mkdir()
    ws = workspace(tmp_path, "devkit")
    code = up.main(["devkit", "--workspace", str(ws), "--devkit", str(tmp_path / "devkit")])
    assert code == 2
    assert up.SKIP_SOURCE in capsys.readouterr().err


def test_a_run_where_every_project_is_current_touches_nothing(tmp_path, capsys, monkeypatch):
    """The scheduled-run property, now at workspace scale: proving nothing is stale
    must not cut a branch, and must not build the source worktree either.

    The stamps here are what `sync-devkit.py --pull` really writes -- an abbreviated
    SHA. Written as `v0.5.3` this passed while the shipped predicate could not
    recognise a single real project, and the whole workspace was pulled every run."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "commit_for", lambda _devkit, _rev: RELEASE_COMMIT)
    monkeypatch.setattr(
        up, "source_at_tag", lambda *_a: pytest.fail("built a worktree with nothing to pull")
    )
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("9d95e44\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert capsys.readouterr().out.count("already on devkit v0.5.3") == 2


def test_a_project_on_an_older_release_is_still_upgraded(tmp_path, capsys, monkeypatch):
    """The other half of the predicate: a stamp naming some *other* commit is not
    current, so the run must still do the work rather than report all clear."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "commit_for", lambda _devkit, _rev: RELEASE_COMMIT)
    monkeypatch.setattr(up, "source_at_tag", _no_worktree)
    monkeypatch.setattr(up, "upgrade_one", done())
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / "DEVKIT_VERSION").write_text("1234567\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert "already on devkit" not in capsys.readouterr().out


def test_one_project_refusing_does_not_stop_the_others(tmp_path, capsys, monkeypatch):
    """Independent repos, independent PRs. Stopping at the first refusal would make
    `--all` useless the moment one checkout is mid-task."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    seen: list[str] = []

    def fake_upgrade(name, project, tag, source=None):
        seen.append(name)
        return up.Outcome(name, 1 if name == "carameli" else 0)

    monkeypatch.setattr(up, "upgrade_one", fake_upgrade)
    monkeypatch.setattr(up, "source_at_tag", _no_worktree)
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 1
    assert seen == ["carameli", "ibkr_trader"]


def test_the_run_reports_the_worst_outcome(tmp_path, monkeypatch):
    """A failure mid-flight (2) outranks a refusal (1), which outranks a clean run:
    the exit code is what a scheduled invocation alerts on."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "source_at_tag", _no_worktree)
    codes = iter([1, 2])
    monkeypatch.setattr(up, "upgrade_one", lambda *_a, **_kw: up.Outcome("stub", next(codes)))
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 2


def test_one_source_worktree_serves_the_whole_run(tmp_path, monkeypatch):
    """Every project adopts the same revision, so checking it out once per project
    would pay the clone cost N times for one tree."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    built: list[str] = []

    @contextlib.contextmanager
    def counting_source(_devkit, tag):
        built.append(tag)
        yield tmp_path / "src"

    monkeypatch.setattr(up, "source_at_tag", counting_source)
    monkeypatch.setattr(up, "upgrade_one", done())
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert built == ["v0.5.3"]


def test_a_dry_run_still_reports_a_refusal(tmp_path, monkeypatch):
    """A dry run is how a scheduled check asks whether the release *could* be adopted.
    Exiting 0 over a project parked on a task branch answers "all clear" for the one
    state that is not."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "upgrade_one", done(1))
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli")
    assert up.main(["--all", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 1


def test_a_dry_run_never_builds_a_source_worktree(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(
        up, "source_at_tag", lambda *_a: pytest.fail("a dry run pulled from somewhere")
    )
    monkeypatch.setattr(up, "upgrade_one", done())
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli")
    assert up.main(["--all", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert "Dry run" in capsys.readouterr().out


@contextlib.contextmanager
def _no_worktree(_devkit, _tag):
    """Stands in for `source_at_tag` when the test does not care about the source."""
    yield None


# --- the pull is self-modifying ----------------------------------------------
#
# `scripts/sync-devkit.py` is itself a MANIFEST entry, so one pass runs the *old*
# release's file list. v0.7.0 added seven entries and retired three; a single pull
# moved 17 files, reported success, and left those ten -- and the only thing that
# noticed was the `devkit-drift` pre-commit hook failing `git commit` afterwards, in
# three consumers at once.


def fake_pull(writes: list[str], returncode: int = 0):
    """A `--pull` stand-in that rewrites the vendored puller with `writes[i]` per pass.

    A repeated entry is what convergence looks like from here: the file the pull would
    write is the file already on disk, so the next pass has nothing left to change.
    """
    calls: list[str] = []

    def pull(project, _source):
        text = writes[min(len(calls), len(writes) - 1)]
        calls.append(text)
        target = project / up.SYNC_SCRIPT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(["pull"], returncode, stdout="pulled", stderr="refused")

    return pull


def vendored(root, text: str):
    """Put a copy of the puller in `root`, the way a consumer carries one."""
    target = root / up.SYNC_SCRIPT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return root


def test_a_release_that_grew_the_manifest_is_pulled_twice(tmp_path):
    """The second pass is the one that runs the new MANIFEST. Without it the entries
    the release *added* are not in the list being copied, and nothing says so."""
    project = vendored(tmp_path, "MANIFEST = old")
    runs, divergence = up.pull_to_fixpoint(
        project, tmp_path / "src", pull=fake_pull(["MANIFEST = new"])
    )
    assert len(runs) == 2
    assert divergence == ""


def test_a_pull_that_leaves_the_puller_alone_runs_once(tmp_path):
    """The common case -- a release that changed no MANIFEST entry -- must not pay for
    a second full copy of the tree."""
    project = vendored(tmp_path, "MANIFEST = same")
    pull = fake_pull(["MANIFEST = same"])
    runs, divergence = up.pull_to_fixpoint(project, tmp_path / "src", pull=pull)
    assert (len(runs), divergence) == (1, "")


def test_a_refused_pull_is_not_retried(tmp_path):
    """Re-running a refusal only refuses again, and the second one is not news."""
    project = vendored(tmp_path, "MANIFEST = old")
    pull = fake_pull(["a", "b", "c"], returncode=1)
    runs, divergence = up.pull_to_fixpoint(project, tmp_path / "src", pull=pull)
    assert (len(runs), divergence) == (1, "")


def test_a_puller_that_never_settles_is_reported_rather_than_looped(tmp_path):
    """Two repos that disagree about the manifest would pull forever. Three passes is
    already one more than a real upgrade needs."""
    project = vendored(tmp_path, "start")
    pull = fake_pull(["a", "b", "c", "d"])
    runs, divergence = up.pull_to_fixpoint(project, tmp_path / "src", pull=pull)
    assert len(runs) == up.MAX_PULL_PASSES
    assert "do not agree on what the MANIFEST is" in divergence


def test_a_project_with_no_vendored_puller_reads_as_empty(tmp_path):
    """`sync_script_bytes` is a comparison, not an assertion: an absent file is a
    value, so a project that has never vendored does not crash the fixpoint."""
    assert up.sync_script_bytes(tmp_path) == b""


# --- the drift check runs here, not at commit time ---------------------------


def upgraded(tmp_path, monkeypatch, git, *, runs, divergence="", check=None):
    """Drive `upgrade_one` past the git plumbing, with the pull's result supplied."""
    monkeypatch.setattr(up.sweep, "inspect", lambda *_a, **_kw: clean())
    monkeypatch.setattr(
        up.sweep,
        "apply_plan",
        lambda name, *_a, **_kw: sweep.Applied(name, up.plan(clean(), "v0.7.0")),
    )
    monkeypatch.setattr(up.sweep, "git_for", lambda _project: git)
    monkeypatch.setattr(up, "pull_to_fixpoint", lambda *_a, **_kw: (runs, divergence))
    monkeypatch.setattr(
        up, "verify_pull", lambda *_a: check or subprocess.CompletedProcess(["check"], 0, "", "")
    )
    (tmp_path / "DEVKIT_VERSION").write_text("1234567\n", encoding="utf-8")
    return up.upgrade_one("carameli", tmp_path, "v0.7.0", source=tmp_path / "src")


def test_drift_the_pull_left_is_refused_before_the_commit_gate_sees_it(tmp_path, monkeypatch):
    """This is the failure as it actually happened: the commit ran, the `devkit-drift`
    hook rejected it, and the operator got a hook id instead of a file list. Checking
    here reports it where the tree that caused it is still the subject."""
    git = RecordingGit()
    outcome = upgraded(
        tmp_path,
        monkeypatch,
        git,
        runs=[subprocess.CompletedProcess(["pull"], 0, "moved 17 file(s)", "")],
        check=subprocess.CompletedProcess(
            ["check"], 1, "DRIFT   scripts/hooks/codex-hook-adapter.py", ""
        ),
    )
    assert outcome.code == 2
    assert "codex-hook-adapter" in outcome.detail
    assert not any(step[0] == "commit" for step in git.calls)


def test_a_divergent_pull_leaves_its_evidence_in_the_checkout(tmp_path, monkeypatch):
    """Unlike a refused pull, this one already copied files. Unwinding the branch from
    under a half-adopted tree hides what went wrong in an unattributed dirty checkout."""
    git = RecordingGit()
    outcome = upgraded(
        tmp_path,
        monkeypatch,
        git,
        runs=[subprocess.CompletedProcess(["pull"], 0, "moved 17 file(s)", "")],
        divergence="never settled",
    )
    assert outcome.code == 2
    assert not any(step[:2] == ("branch", "-d") for step in git.calls)


# --- a fixer hook is not a refusal -------------------------------------------


class CommitGit:
    """A git whose `commit` fails, optionally rewriting the tree as a hook would."""

    def __init__(self, statuses: list[str], commit_codes: list[int]):
        self.statuses = statuses
        self.commit_codes = iter(commit_codes)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        if args[0] == "status":
            # One reading per call, holding the last, so a test spells out the tree as
            # the sequence of states the commit attempts walk it through.
            text = self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)
            return subprocess.CompletedProcess(["git", *args], 0, text, "")
        code = next(self.commit_codes, 0) if args[0] == "commit" else 0
        return subprocess.CompletedProcess(["git", *args], code, "", "hook said no")


def test_a_hook_that_rewrote_the_tree_gets_one_more_commit():
    """`sync-codex` remirrored `.claude/skills/` and failed a commit that had nothing
    wrong with it. Staging the result and committing again is the whole convention."""
    git = CommitGit(["M  a.py\n", "MM a.py\n"], [1, 0])
    result, retried = up.commit_with_hook_retry(git, "Adopt devkit v0.7.0")
    assert (result.returncode, retried) == (0, True)
    assert git.calls.count(("add", "-A")) == 1
    assert [step for step in git.calls if step[0] == "commit"] != []


def test_a_gate_that_refused_is_not_committed_over():
    """A lint error the formatter cannot fix leaves the tree untouched. Retrying it
    fails identically and reports the same thing twice."""
    git = CommitGit(["M  a.py\n"], [1, 1])
    result, retried = up.commit_with_hook_retry(git, "Adopt devkit v0.7.0")
    assert (result.returncode, retried) == (1, False)
    assert ("add", "-A") not in git.calls
    assert len([step for step in git.calls if step[0] == "commit"]) == 1


def test_a_commit_that_worked_is_never_retried():
    git = CommitGit(["M  a.py\n"], [0])
    result, retried = up.commit_with_hook_retry(git, "Adopt devkit v0.7.0")
    assert (result.returncode, retried) == (0, False)
    assert len([step for step in git.calls if step[0] == "commit"]) == 1


def test_the_rewrite_is_detected_by_the_status_code_not_the_path_list():
    """A fixer rewrites files that were *already staged*, so the set of changed paths
    is identical afterwards and only the porcelain code moves. Comparing the parsed
    paths reports "nothing changed" for the one event this exists to detect."""
    git = CommitGit(["M  a.py\n", "MM a.py\n"], [1, 0])
    assert up.changed_paths(git) == up.changed_paths(git)  # same path, both readings
    git = CommitGit(["M  a.py\n", "MM a.py\n"], [1, 0])
    _result, retried = up.commit_with_hook_retry(git, "Adopt devkit v0.7.0")
    assert retried


# --- the failure artifact ----------------------------------------------------


def test_a_clean_run_writes_an_empty_artifact():
    """Empty rather than absent: a stale report is how the next reader gets sent after
    a failure that was fixed two runs ago."""
    assert up.artifact_body("v0.7.0", False, [up.Outcome("carameli", 0)]) == ""
    assert up.artifact_body("v0.7.0", False, []) == ""


def test_the_artifact_carries_each_failure_and_the_command_that_retries_it():
    body = up.artifact_body(
        "v0.7.0",
        False,
        [
            up.Outcome("carameli", 2, "upgrade: carameli -- FAILED at `git commit`"),
            up.Outcome("apt-finder", 1, "upgrade: apt-finder -- 3 uncommitted file(s)"),
            up.Outcome("ibkr_trader", 0),
        ],
    )
    assert "=== carameli (exit 2) ===" in body
    assert "FAILED at `git commit`" in body
    assert "3 uncommitted file(s)" in body
    assert "upgrade-project.py carameli,apt-finder --yes" in body
    assert "ibkr_trader" not in body


def test_the_artifact_says_which_mode_produced_it():
    """A refusal from a dry run and one from an apply need different next steps."""
    refused = [up.Outcome("carameli", 1, "parked on a task branch")]
    assert "(dry run)" in up.artifact_body("v0.7.0", True, refused)
    assert "(apply)" in up.artifact_body("v0.7.0", False, refused)


def test_a_run_that_never_reached_a_project_still_writes_one(tmp_path, monkeypatch, capsys):
    """`no workspace file` and `devkit has no tags` are the two failures most likely
    to be read hours later out of a task terminal nobody was watching."""
    assert up.main(["--all", "--workspace", str(tmp_path / "missing.code-workspace")]) == 2
    written = (up.REPO_ROOT / up.ARTIFACT).read_text(encoding="utf-8")
    assert "no workspace file" in written
    assert up.ARTIFACT.as_posix() in capsys.readouterr().err


def test_a_run_with_nothing_to_do_clears_what_the_last_one_left(tmp_path, monkeypatch):
    """The scheduled-run case: proving the workspace is current must also retract the
    previous run's report, or the artifact outlives the problem it describes."""
    stale = up.write_artifact(up.REPO_ROOT, "=== carameli (exit 2) ===\nold news\n")
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "commit_for", lambda _devkit, _rev: RELEASE_COMMIT)
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / "DEVKIT_VERSION").write_text("9d95e44\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert stale.read_text(encoding="utf-8") == ""


def test_an_unwritable_logs_directory_does_not_fail_the_upgrade(tmp_path):
    """The artifact is a report about the work, never a precondition for it."""
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory", encoding="utf-8")
    assert up.write_artifact(blocked, "anything") is None
