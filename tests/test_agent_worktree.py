"""`scripts/agent-worktree.py`: the git it runs, and what it refuses to run.

The decisions live in `agent_worktrees.py` and are tested next door. What is asserted
here is the seam: which argv reaches git, which run is refused before one does, and that
the two total functions stay total.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from support import load_script

# `support.load_script` rather than `_loader.load_by_path`, for `tests/test_fix_prs.py`'s
# reason: the latter overwrites `sys.modules[name]`, so `agent-box.py` would be loaded a
# second time into a process whose other suites monkeypatch the first copy.
agent_worktree = load_script("scripts/agent-worktree.py")
aw = agent_worktree.aw


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


def test_the_base_list_pins_the_default_branch_first_whatever_its_date(monkeypatch):
    """It is the answer nine times out of ten, and a dropdown that buries it under
    yesterday's task branches is asking a question it knows the answer to."""
    monkeypatch.setattr(
        agent_worktree.sweep,
        "git_for",
        lambda _path: fake_git(
            {
                ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (
                    0,
                    "refs/remotes/origin/main",
                ),
                (
                    "for-each-ref",
                    "--sort=-committerdate",
                    "--format=%(refname:lstrip=3)%09%(committerdate:relative)",
                    "refs/remotes/origin",
                ): (0, "agent/new-0905\t1 hour ago\nHEAD\t1 hour ago\nmain\t2 days ago\n"),
            }
        ),
    )
    rows = agent_worktree.recent_bases(Path("C:/ws/devkit"))
    assert [name for name, _ in rows] == ["main", "agent/new-0905"]
    assert rows[0][1] == "the default branch"


def test_a_checkout_git_cannot_answer_for_still_offers_its_default_branch(monkeypatch):
    """`for-each-ref` failing must not produce an empty list: the dropdown ends at the
    first expression that throws, so a checkout with no rows hides every one after it."""
    monkeypatch.setattr(agent_worktree.sweep, "git_for", lambda _path: fake_git({}))
    assert agent_worktree.recent_bases(Path("C:/ws/devkit")) == [("main", "the default branch")]


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
                ("status", "--porcelain"): (0, " M a.py\n?? b.py\n"),
                ("rev-list", "--count", "agent/topic-0905@{u}..HEAD"): (0, "1\n"),
            }
        )

    monkeypatch.setattr(agent_worktree.sweep, "git_for", git_for)
    found = agent_worktree.trees_for(Path("C:/ws/devkit"))

    assert found == [
        aw.Tree("topic", "C:/ws/devkit/.claude/worktrees/topic", "agent/topic-0905", 2, 1)
    ]
    assert asked[-1] == Path("C:/ws/devkit/.claude/worktrees/topic")


def test_a_checkout_git_will_not_list_contributes_no_rows(monkeypatch):
    """A directory that is not a repository at all is the registry being stale, not a
    crash: the scan covers every registered checkout and one of them may have moved."""
    monkeypatch.setattr(agent_worktree.sweep, "git_for", lambda _p: fake_git({}))
    assert agent_worktree.trees_for(Path("C:/ws/devkit")) == []


def test_the_scan_covers_the_registry_and_skips_a_checkout_that_is_not_on_disk(
    tmp_path, monkeypatch
):
    """`resolve_project` would raise on a registered directory that has gone, and this
    runs as a rider on somebody else's pass — so the missing one is dropped rather than
    taking the whole menu down with it."""
    (tmp_path / "devkit").mkdir()
    registry = tmp_path / "registry.code-workspace"
    registry.write_text(
        '{"folders": [{"path": "devkit"}, {"path": "moved-away"}]}', encoding="utf-8"
    )
    monkeypatch.setattr(agent_worktree, "trees_for", lambda _dir: [])
    monkeypatch.setattr(agent_worktree, "recent_bases", lambda _dir: [("main", "the default")])

    trees, bases = agent_worktree.scan(registry)

    assert list(trees) == ["devkit"]
    assert list(bases) == ["devkit"]


def test_the_parser_defaults_to_the_safe_half_of_both_choices():
    """Both are the answer a mis-click should get: Claude in a tab you can watch, and a
    removal that refuses rather than discards."""
    parser = agent_worktree.build_parser()
    assert parser.parse_args(["new", "--pick=devkit:main"]).agent == "claude"
    assert parser.parse_args(["remove", "--picks="]).force == "keep"


