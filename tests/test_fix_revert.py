"""`scripts/fix_revert.py`: the pure half, then real git in throwaway repositories.

The property of the second half is what a revert does to a tree, and to a later pass
reading it, which a faked git could only assert about itself. `gh` is replaced: what it
would say is the input here.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_findings
import fix_loop
import fix_plan
import fix_revert
import red_seen
import ship_intent

NOW = _dt.datetime(2026, 11, 6, 12, 0, tzinfo=_dt.UTC)
SIG = ("tests/test_x.py::test_y",)
TESTS = frozenset({"Tests"})
LINT = frozenset({"Lint"})
PASSED: frozenset[str] = frozenset()


def commit(sha: str, subject: str = "Add a thing (#1)", when: float = 0.0, author: str = "t"):
    return fix_revert.Commit(sha, subject, when, author=author)


def red(**fields) -> fix_plan.Failure:
    base = {
        "kind": fix_plan.BRANCH,
        "project": "roguelike",
        "number": 0,
        "title": "PR Gate red on main",
        "url": "u",
        "base": "main",
        "sha": "tip",
        "run_id": "9",
        "workflow": "PR Gate",
        "signature": SIG,
    }
    return fix_plan.Failure(**{**base, **fields})


def run(sha: str, conclusion: str, status: str = "completed", run_id: int = 1) -> dict:
    return {"headSha": sha, "conclusion": conclusion, "status": status, "databaseId": run_id}


def job(name: str = "Tests", conclusion: str = "failure", step: str = "Application suite") -> dict:
    return {
        "name": name,
        "conclusion": conclusion,
        "steps": [{"name": step, "conclusion": conclusion}],
    }


# --- the pure half -----------------------------------------------------------------


def test_a_log_record_is_a_commit_with_its_author_and_trailer():
    log = (
        "aaa\x1fp1\x1f100\x1ft\x1fAdd a thing (#1)\x1fAdd a thing\n\nBecause.\n\x1e\n"
        "bbb\x1fp1 p2\x1f200\x1fgithub-actions[bot]\x1fMerge #2\x1f\x1e\n"
        "ccc\x1fp1\x1f300\x1ft\x1fLand it again\x1fLand it\n\nReland-of: aaa\nCloses #4\n\x1e\n"
        "not a commit\n"
    )
    assert fix_revert.parse_log(log) == [
        fix_revert.Commit("aaa", "Add a thing (#1)", 100.0, False, "t"),
        fix_revert.Commit("bbb", "Merge #2", 200.0, True, "github-actions[bot]"),
        fix_revert.Commit("ccc", "Land it again", 300.0, False, "t", "aaa"),
    ]


def test_only_success_and_failure_judge_a_commit():
    """D1: a run cancelled, timed out or never started said nothing about the commit."""
    runs = [
        run("a", "success", run_id=3),
        run("a", "failure", run_id=2),
        run("b", "failure", run_id=4),
        run("c", "", "in_progress"),
        run("d", "cancelled"),
        run("e", "startup_failure"),
        run("f", "timed_out"),
    ]
    assert fix_revert.judged_runs(runs) == {"a": (True, "3"), "b": (False, "4")}


def test_a_failing_job_is_one_that_failed_on_something_the_repo_did():
    assert fix_revert.failed_jobs([job("Tests"), job("Lint", "success")]) == TESTS
    assert fix_revert.failed_jobs([job("Tests", "timed_out")]) == PASSED
    assert fix_revert.failed_jobs([job("Tests", "cancelled")]) == PASSED
    assert fix_revert.failed_jobs([job("Tests", step="Set up job")]) == PASSED
    # A job GitHub refused over billing, or never gave a runner, ran no step.
    assert fix_revert.failed_jobs([{"name": "Tests", "conclusion": "failure", "steps": []}]) == (
        PASSED
    )


def test_the_red_range_runs_from_after_green_to_the_oldest_overlap():
    """Commits with no verdict may be the cause: the first failed run saw them all."""
    commits = [commit(sha) for sha in "abcde"]
    verdicts = {"c": TESTS, "e": TESTS}
    assert fix_revert.red_range(commits, verdicts, TESTS) == commits[:3]


def test_a_release_red_on_the_tips_job_is_not_the_culprit():
    """D1: `[Release vX, Y]` reverted the release and left Y on main for good."""
    release = commit("r", "Release v0.11.84 (#742)")
    culprit = commit("y", "Break the suite (#743)")
    verdicts = {"r": TESTS, "y": TESTS}
    assert fix_revert.red_range([release, culprit], verdicts, TESTS) == [release, culprit]
    assert fix_revert.candidates([release, culprit]) == [culprit]


def test_a_commit_that_failed_other_jobs_clears_itself_and_what_came_before():
    """D1: it ran the tip's jobs clean, so this red is not its."""
    commits = [commit(sha) for sha in "abc"]
    assert fix_revert.red_range(commits, {"b": LINT, "c": TESTS}, TESTS) == commits[2:]


