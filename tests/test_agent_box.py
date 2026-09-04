"""Tests for the four manual verbs that replace the agent-branch hooks.

Everything that decides is pure and is driven directly; everything that acts takes a
runner and is driven with a fake one. The bar the module has to clear is that a verb never
does half its work: `ship` that cannot commit must not push, and `delete` that cannot reap
must not delete the branch -- both are asserted below by counting what the fake runner was
asked to do.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from support import load_script
from support import worktree as shared_worktree

box = load_script("scripts/agent-box.py")


def test_this_module_shares_the_one_worktree_module_rather_than_loading_a_second():
    """`_loader.load_by_path` overwrites `sys.modules[name]`, so reaching `worktree` that
    way would give the process a second copy of it -- and a test that patches one copy
    would then be asserting about the other.

    That is not hypothetical: importing this module first made two `test_preview_task.py`
    tests fail, and only when the whole suite ran. Asserted on identity because that is the
    property, and because the symptom appears somewhere else entirely."""
    assert box.worktree is shared_worktree


class FakeRunner:
    """Records argv and answers from a queue keyed by the first distinctive token."""

    def __init__(self, answers: dict[str, subprocess.CompletedProcess] | None = None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for key, answer in self.answers.items():
            if key in argv:
                return answer
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ran(self, token: str) -> bool:
        return any(token in call for call in self.calls)


def ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def bad(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", stderr)


# --- the terminal ---------------------------------------------------------------------


def test_the_agent_tab_attaches_to_the_window_the_operator_is_looking_at():
    """`-w 0` is "most recently used window, create one only if there is none", which is
    the ask. `resume-sessions.py` uses `-w -1` because it opens a *set* of tabs that belong
    together; one agent in one box is one tab and belongs where the operator already is."""
    argv = box.wt_argv("agent/thing-0903", Path("C:/boxes/x"), "claude")
    assert argv[:3] == ["-w", "0", "new-tab"]
    assert "-NoExit" in argv
    assert argv[-1] == "claude"
    assert argv[argv.index("-d") + 1] == str(Path("C:/boxes/x"))


def test_the_kill_switch_is_exported_into_the_tab_when_it_is_on():
    """Claude reads `env` out of the user settings file. Codex reads no settings file of
    ours, so without this a Codex session in a box would run hooks the operator had
    switched off everywhere else."""
    assert box.agent_command("codex", True).startswith("$env:DEVKIT_HOOKS_OFF='1'; ")
    assert box.agent_command("codex", True).endswith("codex")


def test_nothing_is_exported_when_the_harness_is_running():
    assert box.agent_command("claude", False) == "claude"


def test_asking_for_no_agent_opens_no_terminal(capsys):
    assert box.open_agent("none", Path("C:/boxes/x"), "agent/x", runner=_never) == box.EXIT_OK
    assert "no agent requested" in capsys.readouterr().out


def test_a_machine_without_windows_terminal_is_told_what_to_type(monkeypatch, capsys):
    monkeypatch.setattr(box.shutil, "which", lambda _name: None)
    monkeypatch.setattr(box.harness_switch, "hooks_are_off", lambda *_a: False)
    assert box.open_agent("claude", Path("C:/boxes/x"), "agent/x", runner=_never) == box.EXIT_OK
    assert "run this yourself" in capsys.readouterr().out


def _never(*_args, **_kwargs):
    raise AssertionError("nothing should have been spawned")


# --- picking a box ---------------------------------------------------------------------


def candidates(*names: str) -> list:
    return [box.Candidate(name=f"p--{n}", branch=f"agent/{n}", path=f"C:/boxes/{n}") for n in names]


def test_a_named_branch_skips_the_menu():
    chosen = box.choose(candidates("a", "b"), "agent/b", "ship", reader=_never)
    assert chosen is not None and chosen.branch == "agent/b"


def test_a_box_can_be_named_by_its_box_name_too():
    chosen = box.choose(candidates("a"), "p--a", "ship", reader=_never)
    assert chosen is not None and chosen.name == "p--a"


def test_a_branch_with_no_live_box_is_refused_rather_than_guessed(capsys):
    assert box.choose(candidates("a"), "agent/nope", "ship", reader=_never) is None
    assert "no live box" in capsys.readouterr().err


def test_a_single_box_needs_no_question(capsys):
    chosen = box.choose(candidates("only"), "", "ship", reader=_never)
    assert chosen is not None and chosen.branch == "agent/only"
    assert "One box" in capsys.readouterr().out


def test_the_menu_is_numbered_from_one():
    text = box.candidate_menu(candidates("a", "b"), "ship")
    assert "1. agent/a" in text and "2. agent/b" in text


def test_the_menu_shows_an_open_pr_so_a_second_push_is_not_a_surprise():
    with_pr = [box.Candidate("p--a", "agent/a", "C:/x", pr="42")]
    assert "[PR 42]" in box.candidate_menu(with_pr, "ship")


@pytest.mark.parametrize("raw", ["", " ", "0", "3", "-1", "two", "1x"])
def test_an_answer_that_is_not_a_row_chooses_nothing(raw):
    """Blank included: an operator who hits enter at a destructive prompt meant "no", and
    defaulting to the first row is how that becomes "yes, the first one"."""
    assert box.parse_choice(raw, 2) == -1


def test_a_valid_answer_is_one_based():
    assert box.parse_choice("2", 3) == 1


def test_a_rejected_answer_picks_nothing(capsys):
    assert box.choose(candidates("a", "b"), "", "ship", reader=lambda: "9") is None
    assert "nothing chosen" in capsys.readouterr().err


def test_no_boxes_at_all_says_so(capsys):
    assert box.choose([], "", "ship", reader=_never) is None
    assert "no box to ship" in capsys.readouterr().out


# --- what `ship` may be offered ---------------------------------------------------------


@pytest.fixture
def leases(monkeypatch):
    """`boxes_for`'s unfiltered half, stubbed at the lease table it reads."""

    def _boxes(*names: str) -> None:
        monkeypatch.setattr(
            box.worktree,
            "live_boxes",
            lambda _root: {
                f"p--{n}": type(
                    "B", (), {"name": f"p--{n}", "branch": f"agent/{n}", "project": "p"}
                )
                for n in names
            },
        )
        monkeypatch.setattr(box.worktree, "box_path", lambda _root, name: Path(f"C:/boxes/{name}"))
        monkeypatch.setattr(box.devkit_project, "resolve_project", lambda *_a, **_k: Path("C:/p"))
        monkeypatch.setattr(box.worktree, "known_projects", lambda _w: ["p"])

    return _boxes


