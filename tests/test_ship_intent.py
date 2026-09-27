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
        assert "not a namespaced task branch" in found[0].blocked


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
def test_a_hand_named_branch_with_no_open_pr_stays_blocked(tmp_path, code, out):
    git_for = _one_tree_on(tmp_path, "flag-wired-agent-hooks")
    found = ship_intent.find_intents(tmp_path, ["carameli"], git_for, _gh_listing(code, out, []))
    assert [i.blocked for i in found] == [
        ship_intent.ship.is_shippable("flag-wired-agent-hooks", "main")[1]
    ]


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
        "git add",
        r"C:\py\python.exe scripts/ship.py",
        "git add",
        "git commit",
        "git push",
        "git rev-parse",
    ]
    fix, add, commit, push = run.calls[2:6]
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

    def __init__(self, retired: set[str], taken: set[str] | None = None):
        super().__init__()
        self.retired, self.taken, self.branch = retired, set(taken or ()), "agent/labels-0919"

    def __call__(self, argv, cwd, env=None):
        argv = [str(a) for a in argv]
        if argv[:2] == ["git", "show-ref"]:
            self.calls.append((argv, Path(cwd), env))
            return subprocess.CompletedProcess(argv, 0 if argv[-1][11:] in self.taken else 1)
        if argv[:2] == ["git", "ls-remote"]:
            self.calls.append((argv, Path(cwd), env))
            return subprocess.CompletedProcess(
                argv, 0, "sha\tref\n" if argv[-1] in self.taken else ""
            )
        if argv[:3] == ["git", "switch", "-c"]:
            self.branch = argv[3]
        if argv[:2] == ["git", "commit"] and self.branch in self.retired:
            self.calls.append((argv, Path(cwd), env))
            why = f"[devkit branch policy] commit blocked: branch '{self.branch}' is permanently retired because its PR merged (https://x/pull/426)."
            return subprocess.CompletedProcess(argv, 1, why, "")
        return super().__call__(argv, cwd, env)


def test_an_intent_on_a_retired_branch_is_carried_to_the_next_free_name(tmp_path):
    """A ledger sweep's second intent landed after the pass had shipped its first and the
    PR merged: the policy refused every commit on the retired name, and the fix -- plus
    the ledger group it was resolved against -- sat stranded in the tree (c45826ad). The
    policy's own remedy is one `git switch -c`, which the ship step now takes itself."""
    one = intent(tmp_path)
    run = _RetiredBranch(retired={"agent/labels-0919"}, taken={"agent/labels-0919-2"})
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.SHIPPED
    assert ["git", "switch", "-c", "agent/labels-0919-3"] in [argv for argv, _c, _e in run.calls]
    assert ["git", "push", "-u", "origin", "agent/labels-0919-3"] in [
        argv for argv, _c, _e in run.calls
    ]


def test_a_stored_retired_branch_refusal_is_tried_again(tmp_path):
    """The stranded sweep's refusal was recorded before the carry existed; held as "nothing
    has changed", it would never have been retried, and a fixer would have been sent to
    type the one `git switch -c` the ship step now makes."""
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


def test_a_retired_branch_with_no_free_name_left_is_still_a_refusal(tmp_path):
    one = intent(tmp_path)
    names = {"agent/labels-0919"} | {f"agent/labels-0919-{n}" for n in range(2, 10)}
    run = _RetiredBranch(retired=names)
    out = ship_intent.ship_one(one, "py", "main", run, gh_ok, NOW)
    assert out.stage == ship_intent.REFUSED and "git push" not in run.verbs()


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
    assert run.verbs() == ["git status", "git rev-list"]
    assert run.calls[1][0] == ["git", "rev-list", "--count", "origin/main..HEAD"]
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
    assert run.verbs() == ["git status"], "no fixers, no commit: the stored refusal stands"
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
