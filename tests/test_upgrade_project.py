"""Tests for scripts/upgrade-project.py (adopting a devkit release in a consumer).

**Where the work happens is the interesting half now.** This used to be a suite about
refusals — dirty tree, task branch, unfinished upgrade — each pinned individually, and
each one a state some human had to clear before the tool would run. The adoption moved
into an ephemeral box cut off `origin/<default>`, so those states became unreachable
rather than tolerated, and the tests that pinned them went with them.

What replaced them asserts the property that makes it true: every write is aimed at the
box, and nothing is aimed at the checkout.
"""

import contextlib
import datetime as _dt
import json
import string
import subprocess
import types
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script, sweep

up = load_script("scripts/upgrade-project.py")


@pytest.fixture(autouse=True)
def artifact_elsewhere(tmp_path, monkeypatch):
    """Keep `logs/upgrade.log` out of the real devkit checkout during a test run.

    Every exit from `main` writes the artifact, so without this the suite would
    overwrite whatever a real upgrade had left there -- with the outcome of a run
    against a fixture workspace, which is the most misleading thing it could say."""
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path / "_artifact_root")


def gh_listing(payload, code=0):
    """A `gh` that answers `pr list --json ...` with `payload` (str, or JSON-able)."""
    calls: list[tuple[str, ...]] = []

    def gh(*args: str):
        calls.append(args)
        out = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(["gh", *args], code, out, "")

    gh.calls = calls
    return gh


@pytest.fixture(autouse=True)
def no_open_adoption_pr(monkeypatch):
    """No test reaches the real `gh`, in either direction.

    `main` asks `open_adoption_pr` about every project it judges stale, so without this
    the whole suite would shell out to a CLI whose answer depends on the machine, the
    network and whatever is open in a real repo -- and the tests that fake `upgrade_one`
    would start passing or failing for reasons nothing in them mentions. The tests that
    are *about* that question install their own `gh` over this one.

    Stubbed at `gh_for` rather than at `open_adoption_pr`, so the predicate itself still
    runs for real everywhere it is called -- a stub of the function would hide a crash
    in it from every test but the six that name it."""
    monkeypatch.setattr(up.sweep, "gh_for", lambda _p: gh_listing([]))


def done(code: int = 0):
    """A stand-in `upgrade_one` result, for the tests that fake the per-project work."""
    return lambda *_a, **_kw: up.Outcome("stub", code)


def test_an_argparse_failure_still_writes_the_artifact():
    """argparse exits 2 from inside `parse_args`, before `main` can reach `_finish`.

    Under the scheduler that is the worst spelling of a failure this script has:
    `pythonw` has no stderr, so the whole record on the machine is a Last Result of 2
    beside an artifact still saying whatever the previous run left -- the exact
    signature found on 2026-08-17, unexplainable from the log precisely because
    nothing owed the log anything on that path.
    """
    with pytest.raises(SystemExit) as stop:
        up.main(["--no-such-flag"])
    assert stop.value.code == 2
    text = (up.REPO_ROOT / up.ARTIFACT).read_text(encoding="utf-8")
    assert "--no-such-flag" in text

    # The two explicit `parser.error` refusals in `main` take the same exit.
    with pytest.raises(SystemExit):
        up.main(["carameli", "--all"])
    text = (up.REPO_ROOT / up.ARTIFACT).read_text(encoding="utf-8")
    assert "--all" in text


# --- the checklist that names the checkouts -----------------------------------
#
# `Devkit: Upgrade Projects` used to pass `--all` and nothing else, so adopting a release
# in four consumers of five was a terminal command. The task ticks boxes now and hands
# the ticked names in as the positional; these cover the two ends of that -- what a
# selection is, and what backing out of one has to do.


def test_the_checklist_arrives_as_one_comma_delimited_positional():
    """A VS Code input resolves to ONE string, so the multi-pick joins on `separator`.
    Splitting it back is this function, and it is the whole of the task's contract."""
    assert up.project_selection("carameli,data-lake") == ["carameli", "data-lake"]
    assert up.project_selection("") == []
    assert up.project_selection(None) == []
    # Order is the tick order and duplicates cannot survive: a checkout upgraded twice
    # would cut a second box on a branch the first one already holds.
    assert up.project_selection(" carameli , data-lake ,carameli") == ["carameli", "data-lake"]


def test_an_escaped_checklist_is_never_mind_rather_than_a_checkout_name():
    """Escaping leaves the input unresolved and VS Code passes the literal through.
    Read as a name it is "not in workspace.code-workspace", exit 2 -- an operator error
    reported for the one gesture that means cancel."""
    assert up.picked_nothing("${input:adoptProjects}") is True
    assert up.picked_nothing("carameli,data-lake") is False
    assert up.picked_nothing("") is False
    assert up.picked_nothing(None) is False


def test_backing_out_of_the_checklist_upgrades_nothing_and_empties_the_artifact(capsys):
    """Exit 0, because cancelling is a decision. And the log is emptied rather than
    left: the previous run's failure still sitting in `logs/upgrade.log` is what makes
    "I cancelled it" indistinguishable from "it broke"."""
    (up.REPO_ROOT / up.ARTIFACT).parent.mkdir(parents=True, exist_ok=True)
    (up.REPO_ROOT / up.ARTIFACT).write_text("carameli: the previous run failed", encoding="utf-8")

    assert up.main(["${input:adoptProjects}"]) == 0
    assert "nothing was picked" in capsys.readouterr().out
    assert (up.REPO_ROOT / up.ARTIFACT).read_text(encoding="utf-8") == ""