def test_only_this_projects_boxes_are_offered(leases, monkeypatch):
    leases("a", "b")
    monkeypatch.setattr(box.worktree, "live_boxes", lambda _root: {})
    assert box.boxes_for("p", Path("C:/ws.code-workspace")) == []


def test_a_merged_branch_is_never_offered(leases, monkeypatch):
    leases("done", "live")
    states = {"agent/done": ("7", True), "agent/live": ("", False)}
    monkeypatch.setattr(box, "pr_state_for", lambda _s, branch, _r=None: states[branch])
    offered = box.boxes_for("p", Path("C:/ws.code-workspace"), unmerged_only=True)
    assert [c.branch for c in offered] == ["agent/live"]


def test_an_open_pr_stays_on_the_list_and_is_labelled(leases, monkeypatch):
    """Pushing another commit to an open PR is the ordinary way to answer review; hiding
    it would send the operator looking for a verb that does not exist."""
    leases("open")
    monkeypatch.setattr(box, "pr_state_for", lambda *_a: ("11", False))
    offered = box.boxes_for("p", Path("C:/ws.code-workspace"), unmerged_only=True)
    assert [c.pr for c in offered] == ["11"]
    assert "[PR 11]" in offered[0].label()


def test_the_unfiltered_list_asks_github_nothing(leases, monkeypatch):
    """`attach` and `delete` act on a box whatever its PR says, so the lookup that fills
    in `pr` is skipped rather than made and discarded."""
    leases("a")
    monkeypatch.setattr(box, "pr_state_for", _never)
    assert [c.branch for c in box.boxes_for("p", Path("C:/ws.code-workspace"))] == ["agent/a"]


def test_the_pr_lookup_reads_the_first_row():
    runner = FakeRunner({"pr": ok(json.dumps([{"number": 12, "state": "MERGED"}]))})
    assert box.pr_state_for(Path("C:/p"), "agent/x", runner) == ("12", True)


