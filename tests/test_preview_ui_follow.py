"""Tests for `scripts/preview-ui-follow.py`.

Every git call the script makes goes through one injected runner, so nothing here
fetches, checks out, or needs a repository: `FakeGit` answers the four questions the
script asks and records the argv it was asked with. That is the whole surface -- what
this script decides is *which* git commands to run and *what to say about it*, and both
are visible in the recording.

The one thing deliberately not faked is the copy directory: `copies()` asks the
filesystem whether a served copy exists, because that existence check is how a
box-served preview is excluded, and a test that stubbed it would assert nothing.
"""

from __future__ import annotations

import types
from pathlib import Path

from support import load_script

follow = load_script("scripts/preview-ui-follow.py")

HEAD = "a" * 40
TIP = "b" * 40


class FakeGit:
    """The four git calls the script makes, answered from constructor arguments.

    `fail` names one subcommand that should exit non-zero -- "fetch", "checkout",
    "rev-parse" -- which is how the error paths are reached without a real repository
    in a broken state.
    """

    def __init__(self, head=HEAD, tip=TIP, dirty="", fail=""):
        self.head, self.tip, self.dirty, self.fail = head, tip, dirty, fail
        self.calls: list[list[str]] = []

    def __call__(self, argv, capture_output=False, text=False):
        self.calls.append(list(argv))
        args = argv[3:]
        broke = bool(self.fail) and args[0] == self.fail
        out = ""
        if args[0] == "rev-parse":
            out = self.head if args[-1] == "HEAD" else self.tip
        elif args[0] == "status":
            out = self.dirty
        return types.SimpleNamespace(
            stdout="" if broke else out,
            stderr="fatal: it did not work\nsecond line" if broke else "",
            returncode=1 if broke else 0,
        )

    def ran(self, subcommand: str) -> bool:
        return any(call[3] == subcommand for call in self.calls)


# No return annotation: `follow` is a module object loaded at runtime, so `follow.Copy`
# is a value to mypy and not a name it can resolve in an annotation position.
def make_copy(tmp_path: Path, project="roguelike", ref="main"):
    path = tmp_path / follow.host.UI_PREVIEWS_DIR_NAME / project / follow.host.ref_slug(ref)
    path.mkdir(parents=True, exist_ok=True)
    return follow.Copy(project=project, ref=ref, path=path)


def entry(project="roguelike", ref="main", pid=1) -> dict:
    return {"pid": pid, "project": project, "ref": ref, "port": 5300}


def test_copies_finds_the_served_directory_for_a_recorded_server(tmp_path):
    made = make_copy(tmp_path)
    found = follow.copies(tmp_path, [entry()])
    assert [copy.path for copy in found] == [made.path]
    assert found[0].ref == "main"


def test_a_ref_with_a_slash_resolves_through_the_host_s_own_slug(tmp_path):
    made = make_copy(tmp_path, ref="agent/comic-book-ui-0820")
    found = follow.copies(tmp_path, [entry(ref="agent/comic-book-ui-0820")])
    assert [copy.path for copy in found] == [made.path]


def test_a_box_served_preview_has_no_copy_and_is_skipped(tmp_path):
    """Its registry row is real; there is deliberately nothing under `.ui-previews`."""
    assert follow.copies(tmp_path, [entry(ref="agent/x")]) == []


def test_two_servers_on_one_ref_share_a_copy_and_are_synced_once(tmp_path):
    make_copy(tmp_path)
    found = follow.copies(tmp_path, [entry(pid=1), entry(pid=2)])
    assert len(found) == 1


def test_an_entry_missing_its_project_or_ref_is_ignored(tmp_path):
    make_copy(tmp_path)
    assert follow.copies(tmp_path, [{"pid": 3, "port": 5300}]) == []


def test_every_git_call_addresses_the_copy_rather_than_the_process_cwd():
    """`-C <copy>` is the whole reason this can run from anywhere: a follower started in
    the workspace root would otherwise fetch and check out the workspace's own repo."""
    fake = FakeGit()
    follow.git(Path("x"), ("status",), fake)
    assert fake.calls[-1][:3] == ["git", "-C", "x"]


def test_first_line_prefers_stderr_and_keeps_only_its_first_line():
    result = types.SimpleNamespace(stdout="ignored", stderr="fatal: it did not work\nsecond")
    assert follow.first_line(result) == "fatal: it did not work"


def test_first_line_falls_back_to_stdout_and_then_to_a_placeholder():
    """A git that fails silently still has to produce a report line that reads."""
    assert follow.first_line(types.SimpleNamespace(stdout="said\nmore", stderr="")) == "said"
    assert follow.first_line(types.SimpleNamespace(stdout="", stderr="")) == "no output"


def test_head_of_reads_the_served_commit_and_is_empty_when_git_will_not_say(tmp_path):
    copy = make_copy(tmp_path)
    assert follow.head_of(copy, FakeGit()) == HEAD
    assert follow.head_of(copy, FakeGit(fail="rev-parse")) == ""


def test_tip_of_fetches_first_and_reads_the_ref_that_fetch_just_brought_down(tmp_path):
    """`FETCH_HEAD`, not `origin/<ref>`: the latter can be another run's leftover."""
    fake = FakeGit()
    assert follow.tip_of(make_copy(tmp_path), fake) == (TIP, "")
    assert [call[3] for call in fake.calls] == ["fetch", "rev-parse"]
    assert fake.calls[-1][-1] == "FETCH_HEAD"


def test_tip_of_says_which_half_of_the_fetch_and_read_failed(tmp_path):
    copy = make_copy(tmp_path)
    assert follow.tip_of(copy, FakeGit(fail="fetch"))[1].startswith("could not fetch origin main")
    assert follow.tip_of(copy, FakeGit(fail="rev-parse"))[1].startswith(
        "fetched origin main but could not read it"
    )