# --- naming ------------------------------------------------------------------


def test_the_slug_is_what_the_box_tier_names_the_branch_from():
    """`worktree.plan_new` turns it into `<AUTOMATION_PREFIX><slug>-<mmdd>`, so this
    file no longer builds the branch name itself -- one namer, not two that can
    disagree."""
    assert up.upgrade_slug("v0.9.1") == "devkit upgrade v0.9.1"


def test_two_same_day_releases_do_not_share_a_branch_name():
    """A branch name whose PR merged is permanently retired by the branch policy, and
    `worktree.plan_new` disambiguates only against refs that still exist -- a merged
    `--delete-branch` PR leaves none. So when v0.9.0's adoption merged in the morning
    and v0.9.1 ran in the afternoon, the date-only name collided and every commit was
    refused. The tag in the slug is what keeps two same-day releases apart."""
    day = _dt.date(2026, 8, 17)
    first = up.tb.branch_name(up.tb.slugify(up.upgrade_slug("v0.9.0")), set(), day)
    second = up.tb.branch_name(up.tb.slugify(up.upgrade_slug("v0.9.1")), set(), day)
    assert first != second
    # The tag must survive slugification recognisably, or the PR list becomes a wall
    # of identical branch names again.
    assert "v0-9-1" in second


# --- an adoption already up for review ---------------------------------------


def test_the_branch_stem_is_built_from_the_box_tiers_own_namer():
    """Restating `agent/` or the slug rules here would give the stem a second author,
    and a rename in `task_branch` would stop matching without failing anything."""
    stem = up.upgrade_branch_stem("v0.10.2")
    assert stem == f"{up.tb.AUTOMATION_PREFIX}{up.tb.slugify(up.upgrade_slug('v0.10.2'))}-"
    # And it really is a prefix of what the box tier would cut, on any day.
    cut = up.tb.branch_name(
        up.tb.slugify(up.upgrade_slug("v0.10.2")),
        set(),
        _dt.date(2026, 8, 20),
        prefix=up.tb.AUTOMATION_PREFIX,
    )
    assert cut.startswith(stem)


def test_the_upgrade_branch_says_no_session_asked_for_it():
    """This sweep cuts the same vendoring commit in every consumer, nightly, and a
    reviewer opening `preview-task.py` was being offered all of them ahead of the change
    they had asked to look at. The namespace is what that menu filters on, so it is a
    contract here rather than a naming preference -- and it stays inside `agent/`, so
    the branch still ships like any other."""
    stem = up.upgrade_branch_stem("v0.11.2")
    assert up.tb.is_automation_branch(stem)
    assert up.tb.is_managed_task_branch(stem)


def test_only_one_stem_is_ever_cut_however_many_are_searched_for():
    """`upgrade_branch_stems` widens the *lookup* and must never widen the *naming* --
    a run that cut the legacy stem back would undo the move on the next nightly."""
    stems = up.upgrade_branch_stems("v0.11.2")
    assert stems[0] == up.upgrade_branch_stem("v0.11.2")
    assert any(not up.tb.is_automation_branch(stem) for stem in stems)
    # `str.startswith` takes the tuple as-is; a list would raise at the call site.
    assert isinstance(stems, tuple)


def test_an_open_pr_for_this_release_is_found(tmp_path, monkeypatch):
    """The duplicate this exists to prevent: `DEVKIT_VERSION` on the default branch only
    moves when an adoption *merges*, so a PR that is open-but-red leaves every later run
    judging the project out of date. carameli collected three PRs for v0.10.2 that way."""
    gh = gh_listing(
        [
            {"number": 173, "headRefName": "agent/baseline-drift-0819", "url": "u/173"},
            {
                "number": 170,
                "headRefName": "agent/auto/devkit-upgrade-v0-10-2-0819",
                "url": "u/170",
            },
        ]
    )
    monkeypatch.setattr(up.sweep, "gh_for", lambda _p: gh)
    assert up.open_adoption_pr(tmp_path, "v0.10.2") == "#170 u/170"
    assert gh.calls[0][:4] == ("pr", "list", "--state", "open")


def test_an_adoption_opened_before_the_namespace_move_is_still_found(tmp_path, monkeypatch):
    """The one-run window this whole plural stem exists for. Moving the namespace renames
    what the sweep *cuts*; it cannot rename a PR already open on the old spelling, and on
    the first run after the move every in-flight adoption is on one. Match only the new
    stem and the sweep opens a second PR against every consumer that was mid-adoption --
    the exact failure `open_adoption_pr` was written to stop."""
    gh = gh_listing(
        [{"number": 170, "headRefName": "agent/devkit-upgrade-v0-10-2-0819", "url": "u/170"}]
    )
    monkeypatch.setattr(up.sweep, "gh_for", lambda _p: gh)
    assert up.open_adoption_pr(tmp_path, "v0.10.2") == "#170 u/170"


def test_a_same_day_rerun_of_the_same_release_is_still_a_duplicate(tmp_path, monkeypatch):
    """The second run of a day gets `-2` appended, which is exactly the shape that has
    to keep matching -- #175 was `...-0820-2`."""
    gh = gh_listing(
        [{"number": 174, "headRefName": "agent/auto/devkit-upgrade-v0-10-2-0820-2", "url": "u"}]
    )
    monkeypatch.setattr(up.sweep, "gh_for", lambda _p: gh)
    assert up.open_adoption_pr(tmp_path, "v0.10.2").startswith("#174")