@pytest.mark.parametrize("answer", [bad(), ok("not json"), ok("[]")])
def test_gh_failing_reads_as_not_shipped(answer):
    """No answer must not read as "already merged": hiding a box because GitHub was
    unreachable is how work gets left in one."""
    assert box.pr_state_for(Path("C:/p"), "agent/x", FakeRunner({"pr": answer})) == ("", False)


def test_gh_missing_entirely_reads_as_not_shipped():
    def explode(*_a, **_k):
        raise OSError("no gh")

    assert box.pr_state_for(Path("C:/p"), "agent/x", explode) == ("", False)


# --- the ship argv --------------------------------------------------------------------


def test_the_commit_bypasses_the_pre_commit_gate():
    """The gate's refusal leaves the work uncommitted in a box `reconcile` may reap. The
    same rules run in `PR Gate`, on a branch that exists."""
    assert box.commit_argv("m")[:3] == ["git", "commit", "--no-verify"]


def test_the_push_bypasses_the_pre_push_policy_and_sets_upstream():
    assert box.push_argv("agent/x") == ["git", "push", "--no-verify", "-u", "origin", "agent/x"]


def test_the_branch_policy_is_skipped_in_the_environment_too():
    assert box.ship_env({})["DEVKIT_SKIP_BRANCH_POLICY"] == "1"


def test_the_environment_is_a_copy_not_the_real_one():
    base = {"PATH": "x"}
    box.ship_env(base)
    assert "DEVKIT_SKIP_BRANCH_POLICY" not in base


def test_only_the_deterministic_fixers_run():
    """A findings pass whose output nobody is allowed to act on is a wall of text before
    every ship. Findings belong to the PR gate."""
    argvs = box.lint_fix_argvs("py", Path("C:/box"))
    assert [a[2:4] for a in argvs] == [["ruff", "format"], ["ruff", "check"]]
    assert "--fix" in argvs[1]


def test_the_pr_targets_the_default_branch():
    assert box.pr_argv("T", "B", "main")[:5] == ["gh", "pr", "create", "--base", "main"]


@pytest.mark.parametrize(
    ("branch", "subject"),
    [
        ("agent/fix-the-thing-0903", "Fix the thing"),
        ("agent/0903", "Update"),
        ("codex/add-base-flag-1201", "Add base flag"),
        ("loose", "Loose"),
    ],
)
def test_the_commit_subject_comes_from_the_branch_the_operator_named(branch, subject):
    """Nobody is here to write one, and a subject that says "wip" is worse than the name
    the operator chose when they cut the branch."""
    assert box.commit_message(branch, 3).splitlines()[0] == subject


def test_the_commit_body_counts_the_paths():
    assert "3 file(s) changed" in box.commit_message("agent/x-0903", 3)


# --- the verbs end to end ---------------------------------------------------------------


