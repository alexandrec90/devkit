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


@pytest.fixture(autouse=True)
def no_real_git(monkeypatch):
    """No test here may reach the real git: the reader fetches and asks for tips now.
    A test that wants answers installs its own `git_for` over this one."""
    monkeypatch.setattr(
        ev.sweep, "git_for", lambda _p: lambda *a: subprocess.CompletedProcess(a, 1, "", "")
    )


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
    assert ev.newest_release(tmp_path) == "v0.11.21"
    assert ev.tb.slugify(ev.newest_release(tmp_path)) == "v0-11-21", (
        "the plan compares it to adoption branch names, which are slugs"
    )


def test_the_newest_release_is_read_as_written(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ev.sweep,
        "git_for",
        lambda _p: lambda *a: subprocess.CompletedProcess(a, 0, "v0.11.21\nv0.11.20\n", ""),
    )
    assert ev.newest_release(tmp_path) == "v0.11.21"


def test_no_tags_is_no_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ev.sweep, "git_for", lambda _p: lambda *a: subprocess.CompletedProcess(a, 1, "", "")
    )
    assert ev.newest_release(tmp_path) == ""


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


def test_evidence_already_in_the_worktree_is_left_where_it_is(tmp_path):
    """A refused commit's evidence is written straight into the worktree the fixer
    opens in; copying it onto itself would delete it first."""
    target = tmp_path / "tree" / fix_plan.EVIDENCE_DIR
    target.mkdir(parents=True)
    (target / "pre-commit.log").write_text("x", encoding="utf-8")
    failure = fix_plan.Failure(fix_plan.COMMIT, "carameli", 0, "", "", evidence=str(target))
    assert ev.place(failure, tmp_path / "tree") == target
    assert (target / "pre-commit.log").read_text(encoding="utf-8") == "x"


@pytest.mark.parametrize("evidence", ["", "C:/nowhere/at/all"])
def test_nothing_to_place_places_nothing(tmp_path, evidence):
    failure = fix_plan.Failure(fix_plan.PR, "carameli", 1, "", "", evidence=evidence)
    assert ev.place(failure, tmp_path) is None


# --- a default branch's own gate --------------------------------------------------------


RELEASE_RED = f"FAILED tests/test_new_project.py::{fix_plan.RELEASE_TEST} - bump the constant\n"


def test_one_slot_per_failure_named_for_what_it_is():
    assert ev.evidence_slot(fix_plan.Failure(fix_plan.PR, "carameli", 412, "", "")) == (
        "carameli-pr-412"
    )
    assert ev.evidence_slot(fix_plan.Failure(fix_plan.NIGHTLY, "carameli", 9, "", "")) == (
        "carameli-nightly-9"
    )
    assert ev.evidence_slot(
        fix_plan.Failure(fix_plan.COMMIT, "carameli", 0, "", "", head="agent/i-0919")
    ) == ("carameli-commit-agent-i-0919")
    assert ev.evidence_slot(
        fix_plan.Failure(fix_plan.BRANCH, "devkit", 0, "", "", base="main")
    ) == ("devkit-branch-main")


def test_the_newest_completed_run_at_the_tip_is_the_branchs_verdict(monkeypatch, tmp_path):
    """An unfinished run for some other commit above it says nothing; the completed one
    at the tip is the verdict."""
    runs = [
        {"databaseId": 3, "status": "in_progress", "conclusion": "", "headSha": "elsewhere"},
        {"databaseId": 2, "status": "completed", "conclusion": "success", "headSha": "fb17a310"},
    ]
    tip_world(monkeypatch, runs)
    assert ev.read_default_branch("devkit", tmp_path, tmp_path / "ev") == (True, None)
    tip_world(monkeypatch, runs[:1])
    assert ev.read_default_branch("devkit", tmp_path, tmp_path / "ev") == (None, None)


def branch_world(monkeypatch, conclusion: str, summary: str, tags: str):
    runs = [
        {
            "databaseId": 55,
            "status": "completed",
            "conclusion": conclusion,
            "headSha": "fb17a310",
            "url": "u/55",
            "workflowName": "PR Gate",
        }
    ]

    def gh(*args):
        if args[:2] == ("run", "list"):
            return subprocess.CompletedProcess(args, 0, json.dumps(runs), "")
        if args[:2] == ("run", "download"):
            target = Path(args[-1]) / "test-failures"
            target.mkdir(parents=True)
            (target / "test-failures.log").write_text(summary, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected {args}")

    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: gh)

    def git(*args):
        # `rev-parse origin/main` answers the tip; `tag --points-at` answers the tags.
        out = "fb17a310\n" if args[0] == "rev-parse" else tags
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(ev.sweep, "git_for", lambda _p: git)
    monkeypatch.setattr(ev.tb, "detect_default_branch", lambda _git, fallback="main": "main")


