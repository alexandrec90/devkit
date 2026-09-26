"""`scripts/ship_intent.py`: the intent file, and what the pass does with one.

Every git, pre-commit and gh call goes through an injected runner or a patched `gh_for`,
so the suite asserts the argv and the state file, never a repository.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_plan
import ship_intent

NOW = _dt.datetime(2026, 9, 19, 9, 0, tzinfo=_dt.UTC)


def intent(tmp_path, subject="Teach the sweep about labels", body="Because.") -> ship_intent.Intent:
    tree = tmp_path / "carameli" / ".claude" / "worktrees" / "labels"
    (tree / "logs").mkdir(parents=True, exist_ok=True)
    (tree / ship_intent.INTENT_FILE).write_text(f"{subject}\n\n{body}\n", encoding="utf-8")
    return ship_intent.Intent("carameli", tree, "agent/labels-0919", subject, body)


class Runner:
    """Answers every argv with 0 and `answers[verb]`, recording what ran and where."""

    def __init__(
        self, answers: dict[str, tuple[int, str, str]] | None = None, porcelain=" M a.py\n"
    ):
        self.answers = answers or {}
        self.porcelain = porcelain
        self.calls: list[tuple[list[str], Path, dict | None]] = []

    def __call__(self, argv, cwd, env=None):
        self.calls.append(([str(a) for a in argv], Path(cwd), env))
        verb = " ".join(str(a) for a in argv[:2])
        if argv[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(argv, 0, self.porcelain, "")
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        code, out, err = self.answers.get(verb, (0, "", ""))
        return subprocess.CompletedProcess(argv, code, out, err)

    def verbs(self) -> list[str]:
        return [" ".join(argv[:2]) for argv, _cwd, _env in self.calls]


def gh_ok(*_a):
    return lambda *args: subprocess.CompletedProcess(["gh", *args], 0, "https://x/pull/7", "")


# --- the intent file ----------------------------------------------------------------


def test_the_first_line_is_the_subject_and_the_rest_is_the_body():
    assert ship_intent.parse_intent("# Fix the thing\n\nBecause it\nwas broken.\n") == (
        "Fix the thing",
        "Because it\nwas broken.",
    )
    assert ship_intent.parse_intent("   \n") == ("", "")


def test_the_digest_is_the_words_not_the_file(tmp_path):
    one = intent(tmp_path)
    same = ship_intent.Intent("other", tmp_path, "agent/x", one.subject, one.body)
    assert one.digest == same.digest
    assert one.digest != intent(tmp_path, body="Different.").digest


def test_intents_are_found_across_every_worktree_of_every_checkout(tmp_path, monkeypatch):
    """Through `git worktree list`, so a box, a `--worktree` checkout and the static
    checkout on a task branch are all the same case; the default branch is not."""
    (tmp_path / "carameli").mkdir()
    (tmp_path / "devkit").mkdir()
    ready = intent(tmp_path)
    parked = tmp_path / "carameli"
    (parked / "logs").mkdir()
    (parked / ship_intent.INTENT_FILE).write_text("Never\n", encoding="utf-8")
    listing = (
        f"worktree {parked.as_posix()}\nHEAD 1\nbranch refs/heads/main\n\n"
        f"worktree {ready.tree.as_posix()}\nHEAD 2\nbranch refs/heads/agent/labels-0919\n\n"
        f"worktree {(tmp_path / 'nowhere').as_posix()}\nHEAD 3\nbranch refs/heads/agent/other\n"
    )

    def git_for(project_dir):
        def git(*args):
            if args[:2] == ("worktree", "list"):
                mine = listing if project_dir.name == "carameli" else ""
                return subprocess.CompletedProcess(args, 0, mine, "")
            if args[0] == "symbolic-ref":
                return subprocess.CompletedProcess(args, 0, "refs/remotes/origin/main\n", "")
            return subprocess.CompletedProcess(args, 1, "", "")

        return git

    found = ship_intent.find_intents(tmp_path, ["carameli", "devkit", "ghost"], git_for)
    assert [(i.project, i.branch, i.subject, bool(i.blocked)) for i in found] == [
        ("carameli", "main", "Never", True),
        ("carameli", "agent/labels-0919", "Teach the sweep about labels", False),
    ]
    assert {i.base for i in found} == {"main"}, "the base the PR opens against rides along"
    assert found[0].blocked == ship_intent.ship.is_shippable("main", "main")[1], (
        "an intent on the default branch is reported with the shippable rule's own "
        "reason, never silently passed over: the first ledger sweep left a carameli fix "
        "unstaged on master that way"
    )


def test_a_checkout_git_cannot_list_is_passed_over(tmp_path):
    (tmp_path / "carameli").mkdir()
    assert (
        ship_intent.find_intents(
            tmp_path, ["carameli"], lambda _d: lambda *a: subprocess.CompletedProcess(a, 1, "", "")
        )
        == []
    )


# --- shipping one --------------------------------------------------------------------


def test_the_pass_runs_fixers_commits_with_the_message_pushes_past_the_gate_and_opens_the_pr(
    tmp_path, monkeypatch
):
    one = intent(tmp_path)
    run = Runner()
    plans = []

    def ensure_pr(gh, plan):
        plans.append(plan)
        return "https://x/pull/7", True, ""

    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", ensure_pr)
    out = ship_intent.ship_one(one, r"C:\py\python.exe", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.SHIPPED and out.url == "https://x/pull/7"
    assert run.verbs() == [
        "git status",
        r"C:\py\python.exe scripts/ship.py",
        "git add",
        "git commit",
        "git push",
        "git rev-parse",
    ]
    fix, add, commit, push = run.calls[1:5]
    assert fix[0][1:] == ["scripts/ship.py", "--fix"] and fix[1] == one.tree
    assert add[0] == ["git", "add", "-A"]
    assert commit[0] == ["git", "commit", "-F", str(ship_intent.INTENT_FILE)]
    assert push[0] == ["git", "push", "-u", "origin", "agent/labels-0919"]
    assert push[2]["SKIP"] == ship_intent.SKIP_PUSH_GATE
    plan = plans[0]
    assert (plan.pr_title, plan.pr_body, plan.pr_head, plan.pr_base) == (
        one.subject,
        one.body,
        one.branch,
        "main",
    )
    # A feature session's PR is a prompt-driven change: the vendored auto-merge workflow
    # lands any labelled PR once the gate passes, so the label would make CI the only
    # reviewer.
    assert plan.pr_labels == ()
    state = ship_intent.read_state(one.tree)
    assert state["stage"] == ship_intent.SHIPPED
    assert state["intent"] == one.digest and state["sha"] == "abc123"


def shipped_labels(tmp_path, monkeypatch, *stamps: bool | None) -> tuple[str, ...]:
    """The labels `ship_one` asks for after the tree was stamped once per `stamps`."""
    one = intent(tmp_path)
    for owns in stamps:
        ship_intent.fix_reports.stamp(one.tree, "k", "what", NOW, owns_branch=owns)
    plans = []
    monkeypatch.setattr(
        ship_intent.sweep, "ensure_pr", lambda gh, plan: plans.append(plan) or ("u", True, "")
    )
    assert ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW).stage == "shipped"
    return plans[0].pr_labels


def test_a_pr_from_a_branch_the_pass_cut_for_a_fixer_is_labelled_automerge(tmp_path, monkeypatch):
    """The pass decided on that work itself, so its green gate is the whole review."""
    assert shipped_labels(tmp_path, monkeypatch, True) == (ship_intent.sweep.AUTOMERGE_LABEL,)


def test_a_fixer_sent_back_at_its_own_branch_keeps_the_label(tmp_path, monkeypatch):
    """A fixer's PR that went red, or whose commit was refused, is re-stamped with no
    say about the branch; it is still the pass's own."""
    assert shipped_labels(tmp_path, monkeypatch, True, None) == (ship_intent.sweep.AUTOMERGE_LABEL,)


