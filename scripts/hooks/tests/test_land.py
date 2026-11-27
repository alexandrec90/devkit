"""`scripts/land.py`: a session's change reaches the default branch, and the person's own
checkout, with nobody merging anything by hand.

Driven against real git -- a bare repository standing in for origin, the person's
checkout cloned from it, and a session's worktree off that checkout -- with GitHub faked
at the `gh` boundary. The fake merges for real, by pushing the PR's branch onto origin's
default branch, so "the checkout is synced" is read off the files on disk.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load_module

land = load_module("scripts/land.py")
merger = load_module("scripts/merge-dependabot-prs.py")

SLUG = "me/proj"
MONEY = '[delivery]\nflow = "direct"\nguarded = ["src/execution/"]\n'


def sh(args: list[str], cwd: Path) -> str:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """origin (bare), the person's checkout on `main`, and a session worktree off it."""
    origin, checkout = tmp_path / "origin.git", tmp_path / "proj"
    sh(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], tmp_path)
    sh(["git", "clone", "--quiet", str(origin), str(checkout)], tmp_path)
    # No hooks: this machine's global `core.hooksPath` refuses a commit to the default
    # branch, and the fixture's first commit is one.
    hookless = tmp_path / "no-hooks"
    hookless.mkdir()
    for key, value in (
        ("user.name", "t"),
        ("user.email", "t@example.invalid"),
        ("core.hooksPath", hookless.as_posix()),
        ("commit.gpgsign", "false"),
    ):
        sh(["git", "config", key, value], checkout)
    (checkout / ".devkit.toml").write_text(MONEY, encoding="utf-8")
    (checkout / "README.md").write_text("hello\n", encoding="utf-8")
    sh(["git", "add", "-A"], checkout)
    sh(["git", "commit", "--quiet", "-m", "start"], checkout)
    sh(["git", "push", "--quiet", "-u", "origin", "main"], checkout)
    sh(["git", "remote", "set-head", "origin", "main"], checkout)
    tree = tmp_path / "proj" / ".claude" / "worktrees" / "s1"
    sh(["git", "worktree", "add", "--quiet", "-b", "worktree-s1", str(tree), "main"], checkout)
    (checkout / ".git" / "info" / "exclude").write_text(".claude/\n", encoding="utf-8")
    monkeypatch.setattr(land, "slug_of", lambda url: SLUG)
    return SimpleNamespace(origin=origin, checkout=checkout, tree=tree)


class FakeGitHub:
    """The REST calls `land.py` makes, answered from state; git itself runs for real.

    `queue` is what the merge queue does once a PR is labelled `merge-now`: `merges` it
    by the time the PR is next read, `never` comes, or finds it in `conflict`.
    """

    def __init__(self, world, queue: str = "merges", existing: int = 0):
        self.world, self.queue = world, queue
        self.prs: dict[int, dict] = {}
        self.labels: dict[int, list[str]] = {}
        self.calls: list[list[str]] = []
        self.git_envs: list[dict] = []
        if existing:
            self.prs[existing] = {"number": existing, "head_ref": "worktree-s1"}

    def __call__(self, argv, cwd=None, capture_output=False, text=False, check=False, env=None):
        if argv[0] == "git":
            self.git_envs.append(env or {})
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        self.calls.append(list(argv))
        return SimpleNamespace(stdout=self.answer(argv), stderr="", returncode=0)

    def answer(self, argv) -> str:
        path = next(arg for arg in argv if arg.startswith("repos/"))
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        if method == "POST" and path.endswith("/pulls"):
            return self.create(argv)
        if method == "POST" and path.endswith("/labels"):
            number = int(path.split("/")[-2])
            self.labels.setdefault(number, []).append(argv[-1].split("=", 1)[1])
            return "[]"
        if method == "PUT" and path.endswith("/merge"):
            self.merge(int(path.split("/")[-2]))
            return "{}"
        if method == "DELETE":
            return ""
        if "pulls?" in path:
            head = path.split("head=me:", 1)[1].split("&", 1)[0]
            return json.dumps([pr for pr in self.prs.values() if pr["head_ref"] == head])
        return json.dumps(self.read(int(path.rsplit("/", 1)[1])))

    def field(self, argv, name: str) -> str:
        return next(a.split("=", 1)[1] for a in argv if a.startswith(f"{name}="))

    def create(self, argv) -> str:
        number = 100 + len(self.prs)
        self.prs[number] = {
            "number": number,
            "title": self.field(argv, "title"),
            "body": self.field(argv, "body"),
            "head_ref": self.field(argv, "head"),
        }
        return f"{number}\n"

    def merge(self, number: int) -> None:
        sh(["git", "push", "--quiet", "origin", "HEAD:refs/heads/main"], self.world.tree)
        self.prs[number]["merged"] = True

    def read(self, number: int) -> dict:
        pr = self.prs[number]
        if "merge-now" in self.labels.get(number, []) and not pr.get("merged"):
            if self.queue == "merges":
                self.merge(number)
            elif self.queue == "conflict":
                return {**pr, "state": "open", "mergeable": False}
        return {**pr, "state": "closed" if pr.get("merged") else "open", "mergeable": True}

    def merged_by_put(self) -> bool:
        return any("PUT" in call for call in self.calls)