def test_is_dirty_treats_a_git_that_will_not_answer_as_dirty(tmp_path):
    """The refusal has to fail safe: an unreadable status is not a licence to checkout."""
    copy = make_copy(tmp_path)
    assert follow.is_dirty(copy, FakeGit()) is False
    assert follow.is_dirty(copy, FakeGit(dirty=" M frontend/src/App.tsx")) is True
    assert follow.is_dirty(copy, FakeGit(fail="status")) is True


def test_a_branch_that_moved_is_checked_out_and_reported(tmp_path):
    git = FakeGit()
    line = follow.advance(make_copy(tmp_path), git)
    assert ["checkout", "--detach", TIP] == git.calls[-1][3:]
    assert line.startswith("[followed] roguelike main:")
    assert "aaaaaaa -> bbbbbbb" in line
    assert "reload the tab" in line


def test_a_branch_that_has_not_moved_says_nothing_and_moves_nothing(tmp_path):
    git = FakeGit(tip=HEAD)
    assert follow.advance(make_copy(tmp_path), git) == ""
    assert not git.ran("checkout")


def test_a_copy_with_local_edits_is_reported_and_never_checked_out(tmp_path):
    """The `?edit=1` layout editor saves into the serving copy; a checkout would eat it."""
    git = FakeGit(dirty=" M frontend/src/App.tsx")
    line = follow.advance(make_copy(tmp_path), git)
    assert not git.ran("checkout")
    assert line.startswith("[held] roguelike main has local edits")
    assert str(tmp_path) in line


def test_a_fetch_that_fails_is_reported_without_touching_the_copy(tmp_path):
    git = FakeGit(fail="fetch")
    line = follow.advance(make_copy(tmp_path), git)
    assert not git.ran("checkout")
    assert line == "[warn] roguelike main: could not fetch origin main -- fatal: it did not work"


def test_a_failed_checkout_names_the_commit_it_could_not_reach(tmp_path):
    git = FakeGit(fail="checkout")
    line = follow.advance(make_copy(tmp_path), git)
    assert line.startswith("[warn] roguelike main: could not move onto bbbbbbb")


def test_untracked_files_do_not_count_as_local_edits(tmp_path):
    """Vite writes caches into the tree; treating those as an edit would stop following
    forever, on a copy nobody had touched."""
    git = FakeGit()
    follow.advance(make_copy(tmp_path), git)
    status = next(call for call in git.calls if call[3] == "status")
    assert "--untracked-files=no" in status


def test_a_repeated_refusal_is_said_once_and_a_change_of_state_is_said_again(tmp_path):
    """The latch: 180 identical lines an hour would bury the ones worth reading."""
    copy = make_copy(tmp_path)
    seen: dict[Path, str] = {}
    dirty = FakeGit(dirty=" M a.ts")
    assert len(follow.sync_all([copy], seen, dirty)) == 1
    assert follow.sync_all([copy], seen, dirty) == []
    moved = follow.sync_all([copy], seen, FakeGit())
    assert len(moved) == 1 and moved[0].startswith("[followed]")


def test_a_copy_that_goes_quiet_is_forgotten_so_its_next_refusal_is_reported(tmp_path):
    copy = make_copy(tmp_path)
    seen: dict[Path, str] = {}
    follow.sync_all([copy], seen, FakeGit(dirty=" M a.ts"))
    follow.sync_all([copy], seen, FakeGit(tip=HEAD))
    assert seen == {}
    assert len(follow.sync_all([copy], seen, FakeGit(dirty=" M a.ts"))) == 1


def test_follow_once_syncs_every_copy_and_returns_without_sleeping(tmp_path, monkeypatch, capsys):
    make_copy(tmp_path)
    monkeypatch.setattr(follow.host, "read_registry", lambda *a, **k: [entry()])

    def never(seconds):
        raise AssertionError("--once must not wait")

    git = FakeGit()
    assert follow.follow(tmp_path, 20.0, once=True, sleep=never, run=git) == 0
    assert "[followed] roguelike main" in capsys.readouterr().out


def test_the_loop_re_reads_the_registry_so_a_preview_opened_later_is_picked_up(
    tmp_path, monkeypatch
):
    """Why the registry is read per tick rather than captured: this is meant to be
    started once and left, across every preview being stopped and others opened."""
    make_copy(tmp_path)
    ticks = [[], [entry()]]
    monkeypatch.setattr(follow.host, "read_registry", lambda *a, **k: ticks.pop(0))

    def stop_after_two(seconds):
        if not ticks:
            raise KeyboardInterrupt

    git = FakeGit()
    try:
        follow.follow(tmp_path, 0.0, sleep=stop_after_two, run=git)
    except KeyboardInterrupt:
        pass
    assert git.ran("checkout")


def test_the_parser_defaults_the_interval_to_the_follow_period():
    """The default is the constant rather than a repeated literal: the comment on
    `FOLLOW_SECONDS` is where the twenty seconds is argued for, and a second copy here
    would be the thing that disagrees with it."""
    args = follow.build_parser().parse_args([])
    assert args.interval == follow.FOLLOW_SECONDS
    assert args.workspace is None
    assert args.once is False


def test_the_parser_accepts_a_single_pass_and_a_faster_interval():
    args = follow.build_parser().parse_args(["--once", "--interval", "5"])
    assert args.once is True
    assert args.interval == 5.0


def test_main_refuses_a_workspace_that_is_not_there(tmp_path, capsys):
    assert follow.main(["--workspace", str(tmp_path / "nope.code-workspace")]) == 2
    assert "no workspace registry" in capsys.readouterr().out