def test_an_open_pr_for_a_different_release_is_not_this_one(tmp_path, monkeypatch):
    """v0.10.1's adoption sitting open must not suppress v0.10.2's -- that would be the
    mirror-image bug, an upgrade that never happens. Neither spelling of it may match,
    since the legacy stem widens what counts as a hit."""
    gh = gh_listing(
        [
            {"number": 161, "headRefName": "agent/auto/devkit-upgrade-v0-10-1-0817", "url": "u"},
            {"number": 160, "headRefName": "agent/devkit-upgrade-v0-10-1-0816", "url": "u"},
        ]
    )
    monkeypatch.setattr(up.sweep, "gh_for", lambda _p: gh)
    assert up.open_adoption_pr(tmp_path, "v0.10.2") == ""


def test_no_open_prs_at_all_is_not_a_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(up.sweep, "gh_for", lambda _p: gh_listing([]))
    assert up.open_adoption_pr(tmp_path, "v0.10.2") == ""


def test_a_gh_that_cannot_answer_never_blocks_the_upgrade(tmp_path, monkeypatch):
    """No `gh`, no auth, no remote: fail open. A duplicate PR is a nuisance; a scheduled
    upgrade that silently stops running is not."""
    monkeypatch.setattr(up.sweep, "gh_for", lambda _p: gh_listing("", code=1))
    assert up.open_adoption_pr(tmp_path, "v0.10.2") == ""
    monkeypatch.setattr(up.sweep, "gh_for", lambda _p: gh_listing("not json at all"))
    assert up.open_adoption_pr(tmp_path, "v0.10.2") == ""


def test_the_commit_names_the_release():
    """Unlike a swept commit, the version really is the description here."""
    assert up.commit_message("v0.5.3", 38) == "Adopt devkit v0.5.3 (38 vendored file(s))"


def test_the_planned_commit_does_not_claim_a_file_count_it_cannot_know(
    tmp_path, capsys, monkeypatch
):
    """The plan is printed before the pull runs, so the count does not exist yet.
    It printed `0` there, which described every applied run wrongly."""
    monkeypatch.setattr(up.sweep, "git_for", lambda _p: refs({}))
    monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: "master")
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


# --- is it already current -----------------------------------------------------

# A made-up commit SHA, not a credential — but detect-secrets cannot tell a 40-char hex
# string from a key, so it is allowlisted inline per `.pre-commit-config.yaml`'s note.
RELEASE_COMMIT = "9d95e4471bd60d6f3a2c81e5f7c0a4b8d1e2f3a4"  # pragma: allowlist secret


def refs(contents: dict[str, str], default: str = "master"):
    """A git that serves `git show origin/<default>:<path>` out of `contents`."""
    calls: list[tuple[str, ...]] = []

    def git(*args: str):
        calls.append(args)
        if args[0] == "show":
            path = args[1].split(":", 1)[1]
            if path in contents:
                return subprocess.CompletedProcess(["git", *args], 0, contents[path], "")
            return subprocess.CompletedProcess(["git", *args], 128, "", "no such path")
        if args[:2] == ("symbolic-ref", "refs/remotes/origin/HEAD"):
            return subprocess.CompletedProcess(["git", *args], 0, f"origin/{default}", "")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    git.calls = calls  # type: ignore[attr-defined]
    return git


def test_the_stamp_is_read_off_the_ref_not_the_working_tree(tmp_path, monkeypatch):
    """The question is about the branch a PR would target. A static checkout's copy is
    whatever branch it is parked on, which was repeatedly a different answer in both
    directions -- a checkout left on a merged adoption branch reported the new version
    while main still had the old, and one left on an abandoned branch the reverse."""
    git = refs({"DEVKIT_VERSION": "9d95e44\n"})
    monkeypatch.setattr(up.sweep, "git_for", lambda _p: git)
    monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: "master")
    # The working tree says something else entirely, and is never consulted.
    (tmp_path / "DEVKIT_VERSION").write_text("deadbee\n", encoding="utf-8")

    assert up.is_current_on_remote(tmp_path, RELEASE_COMMIT)
    assert ("show", "origin/master:DEVKIT_VERSION") in git.calls


def test_the_ref_is_fetched_before_it_is_read(tmp_path, monkeypatch):
    """`origin/<default>` is a local ref like any other, and a checkout nobody has
    touched for a week has a week-old copy of it."""
    git = refs({"DEVKIT_VERSION": "9d95e44\n"})
    monkeypatch.setattr(up.sweep, "git_for", lambda _p: git)
    monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: "master")
    up.is_current_on_remote(tmp_path, RELEASE_COMMIT)
    assert ("fetch", "--quiet", "origin") in git.calls


def test_the_stamp_is_never_compared_against_the_tag_name(tmp_path, monkeypatch):
    """DEVKIT_VERSION holds the upstream **SHA** by contract, so a predicate comparing
    it to `v0.5.3` was false for every project forever."""
    git = refs({"DEVKIT_VERSION": "9d95e44\n"})
    monkeypatch.setattr(up.sweep, "git_for", lambda _p: git)
    monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: "master")
    assert not up.is_current_on_remote(tmp_path, "v0.5.3")
    assert up.is_current_on_remote(tmp_path, RELEASE_COMMIT)


def test_a_ref_with_no_stamp_on_it_is_not_current(tmp_path, monkeypatch):
    """A project that never vendored, and a `git show` against a branch that has no
    such path, are the same answer: no."""
    git = refs({})
    monkeypatch.setattr(up.sweep, "git_for", lambda _p: git)
    monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: "master")
    assert not up.is_current_on_remote(tmp_path, RELEASE_COMMIT)