def args(world, title="Add a greeting", body="Because.") -> SimpleNamespace:
    return SimpleNamespace(cwd=str(world.tree), title=title, body=body)


def ticking():
    now = [0.0]

    def clock():
        now[0] += 10.0
        return now[0]

    return clock


def run_land(world, github, **kwargs) -> str:
    return land.land(
        args(world, **kwargs), run=github, sleep=lambda _s: None, clock=ticking(), merger=merger
    )


def test_a_change_merges_and_reaches_the_persons_checkout(world):
    """**The whole contract**, end to end: edit in a worktree, land, and the file is in
    the person's checkout with no gate, no hold and nobody's click."""
    (world.tree / "greeting.py").write_text("print('hi')\n", encoding="utf-8")
    github = FakeGitHub(world)
    assert run_land(world, github) == land.LANDED
    assert github.labels == {100: ["merge-now"]}
    assert (world.checkout / "greeting.py").exists()
    assert sh(["git", "log", "-1", "--format=%s"], world.checkout) == "Add a greeting"


def test_a_file_nothing_could_read_is_said_and_never_pushed(world):
    """The session that wrote it is still here: it fixes the file and lands again."""
    broken = world.tree / "greeting.py"
    broken.write_text("def greet(:\n", encoding="utf-8")
    github = FakeGitHub(world)
    with pytest.raises(land.LandError, match=r"greeting\.py: does not parse"):
        run_land(world, github)
    assert github.prs == {}, "no PR"
    broken.write_text("def greet(): ...\n", encoding="utf-8")
    assert run_land(world, github) == land.LANDED


def test_a_file_broken_on_the_base_already_does_not_stop_a_change(world):
    bad = world.checkout / "fixture.json"
    bad.write_text("{,}", encoding="utf-8")
    sh(["git", "add", "-A"], world.checkout)
    sh(["git", "commit", "--quiet", "-m", "a fixture"], world.checkout)
    sh(["git", "push", "--quiet", "origin", "main"], world.checkout)
    sh(["git", "merge", "--quiet", "--ff-only", "main"], world.tree)
    (world.tree / "fixture.json").write_text("{,,}", encoding="utf-8")
    assert run_land(world, FakeGitHub(world)) == land.LANDED


def test_load_beside_and_refuse_unreadable_read_the_vendored_check(world):
    assert land.load_beside("land_check", land.CHECK_PATH).problem_in("a.json", "{,}")
    repo = land.read_repo(world.tree, land.utf8_run)
    land.refuse_unreadable(repo, ["README.md"], land.utf8_run)


def test_the_pr_carries_the_why(world):
    (world.tree / "a.txt").write_text("a\n", encoding="utf-8")
    github = FakeGitHub(world)
    run_land(world, github, body="It was missing.")
    assert github.prs[100]["body"] == "It was missing."
    assert github.prs[100]["title"] == "Add a greeting"


