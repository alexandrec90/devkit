"""`scripts/agent-worktree.py`: the git it runs, and what it refuses to run.

The decisions live in `agent_worktrees.py` and are tested next door. What is asserted
here is the seam: which argv reaches git, which run is refused before one does, and that
the two total functions stay total.
"""

from __future__ import annotations

import concurrent.futures as futures
import subprocess
import threading
import time
from pathlib import Path

import project_registry
import pytest
from support import load_script

# `support.load_script` rather than `_loader.load_by_path`, for `tests/test_fix_prs.py`'s
# reason: the latter overwrites `sys.modules[name]`, so a module would be loaded a second
# time into a process whose other suites monkeypatch the first copy.
agent_worktree = load_script("scripts/agent-worktree.py")
aw = agent_worktree.aw

# The three agent answers `create` takes, as `agent_models.Launch`.
CLAUDE = agent_worktree.agent_models.Launch("claude")
CODEX = agent_worktree.agent_models.Launch("codex")
NONE = agent_worktree.agent_models.Launch("none")


def register(monkeypatch, *names: str) -> None:
    """Make the project registry list `names`, as `projects.toml` would: the tool reads
    its projects there, and `--workspace` only says where the checkouts sit."""
    monkeypatch.setattr(project_registry, "all_projects", lambda *_args, **_kw: list(names))


class FakeRun:
    """A `subprocess.run` stand-in that records argv and answers from a script."""

    def __init__(self, codes: list[int] | None = None):
        self.calls: list[list[str]] = []
        self.codes = codes or []

    def __call__(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        code = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="")

    def git_args(self) -> list[list[str]]:
        """Each git call with `git -C <dir>` stripped, so assertions read as the verb."""
        return [call[3:] for call in self.calls if call[:1] == ["git"]]


def fake_git(answers: dict[tuple[str, ...], tuple[int, str]], default=(1, "")):
    """A `sweep.git_for`-shaped callable answering from a table keyed by argv."""

    def git(*args: str):
        code, out = answers.get(tuple(args), default)
        return subprocess.CompletedProcess(list(args), code, stdout=out, stderr="")

    return git


# --- counting what a worktree holds -------------------------------------------------


def test_unpushed_counts_against_the_upstream_when_there_is_one():
    git = fake_git({("rev-list", "--count", "agent/topic-0905@{u}..HEAD"): (0, "3\n")})
    assert agent_worktree.unpushed_count(git, "agent/topic-0905") == 3


def test_unpushed_falls_back_to_the_default_branch_when_nothing_is_tracked():
    """A branch that was never pushed has no `@{u}` and is exactly the case where
    unpushed work is most likely, so the fallback is not a nicety."""
    git = fake_git(
        {
            ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (
                0,
                "refs/remotes/origin/main",
            ),
            ("rev-list", "--count", "origin/main..HEAD"): (0, "2\n"),
        }
    )
    assert agent_worktree.unpushed_count(git, "agent/topic-0905") == 2


def test_a_detached_worktree_is_not_counted_against_any_base():
    """`removal_decision` still sees its dirty count; counting commits against a base
    nobody chose would refuse removals for no reason."""
    git = fake_git({}, default=(0, "99\n"))
    assert agent_worktree.unpushed_count(git, "") == 0


@pytest.mark.parametrize(
    ("upstream", "track", "against_default", "expected"),
    [
        ("origin/agent/x", "[ahead 3]", "9 0", 3),
        ("origin/agent/x", "[ahead 1, behind 4]", "9 0", 1),
        ("origin/agent/x", "[behind 4]", "9 0", 0),
        ("origin/agent/x", "", "9 0", 0),
        ("origin/agent/x", "[gone]", "5 2", 5),
        ("", "", "2 7", 2),
        ("", "", "", 0),
    ],
)
def test_one_branch_counts_by_the_same_rule_as_unpushed_count(
    upstream, track, against_default, expected
):
    """The upstream decides when it exists; no upstream, or one deleted on merge, falls
    back to the default branch. An empty `track` is both "level" and "no upstream", which
    only the upstream column tells apart."""
    assert agent_worktree.branch_ahead(upstream, track, against_default) == expected


