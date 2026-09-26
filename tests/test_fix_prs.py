"""`scripts/fix-prs.py`: how a session opens, the planned path, and the CLI.

What *counts* as broken lives in `tests/test_broken_pr_menu.py` with the scan; what to
*send* is `tests/test_fix_plan.py`; what the gate *said* is `tests/test_gate_evidence.py`.
What is here is the acting half -- the worktree on a PR's head branch, the fresh branch
for a failure that has none, the tab or the background session -- and `main`, which
wires the four together and is what the task block's one remaining question reaches.

Every decision in the script is a pure function taking the shapes `gh` returns, so this
suite drives those directly and never a network. The ones that spawn take a runner, and
the tests for them assert the argv rather than the effect.

**Patch the module that owns the name.** `fix-prs.py` reaches the menu tier as
`menu.<name>` and the evidence as `gate_evidence.<name>`, so
`monkeypatch.setattr(fix_prs, "scan", ...)` binds nothing the code reads -- the stub goes
in and the real `gh` path runs anyway.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from support import REPO_ROOT, load_script

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import agent_models
import fix_ledger
import fix_plan

# `support.load_script` rather than `_loader.load_by_path`: the latter overwrites
# `sys.modules[name]`, so reaching a module that way would hand this process a second
# copy of one another suite has already loaded -- and it is the one this suite
# monkeypatches. It also costs no `sys.path` bootstrap here, so this file needs no
# file-wide `noqa` to sit under one.
fix_prs = load_script("scripts/fix-prs.py")
menu = load_script("scripts/broken_pr_menu.py")
evidence = fix_prs.gate_evidence
assert fix_prs.fix_plan is fix_plan, "one copy of the plan, or the stubs bind nothing"

NOW = _dt.datetime(2026, 9, 18, 12, 0, tzinfo=_dt.UTC)


def pr(**fields) -> dict:
    """An open, green, mergeable PR, overridden field by field."""
    base = {
        "number": 412,
        "title": "Teach the sweep about labels",
        "headRefName": "agent/sweep-labels-0904",
        "baseRefName": "main",
        "headRefOid": "abc123",
        "updatedAt": "2026-09-04T09:00:00Z",
        "url": "https://github.com/x/y/pull/412",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }
    base.update(fields)
    return base


def failure(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.PR,
        "project": "carameli",
        "number": 412,
        "title": "T",
        "url": "u/412",
        "head": "agent/sweep-labels-0904",
        "base": "main",
        "sha": "abc123",
        "reason": "1 check failing",
        "signature": ("tests/test_x.py::test_y",),
    }
    base.update(fields)
    return fix_plan.Failure(**base)


@pytest.fixture(autouse=True)
def no_evidence_fetch(monkeypatch):
    """The evidence tier is a network; here every PR's evidence is the PR itself."""
    monkeypatch.setattr(evidence, "read_pr", lambda _dir, found, _root: found)


# --- what the agent is told -------------------------------------------------------


def test_tab_safe_collapses_whitespace_and_leaves_semicolons_to_the_escaper():
    assert fix_prs.tab_safe(" a;\n b  c ") == "a; b c"


# --- the worktree -----------------------------------------------------------------


def fake_git(answers: dict[tuple[str, ...], tuple[int, str]], default=(1, "")):
    """A `git_for`-shaped callable answering from a table keyed by argv.

    Named without the module it comes from, and that is not an oversight:
    `untested_symbols.module_pattern` reads an attribute access on a module's name --
    anywhere in a test file, a docstring included -- as that file joining the module's
    corpus. `gh_for` is monkeypatched all over this suite and tested in none of it, so
    writing the prefix here would retire a real gap from the untested baseline as "now
    covered". That is the exact false negative that function's own docstring is about.
    """

    def git(*args: str):
        code, out = answers.get(tuple(args), default)
        return subprocess.CompletedProcess(list(args), code, stdout=out, stderr="")

    return git