def test_a_change_to_money_waits_for_its_gate_and_is_not_merged(world):
    """The one exception: it is labelled to merge itself once green, and nothing here
    merges it or waits for it."""
    (world.tree / "src" / "execution").mkdir(parents=True)
    (world.tree / "src" / "execution" / "broker.py").write_text("x = 1\n", encoding="utf-8")
    github = FakeGitHub(world)
    assert run_land(world, github) == land.GATED
    assert github.labels == {100: ["automerge"]}
    assert not github.merged_by_put()
    assert not (world.checkout / "src").exists()


def test_a_change_that_drops_its_own_guard_still_waits(world):
    """The guard is read off both the default branch and the change: removing the line
    in the same PR as an order-placing edit must not let it through ungated."""
    (world.tree / ".devkit.toml").write_text('[delivery]\nflow = "direct"\n', encoding="utf-8")
    (world.tree / "src" / "execution").mkdir(parents=True)
    (world.tree / "src" / "execution" / "broker.py").write_text("x = 2\n", encoding="utf-8")
    github = FakeGitHub(world)
    assert run_land(world, github) == land.GATED
    assert github.labels == {100: ["automerge"]}


def test_a_gated_project_waits_for_its_gate_whatever_the_branch_says(world):
    """The flow is read off origin's default branch, as the queue reads it: a branch
    cannot switch its own gate off, and the fallback merge never skips one."""
    sh(["git", "switch", "--quiet", "main"], world.checkout)
    (world.checkout / ".devkit.toml").write_text('[delivery]\nflow = "gated"\n', encoding="utf-8")
    sh(["git", "commit", "--quiet", "-am", "gate it"], world.checkout)
    sh(["git", "push", "--quiet", "origin", "main"], world.checkout)
    (world.tree / ".devkit.toml").write_text('[delivery]\nflow = "direct"\n', encoding="utf-8")
    github = FakeGitHub(world, queue="never")
    assert run_land(world, github) == land.GATED
    assert github.labels == {100: ["automerge"]}
    assert not github.merged_by_put()


def test_a_change_beside_the_money_is_not_impeded(world):
    """Collectors, models and dashboards in a money project land like anything else."""
    (world.tree / "src" / "collectors").mkdir(parents=True)
    (world.tree / "src" / "collectors" / "odds.py").write_text("x = 1\n", encoding="utf-8")
    assert run_land(world, FakeGitHub(world)) == land.LANDED


def test_a_queue_that_never_comes_does_not_strand_the_pr(world):
    """A repository whose queue is missing, or behind, still gets its merge."""
    (world.tree / "b.txt").write_text("b\n", encoding="utf-8")
    github = FakeGitHub(world, queue="never")
    assert run_land(world, github) == land.LANDED
    assert github.merged_by_put()
    assert (world.checkout / "b.txt").exists()


def test_a_conflict_is_left_for_the_resolver_not_forced(world):
    (world.tree / "c.txt").write_text("c\n", encoding="utf-8")
    github = FakeGitHub(world, queue="conflict")
    assert run_land(world, github) == land.CONFLICT
    assert not github.merged_by_put()
    assert github.labels == {100: ["merge-now"]}, "labelled, so it merges once resolved"


def test_work_on_the_default_branch_moves_to_a_branch_of_its_own(world):
    (world.checkout / "d.txt").write_text("d\n", encoding="utf-8")
    github = FakeGitHub(world)
    outcome = land.land(
        SimpleNamespace(cwd=str(world.checkout), title="Add d", body=""),
        run=github,
        sleep=lambda _s: None,
        clock=ticking(),
        merger=merger,
    )
    assert outcome == land.LANDED
    assert github.prs[100]["head_ref"].startswith(land.BRANCH_PREFIX)


def test_an_open_pr_for_the_branch_is_reused(world):
    (world.tree / "e.txt").write_text("e\n", encoding="utf-8")
    github = FakeGitHub(world, existing=7)
    run_land(world, github)
    assert list(github.prs) == [7]
    assert github.labels == {7: ["merge-now"]}


def test_a_dirty_checkout_is_never_touched(world):
    (world.checkout / "README.md").write_text("mine, unsaved\n", encoding="utf-8")
    (world.tree / "f.txt").write_text("f\n", encoding="utf-8")
    assert run_land(world, FakeGitHub(world)) == land.LANDED
    assert (world.checkout / "README.md").read_text(encoding="utf-8") == "mine, unsaved\n"
    assert not (world.checkout / "f.txt").exists()