def test_a_commit_the_gate_passed_ends_the_range_below_it():
    """`green` lagging must not widen a revert to work the gate has already passed."""
    commits = [commit(sha) for sha in "abcde"]
    verdicts = {"b": PASSED, "d": TESTS}
    assert fix_revert.red_range(commits, verdicts, TESTS) == commits[2:4]


def test_nothing_matching_the_tip_is_no_range():
    commits = [commit(sha) for sha in "abc"]
    assert fix_revert.red_range(commits, {"a": TESTS, "c": PASSED}, TESTS) == []
    assert fix_revert.red_range(commits, {}, TESTS) == []
    assert fix_revert.red_range(commits, {"a": TESTS}, PASSED) == []
    assert fix_revert.red_range([], {"a": TESTS}, TESTS) == []


def test_a_range_is_not_overdue_before_the_grace_runs_out():
    assert fix_revert.RED_GRACE == _dt.timedelta(hours=2)
    assert not fix_revert.overdue(NOW, NOW + _dt.timedelta(hours=1, minutes=59))
    assert fix_revert.overdue(NOW, NOW + _dt.timedelta(hours=2))


def test_only_tidys_own_commit_is_exempt_not_its_prefix():
    """D4: a session's `Tidy: drop the helper` is ordinary work."""
    keep = commit("a", "Add a thing (#1)")
    session = commit("b", "Tidy: drop the helper (#2)")
    borrowed = commit("c", fix_revert.TIDY_SUBJECT)
    tidy = commit("d", fix_revert.TIDY_SUBJECT, author=fix_revert.TIDY_AUTHOR)
    undone = commit("e", 'Revert "Add a thing (#1)"')
    release = commit("f", "Release v1.2.3")
    named = fix_revert.candidates([keep, session, borrowed, tidy, undone, release])
    assert named == [keep, session, borrowed]
    assert fix_revert.TIDY_SUBJECT == "Tidy: formatters, baselines and mirrors"
    assert fix_revert.TIDY_AUTHOR == "github-actions[bot]"


@pytest.mark.parametrize(
    "fields",
    [
        {"unread": True},
        {"lost_runs": ("9",)},
        {"signature": ()},
        {"signature": (fix_plan.CONFLICT,)},
        {"signature": (f"Tests {fix_plan.UNSTARTED}",)},
        {"signature": ("Tests / Set up job",)},
        {"signature": (f"Tests / {fix_plan.NO_BILLING}",)},
        {"signature": (f"tests/test_x.py::{fix_plan.RELEASE_TEST}",)},
        {"signature": ("Lint + hook-script tests / Format",)},
        {
            "signature": ("Pre-commit gate / Run every hook over the whole repo",),
            "hooks": (("Pre-commit gate", "ruff-format"),),
        },
        {"kind": fix_plan.LOCAL},
    ],
)
def test_red_that_says_nothing_about_a_commit_reverts_nothing(fields):
    """An outage or a bill must not take a day's merges off the branch."""
    assert not fix_revert.revertible(red(**fields))