def test_ahead_counts_reads_every_branch_in_one_call():
    fmt = agent_worktree.AHEAD_FORMAT.format(default="main")
    git = fake_git(
        {
            ("for-each-ref", fmt, "refs/heads"): (
                0,
                "main\torigin/main\t\t0 0\nagent/x\t\t\t2 1\nagent/y\torigin/agent/y\t[ahead 4]\t6 0\n",
            )
        }
    )
    assert agent_worktree.ahead_counts(git, "main") == {"main": 0, "agent/x": 2, "agent/y": 4}


def test_a_git_too_old_for_ahead_behind_answers_none():
    """`%(ahead-behind:)` is git 2.41; `trees_for` counts per worktree when it is absent."""
    assert agent_worktree.ahead_counts(fake_git({}), "main") is None


def test_trees_for_counts_from_the_checkout_without_a_rev_list_per_worktree(monkeypatch):
    listing = (
        "worktree C:/ws/devkit\nHEAD 1a\nbranch refs/heads/main\n\n"
        "worktree C:/ws/devkit/.claude/worktrees/topic\nHEAD 2b\n"
        "branch refs/heads/agent/topic-0905\n"
    )
    fmt = agent_worktree.AHEAD_FORMAT.format(default="main")
    called: list[tuple[str, ...]] = []

    def git_for(_path: Path):
        table = {
            ("worktree", "list", "--porcelain"): (0, listing),
            ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (
                0,
                "refs/remotes/origin/main",
            ),
            ("for-each-ref", fmt, "refs/heads"): (0, "agent/topic-0905\t\t\t3 0\n"),
            agent_worktree.STATUS: (0, ""),
        }
        answer = fake_git(table)

        def git(*args: str):
            called.append(args)
            return answer(*args)

        return git

    monkeypatch.setattr(agent_worktree.sweep, "git_for", git_for)
    found = agent_worktree.trees_for(Path("C:/ws/devkit"))

    assert [tree.unpushed for tree in found] == [3]
    assert not [args for args in called if args[0] in ("rev-list", "rev-parse")]


def test_known_branches_folds_origin_and_local_into_one_namespace():
    """`tb.branch_name` disambiguates against what it is shown, and a name free locally
    but taken on origin fails at the push — which is after the work, not before it."""
    git = fake_git(
        {
            (
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
                "refs/remotes/origin",
            ): (0, "main\nagent/topic-0905\norigin/main\norigin/agent/other-0904\n")
        }
    )
    assert agent_worktree.known_branches(git) == {
        "main",
        "agent/topic-0905",
        "agent/other-0904",
    }


def test_trees_for_reads_each_worktree_in_its_own_directory(monkeypatch):
    """The counts have to come from inside the worktree, not from the checkout: `git
    status` run in the checkout describes the checkout, and every row would then say the
    same thing.
    """
    asked: list[Path] = []
    listing = (
        "worktree C:/ws/devkit\nHEAD 1a\nbranch refs/heads/main\n\n"
        "worktree C:/ws/devkit/.claude/worktrees/topic\nHEAD 2b\n"
        "branch refs/heads/agent/topic-0905\n"
    )

    def git_for(path: Path):
        asked.append(path)
        return fake_git(
            {
                ("worktree", "list", "--porcelain"): (0, listing),
                agent_worktree.STATUS: (0, " M a.py\n?? b.py\n"),
                ("rev-list", "--count", "agent/topic-0905@{u}..HEAD"): (0, "1\n"),
            }
        )

    monkeypatch.setattr(agent_worktree.sweep, "git_for", git_for)
    found = agent_worktree.trees_for(Path("C:/ws/devkit"))

    assert found == [
        aw.Tree("topic", "C:/ws/devkit/.claude/worktrees/topic", "agent/topic-0905", 2, 1)
    ]
    assert asked[-1] == Path("C:/ws/devkit/.claude/worktrees/topic")


