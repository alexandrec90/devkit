"""`scripts/ship_intent.py`: the intent file, and what the pass does with one.

Every git, pre-commit and gh call goes through an injected runner or a patched `gh_for`,
so the suite asserts the argv and the state file, never a repository.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys
from dataclasses import replace
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


def listing_git(tree: Path, branch: str):
    listing = f"worktree {tree.as_posix()}\nHEAD 2\nbranch refs/heads/{branch}\n"

    def git_for(_project_dir):
        return lambda *args: subprocess.CompletedProcess(
            args,
            0,
            listing if args[:2] == ("worktree", "list") else "refs/remotes/origin/main\n",
            "",
        )

    return git_for


def test_an_intent_that_already_shipped_is_not_reported_from_a_hand_named_branch(tmp_path):
    """The reported case: PR #386 shipped from a tree before `set_aside` existed, so its
    intent stayed; a session then cut `flag-wired-agent-hooks` in that tree, and every
    pass reported the merged change as NOT shipped, telling a person to move it."""
    (tmp_path / "carameli").mkdir()
    one = intent(tmp_path)
    ship_intent.write_state(
        one.tree, {"stage": ship_intent.SHIPPED, "intent": one.digest, "url": "u"}
    )
    git_for = listing_git(one.tree, "flag-wired-agent-hooks")
    assert ship_intent.find_intents(tmp_path, ["carameli"], git_for) == []


def test_an_edited_or_unshipped_intent_is_still_found(tmp_path):
    """Spent means these words shipped. New words in the same tree are a new intent, and
    a refusal on record is a failure still to dispatch."""
    (tmp_path / "carameli").mkdir()
    one = intent(tmp_path, body="Edited after the ship.")
    git_for = listing_git(one.tree, "flag-wired-agent-hooks")
    for state in (
        {"stage": ship_intent.SHIPPED, "intent": "a88840a33784"},
        {"stage": ship_intent.REFUSED, "intent": one.digest},
    ):
        ship_intent.write_state(one.tree, state)
        found = ship_intent.find_intents(tmp_path, ["carameli"], git_for)
        assert [i.subject for i in found] == [one.subject]
        assert found[0].adopt and not found[0].blocked, "an agent tree's hand name is adopted"


def test_spent_needs_a_shipped_stage_and_the_same_words(tmp_path):
    """Unlike `already_shipped`, the words decide: an edited message is a new intent, and
    a dirty tree does not revive words that already shipped."""
    one = intent(tmp_path)
    assert ship_intent.spent(one, {"stage": ship_intent.SHIPPED, "intent": one.digest})
    assert not ship_intent.spent(one, {"stage": ship_intent.SHIPPED, "intent": "x"})
    assert not ship_intent.spent(one, {"stage": ship_intent.REFUSED, "intent": one.digest})
    assert not ship_intent.spent(one, {"stage": ship_intent.FAILED, "intent": one.digest})
    assert not ship_intent.spent(one, {})


def test_a_checkout_git_cannot_list_is_passed_over(tmp_path):
    (tmp_path / "carameli").mkdir()
    assert (
        ship_intent.find_intents(
            tmp_path, ["carameli"], lambda _d: lambda *a: subprocess.CompletedProcess(a, 1, "", "")
        )
        == []
    )


def _one_tree_on(tmp_path, branch):
    """A carameli worktree on `branch` carrying an intent, and the `git_for` that lists it."""
    tree = tmp_path / "carameli" / ".claude" / "worktrees" / "hand"
    (tree / "logs").mkdir(parents=True)
    (tree / ship_intent.INTENT_FILE).write_text("Resolve it\n", encoding="utf-8")
    listing = f"worktree {tree.as_posix()}\nHEAD 1\nbranch refs/heads/{branch}\n"

    def git_for(_project_dir):
        def git(*args):
            if args[:2] == ("worktree", "list"):
                return subprocess.CompletedProcess(args, 0, listing, "")
            return subprocess.CompletedProcess(args, 0, "refs/remotes/origin/main\n", "")

        return git

    return git_for


def _gh_listing(code, out, asked):
    def gh_for(_project_dir):
        def gh(*args):
            asked.append(args)
            return subprocess.CompletedProcess(args, code, out, "")

        return gh

    return gh_for


def test_a_hand_named_branch_that_heads_an_open_pr_is_shippable(tmp_path):
    """devkit #390: opened by hand from `flag-wired-agent-hooks`, so a resolver sent at
    it worked on that branch and its intent was refused on every pass -- the PR could
    only stay conflicted. The PR already exists; pushing to it is the fixer's whole job."""
    git_for = _one_tree_on(tmp_path, "flag-wired-agent-hooks")
    asked: list = []
    found = ship_intent.find_intents(
        tmp_path, ["carameli"], git_for, _gh_listing(0, '[{"number": 390}]', asked)
    )
    assert [(i.branch, i.blocked) for i in found] == [("flag-wired-agent-hooks", "")]
    assert asked == [
        ("pr", "list", "--head", "flag-wired-agent-hooks", "--state", "open", "--json", "number")
    ]


@pytest.mark.parametrize(
    ("code", "out"), [(0, "[]"), (1, ""), (0, "not json")], ids=["no-pr", "gh-failed", "garbage"]
)
def test_a_hand_named_branch_with_no_open_pr_in_an_agent_tree_is_adopted(tmp_path, code, out):
    """32f97dae: a session in social-scraper's spent fixer tree cut
    `reddit-import-memory-cap`, and every pass refused its intent. The tree is disposable
    by where it sits, so the pass renames the branch rather than stranding the work."""
    git_for = _one_tree_on(tmp_path, "flag-wired-agent-hooks")
    found = ship_intent.find_intents(tmp_path, ["carameli"], git_for, _gh_listing(code, out, []))
    assert [(i.blocked, i.adopt) for i in found] == [("", True)]


def test_a_hand_named_branch_in_the_static_checkout_stays_blocked(tmp_path):
    """The checkout itself may sit on a long-lived home branch, which is what the
    namespace rule is there to keep a PR off."""
    checkout = tmp_path / "carameli"
    (checkout / "logs").mkdir(parents=True)
    (checkout / ship_intent.INTENT_FILE).write_text("Resolve it\n", encoding="utf-8")
    git_for = listing_git(checkout, "develop")
    found = ship_intent.find_intents(tmp_path, ["carameli"], git_for, _gh_listing(0, "[]", []))
    assert [(i.blocked, i.adopt) for i in found] == [
        (ship_intent.ship.is_shippable("develop", "main")[1], False)
    ]


@pytest.mark.parametrize(
    ("where", "branch", "adopted"),
    [
        (Path("p/.claude/worktrees/t"), "memory-cap", True),
        (Path(".worktrees/p--t"), "memory-cap", True),
        (Path("p"), "memory-cap", False),
        (Path("p/.claude/worktrees/t"), "main", False),
        (Path("p/.claude/worktrees/t"), "", False),
        (Path("p/.claude/worktrees/t"), "feature/x", False),
    ],
    ids=["claude-tree", "box", "static", "default", "detached", "namespaced"],
)
def test_hand_named_is_an_unnamespaced_task_branch_in_a_disposable_tree(
    tmp_path, where, branch, adopted
):
    assert ship_intent.hand_named(tmp_path / where, branch, "main") is adopted


@pytest.mark.parametrize(
    ("code", "out", "open_pr"),
    [
        (0, '[{"number": 390}]', True),
        (0, "[]", False),
        (0, "", False),
        (0, "{}", False),
        (1, '[{"number": 390}]', False),
        (0, "not json", False),
    ],
    ids=["open", "none", "empty", "not-a-list", "gh-failed", "garbage"],
)
def test_has_open_pr_is_false_whenever_gh_cannot_say_yes(code, out, open_pr):
    asked: list = []
    gh = _gh_listing(code, out, asked)(None)
    assert ship_intent.has_open_pr(gh, "flag-wired-agent-hooks") is open_pr
    assert asked == [
        ("pr", "list", "--head", "flag-wired-agent-hooks", "--state", "open", "--json", "number")
    ]