def test_the_parser_refuses_an_agent_and_a_force_value_it_does_not_know():
    """`choices` rather than a string, so a typo in the workspace file fails at the
    parser instead of reaching git as a branch nobody meant."""
    parser = agent_worktree.build_parser()
    for argv in (["new", "--agent=gemini"], ["remove", "--force=maybe"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


# --- cutting one ---------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A registry with one checkout, and the two lookups `create`/`remove` make on it."""
    root = tmp_path
    (root / "devkit").mkdir()
    file = root / "alex-projects.code-workspace"
    file.write_text('{"folders": [{"path": "devkit"}]}', encoding="utf-8")
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
    monkeypatch.setattr(agent_worktree, "refresh_menu", lambda *a, **k: None)
    return file


def test_a_new_worktree_is_cut_no_track_off_origin_after_a_fetch(workspace, monkeypatch):
    """Both halves copied from `worktree.spawn_plan`: branching off a remote-tracking ref
    without `--no-track` makes `origin/<base>` the upstream, so a later bare push lands
    the task's commits on the base branch — and a base nobody fetched is however stale
    this checkout last was."""
    opened = {}
    monkeypatch.setattr(
        agent_worktree.agent_box,
        "open_agent",
        lambda agent, box, branch, runner: opened.update(agent=agent, box=box, branch=branch) or 0,
    )
    run = FakeRun()
    assert agent_worktree.create("devkit", workspace, "voicemail", "main", "codex", run) == 0

    fetch, add = run.git_args()
    assert fetch == ["fetch", "--quiet", "origin"]
    assert add[:4] == ["worktree", "add", "--no-track", "-b"]
    assert add[4].startswith("agent/voicemail-")  # the date suffix is today's
    assert add[6] == "origin/main"
    assert Path(add[5]).parts[-3:] == (".claude", "worktrees", add[4].partition("/")[2])
    assert opened["agent"] == "codex"
    assert opened["branch"] == add[4]


def test_a_blank_topic_names_the_branch_after_the_checkout(workspace, monkeypatch):
    monkeypatch.setattr(agent_worktree.agent_box, "open_agent", lambda *a, **k: 0)
    run = FakeRun()
    agent_worktree.create("devkit", workspace, "", "main", "none", run)
    assert run.git_args()[1][4].startswith("agent/devkit-")


def test_a_base_origin_does_not_have_is_refused_before_anything_is_cut(workspace, monkeypatch):
    """The failure has to land before `git worktree add`, or the operator gets a branch
    they did not ask for and a worktree they have to clean up."""
    monkeypatch.setattr(
        agent_worktree.sweep,
        "git_for",
        lambda _path: fake_git({}, default=(1, "")),
    )
    run = FakeRun()
    assert agent_worktree.create("devkit", workspace, "topic", "nope", "claude", run) == 2
    assert not [call for call in run.git_args() if call[:2] == ["worktree", "add"]]


def test_no_agent_is_opened_when_the_worktree_was_not_cut(workspace, monkeypatch):
    """A tab in a directory that does not exist is worse than no tab."""
    monkeypatch.setattr(
        agent_worktree.agent_box,
        "open_agent",
        lambda *a, **k: pytest.fail("opened an agent in a worktree that was never cut"),
    )
    assert (
        agent_worktree.create("devkit", workspace, "topic", "main", "claude", FakeRun([0, 1])) == 1
    )


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


def test_a_detached_worktree_leaves_no_branch_to_delete():
    run = FakeRun()
    agent_worktree.remove_one("devkit", Path("C:/ws/devkit"), tree(branch=""), False, run)
    assert [call[0] for call in run.git_args()] == ["worktree"]


def test_a_stale_pick_is_reported_rather_than_failing_the_run(workspace, monkeypatch):
    """The menu is up to a quarter of an hour old, so a row naming a worktree that has
    already gone is the ordinary case, not an error."""
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


# --- the menu and the entry point ----------------------------------------------------


def test_the_menu_is_none_rather_than_a_raise_when_the_registry_cannot_be_read(tmp_path):
    """`worktree.reconcile` calls this on every pass; a reconcile that reaped boxes
    correctly must never fail because a dropdown could not be rebuilt."""
    missing = tmp_path / "nothing.code-workspace"
    assert agent_worktree.refresh_menu(missing, tmp_path / "menu.json") is None


def test_a_registry_that_cannot_be_parsed_leaves_the_previous_menu_alone(tmp_path):
    """`sweep.parse_workspace` answers a file it cannot parse with an empty list rather
    than a raise, so "no checkouts" is what a truncated workspace file looks like from
    here. Writing the empty menu would turn one bad read into two dropdowns that offer
    nothing until the next pass."""
    # Deliberately not the live registry's filename: `refresh_menu` reads the path it is
    # given and nothing else, and naming it after the real file would make this test look
    # like one that needs `@needs_live_workspace`.
    broken = tmp_path / "truncated.code-workspace"
    broken.write_text("{not json", encoding="utf-8")
    target = tmp_path / "menu.json"
    assert agent_worktree.refresh_menu(broken, target) is None
    assert not target.exists()


def test_a_dismissed_picker_runs_nothing_and_exits_zero(capsys):
    """Ahead of argparse: a cancel reported as a usage error is a red icon, a toast and a
    `logs/` artifact for a run the user called off."""
    code = agent_worktree.main(["remove", "--picks=${input:worktreeRow}"], FakeRun())
    assert code == 0
    assert "cancelled" in capsys.readouterr().out


def test_ticking_only_the_sentinel_runs_nothing(workspace, capsys):
    code = agent_worktree.main(
        ["remove", f"--picks=devkit:{aw.NOTHING}", "--workspace", str(workspace)], FakeRun()
    )
    assert code == 0
    assert "nothing ticked" in capsys.readouterr().out


def test_escaping_the_base_picker_cuts_nothing(workspace, capsys):
    """The `new` verb's checkout comes from the same token as its base, so an empty pick
    has no checkout to run in — which must read as a cancel, not as a default."""
    code = agent_worktree.main(
        ["new", "--pick=", "--slug=x", "--workspace", str(workspace)], FakeRun()
    )
    assert code == 0
    assert "nothing to do" in capsys.readouterr().out


def test_the_render_lists_every_checkout_including_the_empty_ones():
    """Same reason the menu draws a sentinel row: a checkout that silently drops out when
    it is empty is indistinguishable from one the scan could not reach."""
    text = agent_worktree.render({"devkit": [tree()], "carameli": []})
    assert "devkit: 1 worktree(s)" in text
    assert "carameli: no worktree(s)" in text
    assert "topic -- agent/topic-0905 -- clean and pushed" in text