def test_a_listing_takes_no_lock_in_a_worktree():
    """A bare `git status` refreshes the index and so takes `index.lock`, in trees a live
    session may be committing from. A listing must not be able to fail its `git add`."""
    assert agent_worktree.STATUS[0] == "--no-optional-locks"
    assert agent_worktree.STATUS[1:3] == ("status", "--porcelain")


def many_trees(project: str, count: int) -> str:
    """`git worktree list --porcelain` for `project` holding `count` agent trees."""
    head = f"worktree C:/ws/{project}\nHEAD 1a\nbranch refs/heads/main\n\n"
    return head + "\n".join(
        f"worktree C:/ws/{project}/.claude/worktrees/t{n}\nHEAD 2b\nbranch refs/heads/t{n}\n"
        for n in range(count)
    )


def test_a_checkouts_statuses_run_wider_than_its_checkouts_fan_out(monkeypatch):
    """worktreeRow drew 96 trees in 2.1s warm and 25s cold at the old width of 8, and 8s
    cold at 32. The wait is on the disk, not on a core. Every status here waits until the
    whole width has arrived, so a pool narrower than `STATUS_WORKERS` breaks the barrier."""
    arrived = threading.Barrier(agent_worktree.STATUS_WORKERS, timeout=20)
    listing = many_trees("devkit", agent_worktree.STATUS_WORKERS)

    def git_for(_path: Path):
        answer = fake_git({("worktree", "list", "--porcelain"): (0, listing)})

        def git(*args: str):
            if args == agent_worktree.STATUS:
                arrived.wait()
                return subprocess.CompletedProcess(args, 0, "", "")
            return answer(*args)

        return git

    monkeypatch.setattr(agent_worktree.sweep, "git_for", git_for)
    found = agent_worktree.trees_for(Path("C:/ws/devkit"))
    assert len(found) == agent_worktree.STATUS_WORKERS


def test_the_status_width_is_shared_across_checkouts(monkeypatch):
    """Eight checkouts at once each with a pool of their own would be a hundred `git status`
    processes on eight cores for one `list`."""
    lock = threading.Lock()
    running = [0, 0]  # now, the most seen

    def git_for(path: Path):
        project = next(part for part in path.parts if part.startswith("p"))
        answer = fake_git(
            {
                ("worktree", "list", "--porcelain"): (
                    0,
                    many_trees(project, agent_worktree.STATUS_WORKERS),
                )
            }
        )

        def git(*args: str):
            if args != agent_worktree.STATUS:
                return answer(*args)
            with lock:
                running[0] += 1
                running[1] = max(running)
            time.sleep(0.01)
            with lock:
                running[0] -= 1
            return subprocess.CompletedProcess(args, 0, "", "")

        return git

    monkeypatch.setattr(agent_worktree.sweep, "git_for", git_for)
    with futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(agent_worktree.trees_for, [Path(f"C:/ws/p{n}") for n in range(3)]))
    assert running[1] <= agent_worktree.STATUS_WORKERS


def test_a_checkout_git_will_not_list_contributes_no_rows(monkeypatch):
    """A directory that is not a repository at all is the registry being stale, not a
    crash: the scan covers every registered checkout and one of them may have moved."""
    monkeypatch.setattr(agent_worktree.sweep, "git_for", lambda _p: fake_git({}))
    assert agent_worktree.trees_for(Path("C:/ws/devkit")) == []


def test_the_scan_covers_the_registry_and_skips_a_checkout_that_is_not_on_disk(
    tmp_path, monkeypatch
):
    """`resolve_project` would raise on a registered directory that has gone, so the
    missing one is dropped rather than taking the whole listing down with it."""
    (tmp_path / "devkit").mkdir()
    register(monkeypatch, "devkit", "moved-away")
    monkeypatch.setattr(agent_worktree, "trees_for", lambda _dir: [])

    assert agent_worktree.scan(tmp_path) == {"devkit": []}