def test_the_default_branch_stays_blocked_without_asking_about_prs(tmp_path):
    git_for = _one_tree_on(tmp_path, "main")
    asked: list = []
    found = ship_intent.find_intents(
        tmp_path, ["carameli"], git_for, _gh_listing(0, '[{"number": 1}]', asked)
    )
    assert found[0].blocked and asked == []


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
        "git rev-parse",  # `lands_nothing` asks after `MERGE_HEAD`,
        "git merge-base",  # then the fork, answered "" here: unknown, so it ships
        "git add",
        r"C:\py\python.exe scripts/ship.py",
        "git add",
        "git commit",
        "git fetch",
        "git merge-base",
        "git push",
        "git rev-parse",
    ]
    fix, add, commit = run.calls[4:7]
    push = run.calls[9]
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
    # A person's tree carries no fix-pass origin mark, so its PR waits for them: the
    # vendored auto-merge workflow lands any labelled PR once the gate passes.
    assert plan.pr_labels == ()
    state = ship_intent.read_state(one.tree)
    assert state["stage"] == ship_intent.SHIPPED
    assert state["intent"] == one.digest and state["sha"] == "abc123"


def capture_plans(monkeypatch) -> list:
    """Every PR plan `ship_one` hands `sweep.ensure_pr`, in order."""
    plans = []

    def ensure_pr(gh, plan):
        plans.append(plan)
        return "u", True, ""

    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", ensure_pr)
    return plans


def shipped_labels(tmp_path, monkeypatch, marked: bool, stamps: int) -> tuple[str, ...]:
    """The labels `ship_one` asks for from a tree the pass did or did not cut, after
    `stamps` dispatches were recorded in it."""
    one = intent(tmp_path)
    if marked:
        (one.tree / ship_intent.fix_reports.ORIGIN_FILE).write_text("fix-pass\n", encoding="utf-8")
    for n in range(stamps):
        ship_intent.fix_reports.stamp(one.tree, f"k{n}", "what", NOW)
    plans = capture_plans(monkeypatch)
    assert ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW).stage == "shipped"
    return plans[0].pr_labels


def test_a_pr_from_a_tree_sent_at_an_issue_closes_that_issue_and_no_other(tmp_path, monkeypatch):
    """A nightly's fixer: the issue it was sent at closes when its fix merges, even if the
    workflow is renamed before it runs green again. A tree not sent at one closes none."""
    one = intent(tmp_path)
    ship_intent.fix_reports.stamp(one.tree, "nightly:carameli:9:t:d:dispatch", "n", NOW)
    ship_intent.fix_reports.note_on_stamp(one.tree, ship_intent.fix_reports.CLOSES, "9")
    plans = capture_plans(monkeypatch)
    assert ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW).stage == "shipped"
    assert plans[0].pr_body == "Because.\n\nCloses #9"
    assert ship_intent.pr_body(replace(one, body="Fixed it. Closes #9")) == "Fixed it. Closes #9"
    ship_intent.fix_reports.stamp(one.tree, "pr:carameli:412:abc:d:dispatch", "n", NOW)
    assert ship_intent.pr_body(one) == "Because."


def test_a_pr_from_a_branch_the_pass_cut_for_a_fixer_is_labelled_automerge(tmp_path, monkeypatch):
    """The pass decided on that work itself, so its green gate is the whole review."""
    assert shipped_labels(tmp_path, monkeypatch, True, 1) == (ship_intent.sweep.AUTOMERGE_LABEL,)


def test_a_fixer_sent_back_at_its_own_branch_keeps_the_label(tmp_path, monkeypatch):
    """A fixer's PR that went red, or whose commit was refused, is re-stamped; it is
    still the pass's own."""
    assert shipped_labels(tmp_path, monkeypatch, True, 2) == (ship_intent.sweep.AUTOMERGE_LABEL,)


def test_a_fixer_sent_at_a_feature_sessions_branch_leaves_it_unlabelled(tmp_path, monkeypatch):
    """Fixing the gate on a feature PR does not make the feature routine: the person
    who asked for it still merges it, however many dispatches were stamped there."""
    assert shipped_labels(tmp_path / "once", monkeypatch, False, 1) == ()
    assert shipped_labels(tmp_path / "twice", monkeypatch, False, 2) == ()


def test_the_commit_half_names_the_step_that_refused(tmp_path):
    one = intent(tmp_path)
    assert ship_intent.commit_intent(one, "py", Runner()) == ("", "")
    step, output = ship_intent.commit_intent(
        one, "py", Runner({"git add": (1, "", "index locked")})
    )
    assert (step, output) == ("add", "index locked")


LOCKED = (
    "fatal: Unable to create 'C:/src/devkit/.git/worktrees/labels/index.lock': File exists.\n\n"
    "Another git process seems to be running in this repository, or the lock file may be stale\n"
)


class _LockedFor(Runner):
    """git refusing the first `times` calls of `verb` because another process holds a lock."""

    def __init__(self, verb: str, times: int):
        super().__init__()
        self.verb, self.left = verb, times

    def __call__(self, argv, cwd, env=None):
        done = super().__call__(argv, cwd, env)
        if " ".join(str(a) for a in argv[:2]) == self.verb and self.left:
            self.left -= 1
            return subprocess.CompletedProcess(argv, 128, "", LOCKED)
        return done


@pytest.mark.parametrize("verb", ["git add", "git commit"])
def test_a_lock_another_git_process_holds_for_a_moment_is_waited_out(tmp_path, monkeypatch, verb):
    """devkit 0927-17: the pass's `git add` met an `index.lock` something else held for
    an instant, and filed "add refused" over a lock gone before the fixer opened."""
    waited: list[float] = []
    monkeypatch.setattr(ship_intent, "_wait", waited.append)
    run = _LockedFor(verb, 2)
    assert ship_intent.commit_intent(intent(tmp_path), "py", run) == ("", "")
    assert waited == list(ship_intent.LOCK_WAITS[:2])
    assert run.verbs().count(verb) == (4 if verb == "git add" else 3)