def test_version_on_reads_a_named_path_at_the_ref():
    git = refs({"DEVKIT_VERSION": " abc1234 \n"})
    assert up.version_on(git, "master") == "abc1234"
    assert up.version_on(git, "master", "nope.json") == ""
    # No base branch is not a question that can be answered, so it is not asked.
    assert up.version_on(git, "") == ""


def test_a_provisional_pull_is_never_current():
    """`--allow-dirty` stamps `<rev>-dirty`; those files are at no release at all."""
    assert not up.names_commit("9d95e44-dirty", RELEASE_COMMIT)


def test_a_stamp_that_is_not_a_sha_proves_nothing():
    """`git_head` writes `unknown` when it cannot read the source's HEAD."""
    assert not up.names_commit("unknown", RELEASE_COMMIT)


def test_a_full_length_stamp_matches_too():
    assert up.names_commit(RELEASE_COMMIT, RELEASE_COMMIT)


def test_an_abbreviation_too_short_to_be_a_sha_is_rejected():
    """Guards the prefix test: a 2-char "commit" would match a sixteenth of them."""
    assert not up.names_commit("9d", RELEASE_COMMIT)


def test_currency_cannot_be_proven_without_the_release_commit(tmp_path, monkeypatch):
    """A rev git could not resolve means "cannot tell", and cannot tell is not
    current -- the run proceeds and finds out by pulling."""
    monkeypatch.setattr(
        up.sweep, "git_for", lambda _p: pytest.fail("asked git without a release commit")
    )
    assert not up.is_current_on_remote(tmp_path, "")


def test_the_release_commit_is_resolved_from_the_devkit_checkout():
    """Against the real repo, because the point of `commit_for` is what git says."""
    head = up.commit_for(REPO_ROOT, "HEAD")
    assert len(head) == 40
    assert all(char in string.hexdigits for char in head)
    assert up.commit_for(REPO_ROOT, "no-such-rev-anywhere") == ""


def test_a_real_checkout_stamped_with_devkits_head_is_current(tmp_path, monkeypatch):
    """End to end, with a real SHA and a real abbreviation of it: this is the shape
    every consumer's DEVKIT_VERSION actually has."""
    head = up.commit_for(REPO_ROOT, "HEAD")
    git = refs({"DEVKIT_VERSION": f"{head[:7]}\n"})
    monkeypatch.setattr(up.sweep, "git_for", lambda _p: git)
    monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: "master")
    assert up.is_current_on_remote(tmp_path, head)


# --- never backwards ----------------------------------------------------------


def receipt(project, tag: str | None):
    """A `DEVKIT_FILES.json` of the shape every pull writes."""
    payload: dict = {"version": 1, "files": {}}
    if tag is not None:
        payload["devkit_tag"] = tag
    (project / "DEVKIT_FILES.json").write_text(json.dumps(payload), encoding="utf-8")
    return project


def test_a_release_this_checkout_has_no_tag_for_is_refused(tmp_path):
    """The release-window state, and what makes this a bug rather than a nuisance:
    carameli had adopted v0.8.0 off the release branch before the tag existed here.
    Every remaining step would then have run backwards -- an older tree pulled over
    it, a commit titled `Adopt devkit v0.7.0`, and a PR calling that an adoption."""
    receipt(tmp_path, "v0.8.0")
    reason = up.unreleased_adoption(tmp_path, ["v0.7.0", "v0.6.0"])
    assert "v0.8.0" in reason and "v0.7.0" in reason
    assert "backwards" in reason


def test_a_release_this_checkout_does_have_is_upgradable(tmp_path):
    receipt(tmp_path, "v0.6.0")
    assert up.unreleased_adoption(tmp_path, ["v0.7.0", "v0.6.0"]) == ""


def test_a_receipt_with_no_recorded_tag_proves_nothing(tmp_path):
    """An `--allow-untagged` pull, or one from before the receipt carried a tag.
    Cannot tell is not ahead -- the line `sync-devkit.stale_pin` draws, for the same
    reason: a check that cried wolf on every un-upgraded project would be ignored."""
    receipt(tmp_path, None)
    assert up.unreleased_adoption(tmp_path, ["v0.7.0"]) == ""


def test_a_project_with_no_receipt_at_all_proves_nothing(tmp_path):
    assert up.unreleased_adoption(tmp_path, ["v0.7.0"]) == ""


def test_nothing_is_concluded_from_an_empty_tag_list(tmp_path):
    """`release_tags` answers [] for a devkit it could not query. Reading "ahead"
    into that would refuse every project in the workspace over one bad git call."""
    receipt(tmp_path, "v0.8.0")
    assert up.unreleased_adoption(tmp_path, []) == ""


def test_the_receipt_is_read_through_sync_devkits_own_reader(tmp_path):
    """Not reparsed here: `DEVKIT_FILES.json` already has a reader that owns its
    format, and a second opinion about which release a project vendored is worse
    than no opinion at all."""
    receipt(tmp_path, "v0.8.0")
    assert up.vendored_release(tmp_path) == "v0.8.0"
    (tmp_path / "DEVKIT_FILES.json").write_text("{ not json", encoding="utf-8")
    assert up.vendored_release(tmp_path) == ""


def test_the_tag_list_is_newest_first_and_agrees_with_the_latest():
    """Against the real checkout, because the split only earns its place if the
    *set* and the *pick* can never disagree about what the newest release is."""
    tags = up.release_tags(REPO_ROOT)
    assert tags and tags[0] == up.latest_tag(REPO_ROOT)
    assert up.release_tags(REPO_ROOT / "no-such-directory") == []


