"""`scripts/fix_trigger.py`: what starts the fix pass at once, and what never does."""

from __future__ import annotations

import datetime as _dt
import itertools
import json
import subprocess
from pathlib import Path

import project_registry
import pytest
from support import load_script

trigger = load_script("scripts/fix_trigger.py")
results = trigger.e2e_results

NOW = _dt.datetime(2026, 11, 25, 12, 0, tzinfo=_dt.UTC)


def commit(oid: str, *conclusions: str | None, workflow: str = "PR Gate") -> dict:
    suites = [
        {"conclusion": said, "workflowRun": {"workflow": {"name": workflow}}}
        for said in conclusions
    ]
    return {"oid": oid, "checkSuites": {"nodes": [{"workflowRun": None}, *suites]}}


def pull(number=7, mergeable="MERGEABLE", state="SUCCESS", draft=False) -> dict:
    return {
        "number": number,
        "isDraft": draft,
        "mergeable": mergeable,
        "headRefOid": "feed",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]},
    }


def repo(commits=(), prs=(), green="") -> dict:
    """`commits` newest first, each the first parent of the one before it."""
    chained = list(commits)
    for child, parent in itertools.pairwise(chained):
        child.setdefault("parents", {"nodes": [{"oid": parent["oid"]}]})
    return {
        "ref": {"target": {"oid": green}} if green else None,
        "defaultBranchRef": {"target": {"history": {"nodes": chained}}},
        "pullRequests": {"nodes": list(prs)},
    }


# --- GitHub --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("conclusions", "says"),
    [
        (("SUCCESS",), True),
        (("FAILURE",), False),
        (("TIMED_OUT",), False),
        (("FAILURE", "SUCCESS"), True),  # a re-run cleared it
        ((None,), None),  # still running
        (("CANCELLED",), None),
        ((), None),
    ],
)
def test_a_commits_verdict_is_its_gates(conclusions, says):
    assert trigger.verdict(commit("a", *conclusions)) is says
    assert trigger.verdict(commit("a", *conclusions, workflow="Nightly")) is None


def test_a_red_under_a_running_tip_is_a_signal():
    """At a hundred merges a day the tip is nearly always running."""
    commits = [commit("c", None), commit("b", "FAILURE"), commit("a", "SUCCESS")]
    assert trigger.branch_signals("me/p", repo(commits)) == {"red:me/p@b"}


def test_a_branch_that_recovered_lets_what_was_held_go():
    commits = [commit("b", "SUCCESS"), commit("a", "FAILURE")]
    assert trigger.branch_signals("me/p", repo(commits)) == {"green:me/p@b"}
    steady = [commit("b", "SUCCESS"), commit("a", "SUCCESS"), commit("0", "FAILURE")]
    assert trigger.branch_signals("me/p", repo(steady)) == set()
    assert trigger.branch_signals("me/p", {"defaultBranchRef": None}) == set()


def test_a_merged_branchs_own_commits_say_nothing_of_the_branch():
    """An adoption merges as a merge commit, so its PR's red and green runs are history."""
    merge = commit("m", None)
    merge["parents"] = {"nodes": [{"oid": "base"}]}
    noise = commit("pr", "FAILURE")
    base = commit("base", "SUCCESS")
    nodes = [merge, noise, base]
    assert [c["oid"] for c in trigger.first_parents(nodes)] == ["m", "base"]
    assert trigger.first_parents([]) == []
    history = {"defaultBranchRef": {"target": {"history": {"nodes": nodes}}}}
    assert trigger.judged(history) == [("base", True)]


def test_green_is_owed_the_newest_commit_that_passed_and_never_goes_back():
    commits = [commit("c", None), commit("b", "SUCCESS"), commit("a", "SUCCESS")]
    assert trigger.green_owed(repo(commits, green="a")) == "b"
    assert trigger.green_owed(repo(commits, green="b")) == ""
    assert trigger.green_owed(repo(commits, green="c")) == "", "already past it"
    assert trigger.green_owed(repo(commits, green="far behind")) == "b"
    assert trigger.green_owed(repo(commits)) == "", "the workflow's to create"
    assert trigger.green_owed(repo([commit("a", "FAILURE")], green="z")) == ""


