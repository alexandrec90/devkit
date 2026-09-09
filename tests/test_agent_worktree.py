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
picker_rows = load_script("scripts/picker_rows.py")
picker_scan = load_script("scripts/picker_scan.py")
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


def test_a_missing_registry_is_a_usage_error_rather_than_a_traceback(tmp_path, capsys):
    """The picker runs this; a traceback would reach the quick-pick as no options at all,
    which is indistinguishable from a machine with no worktrees."""
    missing = tmp_path / "nothing.code-workspace"
    assert agent_worktree.main(["rows", "--workspace", str(missing)]) == agent_worktree.EXIT_USAGE
    assert "no workspace file" in capsys.readouterr().err


def test_a_registry_that_cannot_be_parsed_still_draws_the_sentinel(tmp_path, capsys):
    """`sweep.parse_workspace` answers a file it cannot parse with an empty list rather
    than a raise, so "no checkouts" is what a truncated registry looks like from here.
    The cached menu refused to write in that state, because overwriting a good file with
    an empty one outlived the bad read. There is no file to protect now, so the honest
    answer is the row that says there is nothing -- and it is one click, not a quarter of
    an hour, from being asked again."""
    broken = tmp_path / "truncated.code-workspace"
    broken.write_text("{not json", encoding="utf-8")
    assert agent_worktree.main(["rows", "--workspace", str(broken)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1
    assert printed[0].split(picker_rows.FIELD_SEP)[0] == picker_rows.NOTHING


def test_the_two_picker_verbs_print_their_own_list_and_nothing_else(tmp_path, monkeypatch, capsys):
    """One scan answers both questions, and each verb draws only its half: a base branch
    in the delete list is a row that would refuse, and vice versa."""
    workspace = tmp_path / "alex.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    tree = agent_worktree.aw.Tree("box", "C:/w/box", "agent/x", 0, 0)
    monkeypatch.setattr(
        agent_worktree,
        "scan",
        lambda ws, projects=None: (
            {"devkit": [tree]},
            {"devkit": [("main", "the default branch")]},
        ),
    )

    assert agent_worktree.main(["rows", "--workspace", str(workspace)]) == 0
    drawn = capsys.readouterr().out.splitlines()
    assert [line.split(picker_rows.FIELD_SEP)[0] for line in drawn] == ["devkit:box"]

    assert agent_worktree.main(["bases", "--workspace", str(workspace)]) == 0
    drawn = capsys.readouterr().out.splitlines()
    assert [line.split(picker_rows.FIELD_SEP)[0] for line in drawn] == ["devkit:main"]


def test_cutting_and_removing_leave_no_menu_to_keep_warm(tmp_path):
    """The reversion check for deleting the second writer: `new` and `remove` used to
    rewrite the file as they finished, because a quarter of an hour was a long time to be
    unable to undo a click. A live list needs no such catch-up."""
    assert not hasattr(agent_worktree, "refresh_menu")
    assert not hasattr(agent_worktree, "MENU_CACHE")


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


# --- the checkout stage -------------------------------------------------------


TREE = aw.Tree("box", "C:/w/box", "agent/x", 0, 0)
SCANNED = ({"devkit": [TREE], "carameli": []}, {"devkit": [("main", "the default branch")]})


@pytest.fixture
def two_stage(tmp_path, monkeypatch):
    """A workspace, a stubbed scan, and scan writes kept out of the repo's `logs/`."""
    monkeypatch.setattr(picker_scan, "SCANS_DIR", tmp_path / "scans")
    monkeypatch.setattr(agent_worktree.picker_scan, "SCANS_DIR", tmp_path / "scans")
    workspace = tmp_path / "alex.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(agent_worktree, "scan", lambda _ws, projects=None: SCANNED)
    return workspace


def values(printed: list[str]) -> list[str]:
    return [line.split(picker_rows.FIELD_SEP)[0] for line in printed]


def test_the_two_checkout_verbs_draw_a_checkout_per_row(two_stage, capsys):
    assert agent_worktree.main(["tree-projects", "--workspace", str(two_stage)]) == 0
    drawn = capsys.readouterr().out.splitlines()
    assert [line.split(picker_rows.FIELD_SEP)[1] for line in drawn] == ["devkit", "carameli"]

    assert agent_worktree.main(["base-projects", "--workspace", str(two_stage)]) == 0
    drawn = capsys.readouterr().out.splitlines()
    assert [line.split(picker_rows.FIELD_SEP)[1] for line in drawn] == ["devkit"]


def test_each_checkout_verb_records_the_scan_its_row_verb_reads(two_stage, capsys):
    """The handoff, end to end: the token a checkout row carries names a write that the
    row verb can then serve without scanning again."""
    assert agent_worktree.main(["tree-projects", "--workspace", str(two_stage)]) == 0
    token = picker_scan.parse_projects(values(capsys.readouterr().out.splitlines())[0])[1]

    assert (
        agent_worktree.main(["rows", f"--checkouts=devkit@{token}", "--workspace", str(two_stage)])
        == 0
    )
    assert values(capsys.readouterr().out.splitlines()) == ["devkit:box"]


def test_the_two_verbs_record_under_different_names(two_stage, capsys):
    """Two tasks, each clicked on its own, so neither can rely on a write the other
    made. A token from the base scan must not resolve against the tree scan."""
    assert agent_worktree.main(["base-projects", "--workspace", str(two_stage)]) == 0
    token = picker_scan.parse_projects(values(capsys.readouterr().out.splitlines())[0])[1]
    assert picker_scan.read(agent_worktree.SCAN_NAMES["trees"], token) is None
    assert picker_scan.read(agent_worktree.SCAN_NAMES["bases"], token) is not None


def test_a_token_that_names_no_scan_rescans_the_ticked_checkouts(two_stage, monkeypatch, capsys):
    asked = []

    def fake(_ws, projects=None):
        asked.append(projects)
        return SCANNED

    monkeypatch.setattr(agent_worktree, "scan", fake)
    assert (
        agent_worktree.main(["rows", "--checkouts=devkit@stale", "--workspace", str(two_stage)])
        == 0
    )
    assert asked == [["devkit"]]


def test_picked_rows_serves_a_recorded_scan_and_rescans_only_on_a_miss(
    two_stage, monkeypatch, capsys
):
    """The one function behind both row verbs, called directly: a live token is served
    from the scan the checkout stage recorded, a token naming no scan rescans only the
    ticked checkouts, and an empty pick is the verb typed by hand, which scans the
    whole machine as it did before there was a first stage."""
    assert agent_worktree.main(["tree-projects", "--workspace", str(two_stage)]) == 0
    token = picker_scan.parse_projects(values(capsys.readouterr().out.splitlines())[0])[1]
    asked = []

    def fake(_ws, projects=None):
        asked.append(projects)
        return SCANNED

    monkeypatch.setattr(agent_worktree, "scan", fake)
    assert values(agent_worktree.picked_rows(two_stage, f"devkit@{token}", "trees")) == [
        "devkit:box"
    ]
    assert asked == []
    agent_worktree.picked_rows(two_stage, "devkit@stale", "trees")
    assert asked == [["devkit"]]
    agent_worktree.picked_rows(two_stage, "", "bases")
    assert asked == [["devkit"], None]


def test_ticking_only_empty_checkouts_draws_the_sentinel(two_stage, capsys):
    assert agent_worktree.main(["tree-projects", "--workspace", str(two_stage)]) == 0
    token = picker_scan.parse_projects(values(capsys.readouterr().out.splitlines())[0])[1]
    assert (
        agent_worktree.main(
            ["rows", f"--checkouts=carameli@{token}", "--workspace", str(two_stage)]
        )
        == 0
    )
    assert values(capsys.readouterr().out.splitlines()) == [picker_rows.NOTHING]


# --- the guard on the two stages disagreeing ----------------------------------


def test_a_pick_from_a_checkout_the_first_stage_did_not_return_is_named():
    """Nothing in the two stages can produce this, so it is evidence the chain misfired
    -- and on THIS task a wrongly-filtered list is a list of things to destroy."""
    assert agent_worktree.strayed_picks([("roguelike", "box")], "devkit@tok") == ["roguelike"]
    assert agent_worktree.strayed_picks([("devkit", "box")], "devkit@tok") == []
    assert agent_worktree.strayed_picks([("devkit", "box")], "") == []


def test_a_stray_removal_pick_destroys_nothing(two_stage, monkeypatch, capsys):
    monkeypatch.setattr(
        agent_worktree, "remove", lambda *_a: pytest.fail("nothing may be destroyed")
    )
    code = agent_worktree.main(
        [
            "remove",
            "--picks=roguelike:box",
            "--checkouts=devkit@tok",
            "--workspace",
            str(two_stage),
        ],
        FakeRun(),
    )
    assert code == agent_worktree.EXIT_USAGE
    assert "roguelike" in capsys.readouterr().err


def test_a_stray_base_pick_cuts_nothing(two_stage, monkeypatch, capsys):
    monkeypatch.setattr(agent_worktree, "create", lambda *_a: pytest.fail("nothing may be cut"))
    code = agent_worktree.main(
        [
            "new",
            "--pick=roguelike:main",
            "--checkouts=devkit@tok",
            "--slug=x",
            "--agent=none",
            "--workspace",
            str(two_stage),
        ],
        FakeRun(),
    )
    assert code == agent_worktree.EXIT_USAGE
    assert "roguelike" in capsys.readouterr().err


def test_stray_report_names_the_checkouts_and_says_nothing_happened():
    text = agent_worktree.stray_report(["roguelike", "carameli"], "worktrees")
    assert "worktrees from roguelike, carameli" in text
    assert "nothing was done" in text
    assert "vscode-tasks.md" in text


def test_draw_sends_each_picker_verb_at_its_own_half_of_one_scan(two_stage):
    """Four verbs, two values between them: which half of the scan, and whether this is
    the stage that records or the stage that filters."""
    assert [
        line.split(picker_rows.FIELD_SEP)[1]
        for line in agent_worktree.draw(two_stage, "tree-projects", "")
    ] == ["devkit", "carameli"]
    assert [
        line.split(picker_rows.FIELD_SEP)[1]
        for line in agent_worktree.draw(two_stage, "base-projects", "")
    ] == ["devkit"]
    assert [
        line.split(picker_rows.FIELD_SEP)[0] for line in agent_worktree.draw(two_stage, "rows", "")
    ] == ["devkit:box"]
    assert [
        line.split(picker_rows.FIELD_SEP)[0] for line in agent_worktree.draw(two_stage, "bases", "")
    ] == ["devkit:main"]


def test_every_picker_verb_the_parser_takes_is_one_draw_can_answer():
    """The pairing that would otherwise fail at the click: a verb `main` routes into
    `draw` and `draw` has no entry for is a KeyError on somebody's quick-pick."""
    assert set(agent_worktree.PICKER_VERBS) == {"rows", "bases", "tree-projects", "base-projects"}