def test_a_hold_the_person_asked_for_is_neither_labelled_nor_merged(world):
    """`/land hold`: the merge queue reads the same line and leaves it open too."""
    (world.tree / "h.txt").write_text("h\n", encoding="utf-8")
    github = FakeGitHub(world)
    assert run_land(world, github, body="Why.\n\nHold: yes") == land.HELD
    assert github.labels == {}
    assert not github.merged_by_put()
    assert merger.held({"body": github.prs[100]["body"], "labels": []})


SCHEMA_SOURCE = (
    "from dataclasses import dataclass\n\n\n"
    "@dataclass(frozen=True)\n"
    "class DeliveryConfig:\n"
    '    flow: str = "direct"\n'
    "    guarded: tuple = ()\n"
)


def test_a_key_the_pinned_devkit_does_not_know_is_held_not_merged(world):
    """2026-11-26: `[delivery] e2e`, known only to devkit's main, merged into five projects
    and reddened their vendored contract test until a release. Held, it waits for one."""
    (world.tree / "scripts" / "hooks").mkdir(parents=True)
    (world.tree / "scripts" / "hooks" / "harness_config.py").write_text(
        SCHEMA_SOURCE, encoding="utf-8"
    )
    (world.tree / ".devkit.toml").write_text(MONEY + 'e2e = "ci"\n', encoding="utf-8")
    github = FakeGitHub(world)
    assert run_land(world, github) == land.HELD
    assert github.labels == {100: ["hold", "devkit-skew", "merge-now"]}
    body = github.prs[100]["body"]
    assert "[delivery] e2e" in body and "waits for the devkit release that adds them" in body
    assert not github.prs[100].get("merged") and not github.merged_by_put()


def test_a_key_the_pinned_devkit_knows_lands_as_usual(world):
    (world.tree / "scripts" / "hooks").mkdir(parents=True)
    (world.tree / "scripts" / "hooks" / "harness_config.py").write_text(
        SCHEMA_SOURCE, encoding="utf-8"
    )
    (world.tree / ".devkit.toml").write_text('[delivery]\nflow = "direct"\n', encoding="utf-8")
    assert run_land(world, FakeGitHub(world)) == land.LANDED


def test_a_schema_that_cannot_be_read_never_holds(world):
    """Fail-open: a broken vendored copy is not skew."""
    (world.tree / "scripts" / "hooks").mkdir(parents=True)
    (world.tree / "scripts" / "hooks" / "harness_config.py").write_text(
        "raise RuntimeError('no')\n", encoding="utf-8"
    )
    (world.tree / ".devkit.toml").write_text(MONEY + 'e2e = "ci"\n', encoding="utf-8")
    assert run_land(world, FakeGitHub(world)) == land.LANDED


def test_skew_keys_asks_only_of_a_devkit_toml_change_and_fails_open(tmp_path):
    repo = land.Repo(root=tmp_path, slug=SLUG, default="main")
    unasked = SimpleNamespace(
        skew=lambda *_a: pytest.fail("asked of a change with no .devkit.toml")
    )
    assert land.skew_keys(repo, ["README.md"], merger, None, unasked) == []

    def no_base(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 128, "", "no such path")

    asked = SimpleNamespace(skew=lambda _root, base: ["[delivery] e2e"] if base == {} else [])
    assert land.skew_keys(repo, [".devkit.toml"], merger, no_base, asked) == ["[delivery] e2e"]

    def no_git(_argv, **_kwargs):
        raise OSError("git is not installed")

    assert land.skew_keys(repo, [".devkit.toml"], merger, no_git, asked) == []


