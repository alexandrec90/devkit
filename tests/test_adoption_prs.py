"""`scripts/adoption_prs.py`: naming, finding and superseding a devkit adoption PR.

The names moved here from `upgrade-project.py` keep their sweep-side tests in
`tests/test_upgrade_project.py`, which drives them through the sweep's own bindings.
What is here is each name on its own terms, and the rule that is new with the module:
only the newest release's adoption stands.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import adoption_prs


def gh_listing(payload, code=0):
    """A `gh` that answers every call with `payload` (str, or JSON-able), recording argv."""
    calls: list[tuple[str, ...]] = []

    def gh(*args: str):
        calls.append(args)
        out = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(["gh", *args], code, out, "")

    gh.calls = calls
    return gh


# --- naming -----------------------------------------------------------------------


def test_the_slug_carries_the_release_because_a_merged_branch_name_is_retired():
    assert adoption_prs.upgrade_slug("v0.11.21") == f"{adoption_prs.UPGRADE_SLUG} v0.11.21"


def test_the_stem_is_the_automation_namespace_plus_the_slugified_topic():
    assert adoption_prs.upgrade_branch_stem("v0.11.21") == "agent/auto/devkit-upgrade-v0-11-21-"


def test_both_stems_are_searched_and_only_the_first_is_ever_cut():
    stems = adoption_prs.upgrade_branch_stems("v0.11.21")
    assert stems == ("agent/auto/devkit-upgrade-v0-11-21-", "agent/devkit-upgrade-v0-11-21-")
    assert stems[0] == adoption_prs.upgrade_branch_stem("v0.11.21")


def test_the_adoption_prefixes_are_the_stems_with_the_tag_cut_off():
    """What lets a reader tell an adoption PR from any other without knowing which
    release it adopts. Derived from the same namer as the stems, so the two cannot
    disagree about spelling."""
    prefixes = adoption_prs.adoption_prefixes()
    assert prefixes == ("agent/auto/devkit-upgrade-", "agent/devkit-upgrade-")
    for stem in adoption_prs.upgrade_branch_stems("v0.11.21"):
        assert stem.startswith(prefixes)


def test_the_branch_stem_is_built_from_the_box_tiers_own_namer():
    """Restating `agent/` or the slug rules here would give the stem a second author,
    and a rename in `task_branch` would stop matching without failing anything."""
    stem = adoption_prs.upgrade_branch_stem("v0.10.2")
    assert (
        stem
        == f"{adoption_prs.tb.AUTOMATION_PREFIX}{adoption_prs.tb.slugify(adoption_prs.upgrade_slug('v0.10.2'))}-"
    )
    # And it really is a prefix of what the box tier would cut, on any day.
    cut = adoption_prs.tb.branch_name(
        adoption_prs.tb.slugify(adoption_prs.upgrade_slug("v0.10.2")),
        set(),
        _dt.date(2026, 8, 20),
        prefix=adoption_prs.tb.AUTOMATION_PREFIX,
    )
    assert cut.startswith(stem)


def test_the_upgrade_branch_says_no_session_asked_for_it():
    """This sweep cuts the same vendoring commit in every consumer, nightly, and a
    reviewer opening `preview-task.py` was being offered all of them ahead of the change
    they had asked to look at. The namespace is what that menu filters on, so it is a
    contract here rather than a naming preference -- and it stays inside `agent/`, so
    the branch still ships like any other."""
    stem = adoption_prs.upgrade_branch_stem("v0.11.2")
    assert adoption_prs.tb.is_automation_branch(stem)
    assert adoption_prs.tb.is_managed_task_branch(stem)


def test_only_one_stem_is_ever_cut_however_many_are_searched_for():
    """`upgrade_branch_stems` widens the *lookup* and must never widen the *naming* --
    a run that cut the legacy stem back would undo the move on the next nightly."""
    stems = adoption_prs.upgrade_branch_stems("v0.11.2")
    assert stems[0] == adoption_prs.upgrade_branch_stem("v0.11.2")
    assert any(not adoption_prs.tb.is_automation_branch(stem) for stem in stems)
    # `str.startswith` takes the tuple as-is; a list would raise at the call site.
    assert isinstance(stems, tuple)


# --- finding -----------------------------------------------------------------------


def test_an_open_adoption_for_this_release_is_found_by_stem(tmp_path, monkeypatch):
    gh = gh_listing(
        [
            {"number": 173, "headRefName": "agent/baseline-drift-0819", "url": "u/173"},
            {
                "number": 170,
                "headRefName": "agent/auto/devkit-upgrade-v0-11-21-0917",
                "url": "u/170",
            },
        ]
    )
    monkeypatch.setattr(adoption_prs.sweep, "gh_for", lambda _p: gh)
    assert adoption_prs.open_adoption_pr(tmp_path, "v0.11.21") == "#170 u/170"
    assert adoption_prs.open_adoption_pr(tmp_path, "v0.11.22") == ""


def test_a_gh_that_cannot_answer_finds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(adoption_prs.sweep, "gh_for", lambda _p: gh_listing("", code=1))
    assert adoption_prs.open_adoption_pr(tmp_path, "v0.11.21") == ""


# --- superseding ---------------------------------------------------------------------


def test_an_adoption_of_an_older_release_is_superseded_and_this_ones_is_not():
    rows = [
        {"number": 170, "headRefName": "agent/auto/devkit-upgrade-v0-11-20-0916", "url": "u"},
        {"number": 171, "headRefName": "agent/auto/devkit-upgrade-v0-11-21-0917", "url": "u"},
        {"number": 160, "headRefName": "agent/devkit-upgrade-v0-10-2-0816", "url": "u"},
        {"number": 173, "headRefName": "agent/baseline-drift-0819", "url": "u"},
        "not a row",
    ]
    stale = adoption_prs.superseded_adoptions(rows, "v0.11.21")
    assert [row["number"] for row in stale] == [170, 160]


def test_older_adoptions_are_closed_with_the_successor_named(tmp_path, monkeypatch):
    """A red v0.11.20 adoption beside the v0.11.21 one is not a second chance at the
    older release: merging it lands a vendored copy the next pass replaces, and an
    agent sent to fix it spends a session on a tree nobody wants."""
    gh = gh_listing(
        [
            {"number": 170, "headRefName": "agent/auto/devkit-upgrade-v0-11-20-0916", "url": "u"},
            {"number": 171, "headRefName": "agent/auto/devkit-upgrade-v0-11-21-0917", "url": "u"},
        ]
    )
    monkeypatch.setattr(adoption_prs.sweep, "gh_for", lambda _p: gh)
    assert adoption_prs.close_superseded(tmp_path, "v0.11.21") == ["#170"]
    closes = [call for call in gh.calls if call[:2] == ("pr", "close")]
    assert len(closes) == 1
    assert closes[0][2] == "170"
    assert "--comment" in closes[0] and "v0.11.21" in closes[0][-1]
    assert "--delete-branch" not in closes[0], "the box behind it is reconcile's to reap"


def test_a_gh_that_cannot_list_closes_nothing(tmp_path, monkeypatch):
    """Fails open, on `open_adoption_pr`'s terms: a stale PR is a nuisance, an upgrade
    that stops because the CLI is missing is a silent one."""
    monkeypatch.setattr(adoption_prs.sweep, "gh_for", lambda _p: gh_listing("", code=1))
    assert adoption_prs.close_superseded(tmp_path, "v0.11.21") == []
    monkeypatch.setattr(adoption_prs.sweep, "gh_for", lambda _p: gh_listing("not json"))
    assert adoption_prs.close_superseded(tmp_path, "v0.11.21") == []


def test_a_close_gh_refused_is_not_reported_as_closed(tmp_path, monkeypatch):
    rows = [{"number": 170, "headRefName": "agent/auto/devkit-upgrade-v0-11-20-0916", "url": "u"}]

    def gh(*args):
        if args[:2] == ("pr", "close"):
            return subprocess.CompletedProcess(["gh", *args], 1, "", "no permission")
        return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(rows), "")

    monkeypatch.setattr(adoption_prs.sweep, "gh_for", lambda _p: gh)
    assert adoption_prs.close_superseded(tmp_path, "v0.11.21") == []


def test_only_a_green_labelled_adoption_is_mergeable_unattended():
    rows = [
        {
            "number": 1,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE",
        },
        {
            "number": 2,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        },
        {
            "number": 3,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
        },
        {
            "number": 4,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [],
        },
        {
            "number": 5,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeable": "CONFLICTING",
        },
        {
            "number": 6,
            "headRefName": "agent/feature-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        },
        {
            "number": 7,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "isDraft": True,
        },
    ]
    prefixes = ("agent/auto/devkit-upgrade-", "agent/devkit-upgrade-")
    assert [r["number"] for r in adoption_prs.green_adoptions(rows, prefixes, "automerge")] == [1]