def test_the_parser_defaults_to_the_safe_half_of_both_choices():
    """Both are the answer a mis-click should get: Claude in a tab you can watch, and a
    removal that refuses rather than discards."""
    parser = agent_worktree.build_parser()
    assert parser.parse_args(["new", "--pick=devkit:main"]).agent == "claude"
    assert parser.parse_args(["remove", "--picks="]).force == "keep"


def test_the_parser_refuses_an_agent_and_a_force_value_it_does_not_know():
    """`choices` rather than a string, so a typo fails at the parser instead of reaching
    git as a branch nobody meant."""
    parser = agent_worktree.build_parser()
    for argv in (["new", "--agent=gemini"], ["remove", "--force=maybe"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


# --- cutting one ---------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A projects root with one registered checkout, and the lookups `create`/`remove`
    make on it."""
    root = tmp_path
    (root / "devkit").mkdir()
    register(monkeypatch, "devkit")
    monkeypatch.setattr(
        agent_worktree.sweep,
        "git_for",
        lambda _path: fake_git(
            {
                ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (
                    0,
                    "refs/remotes/origin/main",
                ),
                ("rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"): (0, ""),
                (
                    "for-each-ref",
                    "--format=%(refname:short)",
                    "refs/heads",
                    "refs/remotes/origin",
                ): (0, "main\n"),
            }
        ),
    )
    return root


def test_a_new_worktree_is_cut_no_track_off_origin_after_a_fetch(workspace, monkeypatch):
    """Both halves copied from `worktree.spawn_plan`: branching off a remote-tracking ref
    without `--no-track` makes `origin/<base>` the upstream, so a later bare push lands
    the task's commits on the base branch — and a base nobody fetched is however stale
    this checkout last was."""
    opened = {}
    monkeypatch.setattr(
        agent_worktree.agent_tabs,
        "open_agent",
        lambda launch, box, branch, runner, **_k: (
            opened.update(agent=launch.cli, box=box, branch=branch) or 0
        ),
    )
    run = FakeRun()
    assert agent_worktree.create("devkit", workspace, "voicemail", "main", CODEX, run) == 0

    fetch, add = run.git_args()
    assert fetch == ["fetch", "--quiet", "origin"]
    # `--quiet` (80a224ea): without it git's `Updating files: NN%` checkout progress is
    # most of what the verb prints, and a session then greps it out of every call.
    assert add[:5] == ["worktree", "add", "--quiet", "--no-track", "-b"]
    assert add[5].startswith("agent/voicemail-")  # the date suffix is today's
    assert add[7] == "origin/main"
    assert Path(add[6]).parts[-3:] == (".claude", "worktrees", add[5].partition("/")[2])
    assert opened["agent"] == "codex"
    assert opened["branch"] == add[5]


def test_the_names_a_cut_moved_past_are_the_same_slug_the_same_day():
    existing = {"main", "agent/x-1008", "agent/x-1008-2", "agent/x-1007", "agent/xy-1008"}
    assert agent_worktree.same_task("agent/x-1008", "agent/x-1008-3", existing) == [
        "agent/x-1008",
        "agent/x-1008-2",
    ]
    assert agent_worktree.same_task("agent/y-1008", "agent/y-1008", existing) == []


def test_a_cut_past_a_taken_name_says_whose_pr_holds_it(workspace, monkeypatch, capsys):
    """10-08: a devkit ledger sweep cut data-lake's `archive-read-retry-1008-2` while #55
    from `archive-read-retry-1008` carried the same fix; #55 merged, and the sweep's PR was
    a duplicate, closed unread. The `-2` was the only sign, and nothing said it."""
    plain = agent_worktree.tb.branch_name("voicemail", set())
    refs = f"main\norigin/{plain}\n"
    git = agent_worktree.sweep.git_for(Path("."))

    def with_taken(*args):
        if args[:1] == ("for-each-ref",):
            return subprocess.CompletedProcess(list(args), 0, stdout=refs, stderr="")
        return git(*args)

    monkeypatch.setattr(agent_worktree.sweep, "git_for", lambda _path: with_taken)
    monkeypatch.setattr(agent_worktree.agent_tabs, "open_agent", lambda *a, **k: 0)
    pr = '[{"number": 55, "state": "OPEN", "url": "https://github.com/o/r/pull/55"}]'

    def run(argv, **kwargs):
        out = pr if argv[:3] == ["gh", "pr", "list"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    assert agent_worktree.create("devkit", workspace, "voicemail", "main", NONE, run) == 0
    said = capsys.readouterr().out.splitlines()
    assert said[0].startswith(f"{plain}-2 off origin/main")
    assert said[-1] == (
        f"agent-worktree: {plain} was taken -- PR #55 (OPEN) https://github.com/o/r/pull/55; "
        "if it carries this task, the work is there, not in this tree"
    )


def test_a_taken_name_gh_cannot_answer_for_is_still_named(tmp_path):
    def broken(argv, **kwargs):
        raise OSError("gh is not installed")

    assert agent_worktree.prior_work(tmp_path, ["agent/x-1008"], broken) == [
        "agent-worktree: agent/x-1008 was taken; if it carries this task, "
        "the work is there, not in this tree"
    ]
    assert agent_worktree.prior_work(tmp_path, [], broken) == []


def test_the_model_pick_reaches_the_tab_and_no_pick_reaches_it_as_nothing(workspace, monkeypatch):
    """`create` carries the pair and interprets neither, which is the whole seam.

    `agent-worktree.py` has no opinion about models: it hands `open_agent` the same
    `Launch` every other launcher does, so the flags a tab opens with are decided in one
    place for the whole repo. The second half is the one a regression reaches first -- a
    `new` given no model or effort must still open the CLI's own configuration.
    """
    opened = {}
    monkeypatch.setattr(
        agent_worktree.agent_tabs,
        "open_agent",
        lambda launch, box, branch, runner, **_k: opened.update(launch=launch) or 0,
    )
    chosen = agent_worktree.agent_models.Launch.parse("claude", "claude:claude-sonnet-5", "low")
    agent_worktree.create("devkit", workspace, "t", "main", chosen, FakeRun())
    assert opened["launch"].flags() == ["--model", "claude-sonnet-5", "--effort", "low"]

    agent_worktree.create("devkit", workspace, "t", "main", CLAUDE, FakeRun())
    assert opened["launch"].flags() == []


def test_a_tree_cut_from_fixer_work_is_fixer_work_too(workspace, monkeypatch, tmp_path):
    """A devkit ledger sweep cut carameli's secrets-baseline tree with this verb and never
    copied its `logs/fix-origin`, though its prompt said to -- so carameli #395 opened
    without `automerge` and waited on a person like a person's PR. The verb inherits the
    mark from the checkout running it, so no instruction has to be remembered."""
    monkeypatch.setattr(agent_worktree.agent_tabs, "open_agent", lambda *a, **k: 0)
    fixer = tmp_path / "fixer-tree"
    (fixer / "logs").mkdir(parents=True)
    (fixer / agent_worktree.fix_reports.ORIGIN_FILE).write_text("fix-pass\n", encoding="utf-8")
    monkeypatch.setattr(agent_worktree.fix_reports, "CHECKOUT", fixer)
    run = FakeRun()
    agent_worktree.create("devkit", workspace, "sibling", "main", NONE, run)
    cut = Path(run.git_args()[1][6])
    assert (cut / agent_worktree.fix_reports.ORIGIN_FILE).read_text(
        encoding="utf-8"
    ) == "fix-pass\n"
    monkeypatch.setattr(agent_worktree.fix_reports, "CHECKOUT", tmp_path / "persons-tree")
    run = FakeRun()
    agent_worktree.create("devkit", workspace, "mine", "main", NONE, run)
    assert not (Path(run.git_args()[1][6]) / agent_worktree.fix_reports.ORIGIN_FILE).exists()


def test_a_tree_the_hook_left_unprovisioned_is_provisioned_before_the_agent_opens(
    workspace, monkeypatch
):
    """The git hook skips a tree whose checkout has no `.venv`, which is right for an
    unused project's `worktree add` and wrong for a tree cut to work in: a fixer's
    social-scraper tree came up with no `.venv` and nothing saying why."""
    order: list[str] = []
    monkeypatch.setattr(
        agent_worktree.agent_tabs, "open_agent", lambda *a, **k: order.append("open") or 0
    )
    monkeypatch.setattr(
        agent_worktree.tree_provision, "provision", lambda tree, runner: order.append("provision")
    )
    run = FakeRun()
    agent_worktree.create("devkit", workspace, "cold", "main", NONE, run)
    assert order == ["provision", "open"]


def test_a_tree_the_hook_provisioned_is_not_provisioned_twice(workspace, monkeypatch, tmp_path):
    monkeypatch.setattr(agent_worktree.agent_tabs, "open_agent", lambda *a, **k: 0)
    monkeypatch.setattr(
        agent_worktree.tree_provision,
        "provision",
        lambda tree, runner: pytest.fail("the hook already provisioned this tree"),
    )

    class HookRun(FakeRun):
        """`git worktree add` whose post-checkout hook provisioned the tree."""

        def __call__(self, argv, **kwargs):
            done = super().__call__(argv, **kwargs)
            if "worktree" in argv:
                mark = Path(argv[-2]) / agent_worktree.tree_provision.worktree_env.PROVISIONED
                mark.parent.mkdir(parents=True, exist_ok=True)
                mark.write_text("", encoding="utf-8")
            return done

    agent_worktree.create("devkit", workspace, "warm", "main", NONE, HookRun())


def test_a_tree_the_hook_gave_only_a_venv_still_gets_its_node_modules(
    workspace, monkeypatch, tmp_path
):
    """The hook installs Python alone and marks the tree provisioned, so a roguelike tree
    cut by a fixer came up with a `.venv` and no `node_modules`, and vitest could not run."""
    monkeypatch.setattr(agent_worktree.agent_tabs, "open_agent", lambda *a, **k: 0)
    provisioned: list[Path] = []
    monkeypatch.setattr(
        agent_worktree.tree_provision, "provision", lambda tree, runner: provisioned.append(tree)
    )

    class VenvOnlyRun(FakeRun):
        def __call__(self, argv, **kwargs):
            done = super().__call__(argv, **kwargs)
            if "worktree" in argv:
                tree = Path(argv[-2])
                mark = tree / agent_worktree.tree_provision.worktree_env.PROVISIONED
                mark.parent.mkdir(parents=True, exist_ok=True)
                mark.write_text("", encoding="utf-8")
                (tree / ".devkit.toml").write_text(
                    '[frontend]\nenabled = true\ndir = "."\n', encoding="utf-8"
                )
            return done

    agent_worktree.create("devkit", workspace, "node", "main", NONE, VenvOnlyRun())
    assert len(provisioned) == 1


def test_a_blank_topic_names_the_branch_after_the_checkout(workspace, monkeypatch):
    monkeypatch.setattr(agent_worktree.agent_tabs, "open_agent", lambda *a, **k: 0)
    run = FakeRun()
    agent_worktree.create("devkit", workspace, "", "main", NONE, run)
    assert run.git_args()[1][5].startswith("agent/devkit-")


def test_a_base_origin_does_not_have_is_refused_before_anything_is_cut(workspace, monkeypatch):
    """The failure has to land before `git worktree add`, or the operator gets a branch
    they did not ask for and a worktree they have to clean up."""
    monkeypatch.setattr(
        agent_worktree.sweep,
        "git_for",
        lambda _path: fake_git({}, default=(1, "")),
    )
    run = FakeRun()
    assert agent_worktree.create("devkit", workspace, "topic", "nope", CLAUDE, run) == 2
    assert not [call for call in run.git_args() if call[:2] == ["worktree", "add"]]


def test_no_agent_is_opened_when_the_worktree_was_not_cut(workspace, monkeypatch):
    """A tab in a directory that does not exist is worse than no tab."""
    monkeypatch.setattr(
        agent_worktree.agent_tabs,
        "open_agent",
        lambda *a, **k: pytest.fail("opened an agent in a worktree that was never cut"),
    )
    assert agent_worktree.create("devkit", workspace, "topic", "main", CLAUDE, FakeRun([0, 1])) == 1


# --- destroying one ------------------------------------------------------------------


def tree(**fields) -> object:
    base = {
        "name": "topic",
        "path": "C:/ws/devkit/.claude/worktrees/topic",
        "branch": "agent/topic-0905",
        "dirty": 0,
        "unpushed": 0,
    }
    return aw.Tree(**{**base, **fields})


def test_a_worktree_holding_work_is_named_and_kept():
    run = FakeRun()
    code = agent_worktree.remove_one("devkit", Path("C:/ws/devkit"), tree(dirty=2), False, run)
    assert code == 1
    assert not run.calls


def test_forcing_passes_force_to_git_and_still_only_soft_deletes_the_branch():
    """Forcing is about the worktree, which is disposable by construction. The branch may
    be the only copy of its commits, so `-d` even here — git refusing is the point."""
    run = FakeRun()
    assert agent_worktree.remove_one("devkit", Path("C:/ws/devkit"), tree(dirty=2), True, run) == 0
    remove, branch = run.git_args()
    assert remove == ["worktree", "remove", "--force", "C:/ws/devkit/.claude/worktrees/topic"]
    assert branch == ["branch", "-d", "agent/topic-0905"]


def test_a_clean_worktree_is_removed_without_force():
    run = FakeRun()
    assert agent_worktree.remove_one("devkit", Path("C:/ws/devkit"), tree(), False, run) == 0
    assert run.git_args()[0] == [
        "worktree",
        "remove",
        "C:/ws/devkit/.claude/worktrees/topic",
    ]


class RefusingRun(FakeRun):
    """`git worktree remove` fails with `said`; every other call succeeds."""

    def __init__(self, said: str):
        super().__init__()
        self.said = said

    def __call__(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        code = 1 if [str(a) for a in argv[3:5]] == ["worktree", "remove"] else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr=self.said if code else "")


def test_a_removal_the_filesystem_refused_partway_is_finished_and_the_branch_reaped(tmp_path):
    """2026-10-10: Windows refused one file of a fresh `.venv` (`Invalid argument`). Git
    had already dropped the worktree's record, so the next `remove` said "no worktree
    called", and the directory and its branch were left for nobody to find."""
    husk = tmp_path / "topic"
    (husk / ".venv").mkdir(parents=True)
    (husk / ".venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    run = RefusingRun(f"error: failed to delete '{husk}': Invalid argument")
    code = agent_worktree.remove_one("devkit", tmp_path, tree(path=str(husk)), False, run)
    assert code == 0
    assert not husk.exists()
    verbs = [call[:2] for call in run.git_args()]
    assert verbs == [["worktree", "remove"], ["worktree", "prune"], ["branch", "-d"]]


def test_a_dirty_tree_git_refuses_is_never_finished_by_hand(tmp_path):
    kept = tmp_path / "topic"
    kept.mkdir()
    (kept / ".git").write_text("gitdir: x\n", encoding="utf-8")
    run = RefusingRun("fatal: 'topic' contains modified or untracked files, use --force")
    code = agent_worktree.remove_one("devkit", tmp_path, tree(path=str(kept)), False, run)
    assert code == 1
    assert kept.is_dir()
    assert [call[:2] for call in run.git_args()] == [["worktree", "remove"]]


def test_a_detached_worktree_leaves_no_branch_to_delete():
    run = FakeRun()
    agent_worktree.remove_one("devkit", Path("C:/ws/devkit"), tree(branch=""), False, run)
    assert [call[0] for call in run.git_args()] == ["worktree"]


def test_a_stale_pick_is_reported_rather_than_failing_the_run(workspace, monkeypatch):
    """A token naming a worktree that has already gone is reported, not an error."""
    monkeypatch.setattr(agent_worktree, "trees_for", lambda _dir: [])
    run = FakeRun()
    assert agent_worktree.remove([("devkit", "gone")], workspace, False, run) == 0
    assert not run.calls


def test_one_refusal_does_not_hide_the_removals_beside_it(workspace, monkeypatch):
    """The worst exit code, so a red task still means something went unremoved — and the
    clean pick is still acted on rather than held hostage to the dirty one."""
    monkeypatch.setattr(
        agent_worktree,
        "trees_for",
        lambda _dir: [tree(name="clean"), tree(name="dirty", dirty=1)],
    )
    run = FakeRun()
    picks = [("devkit", "clean"), ("devkit", "dirty")]
    assert agent_worktree.remove(picks, workspace, False, run) == 1
    assert [call[:2] for call in run.git_args()] == [["worktree", "remove"], ["branch", "-d"]]


# --- the entry point -----------------------------------------------------------------


def test_a_missing_registry_is_a_usage_error_rather_than_a_traceback(tmp_path, monkeypatch, capsys):
    """The registry is `projects.toml`; a malformed one is named, not a traceback, and a
    workspace file that is not there is no error at all."""
    registry = agent_worktree.devkit_project.project_registry

    def missing(*_args, **_kwargs):
        raise registry.RegistryError("no projects.toml at x")

    monkeypatch.setattr(registry, "all_projects", missing)
    absent = tmp_path / "nothing.code-workspace"
    assert agent_worktree.main(["list", "--workspace", str(absent)]) == agent_worktree.EXIT_USAGE
    assert "no projects.toml" in capsys.readouterr().err


def test_list_reads_the_root_from_root_or_from_the_deprecated_workspace(
    tmp_path, monkeypatch, capsys
):
    """`--workspace` is accepted and read only for its directory -- no file is opened,
    so one that does not exist names the same root `--root` does."""
    asked: list[Path] = []
    monkeypatch.setattr(agent_worktree, "scan", lambda root: asked.append(root) or {})
    absent = tmp_path / "nothing.code-workspace"
    assert agent_worktree.main(["list", "--workspace", str(absent)]) == 0
    assert agent_worktree.main(["list", "--root", str(tmp_path)]) == 0
    assert asked == [tmp_path.resolve(), tmp_path.resolve()]


def test_the_picker_verbs_are_gone():
    """`rows`, `bases`, `tree-projects` and `base-projects` drew a VS Code quick-pick;
    with the task layer retired they are usage errors, not silent no-ops."""
    for verb in ("rows", "bases", "tree-projects", "base-projects"):
        with pytest.raises(SystemExit) as exc:
            agent_worktree.build_parser().parse_args([verb])
        assert exc.value.code == 2


def test_a_picker_literal_is_an_unknown_checkout_not_a_cancel(workspace, capsys):
    """Nothing reads `${input:...}` as *never mind* any more: it is a token naming a
    checkout the registry does not know, refused as a usage error before git runs."""
    run = FakeRun()
    code = agent_worktree.main(
        ["remove", "--picks=${input:worktreeRow}", "--root", str(workspace)], run
    )
    assert code == agent_worktree.EXIT_USAGE
    assert "unknown project" in capsys.readouterr().err
    assert not run.calls


def test_naming_nothing_removes_nothing(workspace, capsys):
    code = agent_worktree.main(["remove", "--picks=", "--root", str(workspace)], FakeRun())
    assert code == 0
    assert "nothing named" in capsys.readouterr().out


def test_an_empty_base_pick_cuts_nothing(workspace, capsys):
    """The `new` verb's checkout comes from the same token as its base, so an empty pick
    has no checkout to run in -- which must read as nothing to do, not as a default."""
    code = agent_worktree.main(["new", "--pick=", "--slug=x", "--root", str(workspace)], FakeRun())
    assert code == 0
    assert "nothing to do" in capsys.readouterr().out


def test_the_render_lists_every_checkout_including_the_empty_ones():
    """A checkout that silently drops out when it is empty is indistinguishable from one
    the scan could not reach."""
    text = agent_worktree.render({"devkit": [tree()], "carameli": []})
    assert "devkit: 1 worktree(s)" in text
    assert "carameli: no worktree(s)" in text
    assert "topic -- agent/topic-0905 -- clean and pushed" in text