def test_hold_for_skew_labels_and_writes_the_line_once(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    fake_merger = SimpleNamespace(
        HOLD_LABEL=merger.HOLD_LABEL, gh=lambda argv, _run: calls.append(argv)
    )
    labelled: list[tuple[int, str]] = []
    monkeypatch.setattr(land, "label", lambda _repo, n, name, _m, _r: labelled.append((n, name)))
    check = load_module("scripts/land_check.py")
    repo = land.Repo(root=tmp_path, slug=SLUG, default="main")
    keys = ["[delivery] e2e"]

    land.hold_for_skew(repo, {"number": 7, "body": "Why."}, keys, fake_merger, None, check)
    assert labelled == [(7, merger.HOLD_LABEL), (7, check.SKEW_LABEL)]
    assert calls[0][:4] == ["api", "--method", "PATCH", f"repos/{SLUG}/pulls/7"]
    assert check.skew_line(keys) in calls[0][-1]

    calls.clear()
    held = {"number": 7, "body": check.with_skew_line("Why.", keys)}
    land.hold_for_skew(repo, held, keys, fake_merger, None, check)
    assert calls == []


def test_nothing_to_land_says_so(world):
    with pytest.raises(land.LandError, match="nothing to land"):
        run_land(world, FakeGitHub(world))


def test_no_local_hook_gates_the_commit_or_the_push(world):
    (world.tree / "g.txt").write_text("g\n", encoding="utf-8")
    github = FakeGitHub(world)
    run_land(world, github)
    assert all(env.get("DEVKIT_SKIP_PRE_COMMIT_FRAMEWORK") == "1" for env in github.git_envs)


def test_the_queue_gets_its_chance_before_this_merges():
    """Polled until merged or `QUEUE_WAIT`, never longer: a session must not hang."""
    reads = []

    class Slow:
        @staticmethod
        def gh_json(path, run):
            reads.append(path)
            return {"state": "open"}

    clock = ticking()
    pr = land.await_merge(land.Repo(Path("."), SLUG, "main"), 1, Slow, None, lambda s: None, clock)
    assert pr == {"state": "open"}
    assert len(reads) == int(land.QUEUE_WAIT // 10)  # the fake clock ticks 10 s a read


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/me/proj.git",
        "https://github.com/me/proj",
        "git@github.com:me/proj.git",
        "https://github.com/me/proj/",
    ],
)
def test_slug_of_reads_every_remote_spelling(url):
    assert land.slug_of(url) == SLUG


def test_a_remote_that_is_not_github_is_refused():
    with pytest.raises(land.LandError, match="not a GitHub repository"):
        land.slug_of("/srv/git/proj.git")


# --- each step on its own ----------------------------------------------------------


def repo_of(world) -> object:
    return land.read_repo(world.tree, subprocess.run)


def test_read_repo_finds_the_root_and_the_default_branch(world):
    repo = repo_of(world)
    assert (repo.root.resolve(), repo.slug, repo.default) == (world.tree.resolve(), SLUG, "main")


def test_branch_name_is_a_slug_of_the_title_and_the_moment():
    name = land.branch_name("Fix the Broker's  retry!", 0.0)
    assert re.fullmatch(r"land/fix-the-broker-s-retry-\d{4}-\d{6}", name), name
    assert land.branch_name("!!!", 0.0).startswith("land/change-")


def test_commit_stays_on_a_session_branch_and_leaves_the_default_one(world):
    (world.tree / "x.txt").write_text("x\n", encoding="utf-8")
    assert land.commit(repo_of(world), "Add x", "", subprocess.run, 0.0) == "worktree-s1"
    (world.checkout / "y.txt").write_text("y\n", encoding="utf-8")
    on_main = land.read_repo(world.checkout, subprocess.run)
    assert land.commit(on_main, "Add y", "", subprocess.run, 0.0).startswith("land/")


def test_changed_files_lists_what_differs_from_origin(world):
    (world.tree / "z.txt").write_text("z\n", encoding="utf-8")
    repo = repo_of(world)
    land.commit(repo, "Add z", "", subprocess.run, 0.0)
    assert land.changed_files(repo, subprocess.run) == ["z.txt"]


def test_open_pr_creates_once_then_reuses(world):
    github, repo = FakeGitHub(world), repo_of(world)
    first = land.open_pr(repo, "worktree-s1", "T", "B", merger, github)
    assert land.open_pr(repo, "worktree-s1", "T", "B", merger, github) == first == 100
    assert list(github.prs) == [100]


def test_label_applies_one_label(world):
    github = FakeGitHub(world)
    land.label(repo_of(world), 5, "merge-now", merger, github)
    assert github.labels == {5: ["merge-now"]}