class FakeRun:
    """A `subprocess.run` stand-in that records argv and answers 0."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def git_args(self) -> list[list[str]]:
        """Each git call with `git -C <dir>` stripped, so assertions read as the verb."""
        return [call[3:] for call in self.calls if call[:1] == ["git"]]


def listing(*entries: tuple[str, str]) -> str:
    """`git worktree list --porcelain` output for `(path, branch)` pairs."""
    blocks = [f"worktree {path}\nHEAD 1a2b3c4d\nbranch refs/heads/{on}" for path, on in entries]
    return "\n\n".join(blocks)


def checkout_listing(monkeypatch, tmp_path, *entries: tuple[str, str]) -> Path:
    """A checkout whose `git worktree list --porcelain` answers with `entries`."""
    checkout = tmp_path / "carameli"
    checkout.mkdir(exist_ok=True)
    monkeypatch.setattr(
        fix_prs.sweep,
        "git_for",
        lambda _path: fake_git({("worktree", "list", "--porcelain"): (0, listing(*entries))}),
    )
    return checkout


def test_a_worktree_already_on_that_branch_is_reused_rather_than_cut(monkeypatch, tmp_path):
    """This task's ordinary second click is on a PR whose worktree is still open from the
    first, and two worktrees on one branch is a state git will not hold -- so cutting
    again would fail on the very thing that means "ready"."""
    held = f"{(tmp_path / 'carameli').as_posix()}/.claude/worktrees/x"
    checkout = checkout_listing(monkeypatch, tmp_path, (held, "agent/x"))
    assert fix_prs.existing_tree(checkout, "agent/x") == (Path(held), "")


@pytest.mark.parametrize("location", ["carameli", "manual", ".worktrees/carameli--x"])
def test_a_branch_held_outside_the_tier_is_refused_with_the_directory_named(
    monkeypatch, tmp_path, location
):
    """Static checkouts, manual trees and unrecognized boxes still need a named refusal."""
    held = (tmp_path / location).as_posix()
    checkout = checkout_listing(monkeypatch, tmp_path, (held, "agent/x"))
    monkeypatch.setattr(fix_prs.worktree, "live_boxes", lambda root: {})
    tree, refused = fix_prs.existing_tree(checkout, "agent/x")
    assert tree is None
    assert held in refused
    assert fix_prs.aw.TIER_SUMMARY in refused


def test_a_codex_worktree_on_that_branch_is_reused_too(monkeypatch, tmp_path):
    """The reuse rule is about who cut the tree, not about where it sits. A Codex
    worktree of this checkout is as adoptable as a Claude one -- and refusing it would
    send the reader to "remove that worktree" for a tree the harness put them in."""
    home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    held = f"{home.as_posix()}/worktrees/2e51/carameli"
    checkout = checkout_listing(monkeypatch, tmp_path, (held, "agent/x"))
    assert fix_prs.existing_tree(checkout, "agent/x") == (Path(held), "")


@pytest.mark.parametrize("agent", ["codex", "claude", "claude-bg"])
def test_a_pr_in_a_live_devkit_box_opens_in_that_box(monkeypatch, tmp_path, agent):
    branch = "agent/auto/devkit-upgrade"
    box = fix_prs.worktree.Box(name="carameli--upgrade", project="carameli", branch=branch)
    held = fix_prs.worktree.box_path(tmp_path, box.name)
    checkout_listing(monkeypatch, tmp_path, (held.as_posix(), branch))
    monkeypatch.setattr(fix_prs.worktree, "live_boxes", lambda root: {box.name: box})
    monkeypatch.setattr(menu, "pr_view", lambda *_: pr(headRefName=branch, mergeable="CONFLICTING"))
    monkeypatch.setattr(fix_prs, "cut_tree", lambda *_: pytest.fail("reuse the existing box"))
    opened = []

    def record(launch, tree, *_a, **_k):
        opened.append((launch.cli, tree))
        return 0

    monkeypatch.setattr(fix_prs.agent_tabs, "open_agent", record)
    monkeypatch.setattr(fix_prs.agent_tabs, "launch_background", record)

    launch = agent_models.Launch(agent)
    assert fix_prs.run_one(menu.Pick("carameli", 412), tmp_path / "w.code-workspace", launch) == 0
    assert opened == [(launch.cli, held)]


@pytest.mark.parametrize("mismatch", ["project", "branch", "path", "missing"])
def test_a_box_must_match_the_checkout_branch_and_worktree(monkeypatch, tmp_path, mismatch):
    held = tmp_path / ".worktrees" / "carameli--upgrade"
    checkout = checkout_listing(monkeypatch, tmp_path, (held.as_posix(), "agent/x"))
    box = fix_prs.worktree.Box(
        name="carameli--other" if mismatch == "path" else held.name,
        project="other" if mismatch == "project" else "carameli",
        branch="agent/other" if mismatch == "branch" else "agent/x",
    )
    monkeypatch.setattr(
        fix_prs.worktree,
        "live_boxes",
        lambda root: {} if mismatch == "missing" else {box.name: box},
    )
    tree, refused = fix_prs.existing_tree(checkout, "agent/x")
    assert tree is None
    assert held.as_posix() in refused


def test_a_branch_nothing_holds_is_neither_a_tree_nor_a_refusal(monkeypatch, tmp_path):
    """The two empties are the case `cut_tree` exists for, and the launch path branches
    on the difference between them."""
    checkout = checkout_listing(monkeypatch, tmp_path, (str(tmp_path / "carameli"), "main"))
    assert fix_prs.existing_tree(checkout, "agent/x") == (None, "")


def test_git_that_cannot_list_the_worktrees_is_a_refusal_not_a_free_cut(monkeypatch, tmp_path):
    """Reading the empty answer as "nothing holds it" would send `cut_tree` at a branch
    that may already be checked out, which is the one thing this lookup exists to stop."""
    checkout = tmp_path / "carameli"
    checkout.mkdir(exist_ok=True)
    monkeypatch.setattr(fix_prs.sweep, "git_for", lambda _path: fake_git({}))
    tree, refused = fix_prs.existing_tree(checkout, "agent/x")
    assert tree is None
    assert refused


def cut_with(monkeypatch, tmp_path, *, local: bool, remote: bool = True):
    """`cut_tree` against a checkout whose refs answer as asked. Returns `(path, run)`."""
    checkout = tmp_path / "carameli"
    (checkout / ".claude" / "worktrees").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        fix_prs.sweep,
        "git_for",
        lambda _path: fake_git(
            {
                ("rev-parse", "--verify", "--quiet", "refs/heads/agent/x"): (0 if local else 1, ""),
                ("rev-parse", "--verify", "--quiet", "refs/remotes/origin/agent/x"): (
                    0 if remote else 1,
                    "",
                ),
            }
        ),
    )
    run = FakeRun()
    return fix_prs.cut_tree(checkout, "agent/x", run), run


def test_a_worktree_is_cut_in_the_tier_tracking_the_prs_own_remote_branch(monkeypatch, tmp_path):
    """The whole point of the task: the upstream is `origin/<head>`, so a bare push from
    the worktree lands where the PR is looking. The fetch first is `create`'s, and here it
    is also what makes the ref check below it mean anything."""
    path, run = cut_with(monkeypatch, tmp_path, local=False)
    fetch, add = run.git_args()
    assert fetch == ["fetch", "--quiet", "origin"]
    assert add == ["worktree", "add", "--track", "-b", "agent/x", str(path), "origin/agent/x"]
    assert path.parts[-3:] == (".claude", "worktrees", "x")


def test_a_branch_this_checkout_already_has_is_not_recreated_from_origin(monkeypatch, tmp_path):
    """It may carry commits no remote has -- a box reaped while its work was open leaves
    exactly that -- and re-cutting it from origin is the one move that discards them."""
    path, run = cut_with(monkeypatch, tmp_path, local=True)
    assert run.git_args()[1] == ["worktree", "add", str(path), "agent/x"]


def test_a_head_branch_origin_no_longer_has_is_reported_before_git_cuts(
    monkeypatch, tmp_path, capsys
):
    """The stale-scan case one layer below the launch path's state check: a fetch that
    found no such ref means there is nothing to cut from, and the message says which
    branch."""
    path, run = cut_with(monkeypatch, tmp_path, local=False, remote=False)
    assert path is None
    assert run.git_args() == [["fetch", "--quiet", "origin"]]
    assert "origin has no branch" in capsys.readouterr().err


def test_a_directory_name_the_tier_already_uses_does_not_collide(monkeypatch, tmp_path):
    """Two PRs whose head branches end in the same segment want two worktrees, and the
    second landing in the first one's directory is what the counter prevents."""
    (tmp_path / "carameli" / ".claude" / "worktrees" / "x").mkdir(parents=True)
    path, _run = cut_with(monkeypatch, tmp_path, local=False)
    assert path.name == "x-2"


def test_cutting_one_lands_where_the_delete_dropdown_scans(monkeypatch, tmp_path):
    """The delete menu has no file behind it -- `agent-worktree.py rows` scans
    `git worktree list --porcelain` when the picker opens and keeps what `aw.nested`
    calls nested. So the only thing that puts a PR's worktree in that list is cutting it
    under the checkout's own `.claude/worktrees/`, which is what this asserts. Cut it
    anywhere else and it is invisible to the dropdown and to its delete row."""
    checkout = tmp_path / "carameli"
    path, run = cut_with(monkeypatch, tmp_path, local=False)
    _fetch, add = run.git_args()
    assert add[:2] == ["worktree", "add"]
    porcelain = f"worktree {path.as_posix()}\nbranch refs/heads/agent/x\n"
    assert fix_prs.aw.nested(checkout, porcelain) == [(path.name, path.as_posix(), "agent/x")]