# --- the upgrade happens in a box, never in the checkout ----------------------


class BoxRun:
    """Drives `upgrade_one` past the box tier, recording what it was asked to build.

    Everything the real thing does to disk -- cutting a worktree, pulling, verifying --
    is stubbed. What is asserted is *where* each step was pointed, because the whole
    change is that they point at the box rather than at the checkout.
    """

    def __init__(self, tmp_path, monkeypatch, *, changed=("DEVKIT_VERSION",), ok=True):
        self.box = tmp_path / "boxes" / "data-lake--devkit-upgrade-0812"
        self.box.mkdir(parents=True)
        self.spawned: list[tuple[str, str]] = []
        self.prefixes: list[str] = []
        self.pulled: list[Path] = []
        self.git = RecordingGit()
        plan = types.SimpleNamespace(
            path=str(self.box),
            box=types.SimpleNamespace(
                name="data-lake--devkit-upgrade-0812",
                branch="claude/devkit-upgrade-0812",
                project="data-lake",
            ),
        )

        def plan_new(project, _workspace, slug, **kw):
            self.spawned.append((project, slug))
            self.prefixes.append(kw.get("branch_prefix", ""))
            return plan

        def apply_new(_plan, _workspace, **_kw):
            return ok, ["cut it"]

        def fixpoint(project, _source, **_kw):
            self.pulled.append(project)
            return [subprocess.CompletedProcess(["pull"], 0, "moved 36 file(s)", "")], ""

        monkeypatch.setattr(up.worktree, "plan_new", plan_new)
        monkeypatch.setattr(up.worktree, "apply_new", apply_new)
        monkeypatch.setattr(up, "pull_to_fixpoint", fixpoint)
        monkeypatch.setattr(
            up, "verify_pull", lambda *_a: subprocess.CompletedProcess(["check"], 0, "", "")
        )
        monkeypatch.setattr(up, "changed_paths", lambda _g: list(changed))
        monkeypatch.setattr(up, "unreleased_adoption", lambda *_a: "")
        monkeypatch.setattr(up.sweep, "git_for", lambda _p: self.git)
        monkeypatch.setattr(up.sweep, "gh_for", lambda _p: no_pr)
        monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: "main")
        self.pr_plans: list = []

        def ensure_pr(_gh, plan):
            self.pr_plans.append(plan)
            return ("https://example.test/pr/1", True, "")

        monkeypatch.setattr(up.sweep, "ensure_pr", ensure_pr)

    def run(self, tmp_path):
        return up.upgrade_one(
            "data-lake", tmp_path / "checkout", "v0.8.0", tmp_path / "src", tmp_path / "ws.json"
        )


def no_pr(*_args):
    return subprocess.CompletedProcess(["gh"], 0, "[]", "")


def test_the_adoption_is_pulled_into_the_box_not_the_checkout(tmp_path, monkeypatch):
    """The whole change in one assertion. Every refusal this script used to carry named
    a state a long-lived checkout accumulates -- dirty, parked, behind -- and none of
    them is reachable in a worktree younger than the run."""
    run = BoxRun(tmp_path, monkeypatch)
    assert run.run(tmp_path).code == 0
    assert run.spawned == [("data-lake", up.upgrade_slug("v0.8.0"))]
    # And it asks the box tier for the automation namespace, which is the only place
    # `upgrade_branch_stem`'s promise is actually kept -- the stem is what finds a rerun's
    # earlier PR, so a box cut under the session namespace would be a stem that matches
    # nothing and a duplicate PR per run.
    assert run.prefixes == [up.tb.AUTOMATION_PREFIX]
    assert run.pulled == [run.box]


def test_the_commit_and_push_happen_in_the_box(tmp_path, monkeypatch):
    """`git_for` is called for both the checkout (to read refs) and the box (to commit),
    so pointing the write half at the wrong one would not fail loudly."""
    run = BoxRun(tmp_path, monkeypatch)
    run.run(tmp_path)
    assert ("add", "-A") in run.git.calls
    assert ("push", "-u", "origin", "claude/devkit-upgrade-0812") in run.git.calls


def test_the_upgrade_pr_is_labelled_automerge(tmp_path, monkeypatch):
    """An upgrade PR is a vendored copy of an already-released tag, so a green gate
    is the whole review; the label is what lets `reconcile --merge` and the vendored
    workflow land it without a human. Losing it turns every release back into one
    hand-merged PR per consumer."""
    run = BoxRun(tmp_path, monkeypatch)
    assert run.run(tmp_path).code == 0
    assert [plan.pr_labels for plan in run.pr_plans] == [(up.sweep.AUTOMERGE_LABEL,)]


def test_nothing_checks_out_a_branch_in_the_static_checkout(tmp_path, monkeypatch):
    """The old flow ran `checkout -b` in the consumer, which is how a checkout ended up
    parked on an upgrade branch for months afterwards. Nothing does that now."""
    run = BoxRun(tmp_path, monkeypatch)
    run.run(tmp_path)
    assert not [call for call in run.git.calls if call[0] == "checkout"]
    assert not [call for call in run.git.calls if call[0] == "merge"]


def test_the_box_is_left_for_reconcile_to_reap(tmp_path, monkeypatch):
    """Reaping on the strength of a push would destroy the only checkout the work
    exists in whenever the PR call failed. `reconcile` reaps on a *merged* PR."""
    run = BoxRun(tmp_path, monkeypatch)
    monkeypatch.setattr(
        up.worktree, "reap_plan", lambda *_a, **_kw: pytest.fail("reaped its own box")
    )
    assert run.run(tmp_path).code == 0