@pytest.mark.parametrize(
    ("queue", "outcome"),
    [("merges", land.LANDED), ("never", land.LANDED), ("conflict", land.CONFLICT)],
)
def test_merge_it_lands_or_leaves_a_conflict(world, queue, outcome):
    (world.tree / "m.txt").write_text("m\n", encoding="utf-8")
    github, repo = FakeGitHub(world, queue=queue), repo_of(world)
    land.commit(repo, "Add m", "", github, 0.0)
    number = land.open_pr(repo, "worktree-s1", "Add m", "", merger, github)
    land.label(repo, number, "merge-now", merger, github)
    assert land.merge_it(repo, number, merger, github, lambda _s: None, ticking()) == outcome


def test_main_checkout_is_the_one_the_worktrees_hang_off(world):
    assert land.main_checkout(repo_of(world), subprocess.run).resolve() == world.checkout.resolve()


def test_sync_checkout_fast_forwards_and_says_so(world):
    (world.tree / "s.txt").write_text("s\n", encoding="utf-8")
    sh(["git", "add", "-A"], world.tree)
    sh(["git", "commit", "--quiet", "-m", "s"], world.tree)
    sh(["git", "push", "--quiet", "origin", "HEAD:main"], world.tree)
    said = land.sync_checkout(repo_of(world), subprocess.run)
    assert said.endswith("is at origin/main") and (world.checkout / "s.txt").exists()


def test_a_branch_whose_pr_already_merged_lands_from_a_fresh_one(world):
    """c15b7baa: a new worktree handed a name an earlier PR used, whose push the branch
    policy refuses for good."""
    (world.tree / "late.txt").write_text("late\n", encoding="utf-8")
    github = FakeGitHub(world)
    github.prs[7] = {"number": 7, "head_ref": "worktree-s1", "merged_at": "2026-11-26"}
    assert run_land(world, github) == land.LANDED
    [made] = [pr for number, pr in github.prs.items() if number != 7]
    assert made["head_ref"].startswith(land.BRANCH_PREFIX)


def test_merged_before_reads_a_closed_unmerged_pr_or_no_answer_as_no(world):
    github, repo = FakeGitHub(world), repo_of(world)
    github.prs[7] = {"number": 7, "head_ref": "worktree-s1"}
    assert not land.merged_before(repo, "worktree-s1", merger, github)
    github.prs[7]["merged_at"] = "2026-11-26"
    assert land.merged_before(repo, "worktree-s1", merger, github)

    def down(argv, **_kw):
        return SimpleNamespace(stdout="", stderr="HTTP 503", returncode=1)

    assert not land.merged_before(repo, "worktree-s1", merger, down)


def fetch_failing(times: int, said: str):
    """A runner whose first `times` fetches fail saying `said`; every call recorded."""
    calls: list[list[str]] = []

    def run(argv, **_kw):
        calls.append(list(argv))
        failed = "fetch" in argv and calls.count(list(argv)) <= times
        return SimpleNamespace(stdout="", stderr=said if failed else "", returncode=int(failed))

    return run, calls


def test_fetch_waits_out_a_ref_another_fetch_holds(world):
    """81293b9c: two fetches in the checkout's shared refs at once."""
    locked = "error: cannot lock ref 'refs/remotes/origin/main': is at a but expected b"
    run, calls = fetch_failing(2, locked)
    slept: list[float] = []
    land.fetch(repo_of(world), run, slept.append)
    assert len(calls) == 3 and slept == [land.POLL_EVERY] * 2
    run, _ = fetch_failing(land.FETCH_TRIES, locked)
    with pytest.raises(land.LandError, match="cannot lock ref"):
        land.fetch(repo_of(world), run, lambda _s: None)


def test_fetch_raises_any_other_failure_at_once(world):
    run, calls = fetch_failing(1, "fatal: could not read from remote repository")
    with pytest.raises(land.LandError, match="could not read"):
        land.fetch(repo_of(world), run, lambda _s: pytest.fail("slept on a real failure"))
    assert len(calls) == 1