def test_a_git_refusal_yields_no_worktree_rather_than_a_path(monkeypatch, tmp_path):
    """The launch path turns None into an exit 1; a path to a directory git declined to
    create would turn it into an agent opened in nothing."""
    checkout = tmp_path / "carameli"
    (checkout / ".claude" / "worktrees").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        fix_prs.sweep,
        "git_for",
        lambda _path: fake_git(
            {("rev-parse", "--verify", "--quiet", "refs/remotes/origin/agent/x"): (0, "")}
        ),
    )

    def runner(argv, **kwargs):
        code = 0 if "fetch" in [str(a) for a in argv] else 128
        return subprocess.CompletedProcess(argv, code, "", "already exists")

    assert fix_prs.cut_tree(checkout, "agent/x", runner) is None


# --- a fresh branch, for a failure that has none ---------------------------------------


def test_an_upstream_fix_is_named_for_the_failing_test_under_the_agent_prefix():
    sig = (
        "scripts/hooks/tests/test_untested_symbols.py::test_every_public_symbol_is_named_by_a_test",
    )
    decision = fix_plan.Decision(fix_plan.UPSTREAM, "n", (failure(signature=sig),))
    branch = fix_prs.fix_branch(decision, NOW)
    assert branch.startswith(fix_prs.tb.BRANCH_PREFIX + "fix-test-every-public")
    assert branch.endswith("-0918")


def test_a_nightly_fix_is_named_for_the_workflow():
    nightly = failure(kind=fix_plan.NIGHTLY, workflow="Nightly", head="")
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (nightly,))
    assert fix_prs.fix_branch(decision, NOW) == "agent/fix-nightly-0918"


def test_an_upstream_fix_with_no_test_id_is_named_for_the_workflow():
    red = failure(kind=fix_plan.BRANCH, workflow="PR Gate", head="", signature=("Drift",))
    decision = fix_plan.Decision(fix_plan.UPSTREAM, "n", (red,))
    assert fix_prs.fix_branch(decision, NOW) == "agent/fix-pr-gate-0918"


def fresh_with(monkeypatch, tmp_path, taken: tuple[str, ...] = ()):
    checkout = tmp_path / "carameli"
    (checkout / ".claude" / "worktrees").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        fix_prs.sweep,
        "git_for",
        lambda _path: fake_git(
            {("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"): (0, "") for name in taken}
        ),
    )
    run = FakeRun()
    return fix_prs.cut_fresh_tree(checkout, "agent/fix-nightly-0918", "main", run), run


def test_a_fresh_branch_is_cut_off_the_default_branch_after_a_fetch(monkeypatch, tmp_path):
    (path, branch), run = fresh_with(monkeypatch, tmp_path)
    fetch, add = run.git_args()
    assert fetch == ["fetch", "--quiet", "origin"]
    assert add == ["worktree", "add", "--no-track", "-b", branch, str(path), "origin/main"]
    assert branch == "agent/fix-nightly-0918"
    assert path.parts[-3:] == (".claude", "worktrees", "fix-nightly-0918")


def test_a_branch_the_checkout_already_has_gets_a_counter(monkeypatch, tmp_path):
    """Two clicks on two nightlies of one project on one day want two branches."""
    (path, branch), _run = fresh_with(
        monkeypatch, tmp_path, taken=("agent/fix-nightly-0918", "agent/fix-nightly-0918-2")
    )
    assert branch == "agent/fix-nightly-0918-3"
    assert path.name == "fix-nightly-0918-3"


def test_a_git_refusal_on_a_fresh_branch_names_the_branch(monkeypatch, tmp_path):
    checkout = tmp_path / "carameli"
    (checkout / ".claude" / "worktrees").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fix_prs.sweep, "git_for", lambda _path: fake_git({}))

    def runner(argv, **kwargs):
        code = 0 if "fetch" in [str(a) for a in argv] else 128
        return subprocess.CompletedProcess(argv, code, "", "nope")

    assert fix_prs.cut_fresh_tree(checkout, "agent/fix-x", "main", runner) == (None, "agent/fix-x")


# --- opening the session ----------------------------------------------------------


def test_the_three_modes_map_to_two_clis_and_two_ways_of_opening():
    """Codex has no background session, so there is deliberately no `codex-bg`."""
    assert fix_prs.AGENT_MODES["claude"] == ("claude", fix_prs.TAB)
    assert fix_prs.AGENT_MODES["claude-bg"] == ("claude", fix_prs.BACKGROUND)
    assert fix_prs.AGENT_MODES["codex"] == ("codex", fix_prs.TAB)
    assert "codex-bg" not in fix_prs.AGENT_MODES