def test_a_test_failure_says_something_about_a_commit():
    assert fix_revert.revertible(red())


def test_a_tidy_red_past_its_grace_is_eligible_like_any_other():
    """D5: tidy cannot land `.github/workflows`, so someone else has to."""
    formatting = red(signature=("Lint + hook-script tests / Format",), tidy_lapsed=True)
    assert fix_revert.revertible(formatting)


def test_the_reland_task_is_toml_and_says_what_to_do():
    """A subject can hold a quote or a backslash, and the queue reads this with `tomllib`."""
    one = commit("abc1234def", 'Fix "the" C:\\path (#3)')
    task = tomllib.loads(fix_revert.reland_task(one, red()))
    assert task["when"] == "merged"
    prompt = task["agent"]
    assert one.sha in prompt and one.subject in prompt and SIG[0] in prompt
    assert "land it again" in prompt and "PR Gate" in prompt
    assert f"Reland-of: {one.sha}" in prompt, "D4: what ends the cycle at one"
    assert one.task == Path(".devkit-queue") / "reland-abc1234.toml"


def test_a_long_signature_is_cut_in_the_prompt():
    sig = tuple(f"tests/test_{n}.py::t" for n in range(9))
    task = tomllib.loads(fix_revert.reland_task(commit("a"), red(signature=sig)))
    assert "(+3 more)" in task["agent"]


def test_the_revert_is_staged_and_a_merge_commit_names_its_mainline():
    assert fix_revert.revert_argv(commit("a")) == ["git", "revert", "--no-commit", "a"]
    merged = fix_revert.Commit("b", "Merge", 0.0, merge=True)
    assert fix_revert.revert_argv(merged) == ["git", "revert", "--no-commit", "-m", "1", "b"]


def test_the_revert_intent_is_an_intent_the_ship_task_can_read():
    one = commit("abc1234def", "Add a thing (#1)")
    subject, body = ship_intent.parse_intent(fix_revert.revert_intent(one, red()))
    assert subject == 'Revert "Add a thing (#1)"'
    assert ".devkit-queue/reland-abc1234.toml" in body and SIG[0] in body
    assert not ship_intent.asks_hold(body), "a held revert would leave the branch red"


def test_the_revert_intent_of_a_re_land_queues_nothing():
    one = fix_revert.Commit("abc1234def", "Land it", 0.0, reland_of="fff")
    _, body = ship_intent.parse_intent(fix_revert.revert_intent(one, red()))
    assert "`fff`" in body and "nothing is queued" in body and ".devkit-queue" not in body


# --- real git ----------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