def test_a_fetch_that_fails_after_the_merge_is_said_not_raised(world, monkeypatch):
    """The PR merged already: the sync is a courtesy, and its failure must not read as
    the run's (81293b9c)."""

    def refused(*_a, **_kw):
        raise land.LandError("git fetch --quiet origin main: error: cannot lock ref")

    monkeypatch.setattr(land, "fetch", refused)
    said = land.sync_checkout(repo_of(world), subprocess.run)
    assert "left for the scheduled sync" in said and "cannot lock ref" in said


def test_sync_checkout_leaves_a_checkout_on_another_branch(world):
    sh(["git", "switch", "--quiet", "-c", "mine"], world.checkout)
    assert "left as it is" in land.sync_checkout(repo_of(world), subprocess.run)


def test_settings_at_reads_a_ref_and_tolerates_none(world):
    repo = repo_of(world)
    assert land.settings_at(repo, "origin/main", merger, subprocess.run)["delivery"]["guarded"]
    assert land.settings_at(repo, "origin/no-such", merger, subprocess.run) == {}


def test_needs_gate_names_the_money_or_says_nothing(world):
    repo = repo_of(world)
    assert land.needs_gate(repo, ["src/execution/a.py"], merger, subprocess.run) == (
        "touches src/execution/a.py"
    )
    assert land.needs_gate(repo, ["README.md"], merger, subprocess.run) == ""


def test_load_merger_shares_the_queues_reading_of_guarded():
    loaded = land.load_merger()
    assert loaded.guarded_paths(["a/b.py"], ("a/",)) == ["a/b.py"]


def test_guarded_paths_matches_globs_and_directories():
    globs = ("src/execution/", "ext/*.js")
    paths = ["src/execution/x/y.py", "src/execution.py", "ext/a/b.js", "ext/a.css"]
    assert merger.guarded_paths(paths, globs) == ["ext/a/b.js", "src/execution/x/y.py"]
    assert merger.guarded_paths(paths, ()) == []


def test_parse_args_reads_the_body_from_a_file(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Why.\n", encoding="utf-8")
    args = land.parse_args(["--title", "T", "--body-file", str(body)])
    assert (args.title, args.body, args.cwd) == ("T", "Why.\n", ".")
    with pytest.raises(SystemExit):
        land.parse_args(["--body", "x"])


def test_main_reports_a_failure_and_exits_one(tmp_path, capsys):
    assert land.main(["--title", "T", "--cwd", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("land: ")


@pytest.mark.parametrize("name", ["broker.py", "caf\N{LATIN SMALL LETTER E WITH ACUTE}.py"])
def test_a_rename_out_of_the_money_hides_nothing(world, name):
    """Rename detection lists only the new path, and git quotes a non-ASCII one: either
    would let guarded code merge with no gate."""
    old = world.tree / "src" / "execution" / name
    old.parent.mkdir(parents=True)
    old.write_text("x = 1\n" * 30, encoding="utf-8")
    sh(["git", "add", "-A"], world.tree)
    sh(["git", "commit", "--quiet", "-m", "seed"], world.tree)
    sh(["git", "push", "--quiet", "origin", "HEAD:main"], world.tree)
    old.rename(world.tree / "moved.py")
    repo = repo_of(world)
    land.commit(repo, "Move it", "", land.utf8_run, 0.0)
    assert f"src/execution/{name}" in land.changed_files(repo, land.utf8_run)


def test_utf8_run_decodes_what_the_console_code_page_cannot():
    script = r"import sys; sys.stdout.buffer.write(b'\xe2\x80\x9d')"
    done = land.utf8_run([sys.executable, "-c", script], capture_output=True, text=True)
    assert done.stdout == "\N{RIGHT DOUBLE QUOTATION MARK}"


@pytest.mark.parametrize(
    "hold", [{"labels": [{"name": "hold"}]}, {"body": "Why.\n\nHold: yes"}, {"draft": True}]
)
def test_a_reused_pr_that_was_held_stays_held(world, hold):
    (world.tree / "r.txt").write_text("r\n", encoding="utf-8")
    github = FakeGitHub(world, existing=7)
    github.prs[7].update(hold)
    assert run_land(world, github) == land.HELD
    assert github.labels == {} and not github.merged_by_put()