def test_a_fixer_sent_at_a_feature_sessions_branch_leaves_it_unlabelled(tmp_path, monkeypatch):
    """Fixing the gate on a feature PR does not make the feature routine: the person
    who asked for it still merges it."""
    assert shipped_labels(tmp_path, monkeypatch, None) == ()
    assert shipped_labels(tmp_path, monkeypatch, False, None) == ()


def test_a_stamp_from_before_the_field_existed_reads_as_a_feature_branch(tmp_path):
    (tmp_path / ship_intent.fix_reports.STAMP_FILE).parent.mkdir(parents=True)
    (tmp_path / ship_intent.fix_reports.STAMP_FILE).write_text('{"key": "k"}', encoding="utf-8")
    assert ship_intent.pr_labels(tmp_path) == ()


def test_the_commit_half_names_the_step_that_refused(tmp_path):
    one = intent(tmp_path)
    assert ship_intent.commit_intent(one, "py", Runner()) == ("", "")
    step, output = ship_intent.commit_intent(
        one, "py", Runner({"git add": (1, "", "index locked")})
    )
    assert (step, output) == ("add", "index locked")


def test_a_refused_commit_stage_is_recorded_with_its_output_and_nothing_is_pushed(tmp_path):
    one = intent(tmp_path)
    run = Runner({r"C:\py\python.exe scripts/ship.py": (1, "", "Executable `ruff` not found")})
    out = ship_intent.ship_one(one, r"C:\py\python.exe", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.REFUSED
    assert "git push" not in run.verbs()
    state = ship_intent.read_state(one.tree)
    assert state["stage"] == ship_intent.REFUSED and state["step"] == "fixers"
    assert "not found" in state["output"]


def test_a_commit_the_hooks_refuse_is_recorded_at_the_commit_step(tmp_path):
    one = intent(tmp_path)
    run = Runner({"git commit": (1, "detect secrets.....Failed", "")})
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.REFUSED
    assert ship_intent.read_state(one.tree)["step"] == "commit"


def test_a_failed_push_is_a_failure_not_a_refusal_and_leaves_no_refused_state(tmp_path):
    one = intent(tmp_path)
    run = Runner({"git push": (1, "", "could not resolve host")})
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.FAILED and "push" in out.detail
    assert ship_intent.read_state(one.tree) == {}


def test_a_pr_that_could_not_be_opened_is_a_failure_to_retry(tmp_path, monkeypatch):
    one = intent(tmp_path)
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("", False, "no auth"))
    out = ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW)
    assert out.stage == ship_intent.FAILED and "no auth" in out.detail