def test_open_session_is_the_one_place_a_mode_becomes_a_tab_or_a_background(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(
        fix_prs.agent_tabs,
        "open_agent",
        lambda launch, tree, branch, run, **k: opened.append(("tab", launch.cli, k["title"])) or 0,
    )
    monkeypatch.setattr(
        fix_prs.agent_tabs,
        "launch_background",
        lambda launch, tree, prompt, off, run: opened.append(("bg", launch.cli, prompt)) or 0,
    )
    fix_prs.open_session(agent_models.Launch("codex"), tmp_path, "agent/x", "p", "t")
    fix_prs.open_session(agent_models.Launch("claude-bg"), tmp_path, "agent/x", "p", "t")
    assert opened == [("tab", "codex", "t"), ("bg", "claude", "p")]


def test_the_model_and_effort_reach_both_modes_and_neither_invents_a_default(monkeypatch, tmp_path):
    """One pick, two ways of opening, the same flags -- and nothing when nothing was picked.

    The background mode and the tab have to hand the agent the same words for a report
    about one to say anything about the other, and that now covers the model as well as
    the prompt. The empty case is the other half and is the one a regression would reach
    first: a session opened with no pick must carry no `--model` at all, because passing
    the configured value and passing nothing are different things the moment the
    configuration changes between the click and the spawn.
    """
    seen: dict = {}
    monkeypatch.setattr(
        fix_prs.agent_tabs,
        "open_agent",
        lambda launch, tree, branch, run, **_k: seen.update(tab=launch) or 0,
    )
    monkeypatch.setattr(
        fix_prs.agent_tabs,
        "launch_background",
        lambda launch, tree, prompt, off, run: seen.update(bg=launch) or 0,
    )
    flags = ["--model", "claude-opus-5", "--effort", "max"]
    for mode in ("claude", "claude-bg"):
        chosen = agent_models.Launch.parse(mode, "claude:claude-opus-5", "max")
        fix_prs.open_session(chosen, tmp_path, "agent/x", "p", "t")
    assert seen["tab"].flags() == flags and seen["bg"].flags() == flags

    fix_prs.open_session(agent_models.Launch("claude"), tmp_path, "agent/x", "p", "t")
    assert seen["tab"].flags() == []


# --- one PR, by hand ----------------------------------------------------------------


def run_one_with(monkeypatch, tmp_path, view: dict, mode: str = "claude", then: dict | None = None):
    """`run_one` with every subprocess replaced. Returns `(code, opened)`.

    `then` is what a second reading of the PR answers, which only an unresolved `view`
    ever asks for -- see the re-ask on the launch path.
    """
    (tmp_path / "carameli").mkdir(exist_ok=True)
    workspace = tmp_path / "w" / "alex.code-workspace"
    workspace.parent.mkdir(exist_ok=True)
    opened: dict = {}
    answers = iter([view] if then is None else [view, then])
    monkeypatch.setattr(menu, "pr_view", lambda _dir, _n: next(answers, view))
    monkeypatch.setattr(fix_prs, "existing_tree", lambda *a, **k: (None, ""))
    monkeypatch.setattr(fix_prs, "cut_tree", lambda *a, **k: Path("/trees/x"))
    monkeypatch.setattr(
        fix_prs.agent_tabs,
        "open_agent",
        lambda *args, **kwargs: opened.update(args=args, kwargs=kwargs) or 0,
    )
    monkeypatch.setattr(
        fix_prs.agent_tabs, "launch_background", lambda *args, **kwargs: opened.update(bg=args) or 0
    )
    (tmp_path / "w" / "carameli").mkdir(exist_ok=True)
    code = fix_prs.run_one(menu.Pick("carameli", 412), workspace, agent_models.Launch(mode))
    return code, opened


def test_a_pr_that_went_green_since_the_scan_is_reported_not_given_a_worktree(
    monkeypatch, tmp_path, capsys
):
    """Reporting good news as a failure would put a red icon and a toast on a PR that
    fixed itself."""
    code, opened = run_one_with(monkeypatch, tmp_path, pr())
    assert code == 0
    assert not opened
    assert "nothing to do" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_pr_that_left_the_open_set_since_the_scan_gets_no_worktree(
    monkeypatch, tmp_path, capsys, state
):
    """The failure this was written for: a red PR was closed between the scan and the
    click, GitHub deleted its head branch on the way out, and the cut refused a branch
    `origin` no longer has -- an exit 1 and a message about worktrees for what is a stale
    row. A closed PR is still `red` to `broken_reason`, which only ever sees open ones
    from the scan, so the state is checked before it."""
    view = pr(state=state, statusCheckRollup=[{"conclusion": "FAILURE"}])
    code, opened = run_one_with(monkeypatch, tmp_path, view)
    assert code == 0
    assert not opened
    assert "nothing to do" in capsys.readouterr().out


def test_a_pr_view_without_a_state_is_treated_as_open(monkeypatch, tmp_path):
    """`state` missing is `gh` answering a shape this asked for and did not get; the
    scan already filtered to open PRs, so the safe reading is to carry on rather than
    to swallow every pick on the day the field is renamed."""
    view = pr(statusCheckRollup=[{"conclusion": "FAILURE"}])
    view.pop("state")
    code, opened = run_one_with(monkeypatch, tmp_path, view)
    assert code == 0
    assert opened


def test_a_pr_turned_draft_since_the_scan_gets_no_worktree(monkeypatch, tmp_path, capsys):
    """`isDraft` is in the view fields for the same reason as `state`: `broken_reason`
    already excludes drafts, and could not while the field it reads was never asked for."""
    view = pr(isDraft=True, statusCheckRollup=[{"conclusion": "FAILURE"}])
    code, opened = run_one_with(monkeypatch, tmp_path, view)
    assert code == 0
    assert not opened
    assert "nothing to do" in capsys.readouterr().out


def test_the_view_asks_for_every_field_the_launch_path_reads():
    """A field the launch path branches on and `PR_VIEW_FIELDS` omits is always absent,
    which is indistinguishable from the harmless value -- how the closed-PR bug survived.
    `headRefOid` and `baseRefName` are the plan's: the commit the evidence is read at and
    the branch the fix merges into."""
    asked = set(menu.PR_VIEW_FIELDS.split(","))
    needed = {"state", "isDraft", "mergeable", "statusCheckRollup", "headRefName"}
    assert needed | {"headRefOid", "baseRefName"} <= asked
    assert {"headRefOid", "baseRefName"} <= set(menu.PR_LIST_FIELDS.split(","))


@pytest.mark.parametrize("mergeable", ["CONFLICTING", "UNKNOWN"])
def test_a_broken_pr_opens_a_tab_titled_for_the_pr_with_the_planned_prompt(
    monkeypatch, tmp_path, mergeable
):
    """Several tabs can be open at once on branches that all begin `agent/`. The prompt
    is the plan's, not a second spelling: `--picks` is the planned path minus the plan."""
    code, opened = run_one_with(
        monkeypatch, tmp_path, pr(mergeable=mergeable, mergeStateStatus="DIRTY")
    )
    assert code == 0
    assert opened["kwargs"]["title"] == "carameli #412"
    assert "#412" in opened["kwargs"]["prompt"]
    assert "No artifact came down" in opened["kwargs"]["prompt"], "nothing was downloaded here"
    assert "\n" not in opened["kwargs"]["prompt"]


def test_the_launch_path_asks_again_rather_than_calling_an_unjudged_pr_fine(monkeypatch, tmp_path):
    """Anything merging to the base branch between the click and here puts this PR's
    verdict back to `UNKNOWN`, and `broken_reason` reads one as clean -- so without the
    second ask the pick opens nothing, reports success, and leaves the PR as red as it
    was. The scan's own re-ask cannot cover this: it ran before the click."""
    view = pr(mergeable="UNKNOWN", statusCheckRollup=[])
    code, opened = run_one_with(monkeypatch, tmp_path, view, then={"mergeable": "CONFLICTING"})
    assert code == 0
    assert "merge conflict" in opened["kwargs"]["prompt"]


def test_the_background_mode_does_not_open_a_tab(monkeypatch, tmp_path):
    _code, opened = run_one_with(monkeypatch, tmp_path, pr(mergeable="CONFLICTING"), "claude-bg")
    assert "bg" in opened
    assert "kwargs" not in opened


def test_an_open_worktree_on_the_head_branch_is_used_and_nothing_is_cut(monkeypatch, tmp_path):
    """The second click on the same PR. `cut_tree` raises rather than returning, so this
    fails loudly if the reuse branch is ever dropped."""

    def explode(*_a, **_k):
        raise AssertionError("a worktree already on the branch must not be cut again")

    (tmp_path / "carameli").mkdir(exist_ok=True)
    workspace = tmp_path / "w" / "alex.code-workspace"
    workspace.parent.mkdir(exist_ok=True)
    (tmp_path / "w" / "carameli").mkdir(exist_ok=True)
    opened: dict = {}
    monkeypatch.setattr(menu, "pr_view", lambda _dir, _n: pr(mergeable="CONFLICTING"))
    monkeypatch.setattr(fix_prs, "existing_tree", lambda *a, **k: (Path("/trees/held"), ""))
    monkeypatch.setattr(fix_prs, "cut_tree", explode)
    monkeypatch.setattr(
        fix_prs.agent_tabs,
        "open_agent",
        lambda *args, **kwargs: opened.update(args=args) or 0,
    )
    assert (
        fix_prs.run_one(menu.Pick("carameli", 412), workspace, agent_models.Launch("claude")) == 0
    )
    assert opened["args"][1] == Path("/trees/held")


def test_a_branch_held_outside_the_tier_stops_the_run_and_opens_nothing(
    monkeypatch, tmp_path, capsys
):
    """A refusal is not "no worktree yet": cutting anyway is what git would reject, and
    opening an agent somewhere else is what nobody asked for."""

    def explode(*_a, **_k):
        raise AssertionError("nothing should be cut or opened")

    (tmp_path / "carameli").mkdir(exist_ok=True)
    workspace = tmp_path / "w" / "alex.code-workspace"
    workspace.parent.mkdir(exist_ok=True)
    (tmp_path / "w" / "carameli").mkdir(exist_ok=True)
    monkeypatch.setattr(menu, "pr_view", lambda _dir, _n: pr(mergeable="CONFLICTING"))
    monkeypatch.setattr(
        fix_prs, "existing_tree", lambda *a, **k: (None, "agent/x is checked out at C:/ws/devkit")
    )
    monkeypatch.setattr(fix_prs, "cut_tree", explode)
    monkeypatch.setattr(fix_prs.agent_tabs, "open_agent", explode)
    code = fix_prs.run_one(menu.Pick("carameli", 412), workspace, agent_models.Launch("claude"))
    assert code == fix_prs.EXIT_FAILED
    assert "checked out at C:/ws/devkit" in capsys.readouterr().err


def test_a_pr_gh_cannot_read_is_a_failure_rather_than_a_silent_skip(monkeypatch, tmp_path):
    code, opened = run_one_with(monkeypatch, tmp_path, {})
    assert code == fix_prs.EXIT_FAILED
    assert not opened


def test_a_batch_reports_the_worst_outcome(monkeypatch, tmp_path):
    """One failure among three must not be reported as a green run."""
    workspace = tmp_path / "alex.code-workspace"
    codes = iter([0, 1, 0])
    monkeypatch.setattr(fix_prs, "run_one", lambda *a, **k: next(codes))
    picks = [menu.Pick("a", 1), menu.Pick("a", 2), menu.Pick("a", 3)]
    assert fix_prs.run(picks, workspace, agent_models.Launch("claude")) == 1


# --- the planned path ---------------------------------------------------------------


@pytest.fixture
def root(tmp_path):
    """A workspace root with a devkit and a carameli checkout, and the workspace file."""
    for name in ("devkit", "carameli"):
        (tmp_path / name).mkdir()
    (tmp_path / "alex.code-workspace").write_text("{}", encoding="utf-8")
    return tmp_path


def capture_sessions(monkeypatch):
    opened: list[dict] = []
    monkeypatch.setattr(
        fix_prs,
        "open_session",
        lambda launch, tree, branch, prompt, title, runner=None: (
            opened.append(
                {
                    "mode": launch.agent,
                    "tree": tree,
                    "branch": branch,
                    "prompt": prompt,
                    "title": title,
                    "launch": launch,
                }
            )
            or 0
        ),
    )
    return opened


def test_a_planned_pr_gets_its_evidence_placed_and_the_plans_prompt(monkeypatch, root):
    placed = []
    monkeypatch.setattr(fix_prs, "existing_tree", lambda *a: (None, ""))
    monkeypatch.setattr(fix_prs, "refresh_head", lambda *a: "")
    monkeypatch.setattr(
        fix_prs, "cut_tree", lambda *a: root / "carameli" / ".claude" / "worktrees" / "x"
    )
    monkeypatch.setattr(
        evidence, "place", lambda f, tree, sub="": placed.append((f.number, tree, sub))
    )
    opened = capture_sessions(monkeypatch)
    assert fix_prs.dispatch_pr(failure(), root, agent_models.Launch("claude")) == 0
    assert placed == [(412, root / "carameli" / ".claude" / "worktrees" / "x", "")]
    assert opened[0]["branch"] == "agent/sweep-labels-0904"
    assert opened[0]["title"] == "carameli #412"
    assert opened[0]["prompt"] == fix_prs.tab_safe(fix_prs.fix_prompts.pr_prompt(failure()))


def test_a_planned_pr_whose_branch_is_held_elsewhere_opens_nothing(monkeypatch, root, capsys):
    monkeypatch.setattr(fix_prs, "existing_tree", lambda *a: (None, "held at C:/elsewhere"))
    monkeypatch.setattr(fix_prs, "cut_tree", lambda *a: pytest.fail("must not cut"))
    assert (
        fix_prs.dispatch_pr(failure(), root, agent_models.Launch("claude")) == fix_prs.EXIT_FAILED
    )
    assert "held at C:/elsewhere" in capsys.readouterr().err


def test_an_upstream_decision_opens_one_session_in_devkit_with_every_projects_logs(
    monkeypatch, root
):
    """The v0.11.21 shape: three consumers, one devkit worktree, the logs of each under
    their own name, and one prompt naming all three PRs."""
    group = (
        failure(project="carameli", number=412, url="u/412"),
        failure(project="roguelike", number=16, url="u/16"),
    )
    decision = fix_plan.Decision(fix_plan.UPSTREAM, "one vendored failure", group)
    cut = []
    placed = []
    monkeypatch.setattr(fix_prs.tb, "detect_default_branch", lambda _git: "main")
    monkeypatch.setattr(
        fix_prs,
        "cut_fresh_tree",
        lambda project_dir, branch, base, runner: (
            cut.append((project_dir, branch, base))
            or (root / "devkit" / ".claude" / "worktrees" / "fix", branch)
        ),
    )
    monkeypatch.setattr(evidence, "place", lambda f, tree, sub="": placed.append((f.project, sub)))
    opened = capture_sessions(monkeypatch)
    assert fix_prs.dispatch_fresh(decision, root, agent_models.Launch("claude-bg")) == 0
    assert cut[0][0] == root / "devkit"
    assert cut[0][1].startswith("agent/fix-") and cut[0][2] == "main"
    assert placed == [("carameli", "carameli-pr-412"), ("roguelike", "roguelike-pr-16")]
    assert opened[0]["mode"] == "claude-bg"
    assert "u/412" in opened[0]["prompt"] and "u/16" in opened[0]["prompt"]
    assert opened[0]["title"].startswith("devkit agent/fix-")


def test_a_red_default_branch_opens_in_its_own_project_with_the_branch_prompt(monkeypatch, root):
    red = failure(
        kind=fix_plan.BRANCH, head="", number=0, workflow="PR Gate", run_id="55", sha="fb17a310"
    )
    decision = fix_plan.Decision(fix_plan.DISPATCH, "PR Gate failing", (red,))
    cut = []
    monkeypatch.setattr(
        fix_prs,
        "cut_fresh_tree",
        lambda project_dir, branch, base, runner: (
            cut.append((project_dir, branch, base))
            or (root / "carameli" / ".claude" / "worktrees" / "m", branch)
        ),
    )
    monkeypatch.setattr(evidence, "place", lambda *a, **k: None)
    opened = capture_sessions(monkeypatch)
    assert fix_prs.dispatch_fresh(decision, root, agent_models.Launch("claude")) == 0
    assert cut[0][0] == root / "carameli" and cut[0][2] == "main"
    assert "red on origin/main itself" in opened[0]["prompt"]
    assert opened[0]["title"] == "carameli main"


def test_a_nightly_decision_opens_in_its_own_project_off_its_default_branch(monkeypatch, root):
    nightly = failure(
        kind=fix_plan.NIGHTLY, head="", base="master", workflow="Nightly", number=9, run_id="55"
    )
    decision = fix_plan.Decision(fix_plan.DISPATCH, "Nightly failing", (nightly,))
    cut = []
    monkeypatch.setattr(
        fix_prs,
        "cut_fresh_tree",
        lambda project_dir, branch, base, runner: (
            cut.append((project_dir, branch, base))
            or (root / "carameli" / ".claude" / "worktrees" / "n", branch)
        ),
    )
    monkeypatch.setattr(evidence, "place", lambda *a, **k: None)
    opened = capture_sessions(monkeypatch)
    assert fix_prs.dispatch_fresh(decision, root, agent_models.Launch("codex")) == 0
    assert cut == [
        (
            root / "carameli",
            # `tb.branch_name`'s date is the local one: the UTC date differs for hours
            # of every day east or west of Greenwich, and this test went red in them.
            "agent/fix-nightly-" + _dt.date.today().strftime("%m%d"),
            "master",
        )
    ]
    assert "Nightly workflow in carameli" in opened[0]["prompt"]
    assert opened[0]["title"] == "carameli Nightly"


def test_a_fresh_cut_git_refused_opens_nothing(monkeypatch, root, capsys):
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(kind=fix_plan.NIGHTLY, head=""),))
    monkeypatch.setattr(fix_prs, "cut_fresh_tree", lambda *a: (None, "agent/fix-nightly-0918"))
    monkeypatch.setattr(fix_prs, "open_session", lambda *a, **k: pytest.fail("nothing to open in"))
    assert (
        fix_prs.dispatch_fresh(decision, root, agent_models.Launch("claude")) == fix_prs.EXIT_FAILED
    )
    assert "could not cut agent/fix-nightly-0918" in capsys.readouterr().err