def test_a_lock_held_past_every_wait_is_still_refused_with_gits_words(tmp_path, monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(ship_intent, "_wait", waited.append)
    run = _LockedFor("git add", 99)
    assert ship_intent.commit_intent(intent(tmp_path), "py", run) == ("add", LOCKED)
    assert waited == list(ship_intent.LOCK_WAITS)
    assert run.verbs() == ["git add"] * (len(ship_intent.LOCK_WAITS) + 1)


def test_run_git_answers_at_once_when_the_first_try_goes_through(tmp_path, monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(ship_intent, "_wait", waited.append)
    run = Runner()
    assert ship_intent.run_git(["git", "add", "-A"], tmp_path, run).returncode == 0
    assert waited == [] and run.calls == [(["git", "add", "-A"], tmp_path, None)]


def test_a_refusal_that_is_not_a_held_lock_is_not_retried(tmp_path, monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(ship_intent, "_wait", waited.append)
    run = Runner({"git commit": (1, "detect secrets.....Failed", "")})
    assert ship_intent.commit_intent(intent(tmp_path), "py", run)[0] == "commit"
    assert waited == [] and run.verbs().count("git commit") == 1


class _StagedOnlyScanner(Runner):
    """detect-secrets' commit hook as carameli #395 met it: it refuses to scan while the
    baseline has unstaged changes, and a scan that moves a flagged line rewrites the
    baseline -- leaving it unstaged again."""

    def __init__(self, baseline_stale: bool):
        super().__init__()
        self.baseline_staged = False
        self.baseline_stale = baseline_stale

    def __call__(self, argv, cwd, env=None):
        done = super().__call__(argv, cwd, env)
        if argv[:3] == ["git", "add", "-A"]:
            self.baseline_staged = True
        elif argv[1:] == ["scripts/ship.py", "--fix"]:
            # ship.py's own two passes, over whatever the index holds when it starts.
            for _pass in (1, 2):
                if not self.baseline_staged:
                    continue
                if self.baseline_stale:
                    self.baseline_stale, self.baseline_staged = False, False
                    continue
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "Your baseline file is unstaged.")
        return done


def test_the_fixers_run_over_a_staged_tree_so_a_hook_that_needs_its_baseline_staged_can_pass(
    tmp_path,
):
    """carameli #395: `ship.py --fix` over an unstaged tree failed both passes on
    detect-secrets' "baseline is unstaged", which no code change could answer."""
    one = intent(tmp_path)
    run = _StagedOnlyScanner(baseline_stale=False)
    assert ship_intent.commit_intent(one, "py", run) == ("", "")
    assert run.verbs() == ["git add", "py scripts/ship.py", "git add", "git commit"]

    # A scan that rewrites the baseline on the first pass fails ship.py's second; one
    # restage and one more run is what a person committing by hand would do.
    stale = _StagedOnlyScanner(baseline_stale=True)
    assert ship_intent.commit_intent(one, "py", stale) == ("", "")
    assert stale.verbs() == [
        "git add",
        "py scripts/ship.py",
        "git add",
        "py scripts/ship.py",
        "git add",
        "git commit",
    ]


def test_a_finding_that_survives_the_restaged_retry_is_still_a_fixers_refusal(tmp_path):
    one = intent(tmp_path)
    run = Runner({"py scripts/ship.py": (1, "", "a real finding")})
    step, output = ship_intent.commit_intent(one, "py", run)
    assert (step, output) == ("fixers", "a real finding")
    assert run.verbs() == ["git add", "py scripts/ship.py", "git add", "py scripts/ship.py"]


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


class _RetiredBranch(Runner):
    """The branch policy refusing every commit on a name whose PR merged, and any names
    that already exist locally or on origin."""

    def __init__(
        self, retired: set[str], taken: set[str] | None = None, branch: str = "agent/labels-0919"
    ):
        super().__init__()
        self.retired, self.taken, self.branch = retired, set(taken or ()), branch

    def _matching(self, pattern: str) -> list[str]:
        return sorted(name for name in self.taken if name.startswith(pattern.rstrip("*")))

    def __call__(self, argv, cwd, env=None):
        argv = [str(a) for a in argv]
        if argv[:2] == ["git", "for-each-ref"]:
            self.calls.append((argv, Path(cwd), env))
            names = self._matching(argv[-1].removeprefix("refs/heads/"))
            return subprocess.CompletedProcess(argv, 0, "".join(f"{n}\n" for n in names), "")
        if argv[:2] == ["git", "ls-remote"]:
            self.calls.append((argv, Path(cwd), env))
            names = self._matching(argv[-1])
            return subprocess.CompletedProcess(
                argv, 0, "".join(f"sha\trefs/heads/{n}\n" for n in names), ""
            )
        if argv[:3] == ["git", "symbolic-ref", "HEAD"]:
            self.branch = argv[3].removeprefix("refs/heads/")
        if argv[:2] == ["git", "commit"] and self.branch in self.retired:
            self.calls.append((argv, Path(cwd), env))
            why = f"[devkit branch policy] commit blocked: branch '{self.branch}' is permanently retired because its PR merged (https://x/pull/426)."
            return subprocess.CompletedProcess(argv, 1, why, "")
        return super().__call__(argv, cwd, env)


def test_an_intent_on_a_retired_branch_is_carried_to_the_next_free_name(tmp_path):
    """A ledger sweep's second intent landed after the pass had shipped its first and the
    PR merged: the policy refused every commit on the retired name, and the fix -- plus
    the ledger group it was resolved against -- sat stranded in the tree (c45826ad). The
    policy's own remedy is a fresh name, which the ship step now makes itself."""
    one = intent(tmp_path)
    run = _RetiredBranch(retired={"agent/labels-0919"}, taken={"agent/labels-0919-2"})
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.SHIPPED
    argv = [argv for argv, _c, _e in run.calls]
    assert ["git", "branch", "agent/labels-0919-3"] in argv
    assert ["git", "symbolic-ref", "HEAD", "refs/heads/agent/labels-0919-3"] in argv
    assert ["git", "push", "-u", "origin", "agent/labels-0919-3"] in argv


def _resolving(tmp_path, resolution: str):
    """carameli's tree in f95dffbc, as a real repository: `feat` bumped `f`, the trunk
    bumped it another way (`m1`), and a resolver is mid-merge of `m1` with `f` set to
    `resolution`. Since then the trunk landed `f` = "d" and an unrelated `g`, and
    `origin/main` says so. Returns the repository and its `git`."""
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    def commit(message, **files):
        for name, text in files.items():
            (repo / name).write_text(f"{text}\n", encoding="utf-8")
        git("add", *files)
        git("commit", "-qm", message)

    git("init", "-q", "-b", "main")
    # An author, no signing, and no hooks: the machine's `core.hooksPath` is global.
    for key, value in (
        ("user.email", "a@b"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(tmp_path / "no-hooks")),
    ):
        git("config", key, value)
    commit("a", f="a", g="1")
    git("switch", "-qc", "feat")
    commit("b", f="b")
    git("switch", "-q", "main")
    commit("c", f="c")
    git("tag", "m1")
    commit("the bump, landed", f="d")
    commit("a later merge", g="2")
    git("update-ref", "refs/remotes/origin/main", "main")
    git("switch", "-q", "feat")
    assert git("merge", "m1").returncode != 0, "the conflict a resolver is sent at"
    (repo / "f").write_text(f"{resolution}\n", encoding="utf-8")
    git("add", "f")
    return repo, git


def test_the_carry_moves_head_without_switching_so_a_merge_in_progress_survives(tmp_path):
    """f95dffbc: carameli's resolver was mid-merge on a Dependabot branch when its PR
    merged. `git switch -c` refuses while merging, so the carry failed and the commit
    stayed refused on the retired name; `git checkout -b` would drop `MERGE_HEAD`. A
    real repository, because the property is git's, not the argv's."""
    repo, git = _resolving(tmp_path, "c")
    one = ship_intent.Intent("r", repo, "feat", "S", "")
    moved = ship_intent._carry_to_free_branch(one, "feat", ship_intent.run_quiet, set())
    assert isinstance(moved, ship_intent.Intent) and moved.branch == "feat-2"
    assert git("branch", "--show-current").stdout.strip() == "feat-2"
    assert git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0, "still merging"
    assert git("diff", "--cached", "--name-only").stdout.split() == ["f"], "index untouched"


def test_a_carry_whose_head_will_not_move_is_no_carry_and_says_why(tmp_path):
    one = intent(tmp_path)
    run = Runner({"git symbolic-ref": (128, "", "fatal: no")})
    said = ship_intent._carry_to_free_branch(one, "agent/labels-0919", run, set())
    assert said == "`git symbolic-ref HEAD refs/heads/agent/labels-0919-2`: fatal: no"
    taken = Runner({"git branch": (128, "", "fatal: already exists")})
    said = ship_intent._carry_to_free_branch(one, "agent/labels-0919", taken, set())
    assert said == "`git branch agent/labels-0919-2`: fatal: already exists"
    assert "git symbolic-ref" not in taken.verbs()


def test_a_failed_carry_is_written_into_the_recorded_refusal(tmp_path):
    """f95dffbc's stored refusal named only the retired branch, so it read as a carry the
    pass had never tried; the failed `git switch -c` that stopped it was nowhere."""
    one = intent(tmp_path)

    class Stuck(_RetiredBranch):
        def __call__(self, argv, cwd, env=None):
            if [str(a) for a in argv[:2]] == ["git", "branch"]:
                self.calls.append(([str(a) for a in argv], Path(cwd), env))
                return subprocess.CompletedProcess(argv, 128, "", "fatal: cannot carry\n")
            return super().__call__(argv, cwd, env)

    out = ship_intent.ship_one(one, "py", "main", Stuck({"agent/labels-0919"}), gh_ok, NOW)
    assert out.stage == ship_intent.REFUSED
    output = ship_intent.read_state(one.tree)["output"]
    assert "the carry to a fresh name failed: `git branch agent/labels-0919-2`" in output
    assert ship_intent.RETIRED_MARK in ship_intent.refusal_line(output), "same signature"


@pytest.mark.parametrize(
    ("taken", "name"),
    [
        (set(), "agent/memory-cap"),
        ({"agent/memory-cap", "agent/memory-cap-x"}, "agent/memory-cap-2"),
    ],
    ids=["free", "taken"],
)
def test_an_adopted_intent_is_renamed_into_the_namespace_before_it_commits(tmp_path, taken, name):
    """32f97dae: the intent went out on no pass. Renamed, it commits, pushes and opens
    its PR under the namespaced name, and a name already used counts on from it."""
    one = replace(intent(tmp_path), branch="memory-cap", adopt=True)
    run = _RetiredBranch(retired=set(), taken=taken, branch="memory-cap")
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.SHIPPED
    assert (out.intent.branch, out.intent.adopt) == (name, False)
    argv = [argv for argv, _c, _e in run.calls]
    rename = argv.index(["git", "branch", "-m", name])
    assert rename < argv.index(next(a for a in argv if a[:2] == ["git", "commit"]))
    assert ["git", "push", "-u", "origin", name] in argv


def test_a_rename_git_refuses_is_a_failure_to_retry(tmp_path):
    one = replace(intent(tmp_path), branch="memory-cap", adopt=True)
    run = Runner({"git branch": (128, "", "fatal: cannot rename\n")})
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.FAILED
    assert out.detail == "rename: `git branch -m agent/memory-cap`: fatal: cannot rename"
    assert "git commit" not in run.verbs() and "git push" not in run.verbs()
    assert ship_intent.read_state(one.tree)["stage"] == ship_intent.FAILED


def test_the_rename_keeps_a_merge_in_progress(tmp_path):
    """A real repository, because the property is git's: a tree mid-merge keeps its
    `MERGE_HEAD` and its index through `git branch -m`."""
    repo, git = _resolving(tmp_path, "c")
    one = ship_intent.Intent("r", repo, "feat", "S", "", adopt=True)
    moved = ship_intent.adopt_name(one, ship_intent.run_quiet)
    assert isinstance(moved, ship_intent.Intent) and moved.branch == "agent/feat"
    assert git("branch", "--show-current").stdout.strip() == "agent/feat"
    assert git("rev-parse", "-q", "--verify", "refs/heads/feat").returncode != 0, "no stray ref"
    assert git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0, "still merging"
    assert git("diff", "--cached", "--name-only").stdout.split() == ["f"], "index untouched"


def test_ever_precedes_every_resolution_stamp():
    """`harness_triage.carried` compares it with aware stamps, so it must be aware too."""
    ever = _dt.datetime.fromisoformat(ship_intent.EVER)
    assert ever.tzinfo is not None and ever < NOW


def test_a_tree_whose_every_change_already_landed_is_nothing_to_ship(tmp_path, monkeypatch):
    """f95dffbc: the resolver's staged merge matched master, because the bump it was
    resolving had landed meanwhile. Two fixers wrote "nothing to ship" and the pass
    committed each anyway; only the retired-name refusal stopped an empty PR, and the
    group went to the ledger as fixers-exhausted. A later trunk change the tree lacks
    (`g`) is not the tree's to ship."""
    plans = capture_plans(monkeypatch)
    repo, git = _resolving(tmp_path, "d")
    (repo / "logs").mkdir()
    (repo / ".git" / "info" / "exclude").write_text("logs/\n", encoding="utf-8")
    (repo / ship_intent.INTENT_FILE).write_text("Nothing to ship\n\nIt landed.\n", "utf-8")
    one = ship_intent.Intent("r", repo, "feat", "Nothing to ship", "It landed.")
    assert ship_intent.lands_nothing(repo, "main", ship_intent.run_quiet)
    out = ship_intent.ship_one(one, "py", "main", ship_intent.run_quiet, gh_ok, NOW)
    assert out.stage == ship_intent.EMPTY and plans == []
    assert "already on origin/main" in out.detail
    assert git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0, "nothing committed"
    assert not (repo / ship_intent.INTENT_FILE).exists()
    assert ship_intent.read_state(repo)["stage"] == ship_intent.EMPTY


def test_a_resolution_the_trunk_does_not_have_is_still_shipped(tmp_path):
    repo, _git = _resolving(tmp_path, "e")
    assert not ship_intent.lands_nothing(repo, "main", ship_intent.run_quiet)


def test_a_resolution_that_took_the_trunks_side_is_measured_from_the_merged_commit(tmp_path):
    """The resolver kept `m1`'s `f`, and the trunk has moved `f` on since. Measured from
    HEAD's fork alone, that reads as the tree's own edit and conflicts with the trunk;
    the commit will have `MERGE_HEAD` as a parent, so the fork counts it."""
    repo, _git = _resolving(tmp_path, "c")
    assert ship_intent.lands_nothing(repo, "main", ship_intent.run_quiet)


def test_a_change_off_the_trunk_with_no_merge_in_progress_is_measured_from_the_fork(tmp_path):
    repo, git = _resolving(tmp_path, "c")
    git("merge", "--abort")
    assert not ship_intent.lands_nothing(repo, "main", ship_intent.run_quiet), "f = b"
    (repo / "f").write_text("d\n", encoding="utf-8")
    assert ship_intent.lands_nothing(repo, "main", ship_intent.run_quiet)


def test_an_edit_in_the_indexs_own_clock_tick_is_still_read(tmp_path):
    """The probe index is a copy of the real one, and a copy stamped *now* tells git its
    stat cache postdates every file -- so an edit of the same size, in the tick the index
    was written in, read as unchanged (flaky here whenever a second ticked over mid-test).
    Pinned: the index and `f` share one mtime a minute back, as a later pass finds them,
    and `f` was rewritten at the size and mtime the index records for it."""
    repo, git = _resolving(tmp_path, "c")
    git("merge", "--abort")
    f, index = repo / "f", repo / ".git" / "index"
    stamp = f.stat().st_mtime_ns - 60 * 10**9
    os.utime(f, ns=(stamp, stamp))
    git("update-index", "--really-refresh")  # the index now records `f` at `stamp`
    f.write_text("d\n", encoding="utf-8")  # the size of the "b" HEAD has
    for path in (f, index):
        os.utime(path, ns=(stamp, stamp))
    assert git("diff-files", "--name-only").stdout.split() == ["f"], "git itself sees it"
    assert ship_intent.lands_nothing(repo, "main", ship_intent.run_quiet)


def test_the_fork_counts_merge_head_only_while_a_merge_is_in_progress(tmp_path):
    merging = Runner({"git merge-base": (0, "abc\n", "")})
    ship_intent.lands_nothing(tmp_path, "main", merging)
    assert ["git", "merge-base", "origin/main", "HEAD", "MERGE_HEAD"] in [
        c[0] for c in merging.calls
    ]
    plain = Runner({"git merge-base": (0, "abc\n", "")})

    def not_merging(argv, cwd, env=None):
        if argv[:2] == ["git", "rev-parse"]:  # `Runner` answers every rev-parse with a sha
            return subprocess.CompletedProcess(argv, 1, "", "")
        return plain(argv, cwd, env)

    ship_intent.lands_nothing(tmp_path, "main", not_merging)
    assert ["git", "merge-base", "origin/main", "HEAD"] in [c[0] for c in plain.calls]


def test_a_stored_retired_branch_refusal_is_tried_again(tmp_path):
    """The stranded sweep's refusal was recorded before the carry existed; held as "nothing
    has changed", it would never have been retried, and a fixer would have been sent to
    type the one fresh name the ship step now makes."""
    one = intent(tmp_path)
    why = "[devkit branch policy] commit blocked: branch 'x' " + ship_intent.RETIRED_MARK
    state = {"stage": ship_intent.REFUSED, "intent": one.digest, "output": why, "step": "commit"}
    state["tree"] = ship_intent._digest(" M a.py\n")
    assert ship_intent.still_refused(one, state, " M a.py\n") is None
    state["output"] = "ruff.....Failed"
    assert ship_intent.still_refused(one, state, " M a.py\n") is not None


def test_the_retirement_mark_is_the_branch_policys_own_words():
    """Reworded there, the carry above would silently stop firing."""
    policy = Path(ship_intent.__file__).parent / "git_policy" / "branch.py"
    assert ship_intent.RETIRED_MARK in policy.read_text(encoding="utf-8")


def test_a_topic_past_its_ninth_branch_is_still_carried(tmp_path):
    """The carry once tried only `-2`..`-9`. The pass cut sixteen
    `agent/fix-harness-ledger-0927` branches in a day, so a fix on the retired base name
    found every candidate taken and was refused, and a fixer was sent to make the fresh
    name the carry exists to make."""
    one = intent(tmp_path)
    taken = {"agent/labels-0919"} | {f"agent/labels-0919-{n}" for n in range(2, 17)}
    run = _RetiredBranch(retired={"agent/labels-0919"}, taken=taken)
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.SHIPPED
    assert ["git", "push", "-u", "origin", "agent/labels-0919-17"] in [
        argv for argv, _c, _e in run.calls
    ]


def test_a_retired_suffixed_branch_counts_on_from_its_family(tmp_path):
    """Not `agent/labels-0919-14-2`: a carried name stays in the family `tb.branch_name`
    numbers, so the next carry and the next person can both find it."""
    one = replace(intent(tmp_path), branch="agent/labels-0919-14")
    taken = {"agent/labels-0919", "agent/labels-0919-14"}
    run = _RetiredBranch(retired={"agent/labels-0919-14"}, taken=taken, branch=one.branch)
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.SHIPPED
    assert ["git", "branch", "agent/labels-0919-15"] in [argv for argv, _c, _e in run.calls]


def test_a_retired_branch_whose_every_carry_is_retired_too_is_still_a_refusal(tmp_path):
    """Names whose PR merged and whose refs were deleted show in no listing; the carry
    walks past them, but a bounded number of times."""
    one = intent(tmp_path)
    names = {"agent/labels-0919"} | {f"agent/labels-0919-{n}" for n in range(2, 40)}
    run = _RetiredBranch(retired=names)
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.REFUSED and "git push" not in run.verbs()
    assert run.verbs().count("git symbolic-ref") == ship_intent.CARRIES


def test_the_next_free_name_is_above_every_used_one_not_in_a_gap():
    stem = "agent/labels-0919"
    assert ship_intent.next_free_name(stem, set()) == f"{stem}-2"
    assert ship_intent.next_free_name(stem, {stem}) == f"{stem}-2"
    assert ship_intent.next_free_name(stem, {stem, f"{stem}-3", f"{stem}-12"}) == f"{stem}-13"
    unrelated = {f"{stem}-2x", f"{stem}-rc-9", f"other/{stem}-30", f"{stem}0-50"}
    assert ship_intent.next_free_name(stem, unrelated) == f"{stem}-2"


def test_the_branch_stem_drops_only_the_collision_suffix():
    assert ship_intent.branch_stem("agent/x-0927-14") == "agent/x-0927"
    assert ship_intent.branch_stem("agent/x-0927") == "agent/x-0927"
    assert ship_intent.branch_stem("agent/fix-3") == "agent/fix-3", "no date stamp, no suffix"
    assert ship_intent.branch_stem("agent/pr-1234-0919") == "agent/pr-1234-0919"
    assert ship_intent.branch_stem("agent/pr-1234-0919-2") == "agent/pr-1234-0919"


def test_a_failed_push_is_a_failure_not_a_refusal_and_leaves_no_refused_state(tmp_path):
    one = intent(tmp_path)
    run = Runner({"git push": (1, "", "Permission denied (publickey)")})
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.FAILED and "push" in out.detail
    state = ship_intent.read_state(one.tree)
    assert state["stage"] == ship_intent.FAILED, "recorded, and not as a refusal to cache"
    assert state["step"] == "push" and "Permission denied" in state["output"]
    assert state["intent"] == one.digest and state["when"] == NOW.isoformat(timespec="seconds")
    assert ship_intent.still_refused(one, state, "") is None
    assert run.verbs().count("git push") == 1, "a refusal is not asked again"


class Flaky(Runner):
    """A `Runner` whose `git push` fails `errors` times, in order, then goes through."""

    def __init__(self, *errors: str):
        super().__init__()
        self.errors = list(errors)

    def __call__(self, argv, cwd, env=None):
        if argv[:2] == ["git", "push"] and self.errors:
            self.calls.append(([str(a) for a in argv], Path(cwd), env))
            return subprocess.CompletedProcess(argv, 1, "", self.errors.pop(0))
        return super().__call__(argv, cwd, env)


GITHUB_500 = "remote: Internal Server Error\nremote: Request ID CF97:30974F\n! [remote rejected]"


def test_a_push_github_failed_on_its_side_is_asked_again_in_the_pass(tmp_path, monkeypatch):
    """4d942641: one `remote: Internal Server Error` filed a ship as a harness defect."""
    waited: list[float] = []
    monkeypatch.setattr(ship_intent, "_wait", waited.append)
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    run = Flaky(GITHUB_500)
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.SHIPPED
    assert waited == [ship_intent.GITHUB_RETRY_SECONDS[0]]
    assert run.verbs().count("git push") == 2


def test_a_pr_github_failed_on_its_side_is_deferred_not_failed(tmp_path, monkeypatch):
    """a0287c8b: an `HTTP 500` from the labels endpoint, past every retry, reads
    `deferred` -- which the pass does not file -- and the next pass tries again."""
    waited: list[float] = []
    monkeypatch.setattr(ship_intent, "_wait", waited.append)
    answer = "HTTP 500 (https://api.github.com/repos/o/devkit/labels/automerge)"
    asked: list[str] = []
    monkeypatch.setattr(
        ship_intent.sweep, "ensure_pr", lambda gh, plan: asked.append("pr") or ("", False, answer)
    )
    one = intent(tmp_path)
    out = ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW)
    assert (out.stage, out.detail) == (ship_intent.DEFERRED, f"pr: {answer}")
    assert len(asked) == 1 + len(ship_intent.GITHUB_RETRY_SECONDS)
    assert waited == list(ship_intent.GITHUB_RETRY_SECONDS)
    state = ship_intent.read_state(one.tree)
    assert state["stage"] == ship_intent.FAILED, "the state still reads as a ship to retry"
    assert state["since"] == NOW.isoformat(timespec="seconds")
    assert (one.tree / ship_intent.INTENT_FILE).exists()


def test_a_transient_failure_that_lasts_past_the_grace_is_failed_after_all(tmp_path, monkeypatch):
    """GitHub is not down for six hours; a "server error" that lasts that long is
    something about the request, and is filed like any other failure."""
    monkeypatch.setattr(ship_intent, "_wait", lambda seconds: None)
    one = intent(tmp_path)
    run = Runner({"git push": (1, "", GITHUB_500)})
    first = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert first.stage == ship_intent.DEFERRED
    later = NOW + ship_intent.TRANSIENT_GRACE - _dt.timedelta(minutes=1)
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, later).stage == ship_intent.DEFERRED
    assert ship_intent.read_state(one.tree)["since"] == NOW.isoformat(timespec="seconds")
    past = NOW + ship_intent.TRANSIENT_GRACE
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, past).stage == ship_intent.FAILED