def test_an_intent_already_shipped_with_a_clean_tree_is_not_shipped_twice(tmp_path, monkeypatch):
    one = intent(tmp_path)
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    assert (
        ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW).stage == ship_intent.SHIPPED
    )
    again = Runner(porcelain="")
    assert ship_intent.ship_one(one, "py", "main", again, gh_ok, NOW).stage == ship_intent.SKIPPED
    assert again.verbs() == ["git status"]


def test_a_rewritten_intent_on_a_clean_shipped_tree_is_nothing_to_ship(tmp_path, monkeypatch):
    """New words with no changed file have no commit to carry them. The first pass after
    #375 merged found exactly this -- the message edited after the ship, the tree clean --
    and would have pushed nothing and asked for a second PR on a retired branch."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW)
    two = intent(tmp_path, body="Rewritten.")
    run = Runner(porcelain="")
    assert ship_intent.ship_one(two, "py", "main", run, gh_ok, NOW).stage == ship_intent.SKIPPED
    assert run.verbs() == ["git status"]


def test_a_clean_tree_whose_last_ship_failed_still_pushes(tmp_path, monkeypatch):
    """The commits are already there from the attempt whose push failed: nothing to
    commit, everything to push."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    ship_intent.write_state(one.tree, {"stage": ship_intent.FAILED, "intent": one.digest})
    run = Runner(porcelain="")
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.SHIPPED
    assert "git commit" not in run.verbs() and "git push" in run.verbs()