def test_a_box_that_could_not_be_cut_is_a_failure_not_a_refusal(tmp_path, monkeypatch):
    """Exit 2: nothing about the project is wrong, the machine could not make room."""
    run = BoxRun(tmp_path, monkeypatch, ok=False)
    assert run.run(tmp_path).code == 2


def test_an_already_current_project_commits_nothing(tmp_path, monkeypatch):
    """`main` proves currency off the ref before ever calling here, so reaching this
    means the stamp and the files disagreed. The box holds nothing; leave it."""
    run = BoxRun(tmp_path, monkeypatch, changed=())
    assert run.run(tmp_path).code == 0
    assert not [call for call in run.git.calls if call[0] == "commit"]


def test_the_backwards_check_reads_the_box_not_the_checkout(tmp_path, monkeypatch):
    """The one refusal that survives. It has to ask about the tree a PR would be based
    on, which is the box -- the checkout may be parked anywhere."""
    run = BoxRun(tmp_path, monkeypatch)
    seen: list[Path] = []

    def backwards(project, _tags):
        seen.append(project)
        return "vendored devkit v0.9.0, which this checkout has no tag for"

    monkeypatch.setattr(up, "unreleased_adoption", backwards)
    outcome = run.run(tmp_path)
    assert outcome.code == 1
    assert seen == [run.box]
    assert "v0.9.0" in outcome.detail


def test_a_dry_run_cuts_no_box(tmp_path, monkeypatch, capsys):
    run = BoxRun(tmp_path, monkeypatch)
    assert up.upgrade_one("data-lake", tmp_path / "checkout", "v0.8.0").code == 0
    assert run.spawned == []
    assert "worktree.py new" in capsys.readouterr().out


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


# `_abandon` is gone with the branch it used to unwind. It existed because the old flow
# cut `claude/devkit-upgrade-<mmdd>` in the consumer *before* knowing whether there was
# anything to adopt, so a no-op run had to walk itself back out -- and its four tests
# were all about doing that carefully enough not to lose work. A box is cut in a
# directory of its own and adopted by `reconcile`, so a no-op run leaves the checkout
# exactly as it found it with nothing to undo.


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


def stamps_on_main(monkeypatch, default: str = "main"):
    """Serve `git show origin/<default>:<path>` out of each project directory.

    These tests still say "this project is on that release" by writing DEVKIT_VERSION,
    which is the readable way to express it. The predicate reads it through a ref now,
    so this is the bridge -- and it keeps the suite off the real `git`, which a bare
    `tmp_path` is not a repository for anyway.
    """

    def git_for(path):
        def git(*args: str):
            if args[0] == "show":
                target = Path(path) / args[1].split(":", 1)[1]
                if target.is_file():
                    return subprocess.CompletedProcess(
                        ["git", *args], 0, target.read_text(encoding="utf-8"), ""
                    )
                return subprocess.CompletedProcess(["git", *args], 128, "", "no such path")
            return subprocess.CompletedProcess(["git", *args], 0, "", "")

        return git

    monkeypatch.setattr(up.sweep, "git_for", git_for)
    monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: default)


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
    monkeypatch.setattr(
        up.worktree, "plan_new", lambda *_a, **_kw: pytest.fail("cut a box with nothing to pull")
    )
    stamps_on_main(monkeypatch)
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


def test_a_stale_project_whose_adoption_is_already_open_is_left_alone(
    tmp_path, capsys, monkeypatch
):
    """The regression: carameli was stale by the only test this had -- `DEVKIT_VERSION`
    on `master` -- for the whole time #170 sat open with a red gate, so the 03:00 run
    and two reruns each cut a box and opened another identical PR (#174, #175). Being
    stale is not enough; the adoption has to be *missing*."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "commit_for", lambda _devkit, _rev: RELEASE_COMMIT)
    monkeypatch.setattr(
        up, "source_at_tag", lambda *_a: pytest.fail("built a worktree for a PR that exists")
    )
    monkeypatch.setattr(
        up.worktree, "plan_new", lambda *_a, **_kw: pytest.fail("cut a second box for one release")
    )
    monkeypatch.setattr(
        up.sweep,
        "gh_for",
        lambda _p: gh_listing(
            [
                {
                    "number": 170,
                    "headRefName": "agent/auto/devkit-upgrade-v0-5-3-0819",
                    "url": "u/170",
                }
            ]
        ),
    )
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / "DEVKIT_VERSION").write_text("1234567\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert "already up for adoption in #170" in capsys.readouterr().out


def test_one_project_refusing_does_not_stop_the_others(tmp_path, capsys, monkeypatch):
    """Independent repos, independent PRs. Stopping at the first refusal would make
    `--all` useless the moment one checkout is mid-task."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    stamps_on_main(monkeypatch)
    seen: list[str] = []

    def fake_upgrade(name, *_a, **_kw):
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