def test_a_green_default_branch_is_green_and_no_failure(monkeypatch, tmp_path):
    branch_world(monkeypatch, "success", "", "")
    assert ev.read_default_branch("devkit", tmp_path, tmp_path / "ev") == (True, None)


def test_a_red_default_branch_is_a_failure_with_its_run_downloaded(monkeypatch, tmp_path):
    branch_world(monkeypatch, "failure", SUMMARY, "")
    green, failure = ev.read_default_branch("carameli", tmp_path, tmp_path / "ev")
    assert green is False and failure is not None
    assert (failure.kind, failure.project, failure.number) == (fix_plan.BRANCH, "carameli", 0)
    assert (failure.base, failure.sha, failure.run_id) == ("main", "fb17a310", "55")
    assert failure.workflow == "PR Gate" and failure.url == "u/55"
    assert failure.signature == ("tests/test_x.py::test_y",)
    assert Path(failure.evidence) == tmp_path / "ev" / "carameli-branch-main"


def test_a_tagged_release_commits_red_is_green(monkeypatch, tmp_path):
    """The v0.11.23 shape: main's newest gate failed on the newest-tag test and nothing
    else, and the tag now points at that commit -- the release accepted it."""
    branch_world(monkeypatch, "failure", RELEASE_RED, "v0.11.23\n")
    assert ev.read_default_branch("devkit", tmp_path, tmp_path / "ev") == (True, None)


def test_an_untagged_release_commits_red_is_a_failure_for_the_plan_to_skip(monkeypatch, tmp_path):
    branch_world(monkeypatch, "failure", RELEASE_RED, "")
    green, failure = ev.read_default_branch("devkit", tmp_path, tmp_path / "ev")
    assert green is False and failure is not None
    assert fix_plan.is_release_red(failure.signature)


def test_an_unreadable_gate_is_no_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: table({}))
    monkeypatch.setattr(ev.sweep, "git_for", lambda _p: lambda *a: None)
    monkeypatch.setattr(ev.tb, "detect_default_branch", lambda _git, fallback="main": "main")
    assert ev.read_default_branch("devkit", tmp_path, tmp_path / "ev") == (None, None)


def test_a_run_that_is_not_at_the_branch_tip_is_no_verdict(monkeypatch, tmp_path):
    """carameli's gate has no push trigger, so its newest run on master was a May run
    four months behind the tip; a session was spent proving it stale."""
    branch_world(monkeypatch, "failure", SUMMARY, "")
    monkeypatch.setattr(
        ev.sweep,
        "git_for",
        lambda _p: lambda *a: subprocess.CompletedProcess(a, 0, "c70f5f7\n", ""),
    )
    assert ev.read_default_branch("carameli", tmp_path, tmp_path / "ev") == (None, None)


def test_reading_a_pr_marks_it_behind_unless_it_conflicts(monkeypatch, tmp_path):
    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: table({}))
    monkeypatch.setattr(ev.sweep, "git_for", lambda _p: lambda *a: None)
    monkeypatch.setattr(ev, "is_behind", lambda git, base, sha: True)
    plain = ev.read_pr(tmp_path, ev.pr_failure("carameli", pr()), tmp_path / "ev")
    assert plain.behind is True
    conflicted = ev.read_pr(
        tmp_path, ev.pr_failure("carameli", pr(mergeable="CONFLICTING")), tmp_path / "ev"
    )
    assert conflicted.behind is False and fix_plan.CONFLICT in conflicted.signature


def test_every_checkout_on_disk_has_its_default_branch_read(monkeypatch, tmp_path):
    workspace = tmp_path / "alex.code-workspace"
    (tmp_path / "devkit").mkdir()
    monkeypatch.setattr(ev, "read_default_branch", lambda name, _d, _r: (name == "devkit", None))
    assert ev.collect_default_branches(workspace, ["devkit", "missing"]) == {"devkit": (True, None)}