def test_a_checkout_the_workspace_does_not_have_opens_nothing(monkeypatch, root, capsys):
    decision = fix_plan.Decision(
        fix_plan.DISPATCH, "n", (failure(kind=fix_plan.NIGHTLY, head="", project="ghost"),)
    )
    assert (
        fix_prs.dispatch_fresh(decision, root, agent_models.Launch("claude")) == fix_prs.EXIT_FAILED
    )
    assert "no checkout 'ghost'" in capsys.readouterr().err


def planned(monkeypatch, root, failures, latest="v0.11.21"):
    """`run_plan` with the scan, the evidence and the dispatches all replaced."""
    sent: list[str] = []
    monkeypatch.setattr(menu, "scan", lambda _ws: {"carameli": [], "devkit": []})
    monkeypatch.setattr(evidence, "collect", lambda _ws, _found: failures)
    monkeypatch.setattr(evidence, "newest_release", lambda _devkit: latest)
    monkeypatch.setattr(
        fix_prs, "dispatch_pr", lambda f, *_a: sent.append(f"pr {f.project}#{f.number}") or 0
    )
    monkeypatch.setattr(
        fix_prs,
        "dispatch_fresh",
        lambda d, *_a: sent.append(f"{d.action} {','.join(f.project for f in d.failures)}") or 0,
    )
    return sent