def test_a_new_intent_starts_its_own_grace(tmp_path, monkeypatch):
    """`since` belongs to one intent: a fresh one after a long-failing one is not filed on
    its first transient failure."""
    monkeypatch.setattr(ship_intent, "_wait", lambda seconds: None)
    one = intent(tmp_path)
    old = {"stage": ship_intent.FAILED, "intent": "older", "since": "2026-09-01T00:00:00+00:00"}
    ship_intent.write_state(one.tree, old)
    run = Runner({"git push": (1, "", GITHUB_500)})
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.DEFERRED


def test_a_push_that_failed_saying_nothing_is_named_by_its_exit_code(tmp_path):
    one = intent(tmp_path)
    out = ship_intent.ship_one(one, "py", "main", Runner({"git push": (1, "", "")}), gh_ok, NOW)
    assert out.stage == ship_intent.FAILED and out.detail == "push: exit 1"


def test_a_push_refused_over_an_earlier_ship_is_pushed_again_next_pass(tmp_path, monkeypatch):
    """436fc0c8: the tree's last ship read `shipped`, this one's push was refused, and the
    next pass read a clean tree over that old `shipped` as already shipped -- the intent
    set aside, its commit never pushed."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    ship_intent.write_state(one.tree, {"stage": ship_intent.SHIPPED, "intent": "older"})
    refused = Runner({"git push": (1, "", "! [rejected] (non-fast-forward)")})
    assert ship_intent.ship_one(one, "py", "main", refused, gh_ok, NOW).stage == ship_intent.FAILED
    again = Runner(porcelain="")
    assert ship_intent.ship_one(one, "py", "main", again, gh_ok, NOW).stage == ship_intent.SHIPPED
    assert "git push" in again.verbs()


def test_a_branch_origin_does_not_have_yet_needs_no_catching_up(tmp_path):
    """A first push: the fetch finds no such ref, and nothing is compared or merged."""
    run = Runner({"git fetch": (128, "", "fatal: couldn't find remote ref agent/x")})
    assert ship_intent.catch_up(tmp_path, "agent/x", run) == ""
    assert run.verbs() == ["git fetch"]


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
    assert again.verbs() == ["git status", "git rev-parse", "git rev-parse"], (
        "no merge in progress, and HEAD is still the shipped sha"
    )


def test_an_intent_left_over_from_a_ship_is_set_aside_and_said_once(tmp_path, monkeypatch):
    """The first supervised pass found ten: intents whose PRs had merged, from before the
    pass consumed what it shipped, re-read and re-reported on every pass -- and a `plan`
    pass called each "would ship". Set aside, the next pass never sees them."""
    one = intent(tmp_path)
    ship_intent.write_state(one.tree, {"stage": ship_intent.SHIPPED, "intent": one.digest})
    (one.tree / ship_intent.INTENT_FILE).write_text("S\n", encoding="utf-8")
    assert ship_intent.is_spent(one, Runner(porcelain=""))
    assert not ship_intent.is_spent(one, Runner(porcelain=" M a.py\n")), "edits since are new work"
    outcome = ship_intent.ship_one(one, "py", "main", Runner(porcelain=""), gh_ok, NOW)
    assert outcome.stage == ship_intent.SKIPPED and "set aside" in outcome.detail
    assert not (one.tree / ship_intent.INTENT_FILE).exists()
    assert (one.tree / ship_intent.SHIPPED_FILE).read_text(encoding="utf-8") == "S\n"


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
    assert run.verbs() == ["git status", "git rev-parse", "git rev-parse"]


class Merging(Runner):
    """A `Runner` whose tree is mid-merge: `--git-path MERGE_HEAD` names a file that exists."""

    def __init__(self, merge_head: Path, **kwargs):
        super().__init__(**kwargs)
        self.merge_head = merge_head

    def __call__(self, argv, cwd, env=None):
        if argv[:4] == ["git", "rev-parse", "--git-path", "MERGE_HEAD"]:
            self.calls.append(([str(a) for a in argv], Path(cwd), env))
            return subprocess.CompletedProcess(argv, 0, f"{self.merge_head}\n", "")
        return super().__call__(argv, cwd, env)


def test_a_merge_that_changes_no_file_is_committed_not_read_as_shipped(tmp_path, monkeypatch):
    """#538: its resolver merged origin/main in, the merge changed no file (main's side was
    already in through a criss-cross), and the pass read the clean tree at the shipped sha
    as "already shipped" -- the merge parent that would have cleared GitHub's conflict
    was set aside with the intent."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    shipped = {"stage": ship_intent.SHIPPED, "intent": one.digest, "sha": "abc123"}
    ship_intent.write_state(one.tree, shipped)
    merge_head = tmp_path / "MERGE_HEAD"
    merge_head.write_text("78f8e03\n", encoding="utf-8")
    assert not ship_intent.is_spent(one, Merging(merge_head, porcelain=""))
    run = Merging(merge_head, porcelain="")
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.SHIPPED
    verbs = run.verbs()
    assert "git commit" in verbs and verbs.index("git commit") < verbs.index("git push")
    merge_head.unlink()
    assert ship_intent.is_spent(one, Merging(merge_head, porcelain=""))