def ahead(tmp_path, monkeypatch, *names):
    """A workspace where every named checkout vendored a release devkit lacks."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.7.0")
    monkeypatch.setattr(up, "release_tags", lambda _devkit: ["v0.7.0", "v0.6.0"])
    monkeypatch.setattr(up, "commit_for", lambda _devkit, _rev: RELEASE_COMMIT)
    stamps_on_main(monkeypatch)
    for name in names:
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("1234567\n", encoding="utf-8")
        receipt(tmp_path / name, "v0.8.0")
    return workspace(tmp_path, *names)


# The backwards refusal itself moved into `upgrade_one`, where it reads the box rather
# than the checkout — see `test_the_backwards_check_reads_the_box_not_the_checkout`. It
# costs a box now instead of a file read, which is the deliberate trade: the file read
# was answering about whatever branch the checkout was parked on. What `main` still owes
# it is aggregation, and that is what these two pin.


def refuses_backwards(name, *_a, **_kw):
    """An `upgrade_one` that refuses one project the way the box tier really would."""
    if name == "carameli":
        return up.Outcome(name, 1, f"upgrade: {name} -- vendored devkit v0.8.0, no tag for it")
    return up.Outcome(name, 0)


def test_the_backwards_refusal_reaches_the_artifact(tmp_path, monkeypatch):
    """A `--all` interleaves several checkouts' lines, so a refusal that is only
    printed is one that scrolls away. `logs/upgrade.log` is what the next reader has."""
    monkeypatch.setattr(up, "upgrade_one", refuses_backwards)
    monkeypatch.setattr(up, "source_at_tag", _no_worktree)
    ws = ahead(tmp_path, monkeypatch, "carameli")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 1
    written = (up.REPO_ROOT / up.ARTIFACT).read_text(encoding="utf-8")
    assert "carameli" in written and "v0.8.0" in written


def test_a_project_ahead_does_not_stop_the_ones_behind(tmp_path, monkeypatch):
    """The `--all` contract, extended to the new refusal: independent repos, and the
    one release the others are missing is not held up by the one that is ahead."""
    upgraded_names: list[str] = []

    def upgrade(name, *args, **kwargs):
        outcome = refuses_backwards(name, *args, **kwargs)
        if outcome.code == 0:
            upgraded_names.append(name)
        return outcome

    monkeypatch.setattr(up, "source_at_tag", _no_worktree)
    monkeypatch.setattr(up, "upgrade_one", upgrade)
    ws = ahead(tmp_path, monkeypatch, "carameli")
    (tmp_path / "ibkr_trader").mkdir()
    (tmp_path / "ibkr_trader" / "DEVKIT_VERSION").write_text("1234567\n", encoding="utf-8")
    receipt(tmp_path / "ibkr_trader", "v0.6.0")
    ws.write_text(
        json.dumps({"folders": [{"path": "carameli"}, {"path": "ibkr_trader"}]}), encoding="utf-8"
    )
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 1
    assert upgraded_names == ["ibkr_trader"]


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
    """Drive `upgrade_one` past the box tier, with the pull's result supplied."""
    run = BoxRun(tmp_path, monkeypatch)
    monkeypatch.setattr(up.sweep, "git_for", lambda _project: git)
    monkeypatch.setattr(up, "pull_to_fixpoint", lambda *_a, **_kw: (runs, divergence))
    monkeypatch.setattr(
        up, "verify_pull", lambda *_a: check or subprocess.CompletedProcess(["check"], 0, "", "")
    )
    return up.upgrade_one(
        "carameli", tmp_path / "checkout", "v0.7.0", tmp_path / "src", tmp_path / "ws.json"
    ), run


def test_drift_the_pull_left_is_refused_before_the_commit_gate_sees_it(tmp_path, monkeypatch):
    """This is the failure as it actually happened: the commit ran, the `devkit-drift`
    hook rejected it, and the operator got a hook id instead of a file list. Checking
    here reports it where the tree that caused it is still the subject."""
    git = RecordingGit()
    outcome, _ = upgraded(
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


def test_a_divergent_pull_leaves_its_evidence_in_the_box(tmp_path, monkeypatch):
    """Unlike a refused pull, this one already copied files. The box holding a
    half-adopted tree *is* the evidence, and it survives for someone to look at --
    where the old flow had to be talked out of deleting the branch under it."""
    git = RecordingGit()
    outcome, _ = upgraded(
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


def test_a_clean_run_says_so_on_stdout(tmp_path, monkeypatch, capsys):
    """The other half of "empty rather than absent", and the half that was missing: an
    empty artifact and a silent run are the same evidence, so a successful pass left
    nothing that distinguished it from one that died before writing anything. Asked
    whether an upgrade had worked, the only honest reading of what it left was "cannot
    tell". The line names the release, because that is the fact a reader wants."""
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path)
    assert up._finish("v0.7.0", False, [up.Outcome("carameli", 0)]) == 0
    out = capsys.readouterr().out
    assert "v0.7.0" in out
    assert up.ARTIFACT.as_posix() in out
    assert (tmp_path / up.ARTIFACT).read_text(encoding="utf-8") == ""


def test_a_failing_run_never_claims_to_be_clean(tmp_path, monkeypatch, capsys):
    """The reversion check for the line above: it is keyed on the artifact being empty,
    which is the same predicate that decides whether anything needs a human. A run with a
    refusal in it gets the pointer on stderr and no reassurance on stdout."""
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path)
    assert up._finish("v0.7.0", False, [up.Outcome("carameli", 2, "FAILED at `git commit`")]) == 2
    captured = capsys.readouterr()
    assert "clean" not in captured.out
    assert up.ARTIFACT.as_posix() in captured.err


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
    stamps_on_main(monkeypatch)
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


# --- what the tag does not carry ---


def diffs(names: list[str], code: int = 0, default: str = "master"):
    """A git whose `diff --name-only <tag>..origin/<default>` serves `names`."""

    def git(*args: str):
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(["git", *args], code, "\n".join(names), "")
        if args[:2] == ("symbolic-ref", "refs/remotes/origin/HEAD"):
            return subprocess.CompletedProcess(["git", *args], 0, f"origin/{default}", "")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    return git