def test_the_plan_is_printed_and_a_dry_run_opens_nothing(monkeypatch, root, capsys):
    sent = planned(monkeypatch, root, [failure(), failure(head="release/v0.12.0", number=2)])
    workspace = root / "alex.code-workspace"
    assert fix_prs.run_plan(workspace, agent_models.Launch("claude"), dry_run=True, redo=False) == 0
    out = capsys.readouterr().out
    assert "dispatch carameli #412" in out
    assert "skip     carameli #2 -- red by construction" in out
    assert sent == []


def test_a_click_sends_what_is_new_records_it_and_a_second_click_sends_nothing(
    monkeypatch, root, capsys
):
    """The ledger is the whole reason a click is safe to repeat: the second one reports
    the first rather than spending a second session on the same failure."""
    sent = planned(monkeypatch, root, [failure()])
    workspace = root / "alex.code-workspace"
    assert (
        fix_prs.run_plan(workspace, agent_models.Launch("claude"), dry_run=False, redo=False) == 0
    )
    assert sent == ["pr carameli#412"]
    ledger = fix_ledger.read_ledger(fix_prs.worktree.boxes_root(root) / fix_ledger.LEDGER_NAME)
    assert list(ledger) == [
        fix_ledger.decision_key(fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),)))
    ]

    capsys.readouterr()
    assert (
        fix_prs.run_plan(workspace, agent_models.Launch("claude"), dry_run=False, redo=False) == 0
    )
    assert sent == ["pr carameli#412"]
    assert "already dispatched" in capsys.readouterr().out

    assert fix_prs.run_plan(workspace, agent_models.Launch("claude"), dry_run=False, redo=True) == 0
    assert sent == ["pr carameli#412", "pr carameli#412"]


def test_a_repushed_pr_that_is_still_red_is_sent_again(monkeypatch, root):
    """A new head sha is a new key: the fix an agent pushed did not work, and that is
    worth a second look rather than a ledger line saying it was handled."""
    sent = planned(monkeypatch, root, [failure(sha="first")])
    workspace = root / "alex.code-workspace"
    fix_prs.run_plan(workspace, agent_models.Launch("claude"), dry_run=False, redo=False)
    monkeypatch.setattr(evidence, "collect", lambda _ws, _found: [failure(sha="second")])
    fix_prs.run_plan(workspace, agent_models.Launch("claude"), dry_run=False, redo=False)
    assert sent == ["pr carameli#412", "pr carameli#412"]


def test_a_conflict_and_a_refused_commit_go_through_the_branch_path(monkeypatch, root):
    """Both hold a branch in a worktree; neither has a PR number worth a title."""
    sent = planned(
        monkeypatch,
        root,
        [
            failure(number=1, signature=(fix_plan.CONFLICT,)),
            failure(
                kind=fix_plan.COMMIT, number=0, head="agent/i-0919", signature=("commit refused",)
            ),
        ],
    )
    assert (
        fix_prs.run_plan(
            root / "alex.code-workspace", agent_models.Launch("claude"), dry_run=False, redo=False
        )
        == 0
    )
    assert sorted(sent) == ["pr carameli#0", "pr carameli#1"]