def test_merging_is_whether_merge_head_exists_and_unknown_is_no(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert not ship_intent.merging(repo, ship_intent.run_quiet)
    (repo / ".git" / "MERGE_HEAD").write_text("abc\n", encoding="utf-8")
    assert ship_intent.merging(repo, ship_intent.run_quiet)

    def refused(argv, cwd, env=None):
        return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")

    assert not ship_intent.merging(tmp_path, refused)


def test_a_clean_tree_whose_last_ship_failed_still_pushes(tmp_path, monkeypatch):
    """The commits are already there from the attempt whose push failed: nothing to
    commit, everything to push."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    ship_intent.write_state(one.tree, {"stage": ship_intent.FAILED, "intent": one.digest})
    run = Runner(porcelain="")
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.SHIPPED
    assert "git commit" not in run.verbs() and "git push" in run.verbs()


def test_a_failed_push_is_recorded_so_an_older_ship_cannot_hide_its_commit(tmp_path, monkeypatch):
    """3ae36740 (#463): the commit landed, the push was refused, and nothing was written,
    so `ship-state.json` still said `shipped` for an older sha. The next pass read the
    clean tree as that ship and set the intent aside; the fix never reached origin."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    ship_intent.write_state(one.tree, {"stage": ship_intent.SHIPPED, "intent": one.digest})
    refused = Runner({"git push": (1, "", "! [rejected] (non-fast-forward)")})
    out = ship_intent.ship_one(one, "py", "main", refused, gh_ok, NOW)
    assert out.stage == ship_intent.FAILED and "non-fast-forward" in out.detail
    state = ship_intent.read_state(one.tree)
    assert (state["stage"], state["step"], state["intent"]) == ("failed", "push", one.digest)
    again = Runner(porcelain="")
    assert ship_intent.ship_one(one, "py", "main", again, gh_ok, NOW).stage == ship_intent.SHIPPED
    assert "git push" in again.verbs()


def test_a_pr_that_failed_after_the_push_is_recorded_too(tmp_path, monkeypatch):
    one = intent(tmp_path)
    ship_intent.write_state(one.tree, {"stage": ship_intent.SHIPPED, "intent": one.digest})
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("", False, "no auth"))
    ship_intent.ship_one(one, "py", "main", Runner(), gh_ok, NOW)
    assert ship_intent.read_state(one.tree)["step"] == "pr"


def test_a_clean_tree_whose_head_moved_past_its_ship_still_pushes(tmp_path, monkeypatch):
    """The tree #463's reporter left: `shipped` at 035c6b7, HEAD two commits on and
    neither on origin. A record of a ship is a record of *that* sha."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    stale = {"stage": ship_intent.SHIPPED, "intent": one.digest, "sha": "035c6b7"}
    ship_intent.write_state(one.tree, stale)
    assert not ship_intent.is_spent(one, Runner(porcelain=""))
    run = Runner(porcelain="")
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.SHIPPED
    assert "git push" in run.verbs()
    assert not ship_intent.already_shipped(one, stale, "", head="abc123")
    assert ship_intent.already_shipped(one, {**stale, "sha": "abc123"}, "", head="abc123")


def test_origin_moved_ahead_of_the_tree_is_merged_before_the_push(tmp_path, monkeypatch):
    """#463 again: GitHub merged main into the PR 24 times while the tree held a commit
    of its own, so every push was refused non-fast-forward. Merged first, it goes."""
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    run = Runner({"git merge-base": (1, "", "")}, porcelain="")
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.SHIPPED
    argv = [call[0] for call in run.calls]
    remote = "refs/remotes/origin/agent/labels-0919"
    fetch = ["git", "fetch", "--quiet", "origin", f"+refs/heads/agent/labels-0919:{remote}"]
    assert fetch in argv
    assert ["git", "merge-base", "--is-ancestor", remote, "HEAD"] in argv
    merge = ["git", "merge", "--no-edit", remote]
    assert argv.index(merge) < argv.index(["git", "push", "-u", "origin", "agent/labels-0919"])


def test_a_head_that_does_not_merge_is_a_refusal_a_fixer_is_sent_at(tmp_path, monkeypatch):
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    run = Runner(
        {"git merge-base": (1, "", ""), "git merge": (1, "CONFLICT (content): a.py", "")},
        porcelain="",
    )
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.REFUSED and "CONFLICT" in out.detail
    assert ["git", "merge", "--abort"] in [call[0] for call in run.calls]
    assert "git push" not in run.verbs()
    assert ship_intent.read_state(one.tree)["step"] == "merge"


def test_catch_up_merges_only_what_origin_has_that_head_lacks(tmp_path):
    behind = Runner({"git merge-base": (1, "", "")})
    assert ship_intent.catch_up(tmp_path, "b", behind) == ""
    assert ["git", "merge", "--no-edit", "refs/remotes/origin/b"] in [c[0] for c in behind.calls]
    unknown = Runner({"git merge-base": (128, "", "not a commit")})
    assert ship_intent.catch_up(tmp_path, "b", unknown) == ""
    assert "git merge" not in unknown.verbs(), "git could not say: push as before"
    conflict = Runner({"git merge-base": (1, "", ""), "git merge": (1, "CONFLICT (content)", "")})
    said = ship_intent.catch_up(tmp_path, "b", conflict)
    assert said.startswith("origin/b moved") and "CONFLICT" in said
    assert conflict.calls[-1][0] == ["git", "merge", "--abort"]


@pytest.mark.parametrize(
    "answers",
    [{"git fetch": (128, "", "couldn't find remote ref")}, {"git merge-base": (0, "", "")}],
    ids=["first push", "origin already in HEAD"],
)
def test_nothing_is_merged_when_origin_has_nothing_the_tree_lacks(tmp_path, monkeypatch, answers):
    monkeypatch.setattr(ship_intent.sweep, "ensure_pr", lambda gh, plan: ("u", True, ""))
    one = intent(tmp_path)
    run = Runner(answers, porcelain="")
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.SHIPPED
    assert "git merge" not in run.verbs()


def test_a_clean_tree_with_nothing_committed_opens_no_pr_and_sets_the_intent_aside(
    tmp_path, monkeypatch
):
    """The regression: a fixer whose whole fix was a ledger resolution against another
    branch's PR (0927-5) had a clean tree level with `origin/main`. Its intent pushed an
    empty branch and `gh pr create` refused it, as a `ship-failed` on every pass."""
    plans = capture_plans(monkeypatch)
    one = intent(tmp_path)
    run = Runner({"git rev-list": (0, "0\n", "")}, porcelain="")
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.EMPTY and plans == []
    assert run.verbs() == ["git status", "git rev-parse", "git rev-list"]
    assert run.calls[1][0] == ["git", "rev-parse", "--git-path", "MERGE_HEAD"], "no merge"
    assert run.calls[2][0] == ["git", "rev-list", "--count", "origin/main..HEAD"]
    assert not (one.tree / ship_intent.INTENT_FILE).exists()
    assert (one.tree / ship_intent.SHIPPED_FILE).is_file(), "the session's outcome, kept"
    state = ship_intent.read_state(one.tree)
    assert state["stage"] == ship_intent.EMPTY and state["intent"] == one.digest


@pytest.mark.parametrize(
    ("answer", "ahead"),
    [
        ((0, "3\n", ""), 3),
        ((0, "0\n", ""), 0),
        ((128, "", "bad revision"), None),
        ((0, "", ""), None),
    ],
)
def test_commits_ahead_is_none_whenever_git_cannot_say(tmp_path, answer, ahead):
    """Unknown is not zero: a tree with no `origin/<base>` still ships as before."""
    assert ship_intent.commits_ahead(tmp_path, "main", Runner({"git rev-list": answer})) == ahead


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def merged_elsewhere(tmp_path):
    """carameli's minor-and-patch tree (e21febb8): a Dependabot branch one commit ahead of
    the base it was cut from, merging its base in with the result staged, while the PR
    landed on origin as a squash and origin moved on past it (#419's locks)."""
    repo = tmp_path / "tree"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    for key, value in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
        ("core.autocrlf", "false"),
        ("merge.conflictstyle", "merge"),
    ):
        _git(repo, "config", key, value)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
    _commit(repo, {"package.json": "1\n", "reqs.txt": "a\n"}, "cut")
    _git(repo, "switch", "-q", "-c", "theirs")
    _commit(repo, {"package.json": "2-rebased\n"}, "rebased upstream")
    landed = _commit(repo, {"reqs.txt": "b\n"}, "the locks")
    _git(repo, "switch", "-q", "main")
    _git(repo, "switch", "-q", "-c", "dependabot/x")
    _commit(repo, {"package.json": "2\n"}, "bump")
    done = subprocess.run(["git", "merge", "theirs~1"], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 1, "the conflict the fixer was resolving"
    (repo / "package.json").write_text("2-rebased\n", encoding="utf-8")
    _git(repo, "add", "package.json")
    _git(repo, "update-ref", "refs/remotes/origin/main", landed)
    return repo


def test_a_tree_whose_every_change_is_already_on_its_base_lands_nothing(merged_elsewhere):
    """It was dirty and one commit ahead, so it read as work: the commit stage ran, was
    refused on the retired name, and a fixer was sent at a refusal its own intent had
    already called nothing to ship (e21febb8)."""
    repo = merged_elsewhere
    staged = _git(repo, "diff", "--cached", "--name-status")
    assert ship_intent.lands_nothing(repo, "main", ship_intent.run_quiet)
    assert _git(repo, "diff", "--cached", "--name-status") == staged, "the real index is untouched"
    assert (repo / ".git" / "MERGE_HEAD").is_file()


@pytest.mark.parametrize(
    "edit",
    [{"package.json": "3\n"}, {"new.py": "x = 1\n"}],
    ids=["an edit origin lacks", "an untracked file"],
)
def test_anything_origin_lacks_is_something_to_land(merged_elsewhere, edit):
    for name, text in edit.items():
        (merged_elsewhere / name).write_text(text, encoding="utf-8")
    assert not ship_intent.lands_nothing(merged_elsewhere, "main", ship_intent.run_quiet)


def test_a_commit_origin_lacks_is_something_to_land(merged_elsewhere):
    _git(merged_elsewhere, "commit", "-q", "-m", "the merge")
    _commit(merged_elsewhere, {"more.txt": "new\n"}, "more")
    assert not ship_intent.lands_nothing(merged_elsewhere, "main", ship_intent.run_quiet)


def test_lands_nothing_is_false_whenever_git_cannot_say(merged_elsewhere, tmp_path):
    """Unknown is not empty: no `origin/<base>`, or no repository at all, ships as before."""
    assert not ship_intent.lands_nothing(merged_elsewhere, "trunk", ship_intent.run_quiet)
    assert not ship_intent.lands_nothing(tmp_path, "main", ship_intent.run_quiet)
    assert not ship_intent.lands_nothing(merged_elsewhere, "main", Runner())


def test_a_dirty_tree_with_nothing_to_land_is_set_aside_before_the_commit_stage(
    tmp_path, monkeypatch
):
    """The intent said "Nothing to ship", and the pass ran the fixers and the commit over
    it anyway; refused on the retired name, it sent a second fixer to say so again."""
    plans = capture_plans(monkeypatch)
    one = intent(tmp_path, subject="Nothing to ship: the bump merged as #418")
    asked = []
    monkeypatch.setattr(
        ship_intent, "lands_nothing", lambda tree, base, runner: asked.append(base) or True
    )
    run = Runner({"git rev-list": (0, "1\n", "")}, porcelain="M  package.json\n")
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.EMPTY and plans == [] and asked == ["main"]
    assert "git add" not in run.verbs() and "git commit" not in run.verbs()
    assert not (one.tree / ship_intent.INTENT_FILE).exists()
    assert (one.tree / ship_intent.SHIPPED_FILE).is_file()
    assert ship_intent.read_state(one.tree)["stage"] == ship_intent.EMPTY


def test_a_retired_name_is_carried_off_in_the_middle_of_a_merge(merged_elsewhere):
    """`git switch -c` refuses while a merge is in progress ("cannot switch branch while
    merging"), so the carry failed in exactly the tree that needed it, and `git checkout
    -b` would drop the merge's second parent. Re-pointing HEAD keeps both."""
    repo = merged_elsewhere
    (repo / "package.json").write_text("3\n", encoding="utf-8")
    one = ship_intent.Intent("carameli", repo, "dependabot/x", "S", "")
    moved = ship_intent._carry_to_free_branch(one, "dependabot/x", ship_intent.run_quiet, set())
    assert isinstance(moved, ship_intent.Intent) and moved.branch == "dependabot/x-2"
    assert _git(repo, "branch", "--show-current") == "dependabot/x-2"
    assert (repo / ".git" / "MERGE_HEAD").is_file()
    _git(repo, "commit", "-q", "-a", "--no-edit")
    assert len(_git(repo, "log", "-1", "--format=%P").split()) == 2
    refused = Runner({"git branch": (128, "", "fatal: not a valid branch name")})
    said = ship_intent._carry_to_free_branch(one, "dependabot/x", refused, set())
    assert said == "`git branch dependabot/x-2`: fatal: not a valid branch name"
    assert "git symbolic-ref" not in refused.verbs(), "HEAD is never pointed at no branch"


def test_a_tree_git_will_not_read_is_a_failure_never_a_clean_tree(tmp_path):
    """The supervisor's second intent, over 19 modified files, was set aside as "already
    shipped at this intent": git refused the Administrators-owned tree, its empty stdout
    read as a clean one, and a shipped state from the first intent did the rest. The
    work sat uncommitted with its intent renamed away."""
    one = intent(tmp_path)
    ship_intent.write_state(one.tree, {"stage": ship_intent.SHIPPED, "intent": "old"})
    refusal = "fatal: detected dubious ownership in repository at 'C:/w/t'\nTo add ...\n"

    def refused(argv, cwd, env=None):
        return subprocess.CompletedProcess(argv, 128, "", refusal)

    outcome = ship_intent.ship_one(one, "py", "main", refused, gh_ok, NOW)
    assert outcome.stage == ship_intent.FAILED
    assert outcome.detail == (
        "status: git could not read the tree: "
        "fatal: detected dubious ownership in repository at 'C:/w/t'"
    )
    assert (one.tree / ship_intent.INTENT_FILE).is_file(), "kept for the next pass"
    assert not (one.tree / ship_intent.SHIPPED_FILE).exists()
    assert ship_intent.is_spent(one, refused) is False, "a plan pass says it would ship"


def test_retired_at_is_when_the_names_last_pr_merged(tmp_path):
    """The line after which a resolution naming a retired branch can only mean the work
    carried off it -- the same line `fix_verify.relevant` draws. It was the merge-base's
    commit time, which moves on whenever the branch merges its base in: the supervisor's
    carry re-pointed nothing, its resolutions being older than main's newest merge."""
    asked = []
    merged = '[{"mergedAt": "2026-09-28T13:33:19Z"}, {"mergedAt": "2026-09-29T22:21:55Z"}]'

    def gh_for(tree):
        def gh(*args):
            asked.append((tree, args))
            return subprocess.CompletedProcess(["gh", *args], 0, merged, "")

        return gh

    assert ship_intent.retired_at(tmp_path, "agent/x", gh_for) == "2026-09-29T22:21:55Z"
    assert asked == [
        (tmp_path, ("pr", "list", "--head", "agent/x", "--state", "merged", "--json", "mergedAt"))
    ]
    refused = lambda tree: lambda *a: subprocess.CompletedProcess(a, 1, "", "no")
    assert ship_intent.retired_at(tmp_path, "agent/x", refused) == ""
    empty = lambda tree: lambda *a: subprocess.CompletedProcess(a, 0, "[]", "")
    assert ship_intent.retired_at(tmp_path, "agent/x", empty) == ""


def test_a_clean_tree_git_cannot_count_still_pushes(tmp_path, monkeypatch):
    plans = capture_plans(monkeypatch)
    one = intent(tmp_path)
    run = Runner({"git rev-list": (128, "", "unknown revision")}, porcelain="")
    assert ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW).stage == ship_intent.SHIPPED
    assert "git push" in run.verbs() and len(plans) == 1


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
    assert failure.signature == ("fixers refused: Executable not found",), (
        "the why is part of it: bare 'fixers refused' hid the cause, and no toolchain "
        "marker in fix_cycle.HARNESS_REFUSALS could ever match it"
    )


def test_a_refusal_signature_is_stable_across_two_refusals_of_one_kind():
    one = ship_intent.refusal_line("hook failed at line 412 in deadbeef1234: Failed")
    two = ship_intent.refusal_line("hook failed at line 97 in 0123abcd9876: Failed")
    assert one == two


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


def test_a_refusal_is_named_by_the_line_that_says_why():
    """ "fixers refused" was the whole signature, and a session had to open
    `ship-state.json` to learn the branch name was the objection."""
    output = "check yaml....Passed\nship: 'flag-x' is not a namespaced task branch; refusing to ship it.\n"
    assert ship_intent.refusal_line(output).startswith(
        "ship: 'flag-x' is not a namespaced task branch"
    )
    assert ship_intent.refusal_line("a\nlast words\n") == "last words"
    assert ship_intent.refusal_line("") == ""
    assert len(ship_intent.refusal_line("x Failed " + "y" * 500)) == 160


def test_a_refusals_reason_is_the_same_line_as_the_hook_wrote_it():
    """The signature normalises numbers and shas; a reader of the record needs neither
    lost. Same line `refusal_line` picks, so the two never name different causes."""
    output = "Detect secrets......Failed\n- hook id: detect-secrets\nline 141 in deadbeef12\n"
    assert ship_intent.refusal_reason(output) == "Detect secrets......Failed"
    assert ship_intent.refusal_line(output) == ship_intent.refusal_reason(output)
    numbered = "test_x_2 failed at deadbeef12\n"
    assert ship_intent.refusal_reason(numbered) == "test_x_2 failed at deadbeef12"
    assert ship_intent.refusal_line(numbered) == "test_x_N failed at <sha>"
    assert ship_intent.refusal_reason("") == ""


def test_a_branch_policy_refusal_is_named_by_its_reason_not_its_details_pointer():
    """b4b33191: the policy says "commit blocked", which matched nothing, so the line
    kept was the last -- `details: <path>`, a file the next hook run rewrites -- and the
    ledger never said the branch was retired."""
    output = (
        "[devkit branch policy] commit blocked: branch 'agent/x-0926' is permanently "
        "retired because its PR merged (https://github.com/o/r/pull/426).\n"
        "[devkit branch policy] details: C:\\r\\.git\\worktrees\\x\\devkit-branch-policy.json\n"
    )
    assert "retired" in ship_intent.refusal_line(output)


def test_a_refusal_nothing_has_changed_since_is_not_run_again(tmp_path):
    """The third supervised run re-ran carameli's whole commit stage on every pass for a
    refusal held behind a red harness, to the same answer each time."""
    one = intent(tmp_path)
    ship_intent.write_state(
        one.tree,
        {
            "stage": ship_intent.REFUSED,
            "step": "fixers",
            "output": "Detect secrets....Failed",
            "intent": one.digest,
            "tree": ship_intent._digest(" M a.py\n"),
        },
    )
    run = Runner(porcelain=" M a.py\n")
    outcome = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert (
        outcome.stage == ship_intent.REFUSED
        and outcome.detail == "fixers: Detect secrets....Failed"
    )
    # Asked first whether the work has landed on the base since -- a refusal of work
    # that merged elsewhere is nothing to hold (e21febb8) -- and then held, unrun.
    assert run.verbs() == ["git status", "git rev-parse", "git merge-base"], (
        "no fixers, no commit: the stored refusal stands"
    )
    moved = Runner(porcelain=" M a.py\n M b.py\n")
    assert ship_intent.still_refused(one, ship_intent.read_state(one.tree), " M b.py\n") is None
    ship_intent.ship_one(one, "py", "main", moved, gh_ok, NOW)
    assert "git commit" in moved.verbs() or "scripts/ship.py" in " ".join(moved.verbs()), (
        "an edit is a new try"
    )
    reworded = intent(tmp_path, body="Other words.")
    assert (
        ship_intent.still_refused(reworded, ship_intent.read_state(one.tree), " M a.py\n") is None
    )


def test_a_pr_from_a_tree_the_pass_cut_merges_itself_and_a_persons_waits(tmp_path, monkeypatch):
    """Fixer PRs merge once green; only a person's PR waits for a person. The mark is the
    tree's origin, not the dispatch stamp: a fixer sent to repair a person's PR stamps
    that person's tree, and `ensure_pr` labels a reused PR too."""
    plans = capture_plans(monkeypatch)
    fixer = intent(tmp_path / "fixer")

    (fixer.tree / ship_intent.fix_reports.ORIGIN_FILE).write_text("fix-pass\n", encoding="utf-8")
    ship_intent.ship_one(fixer, "py", "main", Runner(), gh_ok, NOW)
    person = intent(tmp_path / "person")
    ship_intent.fix_reports.stamp(person.tree, "pr:devkit:404:a:d:dispatch", "n", NOW)
    ship_intent.ship_one(person, "py", "main", Runner(), gh_ok, NOW)
    assert [p.pr_labels for p in plans] == [(ship_intent.sweep.AUTOMERGE_LABEL,), ()]
    assert ship_intent.labels_for(person.tree) == ()