def test_already_shipped_needs_a_clean_tree_and_not_the_same_words(tmp_path):
    """A message edited after the ship with nothing else changed has no commit to carry
    it: the first pass after a merge would otherwise push nothing and ask for a second
    PR on a retired branch."""
    one = intent(tmp_path)
    shipped = {"stage": ship_intent.SHIPPED, "intent": one.digest}
    assert ship_intent.already_shipped(one, shipped, "")
    assert not ship_intent.already_shipped(one, shipped, " M a.py\n")
    assert ship_intent.already_shipped(one, {"stage": ship_intent.SHIPPED, "intent": "x"}, "")
    assert not ship_intent.already_shipped(one, {}, "")
    assert not ship_intent.already_shipped(one, {"stage": ship_intent.REFUSED}, "")


def test_the_state_file_round_trips_and_a_corrupt_one_reads_as_empty(tmp_path):
    ship_intent.write_state(tmp_path, {"stage": "shipped"})
    assert ship_intent.read_state(tmp_path) == {"stage": "shipped"}
    (tmp_path / ship_intent.STATE_FILE).write_text("{nope", encoding="utf-8")
    assert ship_intent.read_state(tmp_path) == {}


def test_the_one_spawn_is_window_less():
    """The pass is a scheduled job; `tests/test_scheduled_jobs.py` reads the source, and
    this reads the call."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    original = ship_intent.subprocess.run
    ship_intent.subprocess.run = fake_run
    try:
        ship_intent.run_quiet(["git", "status"], Path("."))
    finally:
        ship_intent.subprocess.run = original
    assert seen["creationflags"] == ship_intent.sweep.NO_WINDOW
    assert seen["capture_output"] is True


def test_run_quiet_is_called_the_way_the_dispatcher_calls_subprocess_run(tmp_path):
    """`fix-prs.py` and `agent-box.py` call their runner as they would `subprocess.run`
    -- `check=False` and no `cwd`, or `capture_output` and `text` of their own. The
    first real dispatch died on a `TypeError` here because every test on either side
    had stubbed the other; this one runs the real spawn in each of those shapes."""
    hello = [sys.executable, "-c", "import os; print(os.getcwd())"]
    bare = ship_intent.run_quiet(hello, check=False)
    assert bare.returncode == 0 and bare.stdout.strip()
    captured = ship_intent.run_quiet(hello, capture_output=True, text=True, check=False)
    assert captured.returncode == 0
    there = ship_intent.run_quiet(hello, cwd=str(tmp_path), check=True)
    assert Path(there.stdout.strip()).resolve() == tmp_path.resolve()
    failing = ship_intent.run_quiet([sys.executable, "-c", "raise SystemExit(3)"], check=True)
    assert failing.returncode == 3, "a runner that raised would take the pass down"


# --- a refusal as a failure ----------------------------------------------------------


def test_a_refusal_becomes_a_commit_failure_with_its_output_placed_in_the_worktree(tmp_path):
    one = intent(tmp_path)
    ship_intent.write_state(
        one.tree,
        {"stage": ship_intent.REFUSED, "step": "commit", "output": "FAILED tests/t.py::a - x\n"},
    )
    failure = ship_intent.refusal_failure(ship_intent.Outcome(one, ship_intent.REFUSED), "main")
    assert failure.kind == fix_plan.COMMIT
    assert (failure.project, failure.head, failure.base) == ("carameli", one.branch, "main")
    assert failure.signature == ("tests/t.py::a",)
    assert failure.sha == one.digest
    placed = Path(failure.evidence) / "pre-commit.log"
    assert placed == one.tree / fix_plan.EVIDENCE_DIR / "pre-commit.log"
    assert placed.read_text(encoding="utf-8").startswith("FAILED")


def test_a_refusal_with_no_test_ids_is_signed_by_its_step(tmp_path):
    one = intent(tmp_path)
    ship_intent.write_state(
        one.tree, {"stage": ship_intent.REFUSED, "step": "fixers", "output": "Executable not found"}
    )
    failure = ship_intent.refusal_failure(ship_intent.Outcome(one, ship_intent.REFUSED), "main")
    assert failure.signature == ("fixers refused",)


@pytest.mark.parametrize("branch,shippable", [("agent/x-0919", True), ("main", False)])
def test_only_a_task_branch_is_shippable(branch, shippable):
    assert ship_intent.ship.is_shippable(branch, "main")[0] is shippable


# --- the intent is consumed, so "no intent" means "hands off" everywhere ---------------


def test_a_shipped_intent_is_set_aside_so_a_fixer_in_the_same_tree_is_never_shipped_under(
    tmp_path, monkeypatch
):
    """A fixer sent at a PR reuses the worktree that still held the shipped intent;
    its first uncommitted edit made the tree dirty, and the next pass committed the
    half-done work under the old message and pushed it to the PR."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    assert (
        ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW).stage == ship_intent.SHIPPED
    )
    assert not (one.tree / ship_intent.INTENT_FILE).exists()
    kept = (one.tree / ship_intent.SHIPPED_FILE).read_text(encoding="utf-8")
    assert kept.startswith("Teach the sweep about labels")
    # The fixer is now editing: a dirty tree with no intent is a session still working.
    dirty = Runner(porcelain=" M a.py\n")
    listing = f"worktree {one.tree.as_posix()}\nHEAD 2\nbranch refs/heads/{one.branch}\n"
    (tmp_path / "carameli").mkdir(exist_ok=True)

    def git_for(_project_dir):
        return lambda *args: subprocess.CompletedProcess(
            args, 0, listing if args[:2] == ("worktree", "list") else "origin/main\n", ""
        )

    assert ship_intent.find_intents(tmp_path, ["carameli"], git_for) == []
    assert dirty.calls == []