def test_a_held_decision_is_neither_sent_nor_recorded(monkeypatch, root):
    """A PR behind a red base waits for the base's fixer; the plan says so and the
    click sends nothing at it."""
    red = failure(
        kind=fix_plan.BRANCH,
        number=0,
        head="",
        base="main",
        workflow="PR Gate",
        signature=("t::a",),
    )
    sent = planned(monkeypatch, root, [red, failure(number=1, head="agent/a", signature=("t::a",))])
    assert (
        fix_prs.run_plan(
            root / "alex.code-workspace", agent_models.Launch("claude"), dry_run=False, redo=False
        )
        == 0
    )
    assert sent == ["dispatch carameli"]
    ledger = fix_ledger.read_ledger(fix_prs.worktree.boxes_root(root) / fix_ledger.LEDGER_NAME)
    assert [fix_ledger.key_kind(k) for k in ledger] == [fix_plan.BRANCH]


def test_a_dispatch_stamps_the_worktree_with_the_key_it_is_recorded_under(monkeypatch, root):
    """What lets a blocked report from that tree find its ledger entry, whatever branch
    the tree is on."""
    tree = root / "carameli" / ".claude" / "worktrees" / "x"
    tree.mkdir(parents=True)
    monkeypatch.setattr(fix_prs, "existing_tree", lambda *a: (tree, ""))
    monkeypatch.setattr(fix_prs, "refresh_head", lambda *a: "")
    monkeypatch.setattr(evidence, "place", lambda *a, **k: None)
    capture_sessions(monkeypatch)
    claude = agent_models.Launch("claude")
    assert fix_prs.dispatch_pr(failure(), root, claude, None, "pr:carameli:412:k") == 0
    assert fix_prs.fix_reports.read_stamp(tree)["key"] == "pr:carameli:412:k"
    assert not fix_prs.fix_reports.fixer_owns_branch(tree), "a PR's branch is its author's"
    fresh = root / "devkit" / ".claude" / "worktrees" / "f"
    fresh.mkdir(parents=True)
    monkeypatch.setattr(fix_prs.tb, "detect_default_branch", lambda _git: "main")
    monkeypatch.setattr(fix_prs, "cut_fresh_tree", lambda *a: (fresh, "agent/fix"))
    decision = fix_plan.Decision(fix_plan.UPSTREAM, "one vendored failure", (failure(),))
    assert fix_prs.dispatch_fresh(decision, root, claude, None, "upstream:1:k") == 0
    assert fix_prs.fix_reports.read_stamp(fresh) == {
        "key": "upstream:1:k",
        "what": "one vendored failure",
        "when": fix_prs.fix_reports.read_stamp(fresh)["when"],
        "owns_branch": True,
    }
    unstamped = root / "carameli" / ".claude" / "worktrees" / "u"
    unstamped.mkdir(parents=True)
    monkeypatch.setattr(fix_prs, "existing_tree", lambda *a: (unstamped, ""))
    assert fix_prs.dispatch_pr(failure(), root, claude) == 0
    assert fix_prs.fix_reports.read_stamp(unstamped) == {}, "a hand pick has no ledger key"