def test_ship_stops_before_pushing_when_the_commit_fails(monkeypatch, capsys):
    monkeypatch.setattr(box, "boxes_for", lambda *_a, **_k: candidates("x"))
    monkeypatch.setattr(box.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(box.project_python, "interpreter", lambda *_a, **_k: "py")
    runner = FakeRunner({"status": ok(" M a.py"), "commit": bad()})
    assert box.ship("p", Path("C:/ws"), "agent/x", runner=runner) == box.EXIT_FAILED
    assert not runner.ran("push")
    assert "the commit failed" in capsys.readouterr().err


def test_ship_with_nothing_uncommitted_still_pushes(monkeypatch, capsys):
    """A box whose work is committed but unpushed is the exact state a reaped box loses,
    so "nothing to commit" must not mean "nothing to do"."""
    monkeypatch.setattr(
        box, "boxes_for", lambda *_a, **_k: [box.Candidate("p--x", "agent/x", "C:/b", pr="9")]
    )
    monkeypatch.setattr(box.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(box.project_python, "interpreter", lambda *_a, **_k: "py")
    runner = FakeRunner({"status": ok("")})
    assert box.ship("p", Path("C:/ws"), "agent/x", runner=runner) == box.EXIT_OK
    assert runner.ran("push")
    assert not runner.ran("commit")
    out = capsys.readouterr().out
    assert "PR 9 already open" in out


def test_ship_says_how_to_get_a_reaped_box_back(monkeypatch, capsys):
    monkeypatch.setattr(box, "boxes_for", lambda *_a, **_k: candidates("x"))
    monkeypatch.setattr(box.Path, "is_dir", lambda _self: False)
    assert box.ship("p", Path("C:/ws"), "agent/x", runner=_never) == box.EXIT_FAILED
    assert "worktree.py resume" in capsys.readouterr().err


def test_delete_leaves_the_branch_alone_when_the_reap_fails(monkeypatch, capsys):
    monkeypatch.setattr(box, "boxes_for", lambda *_a: candidates("x"))
    runner = FakeRunner({"reap": bad()})
    assert box.delete("p", Path("C:/ws"), "agent/x", runner=runner) == box.EXIT_FAILED
    assert not runner.ran("branch")
    assert "untouched" in capsys.readouterr().err


def test_delete_forces_the_reap(monkeypatch, capsys):
    """This verb exists to abandon work; a reap that refuses because the box is dirty is
    refusing the thing that was asked for."""
    monkeypatch.setattr(box, "boxes_for", lambda *_a: candidates("x"))
    monkeypatch.setattr(box.devkit_project, "resolve_project", lambda *_a, **_k: Path("C:/p"))
    monkeypatch.setattr(box.worktree, "known_projects", lambda _w: ["p"])
    runner = FakeRunner()
    assert box.delete("p", Path("C:/ws"), "agent/x", runner=runner) == box.EXIT_OK
    assert runner.ran("--force")
    assert runner.ran("-D")
    assert "origin's copy, if any, is untouched" in capsys.readouterr().out


def test_spawn_opens_nothing_when_the_box_was_not_cut(capsys):
    """Half a spawn is worse than none: an agent opened in a directory that does not exist
    reads as a broken CLI rather than as a refused branch name."""
    runner = FakeRunner({"new": bad("origin/nope does not exist")})
    assert (
        box.spawn("p", Path("C:/ws"), "topic", "nope", "claude", runner=runner) == box.EXIT_FAILED
    )
    assert "nothing to open" in capsys.readouterr().err


def test_spawn_passes_the_base_through_and_opens_the_agent(monkeypatch, capsys):
    plan = {"path": "C:/boxes/p--x", "box": {"name": "p--x", "branch": "agent/x-0903"}, "notes": []}
    runner = FakeRunner({"new": ok(json.dumps(plan))})
    opened: list[tuple] = []
    monkeypatch.setattr(box, "open_agent", lambda *args, **_k: opened.append(args) or box.EXIT_OK)
    assert box.spawn("p", Path("C:/ws"), "x", "release/1.2", "codex", runner=runner) == box.EXIT_OK
    assert runner.calls[0][runner.calls[0].index("--base") + 1] == "release/1.2"
    assert opened[0][:3] == ("codex", Path("C:/boxes/p--x"), "agent/x-0903")


def test_spawn_omits_the_base_flag_when_the_default_branch_is_wanted():
    runner = FakeRunner({"new": bad()})
    box.spawn("p", Path("C:/ws"), "x", "", "none", runner=runner)
    assert "--base" not in runner.calls[0]


def test_attach_opens_the_agent_in_the_box_it_was_given(monkeypatch):
    """The fourth task is literally the tail of the first, so it must reach the same
    function -- two copies would be two answers to which window the agent opens in."""
    monkeypatch.setattr(box, "boxes_for", lambda *_a, **_k: candidates("x"))
    opened: list[tuple] = []
    monkeypatch.setattr(box, "open_agent", lambda *args, **_k: opened.append(args) or box.EXIT_OK)
    assert box.attach("p", Path("C:/ws"), "agent/x", "codex", runner=_never) == box.EXIT_OK
    assert opened[0][:3] == ("codex", Path("C:/boxes/x"), "agent/x")


def test_attach_opens_nothing_when_no_box_was_chosen(monkeypatch):
    monkeypatch.setattr(box, "boxes_for", lambda *_a, **_k: [])
    monkeypatch.setattr(box, "open_agent", _never)
    assert box.attach("p", Path("C:/ws"), "", "claude", runner=_never) == box.EXIT_FAILED


def test_a_missing_workspace_file_is_a_usage_error(tmp_path, capsys):
    argv = ["ship", "--project", "p", "--workspace", str(tmp_path / "absent.code-workspace")]
    assert box.main(argv) == box.EXIT_USAGE
    assert "no workspace file" in capsys.readouterr().err