def lag(monkeypatch, changed: list[str], vendored: list[str], code: int = 0):
    monkeypatch.setattr(up.sweep, "git_for", lambda _p: diffs(changed, code))
    monkeypatch.setattr(up.tb, "detect_default_branch", lambda *_a, **_kw: "master")
    monkeypatch.setattr(up, "manifest_paths", lambda: vendored)
    return up.unreleased_vendored_changes(Path("devkit"), "v0.5.3")


def test_a_vendored_fix_that_is_not_tagged_yet_is_reported(tmp_path, monkeypatch):
    """The whole delivery gap: this script pulls from a worktree at the newest tag, so
    a MANIFEST change merged after it reaches nobody, and both ends stay green."""
    found = lag(
        monkeypatch,
        ["scripts/hooks/enforce-capped-bash.py", "README.md"],
        ["scripts/hooks/enforce-capped-bash.py"],
    )
    assert found == ["scripts/hooks/enforce-capped-bash.py"]


def test_a_change_outside_the_manifest_is_not_a_delivery_gap(tmp_path, monkeypatch):
    """devkit's own generator and workspace tooling are never vendored, so main moving
    ahead of the tag there costs a consumer nothing. Warning about it would fire on
    every run and teach the reader to skip the line that matters."""
    assert (
        lag(monkeypatch, ["scripts/new-project.py", "tests/test_sweep.py"], ["MANIFEST.md"]) == []
    )


def test_a_git_that_cannot_answer_is_not_a_gap(tmp_path, monkeypatch):
    """Best-effort: a diagnostic must never fail the upgrade it annotates."""
    assert lag(monkeypatch, ["scripts/hooks/hook.py"], ["scripts/hooks/hook.py"], code=128) == []


def test_the_warning_names_the_files_rather_than_counting_them():
    """A count cannot answer the reader's only question -- whether the fix they came
    for is in it."""
    line = up.unreleased_line(["scripts/hooks/enforce-capped-bash.py"], "v0.5.3")
    assert "scripts/hooks/enforce-capped-bash.py" in line
    assert "v0.5.3" in line


def test_the_warning_names_a_remedy_that_can_be_run():
    """It used to end "Cut a release first (RELEASING.md)" -- a pointer to a five-step
    checklist, i.e. a session's work, quoted at someone who was only running an upgrade.
    The remedy is one task now, so the line names the task and the command behind it."""
    line = up.unreleased_line(["scripts/hooks/lint-fix.py"], "v0.5.3")
    assert up.RELEASE_TASK in line
    assert up.RELEASE_COMMAND in line


def test_no_gap_says_nothing():
    assert up.unreleased_line([], "v0.5.3") == ""


def test_the_gap_is_reported_once_for_the_run_before_any_box(tmp_path, capsys, monkeypatch):
    """Once, and early: the answer is the same for every project, and a reader who came
    for a specific vendored fix needs it before four green adoption lines."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "commit_for", lambda _devkit, _rev: RELEASE_COMMIT)
    monkeypatch.setattr(
        up, "unreleased_vendored_changes", lambda *_a: ["scripts/hooks/enforce-capped-bash.py"]
    )
    stamps_on_main(monkeypatch)
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("9d95e44\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 1
    assert capsys.readouterr().err.count("enforce-capped-bash.py") == 1


def test_the_undelivered_release_reaches_the_artifact_and_the_exit_code(
    tmp_path, capsys, monkeypatch
):
    """The gap this script exists to report was the one thing it reported *only* to
    stderr -- and the nightly pass runs under `pythonw`, whose stderr goes nowhere. So
    the machine's entire record of "every consumer is about to adopt a release missing
    the fix you merged" was an exit code of 0 beside a log saying the run was clean.

    Reverting either half fails here: drop the outcome and the artifact is empty, drop
    the exit code and `schedule_health` never surfaces the log at session start."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "commit_for", lambda _devkit, _rev: RELEASE_COMMIT)
    monkeypatch.setattr(up, "unreleased_vendored_changes", lambda *_a: ["scripts/hooks/hook.py"])
    stamps_on_main(monkeypatch)
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / "DEVKIT_VERSION").write_text("9d95e44\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli")

    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 1
    written = (up.REPO_ROOT / up.ARTIFACT).read_text(encoding="utf-8")
    assert "scripts/hooks/hook.py" in written
    assert up.RELEASE_TASK in written


def test_a_run_level_outcome_is_not_offered_as_a_retry_target():
    """`(run)` and `(release)` belong to no checkout, and the header used to feed them
    to the retry line anyway -- handing whoever found the artifact
    `upgrade-project.py (run) --yes`, a command that exits 2 on its own argument,
    offered as the way out of a failure."""
    only_run_scoped = up.artifact_body(
        "v0.7.0", False, [up.Outcome(up.RUN_SCOPED, 2, "upgrade: no workspace file at X")]
    )
    assert "no workspace file" in only_run_scoped
    assert "# retry:" not in only_run_scoped

    mixed = up.artifact_body(
        "v0.7.0",
        False,
        [
            up.Outcome(up.RELEASE_SCOPED, 1, "cut a release"),
            up.Outcome("carameli", 2, "FAILED at `git commit`"),
        ],
    )
    assert "# retry: python scripts/upgrade-project.py carameli --yes" in mixed
    assert up.RELEASE_SCOPED not in mixed.split("# unresolved:")[0]
