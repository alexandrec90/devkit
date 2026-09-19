"""`scripts/gate_evidence.py`: what the gate said, fetched through a `gh` that is a table.

Every `gh` here is a callable answering from a dict keyed by the argv prefix, so the
suite asserts what was asked and what was made of the answer, and never opens a network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_plan
import gate_evidence as ev

SUMMARY = "FAILED tests/test_x.py::test_y - assert 1 == 2\n"


class Table:
    """A `gh(*args)` answering `answers[prefix]` (JSON-dumped unless str) for the longest
    matching prefix, and exit 1 for anything unlisted. Records every argv in `calls`."""

    def __init__(self, answers: dict[tuple[str, ...], object], code: int = 0):
        self.answers = answers
        self.code = code
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        for length in range(len(args), 0, -1):
            if args[:length] in self.answers:
                out = self.answers[args[:length]]
                text = out if isinstance(out, str) else json.dumps(out)
                return subprocess.CompletedProcess(["gh", *args], self.code, text, "")
        return subprocess.CompletedProcess(["gh", *args], 1, "", "no")


def table(answers: dict[tuple[str, ...], object], code: int = 0) -> Table:
    return Table(answers, code)


# --- the gate run behind a PR ----------------------------------------------------------


def test_the_run_at_the_head_sha_is_the_one_read():
    runs = [
        {"databaseId": 2, "headSha": "new", "conclusion": "failure"},
        {"databaseId": 1, "headSha": "old", "conclusion": "failure"},
    ]
    gh = table({("run", "list"): runs})
    assert ev.gate_run(gh, "agent/x", "old")["databaseId"] == 1
    assert gh.calls[0][:6] == ("run", "list", "--branch", "agent/x", "--workflow", "pr-gate.yml")


def test_a_run_for_another_sha_is_not_this_prs_evidence():
    """The list is the branch's recent runs; the one at a superseded push says nothing
    about the commit the PR is red on now."""
    gh = table({("run", "list"): [{"databaseId": 2, "headSha": "new"}]})
    assert ev.gate_run(gh, "agent/x", "old") == {}


def test_with_no_sha_the_newest_run_is_taken():
    gh = table({("run", "list"): [{"databaseId": 2, "headSha": "new"}, {"databaseId": 1}]})
    assert ev.gate_run(gh, "agent/x", "")["databaseId"] == 2


def test_a_gh_that_cannot_list_runs_is_no_run():
    assert ev.gate_run(table({}), "agent/x", "sha") == {}
    assert ev.gate_run(table({("run", "list"): "not json"}), "agent/x", "sha") == {}


def test_the_jobs_come_with_their_steps():
    jobs = [
        {
            "name": "Tests",
            "conclusion": "failure",
            "steps": [{"name": "s", "conclusion": "failure"}],
        }
    ]
    gh = table({("run", "view", "7"): {"jobs": jobs}})
    assert ev.run_jobs(gh, "7") == jobs
    assert ev.run_jobs(table({}), "7") == []


def test_the_download_lands_under_dest_and_the_logs_are_read_back(tmp_path):
    dest = tmp_path / "evidence"
    (dest / "stale.log").parent.mkdir()
    (dest / "stale.log").write_text("FAILED old::one", encoding="utf-8")

    def gh(*args):
        assert args[:3] == ("run", "download", "7")
        target = Path(args[args.index("-D") + 1]) / "test-failures"
        target.mkdir(parents=True)
        (target / "test-failures.log").write_text(SUMMARY, encoding="utf-8")
        return subprocess.CompletedProcess(["gh", *args], 0, "", "")

    texts = ev.download_logs(gh, "7", dest)
    assert texts == [SUMMARY]
    assert not (dest / "stale.log").exists(), "an earlier run's logs must not survive"


def test_a_download_that_fails_reads_nothing(tmp_path):
    assert ev.download_logs(table({}), "7", tmp_path / "e") == []


# --- scheduled failures ----------------------------------------------------------------


def test_only_the_reporters_issues_are_nightlies():
    issues = [
        {"number": 7, "title": "Nightly workflow is failing", "body": "", "url": "u"},
        {"number": 8, "title": "Nightly is slow", "body": "", "url": "u"},
    ]
    gh = table({("issue", "list"): issues})
    assert [i["number"] for i in ev.nightly_issues(gh)] == [7]
    assert ev.workflow_from_title("Nightly workflow is failing") == "Nightly"
    assert ev.workflow_from_title("Nightly is slow") == ""


def test_the_run_is_read_off_the_issues_facts_table():
    body = "| Run | https://github.com/x/y/actions/runs/35262286741 |"
    assert ev.run_id_from_body(body) == "35262286741"
    assert ev.run_id_from_body("no run here") == ""


# --- the newest release --------------------------------------------------------------


def test_the_newest_tag_is_slugified_like_a_branch(monkeypatch, tmp_path):
    def git(*args):
        assert args == ("tag", "--sort=-v:refname")
        return subprocess.CompletedProcess(["git", *args], 0, "v0.11.21\nv0.11.20\n", "")

    monkeypatch.setattr(ev.sweep, "git_for", lambda _p: git)
    assert ev.latest_tag(tmp_path) == "v0-11-21"


def test_no_tags_is_no_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ev.sweep, "git_for", lambda _p: lambda *a: subprocess.CompletedProcess(a, 1, "", "")
    )
    assert ev.latest_tag(tmp_path) == ""


# --- collecting everything red ------------------------------------------------------------


def pr(**fields) -> dict:
    base = {
        "number": 412,
        "title": "T",
        "url": "u/412",
        "headRefName": "agent/x",
        "baseRefName": "main",
        "headRefOid": "sha1",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"conclusion": "FAILURE"}],
    }
    base.update(fields)
    return base


def test_the_evidence_root_sits_beside_the_workspace_file(tmp_path):
    """Outside every checkout, so no project's tree gains an untracked directory, and
    beside the registry so one machine has one place to clear."""
    assert ev.evidence_root(tmp_path / "alex.code-workspace") == tmp_path / ".evidence"


def test_a_pr_becomes_a_failure_with_its_branch_sha_and_reason():
    failure = ev.pr_failure("carameli", pr())
    assert (failure.kind, failure.project, failure.number) == (fix_plan.PR, "carameli", 412)
    assert (failure.head, failure.base, failure.sha) == ("agent/x", "main", "sha1")
    assert failure.reason == "1 check failing"


def test_reading_a_pr_downloads_the_run_at_its_sha_and_signs_it(monkeypatch, tmp_path):
    runs = [{"databaseId": 7, "headSha": "sha1"}]

    def gh(*args):
        if args[:2] == ("run", "list"):
            return subprocess.CompletedProcess(args, 0, json.dumps(runs), "")
        if args[:2] == ("run", "download"):
            target = Path(args[-1]) / "test-failures"
            target.mkdir(parents=True)
            (target / "test-failures.log").write_text(SUMMARY, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected {args}")

    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: gh)
    failure = ev.read_pr(tmp_path, ev.pr_failure("carameli", pr()), tmp_path / "ev")
    assert failure.signature == ("tests/test_x.py::test_y",)
    assert failure.run_id == "7"
    assert Path(failure.evidence) == tmp_path / "ev" / "carameli-pr-412"


def test_without_an_artifact_the_steps_are_asked_for(monkeypatch, tmp_path):
    gh = table(
        {
            ("run", "list"): [{"databaseId": 7, "headSha": "sha1"}],
            ("run", "view"): {"jobs": [{"name": "Drift", "conclusion": "failure", "steps": []}]},
        }
    )
    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: gh)
    failure = ev.read_pr(tmp_path, ev.pr_failure("carameli", pr()), tmp_path / "ev")
    assert failure.signature == ("Drift",)
    assert failure.evidence == ""


def test_a_conflict_with_no_run_is_still_signed(monkeypatch, tmp_path):
    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: table({}))
    conflicted = pr(mergeable="CONFLICTING", statusCheckRollup=[])
    failure = ev.read_pr(tmp_path, ev.pr_failure("carameli", conflicted), tmp_path / "ev")
    assert failure.signature == (fix_plan.CONFLICT,)


def test_an_issue_becomes_a_nightly_failure_on_the_default_branch(monkeypatch, tmp_path):
    issue = {
        "number": 9,
        "title": "Nightly workflow is failing",
        "body": "| Run | https://github.com/x/y/actions/runs/55 |",
        "url": "u/9",
    }
    gh = table(
        {("run", "view"): {"jobs": [{"name": "Suite", "conclusion": "failure", "steps": []}]}}
    )
    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: gh)
    monkeypatch.setattr(
        ev.sweep, "git_for", lambda _p: lambda *a: subprocess.CompletedProcess(a, 1, "", "")
    )
    monkeypatch.setattr(ev.tb, "detect_default_branch", lambda _git, fallback="main": "master")
    failure = ev.read_issue("carameli", tmp_path, issue, tmp_path / "ev")
    assert (failure.kind, failure.number, failure.workflow) == (fix_plan.NIGHTLY, 9, "Nightly")
    assert (failure.base, failure.run_id, failure.signature) == ("master", "55", ("Suite",))


def test_collect_reads_every_red_pr_and_every_tracker_issue(monkeypatch, tmp_path):
    workspace = tmp_path / "w" / "alex.code-workspace"
    workspace.parent.mkdir()
    seen: list[str] = []
    monkeypatch.setattr(
        ev, "read_pr", lambda _d, f, _r: seen.append(f"pr {f.project}#{f.number}") or f
    )
    monkeypatch.setattr(
        ev,
        "read_issue",
        lambda p, _d, i, _r: (
            seen.append(f"issue {p}#{i['number']}")
            or fix_plan.Failure(fix_plan.NIGHTLY, p, i["number"], "", "")
        ),
    )
    issues = {
        "carameli": [{"number": 9, "title": "Nightly workflow is failing", "body": "", "url": ""}]
    }
    monkeypatch.setattr(
        ev.sweep, "gh_for", lambda d: table({("issue", "list"): issues.get(d.name, [])})
    )
    found = {"carameli": [pr(number=1), pr(number=2)], "devkit": []}
    failures = ev.collect(workspace, found)
    assert seen == ["pr carameli#1", "pr carameli#2", "issue carameli#9"]
    assert len(failures) == 3


def test_the_evidence_is_placed_under_logs_gate_in_the_worktree(tmp_path):
    source = tmp_path / "ev" / "carameli-pr-1"
    source.mkdir(parents=True)
    (source / "test-failures.log").write_text(SUMMARY, encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    failure = fix_plan.Failure(fix_plan.PR, "carameli", 1, "", "", evidence=str(source))
    placed = ev.place(failure, tree)
    assert placed == tree / "logs" / "gate"
    assert (placed / "test-failures.log").read_text(encoding="utf-8") == SUMMARY
    nested = ev.place(failure, tree, "carameli")
    assert nested == tree / "logs" / "gate" / "carameli"


@pytest.mark.parametrize("evidence", ["", "C:/nowhere/at/all"])
def test_nothing_to_place_places_nothing(tmp_path, evidence):
    failure = fix_plan.Failure(fix_plan.PR, "carameli", 1, "", "", evidence=evidence)
    assert ev.place(failure, tmp_path) is None