class World:
    """An origin, an author pushing to it, and the checkout the pass works from."""

    def __init__(self, tmp: Path, monkeypatch) -> None:
        self.tmp = tmp
        self.origin, self.author = tmp / "o.git", tmp / "author"
        self.root = tmp / "ws"
        self.checkout = self.root / "roguelike"
        self.pulls: dict[str, list] = {}
        self.runs: list[dict] = []
        self.jobs: dict[str, list[dict]] = {}
        self.gh_fails = False
        self.view_fails = False
        self.started = _dt.datetime.now(_dt.UTC)
        _git(tmp, "init", "--bare", "-b", "main", str(self.origin))
        _git(tmp, "clone", str(self.origin), str(self.author))
        self.config(self.author)
        (self.author / ".gitignore").write_text(".claude/worktrees/\nlogs/\n", encoding="utf-8")
        self.write("a.txt", "one\n", "base")
        _git(self.author, "push", "origin", "main:green")
        _git(tmp, "-c", "core.autocrlf=false", "clone", str(self.origin), str(self.checkout))
        self.config(self.checkout)
        _git(self.checkout, "remote", "set-head", "origin", "main")
        monkeypatch.setattr(fix_revert.sweep, "gh_for", lambda _dir: self.gh)
        monkeypatch.setattr(fix_revert.fix_trees.sweep, "gh_for", lambda _dir: self.gh)
        self.journal = fix_findings.Journal(self.root / "devkit")

    def config(self, repo: Path) -> None:
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            _git(repo, "config", key, value)
        _git(repo, "config", "commit.gpgsign", "false")
        _git(repo, "config", "core.hooksPath", str(self.tmp / "no-hooks"))
        _git(repo, "config", "core.autocrlf", "false")

    def write(self, path: str, text: str, subject: str, *message: str) -> str:
        (self.author / path).parent.mkdir(parents=True, exist_ok=True)
        (self.author / path).write_text(text, encoding="utf-8", newline="\n")
        _git(self.author, "add", ".")
        _git(self.author, "commit", "-m", subject, *message)
        _git(self.author, "push", "origin", "main")
        return _git(self.author, "rev-parse", "HEAD")

    def tidy(self, path: str, text: str) -> str:
        (self.author / path).write_text(text, encoding="utf-8", newline="\n")
        _git(self.author, "add", ".")
        bot = f"user.name={fix_revert.TIDY_AUTHOR}"
        _git(self.author, "-c", bot, "commit", "-m", fix_revert.TIDY_SUBJECT)
        _git(self.author, "push", "origin", "main")
        return _git(self.author, "rev-parse", "HEAD")

    def fail(self, sha: str, *jobs: str, conclusion: str = "failure") -> None:
        """The gate failed `sha` on `jobs`: this run is the newest."""
        run_id = str(len(self.runs) + 100)
        self.runs.insert(0, run(sha, conclusion, run_id=int(run_id)))
        self.jobs[run_id] = [job(name) for name in jobs or ("Tests",)]

    def passes(self, sha: str) -> None:
        self.runs.insert(0, run(sha, "success", run_id=len(self.runs) + 100))

    def gh(self, *args):
        if args[:2] == ("run", "list"):
            return subprocess.CompletedProcess(args, 0, json.dumps(self.runs), "")
        if args[:2] == ("run", "view"):
            if self.view_fails:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
            jobs = {"jobs": self.jobs[args[2]]}
            return subprocess.CompletedProcess(args, 0, json.dumps(jobs), "")
        if args[:2] == ("pr", "list"):
            if self.gh_fails:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
            head = args[args.index("--head") + 1]
            return subprocess.CompletedProcess(args, 0, json.dumps(self.pulls.get(head, [])), "")
        raise AssertionError(f"unexpected gh {args}")

    def fetch(self) -> None:
        _git(self.checkout, "fetch", "--quiet", "origin")

    def ctx(self, mode: str = fix_cycle.DISPATCH, direct=("roguelike",), hours: float = 0.0):
        return fix_loop.Context(
            self.root,
            ["roguelike"],
            self.root / "devkit",
            self.root / "ledger",
            self.root / "history",
            mode,
            self.started + _dt.timedelta(hours=hours),
            None,
            frozenset(direct),
        )

    def failure(self, **fields) -> fix_plan.Failure:
        return red(sha=_git(self.author, "rev-parse", "HEAD"), **fields)

    def a_pass(self, hours: float = 0.0, mode=fix_cycle.DISPATCH, direct=("roguelike",)):
        self.fetch()
        ctx = self.ctx(mode, direct, hours)
        return fix_revert.tend(ctx, [self.failure()], self.journal)

    def tend(self, mode=fix_cycle.DISPATCH, direct=("roguelike",)) -> list[str]:
        """A pass that sees the red, then the one past its grace."""
        assert self.a_pass(direct=direct) == []
        return self.a_pass(3.0, mode, direct)

    def tree_of(self, branch: str) -> Path:
        listed = _git(self.checkout, "worktree", "list", "--porcelain")
        blocks = [block.splitlines() for block in listed.split("\n\n")]
        found = [
            b[0].removeprefix("worktree ") for b in blocks if f"branch refs/heads/{branch}" in b
        ]
        return Path(found[0])

    def no_tree_was_cut(self) -> bool:
        return _git(self.checkout, "worktree", "list").count("\n") == 0


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_a_range_past_its_grace_becomes_a_revert_left_for_the_ship_task(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    [line] = world.tend()
    branch = f"agent/revert-{bad[:7]}"
    assert branch in line and "FAILED" not in line
    tree = world.tree_of(branch)
    assert (tree / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (tree / ship_intent.fix_reports.ORIGIN_FILE).is_file()
    subject, _ = ship_intent.parse_intent(
        (tree / ship_intent.INTENT_FILE).read_text(encoding="utf-8")
    )
    assert subject == 'Revert "Break a thing (#1)"'
    task = tomllib.loads((tree / ".devkit-queue" / f"reland-{bad[:7]}.toml").read_text("utf-8"))
    assert task["when"] == "merged" and bad in task["agent"]
    assert _git(tree, "diff", "--cached", "--name-only") == "a.txt", "staged for the ship task"


def test_it_ships_as_a_merge_now_pr_the_pass_owns(world):
    """The tree is the road every other change takes: the label, and that the pass may
    close it, both come from the mark `revert_one` leaves."""
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    world.tend()
    tree = world.tree_of(f"agent/revert-{bad[:7]}")
    one = ship_intent.Intent("roguelike", tree, f"agent/revert-{bad[:7]}", "S", "B", direct=True)
    assert ship_intent.labels_for(tree, direct=True) == ("merge-now",)
    assert ship_intent.pass_owned(one)


def test_the_culprit_is_the_commit_after_a_release_not_the_release(world):
    """D1, as it happened: `[Release vX, Y]` with Y's run on the same job."""
    release = world.write("v.txt", "1\n", "Release v0.11.84 (#742)")
    culprit = world.write("a.txt", "two\n", "Break the suite (#743)")
    world.fail(release)
    world.fail(culprit)
    assert [line.split()[1] for line in world.tend()] == [culprit[:7]]


def test_a_run_that_was_refused_is_not_the_first_red(world):
    """D1: an outage on the older commit must not name it."""
    older = world.write("b.txt", "b\n", "Add b (#1)")
    bad = world.write("a.txt", "two\n", "Break a thing (#2)")
    world.fail(older, conclusion="startup_failure")
    world.fail(bad)
    lines = world.tend()
    assert [line.split()[1] for line in lines] == [bad[:7], older[:7]], "unjudged, so in range"
    assert world.tree_of(f"agent/revert-{older[:7]}")


def test_a_commit_that_failed_other_jobs_is_left_alone(world):
    lint = world.write("b.txt", "b\n", "Lint it red (#1)")
    bad = world.write("a.txt", "two\n", "Break a thing (#2)")
    world.fail(lint, "Lint")
    world.fail(bad, "Tests")
    assert [line.split()[1] for line in world.tend()] == [bad[:7]]


def test_nothing_is_reverted_when_nothing_matches_the_tips_jobs(world):
    """The tip's own run did not conclude `failure`: it is the fixer's."""
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad, conclusion="timed_out")
    assert world.tend() == [] and world.no_tree_was_cut()


def test_a_job_read_that_fails_reverts_nothing_rather_than_guess(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    world.view_fails = True
    assert world.tend() == [] and world.no_tree_was_cut()


def test_each_commit_of_the_range_is_reverted_newest_first_and_tidy_is_not(world):
    one = world.write("b.txt", "b\n", "Add b (#1)")
    world.tidy("a.txt", "one\n\n")
    two = world.write("c.txt", "c\n", "Add c (#2)")
    world.fail(two)
    lines = world.tend()
    assert [line.split()[1] for line in lines] == [two[:7], one[:7]]
    assert not [line for line in lines if "Tidy" in line]


def test_a_tidy_commit_over_the_target_is_reverted_with_it(world):
    """D2: tidy reformats the lines of the commit it follows, so `git revert X` alone
    conflicts in the normal case. The tree must also commit: the ship task does."""
    bad = world.write("code.py", "x=1\ny=2\n", "Break a thing (#1)")
    tidy = world.tidy("code.py", "x = 1\ny = 2\n")
    other = world.write("b.txt", "b\n", "Add b (#2)")
    world.fail(other)
    world.fail(bad)
    [line] = world.tend()
    assert "FAILED" not in line and "conflicts" not in line
    tree = world.tree_of(f"agent/revert-{bad[:7]}")
    assert not (tree / "code.py").exists() and (tree / "b.txt").is_file()
    intent = (tree / ship_intent.INTENT_FILE).read_text(encoding="utf-8")
    assert f"`{tidy[:7]}`" in intent and "the next tidy formats again" in intent
    _git(tree, "add", "-A")
    _git(tree, "commit", "-m", "the ship task's commit")
    assert "code.py" not in _git(tree, "ls-files")


def test_a_tidy_commit_over_the_target_conflicts_without_being_reverted(world):
    """That D2 is real: the same history, with the tidy step taken away."""
    bad = world.write("code.py", "x=1\ny=2\n", "Break a thing (#1)")
    world.tidy("code.py", "x = 1\ny = 2\n")
    world.fetch()
    tree = world.tmp / "plain"
    _git(world.checkout, "worktree", "add", "--detach", str(tree), "origin/main")
    plain = subprocess.run(
        ["git", "revert", "--no-commit", bad], cwd=tree, capture_output=True, text=True, check=False
    )
    assert plain.returncode != 0


def test_a_tidy_commit_over_other_files_stays(world):
    bad = world.write("code.py", "x=1\n", "Break a thing (#1)")
    world.tidy("a.txt", "one\n\n")
    tip = world.write("b.txt", "b\n", "Add b (#2)")
    world.fail(tip)
    world.fail(bad)
    world.tend()
    tree = world.tree_of(f"agent/revert-{bad[:7]}")
    assert (tree / "a.txt").read_text(encoding="utf-8") == "one\n\n"


def test_a_session_commit_with_a_tidy_subject_is_reverted(world):
    """D4."""
    bad = world.write("a.txt", "two\n", "Tidy: drop the helper (#1)")
    world.fail(bad)
    assert [line.split()[1] for line in world.tend()] == [bad[:7]]


def test_a_first_pass_after_a_sleep_gives_the_fixer_its_chance(world):
    """D3: measured from the commit, the first pass after the PC woke sent a fixer and the
    revert together. Here the commit is five hours old when a pass first sees it."""
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    assert world.a_pass(5.0) == []
    assert world.a_pass(6.5) == [], "the grace runs from that first pass"
    assert world.a_pass(7.0) != []


def test_unreadable_state_starts_the_grace_again(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    assert world.a_pass() == []
    state = world.root / "devkit" / red_seen.STATE_DIR / "roguelike.json"
    state.write_text("not json", encoding="utf-8")
    assert world.a_pass(3.0) == []
    assert world.a_pass(5.0) != []


def test_a_new_culprit_starts_its_own_grace(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    assert world.a_pass() == []
    world.passes(bad)
    other = world.write("b.txt", "b\n", "Break another (#2)")
    world.fail(other)
    assert world.a_pass(3.0) == []
    assert world.a_pass(5.0) != []


def test_a_re_land_that_goes_red_is_reverted_once_more_and_queues_nothing(world):
    """D4: the cycle ends at one, with the devkit session told."""
    original = "a" * 40
    bad = world.write("a.txt", "two\n", "Land it again (#3)", "-m", f"Reland-of: {original}")
    world.fail(bad)
    [line] = world.tend()
    assert "FAILED" not in line
    tree = world.tree_of(f"agent/revert-{bad[:7]}")
    assert not (tree / ".devkit-queue").exists()
    assert (tree / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert [f.kind for f in world.journal.findings] == ["reland-red-again"]
    assert original in Path(world.journal.findings[0].evidence).read_text(encoding="utf-8")


def test_a_gated_project_is_never_reverted(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    assert world.tend(direct=()) == [] and world.no_tree_was_cut()


def test_a_project_with_no_green_yet_has_no_range(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    _git(world.author, "push", "origin", "--delete", "green")
    _git(world.checkout, "fetch", "--quiet", "--prune", "origin")
    assert world.tend() == []


def test_a_plan_says_what_it_would_revert_and_cuts_nothing(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    assert world.tend(mode=fix_cycle.PLAN) == [
        f"roguelike {bad[:7]} -- would revert: Break a thing (#1)"
    ]
    assert world.no_tree_was_cut()


def test_a_plan_starts_no_clock(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    assert world.a_pass(mode=fix_cycle.PLAN) == []
    assert not (world.root / "devkit" / red_seen.STATE_DIR).exists()


def test_one_revert_per_sha_while_its_intent_waits_for_the_ship_task(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    assert world.tend()
    assert world.a_pass(4.0) == [], "the branch is here, if not on origin yet"


def test_one_revert_per_sha_when_the_branch_is_on_origin(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    _git(world.author, "push", "origin", f"HEAD:refs/heads/agent/revert-{bad[:7]}")
    assert world.tend() == []


def test_one_revert_per_sha_after_its_merged_branch_was_deleted(world):
    """Judged by the branch alone this would revert again on every pass while the branch
    stayed red: merging deletes it, and a PR that ever headed it is what remains."""
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    world.pulls[f"agent/revert-{bad[:7]}"] = [{"number": 9}]
    assert world.tend() == []


def test_a_gh_that_cannot_say_reverts_nothing(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    world.gh_fails = True
    assert world.tend() == []


def test_a_revert_that_conflicts_is_aborted_and_filed_and_tried_again_later(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    tip = world.write("a.txt", "three\n", "Build on the thing (#2)")
    world.fail(bad)
    world.fail(tip)
    [line] = world.tend()
    assert "aborted and filed" in line and "FAILED" not in line
    assert [f.kind for f in world.journal.findings] == ["revert-conflict"]
    assert bad in Path(world.journal.findings[0].evidence).read_text(encoding="utf-8")
    assert world.no_tree_was_cut(), "the tree went"
    branch = f"agent/revert-{bad[:7]}"
    assert _git(world.checkout, "branch", "--list", branch) == ""
    assert "aborted and filed" in world.a_pass(4.0)[0], "no branch, so the next pass may try"


def test_a_commit_already_undone_on_the_branch_leaves_nothing_behind(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    undone = world.write("a.txt", "one\n", "Undo it by hand (#2)")
    world.fail(bad)
    world.fail(undone)
    [line] = world.tend()
    assert "already undone" in line and world.no_tree_was_cut()


def test_a_range_too_long_to_trust_is_filed_and_none_reverted(world, monkeypatch):
    """A stuck `green` makes every commit since look like the cause."""
    monkeypatch.setattr(fix_revert, "MOST_REVERTS", 2)
    world.write("b.txt", "b\n", "Add b (#1)")
    world.write("c.txt", "c\n", "Add c (#2)")
    bad = world.write("d.txt", "d\n", "Add d (#3)")
    world.fail(bad)
    [line] = world.tend()
    assert "none reverted" in line
    assert [f.kind for f in world.journal.findings] == ["revert-range-long"]
    assert world.no_tree_was_cut()


def test_a_merge_commit_is_reverted_against_its_first_parent(world):
    _git(world.author, "switch", "-c", "feature")
    world_file = world.author / "feature.txt"
    world_file.write_text("f\n", encoding="utf-8", newline="\n")
    _git(world.author, "add", ".")
    _git(world.author, "commit", "-m", "work")
    _git(world.author, "switch", "main")
    _git(world.author, "merge", "--no-ff", "-m", "Merge pull request #4", "feature")
    _git(world.author, "push", "origin", "main")
    bad = _git(world.author, "rev-parse", "HEAD")
    world.fail(bad)
    [line] = world.tend()
    assert "FAILED" not in line
    assert not (world.tree_of(f"agent/revert-{bad[:7]}") / "feature.txt").exists()


def test_the_steps_of_one_revert_each_read_on_their_own(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    world.fetch()
    failure = world.failure()
    [one] = fix_revert.read_range(world.checkout, failure)
    assert one.sha == bad and not fix_revert.reverted_before(world.checkout, one)
    assert fix_revert.touched(world.checkout, one) == {"a.txt"}
    assert fix_revert.tidied_after(world.checkout, one, "main") == ()
    line = fix_revert.revert_one("roguelike", world.checkout, one, failure, world.journal)
    assert one.branch in line and fix_revert.reverted_before(world.checkout, one)
    tree = world.tree_of(one.branch)
    assert fix_revert.discard(world.checkout, tree, one.branch) == ""
    assert not tree.exists() and not fix_revert.reverted_before(world.checkout, one)
    assert fix_revert.revert_range(world.ctx(), failure, world.journal) == []
    lines = fix_revert.revert_range(world.ctx(hours=3.0), failure, world.journal)
    assert [text.split()[1] for text in lines] == [one.short]


def test_a_tree_git_will_not_remove_is_said_in_a_line_the_pass_files(world, tmp_path):
    left = fix_revert.discard(world.checkout, tmp_path / "not-a-worktree", "agent/nowhere")
    assert "FAILED to discard" in left


def test_red_on_a_gate_that_implicates_nothing_cuts_nothing(world):
    bad = world.write("a.txt", "two\n", "Break a thing (#1)")
    world.fail(bad)
    world.fetch()
    lost = world.failure(signature=(f"Tests {fix_plan.UNSTARTED}",))
    assert fix_revert.tend(world.ctx(hours=3.0), [lost], world.journal) == []


def test_green_sha_is_where_green_stands(world):
    world.fetch()
    assert fix_revert.green_sha(world.checkout) == _git(world.checkout, "rev-parse", "origin/green")


# --- the tidy grace -----------------------------------------------------------------


FORMAT = ("Lint + hook-script tests / Format",)


def test_a_tidy_red_is_marked_once_its_grace_is_up_and_not_before(world):
    """D5: a skip that held forever gave a permanent red no fixer."""
    world.fetch()
    failure = world.failure(signature=FORMAT)
    assert fix_revert.mark_tidy_lapsed(world.ctx(), [failure]) == [failure]
    assert fix_revert.mark_tidy_lapsed(world.ctx(hours=0.5), [failure]) == [failure]
    [marked] = fix_revert.mark_tidy_lapsed(world.ctx(hours=1.0), [failure])
    assert marked.tidy_lapsed


def test_a_tidy_red_on_a_gated_project_or_any_other_red_is_not_marked(world):
    world.fetch()
    formatting = world.failure(signature=FORMAT)
    assert fix_revert.mark_tidy_lapsed(world.ctx(direct=(), hours=2.0), [formatting]) == [
        formatting
    ]
    other = world.failure()
    assert fix_revert.mark_tidy_lapsed(world.ctx(hours=2.0), [other]) == [other]


def test_green_moving_on_starts_the_tidy_clock_again(world):
    """The same formatting red next week is not this one."""
    world.fetch()
    failure = world.failure(signature=FORMAT)
    fix_revert.mark_tidy_lapsed(world.ctx(), [failure])
    moved = world.write("b.txt", "b\n", "Add b (#1)")
    _git(world.author, "push", "origin", f"{moved}:refs/heads/green")
    world.fetch()
    assert fix_revert.mark_tidy_lapsed(world.ctx(hours=2.0), [failure]) == [failure]


def test_a_plan_marks_nothing_and_writes_nothing(world):
    world.fetch()
    failure = world.failure(signature=FORMAT)
    assert fix_revert.mark_tidy_lapsed(world.ctx(fix_cycle.PLAN, hours=2.0), [failure]) == [failure]
    assert not (world.root / "devkit" / red_seen.STATE_DIR).exists()
