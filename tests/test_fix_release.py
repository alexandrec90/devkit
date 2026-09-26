"""`scripts/fix_release.py`: the fix pass's adoptions and the release it starts.

Every `gh`, `git` and spawn is replaced; the pipeline's own predicate is tested in
`test_release_pipeline.py`, and what is here is when the pass acts on it.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import fix_release

log_wrap = load_script("scripts/log-wrap.py")

NOW = _dt.datetime(2026, 9, 26, 1, 0, tzinfo=_dt.UTC)
OWED = [".claude/rules/session-scope.md", "scripts/sync-devkit.py"]


def gh_returning(rows):
    return lambda _d: lambda *a: subprocess.CompletedProcess(a, 0, stdout=json.dumps(rows))


# --- adoptions ----------------------------------------------------------------------------


def test_pending_adoptions_names_the_consumers_still_adopting(monkeypatch, tmp_path):
    for name in ("devkit", "carameli", "roguelike"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(
        fix_release.adoption_prs,
        "open_adoption_pr",
        lambda d, tag: "#1 u" if d.name == "roguelike" else "",
    )
    assert fix_release.pending_adoptions(
        tmp_path, ["devkit", "carameli", "roguelike"], "v0.11.22"
    ) == ["roguelike"]
    assert fix_release.pending_adoptions(tmp_path, ["carameli"], "") == []


def test_green_adoptions_are_merged_through_the_reconcile_merge(monkeypatch, tmp_path):
    (tmp_path / "carameli").mkdir()
    rows = [
        {
            "number": 5,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE",
        }
    ]
    monkeypatch.setattr(fix_release.sweep, "gh_for", gh_returning(rows))
    merged = []
    monkeypatch.setattr(
        fix_release.worktree,
        "merge_pr",
        lambda gh, n: merged.append(n) or (True, f"merged PR #{n}"),
    )
    lines = fix_release.merge_green_adoptions(tmp_path, ["devkit", "carameli"])
    assert merged == [5] and lines == ["carameli #5 -- merged PR #5"]


# --- when a release starts ----------------------------------------------------------------


def test_nothing_owed_is_no_refusal_and_no_line():
    assert fix_release.release_refusal([], "v0.11.25", True, 0, None, NOW) == ""


def test_an_owed_release_on_a_green_main_with_nothing_in_flight_goes():
    assert fix_release.release_refusal(OWED, "v0.11.25", True, 0, None, NOW) == ""


def test_no_tag_is_never_a_first_release_by_the_pass():
    """`release_needed` has nothing to diff against, and a first release is a person's
    call -- a tag list git refused to read once planned v0.1.0 over v0.9.1."""
    why = fix_release.release_refusal(OWED, "", True, 0, None, NOW)
    assert "no release tag" in why


@pytest.mark.parametrize("green", [False, None, "running"])
def test_a_main_that_is_not_green_holds_the_release(green):
    why = fix_release.release_refusal(OWED, "v0.11.25", green, 0, None, NOW)
    assert "2 change(s) v0.11.25 cannot deliver" in why and "not green" in why


def test_a_release_pr_already_up_is_a_release_in_flight():
    why = fix_release.release_refusal(OWED, "v0.11.25", True, 401, None, NOW)
    assert "#401" in why


def test_a_recent_start_waits_out_the_cooldown_and_an_old_one_does_not():
    recent = NOW - fix_release.STARTED_COOLDOWN + _dt.timedelta(minutes=1)
    stale = NOW - fix_release.STARTED_COOLDOWN - _dt.timedelta(minutes=1)
    assert "started one at" in fix_release.release_refusal(OWED, "v0.11.25", True, 0, recent, NOW)
    assert fix_release.release_refusal(OWED, "v0.11.25", True, 0, stale, NOW) == ""


def test_the_release_pr_is_found_by_its_branch_namespace(monkeypatch, tmp_path):
    rows = [
        {"number": 7, "headRefName": "agent/x-0926"},
        {"number": 9, "headRefName": "release/v0.11.26"},
    ]
    monkeypatch.setattr(fix_release.sweep, "gh_for", gh_returning(rows))
    assert fix_release.release_pr_in_flight(tmp_path) == 9
    monkeypatch.setattr(fix_release.sweep, "gh_for", gh_returning(rows[:1]))
    assert fix_release.release_pr_in_flight(tmp_path) == 0


def test_an_unreadable_pr_list_is_no_release_in_flight(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fix_release.sweep,
        "gh_for",
        lambda _d: lambda *a: subprocess.CompletedProcess(a, 1, stdout="", stderr="auth"),
    )
    assert fix_release.release_pr_in_flight(tmp_path) == 0


def test_the_start_stamp_is_the_cooldown_clock(tmp_path):
    assert fix_release.last_started(tmp_path) is None
    stamp = tmp_path / fix_release.STARTED
    stamp.parent.mkdir(parents=True)
    stamp.write_text("", encoding="utf-8")
    moment = NOW.timestamp()
    os.utime(stamp, (moment, moment))
    assert fix_release.last_started(tmp_path) == NOW


# --- the start ----------------------------------------------------------------------------


def test_the_start_is_the_nightly_command_under_the_passs_own_label(tmp_path):
    workspace = tmp_path / "alex.code-workspace"
    argv = fix_release.release_argv(tmp_path, workspace, "python.exe")
    wrapper = argv.index(str(tmp_path / "scripts" / "log-wrap.py"))
    assert argv[wrapper + 1 : wrapper + 4] == ["--always", fix_release.LABEL, "--"]
    pipeline = argv.index(str(tmp_path / "scripts" / "release-pipeline.py"))
    assert argv[pipeline + 1 :] == [
        *fix_release.release_schedule.PIPELINE_ARGS,
        "--workspace",
        str(workspace),
    ]
    assert fix_release.LABEL != fix_release.release_schedule.LABEL, (
        "the nightly run's record is the one a start must not overwrite"
    )


def test_the_record_path_is_the_one_log_wrap_writes_for_the_label():
    assert fix_release.RECORD == Path("logs") / f"{log_wrap.slug(fix_release.LABEL)}.log"


def test_a_start_spawns_detached_and_stamps(monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(
        fix_release.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw))
    )
    assert fix_release.start_release(tmp_path, tmp_path / "ws") == ""
    (_argv, kw) = spawned[0]
    assert kw["cwd"] == str(tmp_path)
    assert kw["stdin"] is subprocess.DEVNULL and kw["stdout"] is subprocess.DEVNULL
    assert kw["creationflags"] == fix_release.sweep.NO_WINDOW | fix_release.NEW_GROUP
    assert fix_release.last_started(tmp_path) is not None


def test_a_start_that_could_not_spawn_says_why_and_leaves_no_stamp(monkeypatch, tmp_path):
    def refuse(argv, **kw):
        raise OSError("no such interpreter")

    monkeypatch.setattr(fix_release.subprocess, "Popen", refuse)
    assert fix_release.start_release(tmp_path, tmp_path / "ws") == "no such interpreter"
    assert fix_release.last_started(tmp_path) is None


# --- the step -----------------------------------------------------------------------------


@pytest.fixture
def devkit(tmp_path, monkeypatch):
    """A workspace beside a devkit checkout, with git, gh and the spawn replaced."""
    (tmp_path / "devkit").mkdir()
    table = {"owed": list(OWED), "in_flight": 0, "started": []}
    monkeypatch.setattr(
        fix_release.sweep,
        "git_for",
        lambda _d: lambda *a: subprocess.CompletedProcess(a, 0, stdout=""),
    )
    monkeypatch.setattr(
        fix_release.release_pipeline, "release_needed", lambda d, tag: table["owed"]
    )
    monkeypatch.setattr(fix_release, "release_pr_in_flight", lambda d: table["in_flight"])
    monkeypatch.setattr(
        fix_release,
        "start_release",
        lambda d, ws: table["started"].append((d, ws)) or "",
    )
    table["workspace"] = tmp_path / "alex.code-workspace"
    return table


def test_the_step_starts_an_owed_release_when_dispatching(devkit):
    line = fix_release.cut_release(devkit["workspace"], "v0.11.25", True, True, NOW)
    assert line.startswith("started -- 2 change(s) v0.11.25 cannot deliver")
    assert fix_release.RECORD.as_posix() in line
    assert devkit["started"] == [(devkit["workspace"].parent / "devkit", devkit["workspace"])]


def test_the_step_only_says_it_would_when_planning(devkit):
    line = fix_release.cut_release(devkit["workspace"], "v0.11.25", True, False, NOW)
    assert line.startswith("would start -- ")
    assert devkit["started"] == []


def test_the_step_says_nothing_when_nothing_is_owed(devkit):
    devkit["owed"] = []
    assert fix_release.cut_release(devkit["workspace"], "v0.11.25", True, True, NOW) == ""
    assert devkit["started"] == []


def test_the_step_names_its_refusal_and_starts_nothing(devkit):
    devkit["in_flight"] = 401
    line = fix_release.cut_release(devkit["workspace"], "v0.11.25", True, True, NOW)
    assert line.startswith("not started -- ") and "#401" in line
    assert devkit["started"] == []


def test_a_failed_start_is_on_the_record(devkit, monkeypatch):
    monkeypatch.setattr(fix_release, "start_release", lambda d, ws: "no such interpreter")
    line = fix_release.cut_release(devkit["workspace"], "v0.11.25", True, True, NOW)
    assert line == "FAILED to start -- no such interpreter"


def test_no_devkit_checkout_is_no_release_step(tmp_path):
    assert fix_release.cut_release(tmp_path / "ws", "v0.11.25", True, True, NOW) == ""