# --- the verdicts that are not verdicts -------------------------------------------------


def tip_world(monkeypatch, runs: list[dict], tip: str = "fb17a310"):
    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: table({("run", "list"): runs}))
    monkeypatch.setattr(
        ev.sweep,
        "git_for",
        lambda _p: lambda *a: subprocess.CompletedProcess(a, 0, f"{tip}\n", ""),
    )
    monkeypatch.setattr(ev.tb, "detect_default_branch", lambda _git, fallback="main": "main")


def test_a_gate_still_running_at_the_tip_is_running_not_unreadable(monkeypatch, tmp_path):
    """Every pass in the minutes after a merge to devkit main read the newest *completed*
    run, found it at the previous sha, said "could not be read" and held every project
    fixer behind a gate that was merely running."""
    runs = [
        {"databaseId": 3, "status": "in_progress", "conclusion": "", "headSha": "fb17a310"},
        {"databaseId": 2, "status": "completed", "conclusion": "success", "headSha": "older"},
    ]
    tip_world(monkeypatch, runs)
    assert ev.read_default_branch("devkit", tmp_path, tmp_path / "ev") == (fix_plan.RUNNING, None)


def test_a_cancelled_run_at_the_tip_is_no_verdict_rather_than_red(monkeypatch, tmp_path):
    """Anything but success read as red, so a cancelled run became a devkit session sent
    at an empty signature."""
    runs = [
        {"databaseId": 3, "status": "completed", "conclusion": "cancelled", "headSha": "fb17a310"}
    ]
    tip_world(monkeypatch, runs)
    assert ev.read_default_branch("devkit", tmp_path, tmp_path / "ev") == (None, None)


def test_the_default_branch_runs_are_the_listed_dicts_newest_first_or_nothing():
    runs = [{"databaseId": 3, "status": "in_progress"}, "junk", {"databaseId": 2}]
    assert ev.default_branch_runs(table({("run", "list"): runs}), "main") == [runs[0], runs[2]]
    assert ev.default_branch_runs(table({}), "main") == []
    assert ev.default_branch_runs(table({("run", "list"): {"not": "a list"}}), "main") == []


def test_the_runs_behind_failing_checks_are_read_off_the_rollup():
    rollup = [
        {"conclusion": "FAILURE", "detailsUrl": "https://github.com/x/y/actions/runs/91/job/5"},
        {"conclusion": "SUCCESS", "detailsUrl": "https://github.com/x/y/actions/runs/92/job/6"},
        {"state": "FAILURE", "targetUrl": "https://github.com/x/y/actions/runs/93/job/7"},
        {"conclusion": "FAILURE", "detailsUrl": "https://github.com/x/y/actions/runs/91/job/8"},
        {"conclusion": "FAILURE", "detailsUrl": "https://example.com/not-a-run"},
        "junk",
    ]
    assert ev.run_ids_from_rollup(rollup) == ("91", "93")
    assert ev.run_ids_from_rollup(None) == ()
    assert ev.pr_failure("carameli", pr(statusCheckRollup=rollup)).check_runs == ("91", "93")


def test_a_failing_check_from_another_workflow_is_its_own_evidence(monkeypatch, tmp_path):
    """Three of nine ledger entries were "no artifact and no failed step named": the
    gate workflow's run list had nothing at the sha because the failing check belonged
    to another workflow, and a session was sent blind at a run the rollup named."""
    rollup = [
        {"conclusion": "FAILURE", "detailsUrl": "https://github.com/x/y/actions/runs/91/job/5"}
    ]
    downloaded = []

    def gh(*args):
        if args[:2] == ("run", "list"):
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:2] == ("run", "download"):
            downloaded.append(args[2])
            target = Path(args[-1]) / "test-failures"
            target.mkdir(parents=True)
            (target / "test-failures.log").write_text(SUMMARY, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected {args}")

    monkeypatch.setattr(ev.sweep, "gh_for", lambda _p: gh)
    monkeypatch.setattr(ev, "is_behind", lambda git, base, sha: False)
    failure = ev.read_pr(
        tmp_path, ev.pr_failure("carameli", pr(statusCheckRollup=rollup)), tmp_path / "ev"
    )
    assert downloaded == ["91"]
    assert failure.run_id == "91" and failure.signature == ("tests/test_x.py::test_y",)
    assert Path(failure.evidence) == tmp_path / "ev" / "carameli-pr-412"