def tree_git(porcelain: str = "", ff_ok: bool = True):
    """A `git_for` whose tree is `porcelain`-dirty and whose fast-forward may fail."""
    calls: list[tuple[str, ...]] = []

    def git_for(_tree):
        def git(*args):
            calls.append(args)
            if args[0] == "status":
                return subprocess.CompletedProcess(args, 0, porcelain, "")
            if args[0] == "merge":
                return subprocess.CompletedProcess(args, 0 if ff_ok else 128, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        return git

    return git_for, calls


def test_a_clean_reused_tree_is_fast_forwarded_to_origin_and_a_dirty_one_is_left(tmp_path):
    """The prompt tells the fixer the branch already carries its base, which is true
    of origin's head after the pass's update and not of a local branch checked out
    before it; a push from the stale one is refused every pass after."""
    git_for, calls = tree_git()
    assert fix_prs.refresh_head(tmp_path, "agent/x", git_for) == ""
    assert calls == [
        ("fetch", "--quiet", "origin", "agent/x"),
        ("status", "--porcelain"),
        ("merge", "--ff-only", "origin/agent/x"),
    ]
    git_for, calls = tree_git(porcelain=" M a.py\n")
    assert "uncommitted" in fix_prs.refresh_head(tmp_path, "agent/x", git_for)
    assert calls[-1][0] == "status", "a session's edits are never merged over"
    git_for, _calls = tree_git(ff_ok=False)
    assert "diverged" in fix_prs.refresh_head(tmp_path, "agent/x", git_for)


def test_a_planned_pr_is_refreshed_before_its_fixer_opens_and_a_refused_commit_is_not(
    monkeypatch, root, capsys
):
    """A refused commit's tree is the session's own edits, dirty by definition."""
    refreshed = []
    monkeypatch.setattr(fix_prs, "existing_tree", lambda *a: (root / "carameli" / "t", ""))
    monkeypatch.setattr(
        fix_prs, "refresh_head", lambda tree, branch: refreshed.append(branch) or "stale"
    )
    monkeypatch.setattr(evidence, "place", lambda *a, **k: None)
    capture_sessions(monkeypatch)
    assert fix_prs.dispatch_pr(failure(), root, agent_models.Launch("claude")) == 0
    refused = failure(kind=fix_plan.COMMIT, number=0, head="agent/i", signature=("x refused",))
    assert fix_prs.dispatch_pr(refused, root, agent_models.Launch("claude")) == 0
    assert refreshed == ["agent/sweep-labels-0904"]
    assert "stale" in capsys.readouterr().out


def test_a_refused_commit_is_titled_by_its_branch(monkeypatch, root):
    monkeypatch.setattr(fix_prs, "existing_tree", lambda *a: (root / "carameli" / "t", ""))
    monkeypatch.setattr(fix_prs, "refresh_head", lambda *a: "")
    monkeypatch.setattr(evidence, "place", lambda *a, **k: None)
    opened = capture_sessions(monkeypatch)
    refused = failure(
        kind=fix_plan.COMMIT, number=0, head="agent/i-0919", signature=("commit refused",)
    )
    assert fix_prs.dispatch_pr(refused, root, agent_models.Launch("claude")) == 0
    assert opened[0]["title"] == "carameli agent/i-0919"
    assert "commit stage refused" in opened[0]["prompt"]


def test_an_upstream_group_and_a_nightly_go_through_the_fresh_branch_path(monkeypatch, root):
    vendored = ("scripts/hooks/tests/test_untested_symbols.py::t",)
    sent = planned(
        monkeypatch,
        root,
        [
            failure(project="carameli", number=1, signature=vendored),
            failure(project="roguelike", number=2, signature=vendored),
            failure(
                kind=fix_plan.NIGHTLY, head="", number=9, sha="", run_id="55", workflow="Nightly"
            ),
        ],
    )
    assert (
        fix_prs.run_plan(
            root / "alex.code-workspace", agent_models.Launch("claude"), dry_run=False, redo=False
        )
        == 0
    )
    assert sorted(sent) == ["dispatch carameli", "upstream carameli,roguelike"]


def test_a_dispatch_that_failed_to_open_is_not_recorded_and_is_the_exit_code(monkeypatch, root):
    planned(monkeypatch, root, [failure()])
    monkeypatch.setattr(fix_prs, "dispatch_pr", lambda *_a: fix_prs.EXIT_FAILED)
    workspace = root / "alex.code-workspace"
    assert (
        fix_prs.run_plan(workspace, agent_models.Launch("claude"), dry_run=False, redo=False)
        == fix_prs.EXIT_FAILED
    )
    assert fix_ledger.read_ledger(fix_prs.worktree.boxes_root(root) / fix_ledger.LEDGER_NAME) == {}


def test_a_superseded_adoption_is_neither_sent_nor_recorded(monkeypatch, root):
    sent = planned(monkeypatch, root, [failure(head="agent/auto/devkit-upgrade-v0-11-20-0916")])
    assert (
        fix_prs.run_plan(
            root / "alex.code-workspace", agent_models.Launch("claude"), dry_run=False, redo=False
        )
        == 0
    )
    assert sent == []


# --- the CLI ----------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "alex.code-workspace"
    path.write_text("{}", encoding="utf-8")
    return path


def test_a_dismissed_picker_runs_nothing_and_is_not_a_failure(workspace, capsys):
    """Ahead of argparse: a cancel reported as a usage error is a red icon, a toast and
    a `logs/` artifact for a run the user called off. `--agent` carries `choices=`, which
    would turn the literal into a usage error on its own."""
    code = fix_prs.main(["--agent", "${input:fixAgent}", "--workspace", str(workspace)])
    assert code == 0
    assert "cancelled" in capsys.readouterr().out


def test_no_picks_is_the_planned_path(workspace, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        fix_prs, "run_plan", lambda ws, launch, dry_run, redo: seen.update(locals()) or 0
    )
    assert fix_prs.main(["--agent", "codex", "--dry-run", "--workspace", str(workspace)]) == 0
    assert (seen["launch"].agent, seen["dry_run"], seen["redo"]) == ("codex", True, False)
    assert seen["ws"] == workspace.resolve()


def test_picks_by_hand_skip_the_plan(workspace, monkeypatch):
    ran = {}
    monkeypatch.setattr(fix_prs, "run_plan", lambda *a, **k: pytest.fail("picks must not plan"))
    monkeypatch.setattr(
        fix_prs, "run", lambda picks, ws, launch: ran.update(picks=picks, mode=launch.agent) or 0
    )
    code = fix_prs.main(
        ["--picks", "devkit:88 roguelike:16", "--agent", "claude-bg", "--workspace", str(workspace)]
    )
    assert code == 0
    assert ran == {
        "picks": [menu.Pick("devkit", 88), menu.Pick("roguelike", 16)],
        "mode": "claude-bg",
    }


def test_the_cli_carries_the_model_pick_to_every_session_it_opens(workspace, monkeypatch):
    """`--model`/`--effort` are carried, never interpreted, all the way from the picker.

    The pair reaches `main` as the quick-pick's own tokens -- `<agent>:<id>` and a bare
    level -- and `main`'s only job is to turn them into the `Options` every dispatch
    below it takes. It is the flags that must arrive, so this asserts those rather than
    the tokens: they are what the agent is actually opened with, and they are where the
    two CLIs stop agreeing.
    """
    seen = {}
    monkeypatch.setattr(
        fix_prs, "run_plan", lambda ws, launch, dry_run, redo: seen.update(o=launch) or 0
    )
    assert (
        fix_prs.main(
            [
                "--agent",
                "codex",
                "--dry-run",
                "--model=codex:gpt-6-astra",
                "--effort=xhigh",
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    assert seen["o"].flags("codex") == [
        "-m",
        "gpt-6-astra",
        "-c",
        'model_reasoning_effort="xhigh"',
    ]


def test_the_default_rows_reach_the_cli_as_no_flags_at_all(workspace, monkeypatch):
    """`default` is the picker's "leave it alone", and it must not become a flag.

    Both pickers always draw a first row, so `--model=default --effort=default` is the
    argument list of an ordinary click that answered neither question. It has to open
    exactly the session that today's `fix-prs.py` opens with no flags: passing the
    configured value instead would pin it at the moment of the click, which is not what
    the row says.
    """
    seen = {}
    monkeypatch.setattr(
        fix_prs, "run_plan", lambda ws, launch, dry_run, redo: seen.update(o=launch) or 0
    )
    fix_prs.main(
        [
            "--dry-run",
            "--model=default",
            "--effort=default",
            "--workspace",
            str(workspace),
        ]
    )
    assert seen["o"].flags("claude") == [] and seen["o"].flags("codex") == []


def test_a_missing_workspace_file_is_a_usage_error(tmp_path, capsys):
    assert fix_prs.main(["--workspace", str(tmp_path / "nope")]) == fix_prs.EXIT_USAGE
    assert "no workspace file" in capsys.readouterr().err


def test_list_prints_what_is_red_per_checkout(workspace, monkeypatch, capsys):
    monkeypatch.setattr(
        menu, "scan", lambda _ws: {"devkit": [pr(mergeable="CONFLICTING")], "carameli": []}
    )
    assert fix_prs.main(["--list", "--workspace", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert "devkit: 1 broken" in out
    assert "#412 agent/sweep-labels-0904 -- merge conflict" in out
    assert "carameli: nothing broken" in out


def test_an_unknown_checkout_is_a_usage_error_not_a_traceback(workspace, capsys):
    code = fix_prs.main(["--picks", "nosuch:1", "--workspace", str(workspace)])
    assert code == fix_prs.EXIT_USAGE
    assert "unknown checkout" in capsys.readouterr().err


def test_a_malformed_pick_is_a_usage_error(workspace, capsys):
    assert (
        fix_prs.main(["--picks", "carameli:head", "--workspace", str(workspace)])
        == fix_prs.EXIT_USAGE
    )
    assert "does not name a PR number" in capsys.readouterr().err


def test_the_terminal_listing_is_per_checkout_fullest_first():
    found = {"devkit": [pr(mergeable="CONFLICTING")], "carameli": []}
    text = fix_prs.render_scan(found)
    assert text.splitlines()[0] == "devkit: 1 broken"
    assert "  #412 agent/sweep-labels-0904 -- merge conflict" in text
    assert "carameli: nothing broken" in text


def test_the_parser_defaults_to_a_watchable_tab_and_offers_only_the_known_modes():
    """The default is the tab because a session that pushes to a real branch and can merge
    a real PR is one worth being able to interrupt; `choices` is `AGENT_MODES` so a row in
    the task and a mode here can never drift apart. No picks and no flags is the plan."""
    parser = fix_prs.build_parser()
    args = parser.parse_args([])
    assert (args.agent, args.picks, args.list, args.dry_run, args.redo) == (
        "claude",
        "",
        False,
        False,
        False,
    )
    action = next(a for a in parser._actions if a.dest == "agent")
    assert sorted(action.choices) == sorted(fix_prs.AGENT_MODES)