def test_a_failed_push_keeps_the_intent_for_the_next_pass(tmp_path, monkeypatch):
    one = intent(tmp_path)
    run = Runner({"git push": (1, "", "rejected")})
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.FAILED
    assert (one.tree / ship_intent.INTENT_FILE).exists()


def test_set_aside_moves_the_intent_and_says_whether_there_was_one(tmp_path):
    one = intent(tmp_path)
    assert (
        ship_intent.set_aside(one.tree, ship_intent.REFUSED_FILE)
        == one.tree / ship_intent.REFUSED_FILE
    )
    assert not (one.tree / ship_intent.INTENT_FILE).exists()
    assert (one.tree / ship_intent.REFUSED_FILE).read_text(encoding="utf-8").startswith("Teach")
    assert ship_intent.set_aside(one.tree, ship_intent.REFUSED_FILE) is None
    two = intent(tmp_path, subject="Again")
    assert ship_intent.set_aside(two.tree, ship_intent.REFUSED_FILE) is not None, (
        "a second set-aside replaces the first: the newest refused message is the one worth keeping"
    )
    assert (two.tree / ship_intent.REFUSED_FILE).read_text(encoding="utf-8").startswith("Again")


def test_a_refusal_names_the_worktree_the_fixer_opens_in(tmp_path):
    one = intent(tmp_path)
    ship_intent.write_state(
        one.tree, {"stage": ship_intent.REFUSED, "step": "commit", "output": ""}
    )
    failure = ship_intent.refusal_failure(ship_intent.Outcome(one, ship_intent.REFUSED), "main")
    assert Path(failure.tree) == one.tree