def test_green_moves_forward_only_and_a_refusal_moves_nothing():
    asked: list[list[str]] = []

    def run(argv, _timeout):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 0 if "me/a" in argv[4] else 1, "", "422")

    owed = repo([commit("b", "SUCCESS"), commit("a", "SUCCESS")], green="a")
    assert trigger.move_green({"me/a": owed, "me/b": owed, "me/c": repo()}, run) == ["me/a to b"]
    assert all("force=false" in argv for argv in asked) and len(asked) == 2
    assert not trigger.advance_green("me/b", "b", run)


def test_a_passing_commit_is_devkits_to_release_and_every_projects_to_run():
    passing = repo([commit("b", None), commit("a", "SUCCESS")])
    red = repo([commit("b", "FAILURE"), commit("a", "SUCCESS")])
    assert trigger.ready_signals("me/devkit", passing) == {"ready:me/devkit@a"}
    assert trigger.ready_signals("me/devkit", red) == set() == trigger.ready_signals("", {})
    assert trigger.passed_signals("me/p", red) == {"passed:me/p@a"}
    assert trigger.passed_signals("me/p", repo()) == set()


def test_a_conflicted_or_red_pr_is_a_signal_and_an_undecided_or_draft_one_is_not():
    prs = [
        pull(1, mergeable="CONFLICTING"),
        pull(2, state="FAILURE"),
        pull(3, mergeable="UNKNOWN"),
        pull(4, mergeable="CONFLICTING", draft=True),
        {"number": 5, "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}},
    ]
    assert trigger.pr_signals("me/p", repo(prs=prs)) == {
        "conflict:me/p#1@feed",
        "pr-red:me/p#2@feed",
    }


def checkout(root: Path, name: str, url: str) -> None:
    (root / name / ".git").mkdir(parents=True)
    text = f'[remote "origin"]\n\turl = {url}\n\tfetch = x\n'
    (root / name / ".git" / "config").write_text(text, encoding="utf-8")


def test_every_repository_is_asked_in_one_call(tmp_path):
    checkout(tmp_path, "a", "https://github.com/me/a.git")
    checkout(tmp_path, "b", "git@github.com:me/b")
    checkout(tmp_path, "c", "https://example.com/me/c.git")
    asked: list[list[str]] = []

    def run(argv, timeout):
        asked.append(argv)
        data = {"r0": repo([commit("x", "FAILURE")]), "r1": None}
        # What `gh` does when one repository errors: the rest on stdout, and exit 1.
        return subprocess.CompletedProcess(argv, 1, json.dumps({"data": data}), "gone")

    assert trigger.slug_of(tmp_path / "c") == "" == trigger.slug_of(tmp_path / "missing")
    repos = trigger.repositories(tmp_path, ["a", "b", "c"], run)
    assert trigger.github_signals(tmp_path, repos) == {"red:me/a@x"}
    [argv] = asked
    assert 'owner: "me", name: "b"' in argv[-1] and "fragment R" in argv[-1]


@pytest.mark.parametrize("said", ["", "not json", "[]", '{"data": null}'])
def test_an_unreadable_github_is_no_signal(said):
    assert (
        trigger.ask(["me/a"], lambda argv, _t: subprocess.CompletedProcess(argv, 1, said, "")) == {}
    )
    assert trigger.ask([], lambda *_a: pytest.fail("nothing to ask")) == {}
    assert trigger.query_for(["me/a"]).startswith("query {")


# --- this machine --------------------------------------------------------------------------


def test_a_red_local_run_is_a_signal_whichever_tier_found_it(tmp_path):
    red = results.Result("p", results.FAILED, sha="abc", signature=("t::x",))
    results.write(red, tmp_path)
    results.write(red, tmp_path, results.MEDIUM)
    results.write(results.Result("q", results.PASSED, sha="abc"), tmp_path)
    found = trigger.local_signals(tmp_path, ["p", "q", "absent"])
    assert {key.split("@")[0] for key in found} == {"local:e2e/p", "local:medium-tier/p"}
    again = results.Result("p", results.FAILED, sha="abc", signature=("t::x",), finished="later")
    results.write(again, tmp_path)
    assert trigger.local_signals(tmp_path, ["p"]) == found, "the same red, re-run"


def tree(root: Path, name="t1", project="p") -> Path:
    logs = root / project / ".claude" / "worktrees" / name / "logs"
    logs.mkdir(parents=True)
    return logs


def test_what_a_session_leaves_for_the_pass_is_a_signal(tmp_path):
    logs = tree(tmp_path)
    (logs / "fix-blocked.md").write_text("no key\n", encoding="utf-8")
    (logs / "friction.md").write_text("\n", encoding="utf-8")
    (logs / "ship-state.json").write_text('{"stage": "refused"}', encoding="utf-8")
    shipped = tree(tmp_path, "t2")
    (shipped / "ship-state.json").write_text('{"stage": "shipped"}', encoding="utf-8")
    found = trigger.tree_signals(tmp_path, ["p", "absent"])
    assert {key.rsplit(":", 1)[0] for key in found} == {
        "tree:t1/fix-blocked.md",
        "tree:t1/ship-state.json",
    }
    assert [t.name for t in trigger.worktrees(tmp_path, ["p"])] == ["t1", "t2"]
    assert not trigger.refused("not json") and not trigger.refused("[]")


def row(event: str, stamp="2026-11-25T12:00:00", **fields) -> str:
    return "\t".join([stamp, f"event={event}", *(f"{k}={v}" for k, v in fields.items())])


def test_a_failed_job_is_one_signal_however_often_it_fails():
    first = row("scheduled-job-failed", project="p", cause="no disk", message="exit 1 at 12:00")
    again = row("scheduled-job-failed", "later", project="p", cause="no disk", message="at 12:02")
    other = row("scheduled-job-failed", project="p", cause="no network")
    assert trigger.event_key(first) == trigger.event_key(again) != ""
    assert trigger.event_key(other) != trigger.event_key(first)
    assert trigger.event_key(row("agent-report", message="a")) != trigger.event_key(
        row("agent-report", message="b")
    )


@pytest.mark.parametrize(
    "line", ["", "junk", row("fix-pass-finding"), row("session-friction"), row("triage-resolved")]
)
def test_what_the_pass_writes_itself_never_starts_the_next_one(line):
    assert trigger.event_key(line) == ""


def test_the_ledgers_rows_are_read_off_its_end(tmp_path):
    (tmp_path / "logs").mkdir()
    ledger = tmp_path / "logs" / "harness-events.log"
    filler = "x" * trigger.LEDGER_TAIL_BYTES
    old = row("agent-report", message="old")
    ledger.write_text(f"{old}\n{filler}\n{row('agent-report', message='new')}\n", encoding="utf-8")
    assert trigger.event_signals(tmp_path) == {
        trigger.event_key(row("agent-report", message="new"))
    }


# --- what is new, and starting the pass ------------------------------------------------------


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """The project registry `Machine` stands for: the trigger reads its projects there,
    and the machine file at the root only for `devkit.fixPass`."""
    monkeypatch.setattr(project_registry, "all_projects", lambda *_a, **_k: ["devkit"])


class Machine:
    def __init__(self, tmp_path: Path, mode="dispatch", started=0):
        self.root, self.started = tmp_path, started
        machine = tmp_path / project_registry.MACHINE_FILE_NAME
        machine.write_text(json.dumps({"devkit.fixPass": mode}), encoding="utf-8")
        checkout(tmp_path, "devkit", "https://github.com/me/devkit.git")
        self.repo = repo()
        self.calls: list[list[str]] = []
        self.busy: set[str] = set()

    def run(self, argv, _timeout):
        self.calls.append(argv)
        if argv[0] == "gh":
            out = json.dumps({"data": {"r0": self.repo}})
            return subprocess.CompletedProcess(argv, 0, out, "")
        if "/Query" in argv:
            says = "Running" if argv[3] in self.busy else "Ready"
            return subprocess.CompletedProcess(argv, 0, f'"{argv[3]}","x","{says}"', "")
        return subprocess.CompletedProcess(argv, self.started, "", "ERROR: no such task")

    def fire(self, now=NOW) -> int:
        return trigger.run_once(self.root, self.run, now)

    @property
    def starts(self) -> int:
        return self.started_tasks.count(trigger.FIX_TASK)

    @property
    def started_tasks(self) -> list[str]:
        return [argv[3] for argv in self.calls if argv[:2] == ["schtasks", "/Run"]]


def test_a_new_red_starts_one_pass_and_the_same_red_no_second(tmp_path):
    machine = Machine(tmp_path)
    assert machine.fire() == 0 and machine.starts == 0, "nothing red"
    machine.repo = repo([commit("a", "FAILURE")])
    assert machine.fire() == 0 and machine.starts == 1
    assert machine.calls[-1] == ["schtasks", "/Run", "/TN", "devkit-fix-pass"]
    assert machine.fire() == 0 and machine.starts == 1
    machine.repo = repo([commit("b", "FAILURE"), commit("a", "FAILURE")])
    assert machine.fire() == 0 and machine.starts == 2


def test_a_red_found_while_a_pass_runs_starts_the_next_one(tmp_path):
    """The running pass may already have read that gate, so it does not spend the signal."""
    machine = Machine(tmp_path)
    machine.repo = repo([commit("a", "FAILURE")])
    lock = tmp_path / ".worktrees" / trigger.FIX_LOCK
    lock.mkdir(parents=True)
    assert machine.fire() == 0 and machine.starts == 0
    lock.rmdir()
    assert machine.fire() == 0 and machine.starts == 1


def test_a_lock_a_dead_pass_left_holds_nothing(tmp_path):
    lock = tmp_path / ".worktrees" / trigger.FIX_LOCK
    assert not trigger.pass_running(tmp_path)
    lock.mkdir(parents=True)
    made = lock.stat().st_mtime
    assert trigger.pass_running(tmp_path, lambda: made + 60)
    assert not trigger.pass_running(tmp_path, lambda: made + trigger.FIX_LOCK_STALE)


def test_a_pass_that_would_not_start_is_tried_again_and_fails_the_job(tmp_path, capsys):
    machine = Machine(tmp_path, started=1)
    machine.repo = repo([commit("a", "FAILURE")])
    assert machine.fire() == 1
    assert "error: could not start devkit-fix-pass: ERROR: no such task" in capsys.readouterr().err
    machine.started = 0
    assert machine.fire() == 0 and machine.starts == 2
    assert trigger.start(trigger.FIX_TASK, machine.run) == ""
    assert trigger.pass_for(tmp_path, ["red:x"], machine.run) == (True, 0)
    assert trigger.pass_for(tmp_path, [], machine.run) == (True, 0)


@pytest.mark.parametrize("mode", ["off", "plan"])
def test_nothing_starts_where_the_pass_sends_no_one(tmp_path, mode, capsys):
    machine = Machine(tmp_path, mode)
    machine.repo = repo([commit("a", "FAILURE")])
    assert machine.fire() == 0 and machine.starts == 0
    assert f"devkit.fixPass is {mode}" in capsys.readouterr().out


def test_a_commit_that_passed_moves_green_and_then_what_runs_it(tmp_path):
    """A fire each, because the collectors' redeploy reads the checkout the sync moves."""
    machine = Machine(tmp_path, mode="off")
    machine.repo = repo([commit("b", "SUCCESS"), commit("a", "SUCCESS")], green="a")
    assert machine.fire() == 0
    assert any("PATCH" in argv for argv in machine.calls)
    assert machine.started_tasks == ["devkit-worktree-reconcile"]
    machine.repo = repo([commit("b", "SUCCESS"), commit("a", "SUCCESS")], green="b")
    machine.busy = {"devkit-collectors"}
    assert machine.fire() == 0 and len(machine.started_tasks) == 1, "never beside itself"
    machine.busy = set()
    assert machine.fire() == 0
    assert machine.started_tasks == list(trigger.FOLLOWERS)
    assert machine.fire() == 0 and len(machine.started_tasks) == 2, "once per commit"


def test_devkits_passing_commit_starts_a_pass_which_is_what_releases_it(tmp_path):
    machine = Machine(tmp_path)
    machine.repo = repo([commit("b", "SUCCESS")], green="b")
    assert machine.fire() == 0 and machine.starts == 1
    assert machine.fire() == 0 and machine.starts == 1


def test_a_follower_that_will_not_start_is_dropped(tmp_path):
    machine = Machine(tmp_path, started=1)
    assert trigger.start_follower(list(trigger.FOLLOWERS), machine.run) == ["devkit-collectors"]
    assert trigger.start_follower([], machine.run) == []
    assert not trigger.running("devkit-collectors", machine.run)


def test_signals_gathers_every_source(tmp_path):
    machine = Machine(tmp_path)
    machine.repo = repo([commit("a", "FAILURE")], [pull(mergeable="CONFLICTING")])
    (tree(tmp_path, project="devkit") / "friction.md").write_text("x", encoding="utf-8")
    red = results.Result("devkit", results.FAILED, sha="a")
    results.write(red, tmp_path / "devkit")
    ledger = tmp_path / "devkit" / "logs" / "harness-events.log"
    ledger.write_text(row("workspace-failure", project="p") + "\n", encoding="utf-8")
    repos = trigger.repositories(tmp_path, ["devkit"], machine.run)
    found = trigger.signals(tmp_path, ["devkit"], repos)
    assert {key.split(":")[0] for key in found} == {"red", "conflict", "tree", "local", "event"}


def test_a_red_whose_revert_clock_reached_each_mark_starts_a_pass_once_per_mark(tmp_path):
    """A red nothing lands on raises no other signal: without this, the half-hourly
    backstop would stretch the revert grace by up to thirty minutes."""
    devkit = tmp_path / "devkit"
    seen = trigger.red_seen.first_seen(devkit, "roguelike", "revert", "a" * 40, NOW)
    grace, ceiling = trigger.red_seen.REVERT_GRACE, trigger.red_seen.REVERT_CEILING
    assert trigger.revert_signals(devkit, seen + grace - _dt.timedelta(seconds=1)) == set()
    at = f"revert-due:roguelike@{'a' * 12}"
    assert trigger.revert_signals(devkit, seen + grace) == {f"{at}:grace"}
    assert trigger.revert_signals(devkit, seen + ceiling) == {f"{at}:grace", f"{at}:ceiling"}
    repos: dict[str, dict] = {}
    assert "revert-due" not in str(trigger.signals(tmp_path, ["roguelike"], repos)), "no clock"
    assert trigger.revert_signals(tmp_path / "nowhere", NOW + ceiling) == set()


def test_what_still_stands_is_remembered_and_the_rest_forgotten(tmp_path):
    long_ago = (NOW - trigger.REMEMBER - _dt.timedelta(days=1)).isoformat()
    kept = trigger.remember({"gone": long_ago, "standing": long_ago}, ["standing"], NOW)
    assert kept == {"standing": NOW.isoformat()}
    path = trigger.state_path(tmp_path)
    assert trigger.read_state(path) == ({}, [])
    trigger.write_state(path, kept, ["devkit-collectors"])
    assert trigger.read_state(path) == (kept, ["devkit-collectors"])
    trigger.write_state(path, kept, ["not a follower"])
    assert trigger.read_state(path) == (kept, [])
    path.write_text("[]", encoding="utf-8")
    assert trigger.read_state(path) == ({}, [])


def test_the_names_spelled_here_are_their_owners():
    fix_pass = load_script("scripts/fix-pass.py")
    installer = load_script("scripts/install-fix-pass-task.py")
    assert trigger.FIX_TASK == installer.TASK_NAME
    assert trigger.FIX_LOCK == fix_pass.RUN_LOCK_NAME
    assert trigger.FIX_LOCK_STALE == fix_pass.RUN_LOCK_STALE
    assert trigger.GREEN == load_script("scripts/green-ref.py").GREEN
    assert trigger.FOLLOWERS == (
        load_script("scripts/install-reconcile-task.py").TASK_NAME,
        load_script("scripts/install-collectors.py").TASK_NAME,
    )
    assert trigger.REFUSED == fix_pass.ship_intent.REFUSED
    assert trigger.SHIP_STATE == fix_pass.ship_intent.STATE_FILE.name


def test_main_needs_no_workspace_file_and_takes_a_deprecated_one_as_its_root(
    tmp_path, monkeypatch
):
    fired = []
    monkeypatch.setattr(trigger, "run_once", lambda root: fired.append(root) or 0)
    assert trigger.main(["--root", str(tmp_path)]) == 0
    assert trigger.main(["--workspace", str(tmp_path / "none" / "w.code-workspace")]) == 0
    assert fired == [tmp_path.resolve(), (tmp_path / "none").resolve()]
