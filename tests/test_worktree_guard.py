"""Tests for the PreToolUse hook that spawns a box instead of refusing an edit.

The decision half is what matters and it is pure: `redirect_decision` gets a path, a
cwd, and the registry, and says whether this edit needs its own box. Everything it
returns None for is a call some other part of the harness already owns, so each of
those is a named test — a hook that fires on the ordinary project session would
cut a worktree per edit rather than one per (session, project).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from support import devkit_project, load_script

guard = load_script("scripts/worktree-guard.py")

# Read from the registry module rather than through `guard`: the coupling under test is
# that the hook *uses* this, and reaching for it via the hook would make a revert that
# drops the import an ImportError at collection instead of a failing assertion.
NOT_PROJECTS = devkit_project.NOT_PROJECTS

PROJECTS = ["carameli", "carameli-b", "ibkr_trader", "apt-finder", "apt-finder-b", "devkit"]


@pytest.fixture
def root(tmp_path):
    """A workspace root with the checkouts on disk, resolved as the hook resolves them."""
    base = tmp_path.resolve()
    for name in PROJECTS:
        (base / name).mkdir()
    (base / ".worktrees" / "carameli--x-0806").mkdir(parents=True)
    return base


@pytest.fixture(autouse=True)
def ledger_root(tmp_path, monkeypatch):
    """Redirect the harness-events ledger into tmp for every test.

    `guard.LEDGER_ROOT` resolves to the checkout the hook lives in -- during a test
    run, the real one -- and many tests here drive blocking flows, each of which
    appends a ledger line. Without this, a green run would salt the workspace's actual
    `logs/harness-events.log` with phantom `guard-spawn-failed` events, which is
    exactly the class `workspace-status.py` surfaces for triage.
    """
    base = tmp_path / "ledger"
    monkeypatch.setattr(guard, "LEDGER_ROOT", base)
    return base


def payload(tool: str = "Edit", path: str = "", cwd: str = "", session: str = "s1") -> str:
    return json.dumps(
        {
            "tool_name": tool,
            "tool_input": {"file_path": path},
            "cwd": cwd,
            "session_id": session,
        }
    )


# --- reading the hook payload -----------------------------------------------


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null"])
def test_malformed_payloads_parse_to_none(raw):
    assert guard.parse_hook_input(raw) is None


@pytest.mark.parametrize("key", ["file_path", "filePath", "path", "notebook_path", "notebookPath"])
def test_edited_path_reads_every_spelling_the_tools_use(key):
    """Edit/Write say file_path, apply_patch says path, NotebookEdit says notebook_path.
    Missing one means the hook silently allows that tool through."""
    assert guard.edited_path({"tool_input": {key: "/ws/carameli/a.py"}}) == "/ws/carameli/a.py"


def test_edited_path_tolerates_camel_case_tool_input():
    assert guard.edited_path({"toolInput": {"file_path": "/ws/carameli/a.py"}})


def test_edited_path_is_empty_when_there_is_none():
    assert guard.edited_path({"tool_input": {}}) == ""
    assert guard.edited_path({"tool_input": "nonsense"}) == ""


# --- who owns a path --------------------------------------------------------


def test_owning_project_finds_the_checkout_containing_the_file(root):
    assert guard.owning_project(root / "carameli" / "app" / "main.py", root, PROJECTS) == "carameli"


def test_owning_project_prefers_the_longest_match(root):
    """`apt-finder` is a prefix of `apt-finder-b`, and they are separate checkouts of
    one repo -- routing an edit to the wrong one is worse than not routing it."""
    target = root / "apt-finder-b" / "src" / "x.py"
    assert guard.owning_project(target, root, PROJECTS) == "apt-finder-b"


def test_owning_project_is_empty_outside_every_checkout(root):
    assert guard.owning_project(root / "notes.md", root, PROJECTS) == ""


# --- the decision -----------------------------------------------------------


def test_an_edit_from_the_workspace_root_gets_its_own_box(root):
    """The case the hook exists for: today this write lands on carameli's home branch
    with no task branch under it, and the next sweep reports it as `needs-branch`."""
    decision = guard.redirect_decision(
        str(root / "carameli" / "app" / "main.py"), str(root), root, PROJECTS
    )
    assert decision == ("carameli", str(Path("app/main.py")))


def on_branch(name: str):
    """A `branch_of` stub: the checkout is on `name`, whatever is asked."""
    return lambda checkout: name


def has_commits(value: bool):
    """A `commits_of_own` stub: the checkout's branch carries work, or does not."""
    return lambda checkout: value


def test_an_edit_inside_a_checkout_on_a_task_branch_is_left_alone(root):
    """Something deliberately checked that branch out -- the "fix PR #42" case.

    Routing it to a fresh box would put the fix somewhere the PR never sees. The
    branch has commits of its own, which is what makes it that case rather than an
    abandoned one; injected, because `root` is a tmp tree and the real probe would
    fail closed and pass this test for the wrong reason.
    """
    assert (
        guard.redirect_decision(
            str(root / "carameli" / "app" / "main.py"),
            str(root / "carameli"),
            root,
            PROJECTS,
            branch_of=on_branch("claude/fix-pr-42-0806"),
            commits_of_own=has_commits(True),
        )
        is None
    )


def test_an_edit_inside_a_checkout_on_its_home_branch_gets_a_box(root):
    """The case `branch-on-write.py` used to own. With that hook retired, an edit here
    would land on the home branch with no task branch under it."""
    decision = guard.redirect_decision(
        str(root / "carameli" / "app" / "main.py"),
        str(root / "carameli"),
        root,
        PROJECTS,
        branch_of=on_branch("master"),
    )
    assert decision == ("carameli", str(Path("app/main.py")))


def test_a_cross_checkout_edit_respects_the_branch_already_checked_out(root):
    """The same "fix PR #42" decline, reached from *outside* the checkout -- which is
    how a workspace-level session reaches it at all.

    `upgrade-project.py` leaves exactly this state when a consumer's commit gate
    rejects an adoption: the checkout is parked on `claude/devkit-upgrade-<mmdd>`
    holding the work, and the fix belongs on that branch. A box cut from
    `origin/<default>` puts it where that branch and its PR never see it, and the
    session sitting in devkit rather than in the consumer changes nothing about that.
    """
    assert (
        guard.redirect_decision(
            str(root / "carameli" / "app" / "main.py"),
            str(root / "devkit"),
            root,
            PROJECTS,
            branch_of=on_branch("claude/devkit-upgrade-0810"),
        )
        is None
    )


def test_a_cross_checkout_edit_is_boxed_when_git_will_not_name_the_branch(root):
    """Asymmetric with the decline inside a checkout, on purpose. There, an unnameable
    branch declines because git may simply be unavailable. From outside, silence is not
    consent: without a name that is positively a task branch, the edit gets a box."""
    for branch in ("", "master"):
        decision = guard.redirect_decision(
            str(root / "carameli" / "app" / "main.py"),
            str(root / "devkit"),
            root,
            PROJECTS,
            branch_of=on_branch(branch),
        )
        assert decision == ("carameli", str(Path("app/main.py"))), branch


def test_an_edit_from_a_subdirectory_is_judged_by_the_checkouts_branch(root):
    for branch, expected in (("claude/x-0806", None), ("master", "carameli")):
        decision = guard.redirect_decision(
            str(root / "carameli" / "app" / "main.py"),
            str(root / "carameli" / "app"),
            root,
            PROJECTS,
            branch_of=on_branch(branch),
        )
        assert (decision[0] if decision else None) == expected


def test_an_unreadable_branch_declines_rather_than_guessing(root):
    """Detached HEAD and "git did not answer" are indistinguishable from here, and
    blocking every edit on a machine without git is the worse failure. `sweep.py` is
    still running and is what catches a detached HEAD."""
    assert (
        guard.redirect_decision(
            str(root / "carameli" / "app" / "main.py"),
            str(root / "carameli"),
            root,
            PROJECTS,
            branch_of=on_branch(""),
        )
        is None
    )


def test_needs_box_is_the_home_branch_predicate():
    assert guard.needs_box("master") is True
    assert guard.needs_box("carameli-b") is True
    assert guard.needs_box("claude/voicemail-0806") is False
    assert guard.needs_box("") is False


def test_needs_box_routes_a_task_branch_with_nothing_on_it():
    """A `claude/...` branch carrying no commits protects no PR, so it is not a reason
    to decline -- it is either freshly cut or already merged."""
    assert guard.needs_box("claude/voicemail-0806", protects_open_work=False) is True
    assert guard.needs_box("claude/voicemail-0806", protects_open_work=True) is False
    # A home branch is routed regardless: there is nothing to protect either way.
    assert guard.needs_box("master", protects_open_work=True) is True
    # And an unnameable branch still declines -- git may simply be unavailable.
    assert guard.needs_box("", protects_open_work=False) is False


def test_an_edit_on_a_spent_task_branch_gets_a_box(root):
    """The regression. Being a `claude/...` branch used to be the whole test, so the
    first session to leave one checked out disabled this hook for every session after
    it -- including one that arrived to find a branch whose PR had already merged, with
    nothing left on it to protect. Two sessions landed in one checkout that way."""
    decision = guard.redirect_decision(
        str(root / "carameli" / "app" / "main.py"),
        str(root / "carameli"),
        root,
        PROJECTS,
        branch_of=on_branch("claude/merged-last-week-0801"),
        commits_of_own=has_commits(False),
    )
    assert decision == ("carameli", str(Path("app/main.py")))


def test_an_edit_from_outside_onto_a_spent_task_branch_gets_a_box(root):
    """Same rule reached from the workspace root, where the decline is stricter."""
    decision = guard.redirect_decision(
        str(root / "carameli" / "app" / "main.py"),
        str(root),
        root,
        PROJECTS,
        branch_of=on_branch("claude/merged-last-week-0801"),
        commits_of_own=has_commits(False),
    )
    assert decision == ("carameli", str(Path("app/main.py")))


def test_an_edit_from_outside_onto_a_task_branch_holding_work_is_left_alone(root):
    """`upgrade-project.py` leaves a checkout parked on `claude/devkit-upgrade-<mmdd>`
    holding the adoption its commit gate rejected. A box would put a fix somewhere that
    branch never sees, and the session being elsewhere does not change that."""
    assert (
        guard.redirect_decision(
            str(root / "carameli" / "app" / "main.py"),
            str(root),
            root,
            PROJECTS,
            branch_of=on_branch("claude/devkit-upgrade-0810"),
            commits_of_own=has_commits(True),
        )
        is None
    )


def test_branch_has_own_commits_fails_closed_when_git_will_not_answer(tmp_path):
    """A directory that is not a repo stands in for every way the probe can fail. The
    hook must not start diverting edits into boxes on the strength of a failed
    subprocess: declining is what it did for every task branch before this existed."""
    assert guard.branch_has_own_commits(tmp_path) is True


def test_an_edit_already_inside_a_box_is_left_alone(root):
    """Otherwise the hook would spawn a box for every edit made in the box it spawned."""
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    assert guard.redirect_decision(str(target), str(root), root, PROJECTS) is None


def test_an_edit_outside_every_checkout_is_left_alone(root):
    """A scratch note beside the projects, the multi-root workspace file itself, the
    lease file -- none of them belongs to a repo, so none of them needs a box."""
    assert guard.redirect_decision(str(root / "notes.md"), str(root), root, PROJECTS) is None


def test_a_relative_path_is_resolved_against_the_session_cwd(root):
    decision = guard.redirect_decision("carameli/app/main.py", str(root), root, PROJECTS)
    assert decision is not None and decision[0] == "carameli"


def test_a_cross_checkout_edit_between_two_projects_is_redirected(root):
    """A devkit session editing a sibling checkout is the same mistake as a
    workspace-root one, which is why devkit wires this hook on itself."""
    decision = guard.redirect_decision(
        str(root / "carameli" / "a.py"), str(root / "devkit"), root, PROJECTS
    )
    assert decision is not None and decision[0] == "carameli"


def test_an_edit_between_a_worktree_pair_is_redirected(root):
    """`carameli` and `carameli-b` are separate checkouts on separate branches. An edit
    from one into the other lands on the other's home branch, exactly like any other
    cross-checkout write."""
    decision = guard.redirect_decision(
        str(root / "carameli-b" / "a.py"), str(root / "carameli"), root, PROJECTS
    )
    assert decision is not None and decision[0] == "carameli-b"


def test_no_path_means_no_decision(root):
    assert guard.redirect_decision("", str(root), root, PROJECTS) is None


# --- git-ignored paths ------------------------------------------------------


def make_repo(path: Path, gitignore: str) -> Path:
    """A real repo on disk: `check-ignore` is the oracle under test, so stubbing git
    here would test nothing but the stub."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(path)], check=True, capture_output=True)
    (path / ".gitignore").write_text(gitignore, encoding="utf-8")
    return path


def test_path_is_ignored_reads_the_projects_own_gitignore(tmp_path):
    """What is ignored is per-project and already written down, which is why this asks
    git rather than carrying a hard-coded list of names."""
    repo = make_repo(tmp_path / "carameli", ".env\n.env.*\nlogs/\n")
    assert guard.path_is_ignored(repo, repo / ".env") is True
    assert guard.path_is_ignored(repo, repo / ".env.local") is True
    assert guard.path_is_ignored(repo, repo / "logs" / "runtime.log") is True


def test_path_is_ignored_is_false_for_an_ordinary_source_file(tmp_path):
    repo = make_repo(tmp_path / "carameli", "*.md\n")
    assert guard.path_is_ignored(repo, repo / "app" / "main.py") is False


def test_a_tracked_file_matching_an_ignore_rule_is_not_ignored(tmp_path):
    """`check-ignore` consults the index (no `--no-index`) on purpose: tracked is
    tracked, and an edit to a tracked file lands on the home branch however its name
    reads -- which is the case the hook exists for."""
    repo = make_repo(tmp_path / "carameli", "*.md\n")
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", "README.md"], check=True, capture_output=True
    )
    assert guard.path_is_ignored(repo, repo / "README.md") is False


def test_path_is_ignored_fails_closed_when_git_will_not_answer(tmp_path):
    """A directory that is not a repo stands in for every way the probe can fail. A hook
    that cannot read the repo must not start letting edits through on the strength of a
    failed subprocess -- it routes them, as it always did."""
    assert guard.path_is_ignored(tmp_path, tmp_path / ".env") is False


def is_ignored(value: bool):
    """An `ignored` stub: the path is git-ignored inside its checkout, or is not."""
    return lambda checkout, target: value


def test_an_edit_to_a_gitignored_path_is_left_alone(root):
    """The premise of every block -- "this would land on the home branch" -- is false for
    an ignored path, so a box protects nothing. Worse, the box has its own seeded `.env`,
    so re-issuing the edit there writes the value into a worktree that is destroyed
    without ever shipping it, and the file that was meant to be configured is unchanged.
    """
    assert (
        guard.redirect_decision(
            str(root / "carameli" / ".env"),
            str(root),
            root,
            PROJECTS,
            branch_of=on_branch("master"),
            ignored=is_ignored(True),
        )
        is None
    )


def test_a_tracked_path_in_the_same_checkout_still_gets_a_box(root):
    """The exemption is per-path, not per-checkout: being ignored is the whole reason it
    is allowed, and the file beside it is unaffected."""
    decision = guard.redirect_decision(
        str(root / "carameli" / "app" / "main.py"),
        str(root),
        root,
        PROJECTS,
        branch_of=on_branch("master"),
        ignored=is_ignored(False),
    )
    assert decision == ("carameli", str(Path("app/main.py")))


# --- what the agent reads ---------------------------------------------------


def test_the_deny_message_leads_with_the_path_to_use():
    message = guard.deny_message(
        "carameli",
        "app/main.py",
        "/ws/.worktrees/carameli--ws-abc-0806",
        "carameli--ws-abc-0806",
        [],
    )
    assert "app/main.py" in message
    assert ".worktrees/carameli--ws-abc-0806" in message.replace("\\", "/")


def test_the_deny_message_names_the_way_out():
    """A block that does not say how to finish is a dead end, which is the thing this
    hook is supposed to not be."""
    message = guard.deny_message("carameli", "a.py", "/ws/.worktrees/b", "b", [])
    assert "/ship" in message
    assert "provision b --yes" in message


def test_the_deny_message_does_not_tell_the_agent_to_reap():
    """It used to, and that now contradicts both `reconcile` and the /ship skill's
    "do not clean up after yourself".

    Reaping at /ship time destroys the box on the strength of the *push* rather than
    the merge -- and if the PR step failed, the push is the one moment the work exists
    only in the box. An instruction that races the thing that owns the lifecycle is
    worse than no instruction.
    """
    message = guard.deny_message("carameli", "a.py", "/ws/.worktrees/b", "b", [])
    assert "reap" not in prose_of(message)
    assert "reconcile" in message
    assert "MERGED" in message


def prose_of(message: str) -> str:
    """`message` up to the "Do NOT reap" line, with this checkout's path elided.

    The `provision` line names `worktree.py` by absolute path, and that path is wherever
    the clone happens to live. devkit's own suite runs from an ephemeral box, and a box
    is named after the task it was cut for -- so the session that changed `reap` was
    working in `devkit--reap-a-box-whose-pr-was-closed-0820`, and the assertion above
    failed on its own working directory. A path is not an instruction to anybody: only
    the prose is read, and the elision is the whole reason this helper exists rather
    than the assertion being loosened.
    """
    return message.replace(str(Path(guard.__file__).parent), "<devkit>").split("Do NOT reap")[0]


def test_the_reap_check_reads_the_prose_and_not_the_checkout_path(monkeypatch, tmp_path):
    """Regression for `prose_of`. `deny_message` resolves its own directory through the
    module global, so a checkout path carrying the word is reproducible rather than
    something only the box that hit it could show."""
    monkeypatch.setattr(guard, "__file__", str(tmp_path / "devkit--reap-0820" / "guard.py"))
    message = guard.deny_message("carameli", "a.py", "/ws/.worktrees/b", "b", [])
    assert "reap" in message.split("Do NOT reap")[0]  # the path is there...
    assert "reap" not in prose_of(message)  # ...and it is not prose


def test_the_block_message_gives_an_absolute_path_from_a_relative_workspace(
    root, monkeypatch, capsys
):
    """The path in the message is the actionable part, and the agent's next tool call
    does not necessarily run in the cwd this hook was invoked from."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    guard.main(["--workspace", workspace.name])
    quoted = capsys.readouterr().err
    assert str(root / ".worktrees" / "carameli--ws-s1-0806") in quoted


def test_the_deny_message_names_the_install_the_box_did_not_get():
    """The hook cuts the box without provisioning it -- an install is minutes and this is
    a tool call the agent is blocked on. So the box that comes back has no `.venv`, and an
    agent that goes straight to `/ship` meets the changed-scope lint gate with no ruff in
    the box to run it."""
    message = guard.deny_message("carameli", "a.py", "/ws/.worktrees/b", "b", [])
    assert "provision b --yes" in message
    assert ".venv" in message


def test_the_deny_message_says_to_ship_with_the_box_as_the_working_directory():
    """`/ship`'s first step is a relative `python scripts/ship.py`, so it reads whichever
    repo it is run in. Run from the session that was blocked, it would preflight THAT
    checkout and report that the box's branch is not the current branch. "ship from
    inside the box" was true and, to a session that cannot cd, not actionable."""
    message = guard.deny_message("carameli", "a.py", "/ws/.worktrees/b", "b", [])
    assert "cd /ws/.worktrees/b" in message


def test_the_block_reason_names_the_branch_it_judged():
    """Whatever the state, the agent can check the message against `git branch` -- which
    is the only way it can tell a wrong reason from a wrong decision."""
    for branch, inside in (
        ("master", True),
        ("master", False),
        ("claude/x-0813", True),
        ("claude/x-0813", False),
    ):
        reason = guard.block_reason("carameli", "a.py", branch, inside)
        assert branch in reason, (branch, inside)


def test_a_freshly_cut_task_branch_is_not_called_a_home_branch():
    """The defect a carameli session reported. It was on `claude/...-0813`, the branch
    carried no commits yet, and the message said the checkout was "parked on a home
    branch" -- so the agent concluded the hook inferred "no task branch" from HEAD being
    level with origin/master and filed that instead of re-issuing the edit in the box.

    The decision was right (a task branch with nothing on it strands nothing, and being
    a task branch used to be the whole test -- see `needs_box`). The sentence was wrong
    twice over, and a message an agent can disprove by looking at its own checkout is
    one it will route around.
    """
    reason = guard.block_reason("carameli", "a.py", "claude/dual-vendor-status-audit-0813", True)
    assert "home branch" not in reason
    assert "claude/dual-vendor-status-audit-0813" in reason
    assert "no commits of its own" in reason


def test_the_block_reason_does_not_tell_an_inside_session_it_is_outside():
    """The first version of the same mistake: telling a session sitting in carameli that
    it "is not inside carameli" reads as a hook bug and invites working around it."""
    assert "not inside" not in guard.block_reason("carameli", "a.py", "master", True)
    assert "not inside" in guard.block_reason("carameli", "a.py", "master", False)


def test_the_block_reason_says_so_when_git_would_not_name_the_branch():
    """Reachable from outside the checkout only -- inside, an unnameable branch declines.
    Naming no branch at all is honest; claiming a home branch would not be."""
    reason = guard.block_reason("carameli", "a.py", "", False)
    assert "would not name a branch" in reason


def test_a_failed_spawn_still_blocks_and_says_how_to_do_it_by_hand():
    """Allowing the edit through would land it on the home branch -- the failure mode
    the hook exists to prevent, so a broken spawn must not fall back to it."""
    message = guard.failure_message("carameli", "a.py", "git worktree add: boom")
    assert "boom" in message
    assert "worktree.py new carameli" in message


def test_session_slug_is_stable_for_one_session():
    assert guard.session_slug("abcdef1234") == guard.session_slug("abcdef1234")
    assert guard.session_slug("abcdef1234") != guard.session_slug("zzzzzz9999")


def test_session_slug_survives_a_missing_session_id():
    assert guard.session_slug("") == "ws"


# --- the shell --------------------------------------------------------------


def test_absent_workspace_file_allows_everything(tmp_path, monkeypatch):
    """CI, a fresh clone, anyone else's machine: no multi-root registry means no
    cross-checkout edit is possible, so silence is correct rather than an error."""
    monkeypatch.setattr("sys.stdin", _stdin(payload(path="/anything")))
    assert guard.main(["--workspace", str(tmp_path / "nope.code-workspace")]) == guard.EXIT_ALLOW


def test_a_non_mutating_tool_is_allowed(tmp_path, root, monkeypatch):
    workspace = _workspace(root)
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(tool="Read", path=str(root / "carameli" / "a.py")))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_an_in_checkout_edit_on_a_task_branch_is_allowed(root, monkeypatch):
    workspace = _workspace(root)
    monkeypatch.setattr(guard, "current_branch", on_branch("claude/voicemail-0806"))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root / "carameli"))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


@pytest.mark.parametrize("reference", sorted(NOT_PROJECTS))
def test_an_edit_into_a_reference_checkout_is_allowed(root, monkeypatch, reference):
    """A checkout in `folders` but not in the registry gets no box, whatever branch it
    is parked on.

    The wiring is the assertion: `main` builds its project list with `known_projects`,
    which subtracts `NOT_PROJECTS`, rather than with `sweep.parse_workspace` directly.
    Read raw, the registry would put `VanillaLand` behind a block and cut it an
    ephemeral box on a `claude/...` branch -- for a reference checkout that ships
    nothing, has no harness, and whose Azure DevOps remote has no PR for that branch to
    become. `redirect_decision` cannot express this: it takes the project list as an
    argument, so only the shell can be wrong about it.
    """
    (root / reference).mkdir()
    workspace = _workspace(root, extra=[reference])
    # A home branch, which is the case that would otherwise be boxed.
    monkeypatch.setattr(guard, "current_branch", on_branch("develop"))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(payload(path=str(root / reference / "AppCode" / "a.cs"), cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


@pytest.mark.parametrize("reference", sorted(NOT_PROJECTS))
def test_a_project_scoped_session_may_still_edit_a_reference_checkout(root, monkeypatch, reference):
    """The exemption is a property of the *target*, not of where the session is rooted.

    The case above runs from the workspace root, which is the multi-root session. The
    commoner one is a session scoped to a single project -- an agent working in carameli
    that is asked to change the reference checkout too -- and it reaches this hook with a
    cwd inside a registered checkout, which is the shape that IS boxed when the target is
    a project. Nothing may make that difference decide this: the reference checkout has
    no PR to open and no branch to open it from, so a box for it is a dead end wherever
    the edit was issued from. `main` resolves its registry from `--workspace`, never from
    the cwd, which is what keeps the two shapes identical.
    """
    (root / reference).mkdir()
    workspace = _workspace(root, extra=[reference])
    monkeypatch.setattr(guard, "current_branch", on_branch("develop"))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(path=str(root / reference / "AppCode" / "a.cs"), cwd=str(root / "carameli"))
        ),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_an_in_checkout_edit_on_a_home_branch_is_blocked_and_says_why(root, monkeypatch, capsys):
    """The message must not tell a session sitting in carameli that it "is not inside
    carameli" -- that reads as a hook bug and invites working around it."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard, "current_branch", on_branch("master"))
    monkeypatch.setattr(guard.worktree, "plan_new", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root / "carameli"))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    err = capsys.readouterr().err
    assert "parked on 'master', a home branch" in err
    assert "not inside" not in err


def test_the_block_message_names_the_branch_the_decision_was_made_on(root, monkeypatch, capsys):
    """The wiring, not the wording: `main` judges the branch and then has to report the
    same one. Reading it a second time for the message would be a second subprocess and
    could name a different branch than the one that was judged.

    The state is the reported defect end to end -- inside the checkout, on a task branch
    with no commits of its own -- which `block_reason` alone cannot pin, because it takes
    the branch as an argument and only the shell can fail to supply it.
    """
    workspace = _workspace(root)
    monkeypatch.setattr(guard, "current_branch", on_branch("claude/dual-vendor-status-audit-0813"))
    monkeypatch.setattr(guard, "branch_has_own_commits", has_commits(False))
    monkeypatch.setattr(guard.worktree, "plan_new", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root / "carameli"))),
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    err = capsys.readouterr().err
    assert "claude/dual-vendor-status-audit-0813" in err
    assert "home branch" not in err


def test_a_session_reuses_the_box_it_already_has(root, monkeypatch, capsys):
    """One box per (session, project). Without this the hook cuts a worktree per edit,
    which would be worse than the problem it solves."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    err = capsys.readouterr().err
    assert "carameli--ws-s1-0806" in err
    # "a box has been spawned" on the fortieth edit is simply untrue, and a message
    # that misdescribes what happened is how an agent concludes it is looping.
    assert "already has a box" in err
    assert "has been spawned" not in err


def test_the_hook_never_waits_for_an_install(root, monkeypatch, capsys):
    """A PreToolUse hook holds the agent's tool call open for as long as it runs, and a
    cold `uv sync` is minutes. Provisioning here is experienced as a hang and eventually
    killed by the harness, leaving a half-installed box and no message at all -- so the
    hook cuts the box, and `deny_message` carries the install command instead."""
    workspace = _workspace(root)
    seen: dict = {}

    def fake_apply_new(plan, ws, timeout=300.0, provision=True):
        seen["timeout"] = timeout
        seen["provision"] = provision
        return True, []

    monkeypatch.setattr(guard.worktree, "plan_new", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", fake_apply_new)
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert seen == {"timeout": guard.SPAWN_TIMEOUT, "provision": False}
    assert "provision carameli--ws-s1-0806 --yes" in capsys.readouterr().err


def test_the_spawn_is_planned_quietly(root, monkeypatch, capsys):
    """This process's stderr IS the block message -- Claude Code surfaces an exit-2
    hook's stderr as `PreToolUse:<tool> hook error`, every line of it. `plan_provision`
    warns on stderr when a project's interpreter pin has to be inferred (no `[python]
    version` in `.devkit.toml`), so without `quiet=True` that warning opened every
    guard block in such a project, ahead of the actual reason, reading as a hook
    failure. The warning is not lost: `worktree.py new` and `provision` still plan
    loudly, and provisioning is where it is actionable."""
    workspace = _workspace(root)
    seen: dict = {}

    def fake_plan_new(*args, **kwargs):
        seen.update(kwargs)
        return _plan(root)

    monkeypatch.setattr(guard.worktree, "plan_new", fake_plan_new)
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert seen.get("quiet") is True


def _plan(root):
    """The SpawnPlan a stubbed `plan_new` hands back."""
    name = "carameli--ws-s1-0806"
    return guard.worktree.SpawnPlan(
        box=guard.worktree.Box(name=name, project="carameli", branch="claude/ws-s1-0806"),
        path=str(root / ".worktrees" / name),
    )


def test_an_edit_into_the_sessions_own_box_is_left_alone(root, monkeypatch):
    """The ownership gate must not tax the normal case: the fortieth edit into the box
    this session was routed to stays silent."""
    workspace = _workspace(root)
    _lease(root, "carameli--x-0806", project="carameli", session="s1")
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    monkeypatch.setattr("sys.stdin", _stdin(payload(path=str(target), cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_an_edit_into_another_sessions_box_is_blocked_toward_its_own(root, monkeypatch, capsys):
    """The collision this prevents happened: a second session found a live box through
    `worktree.py list`, adopted it because the topic matched its task, and two sessions'
    edits interleaved in one worktree until one noticed files changing under it
    mid-turn. One box per (session, project) only holds if the box side of the boundary
    is guarded too, not just the checkout side."""
    workspace = _workspace(root)
    _lease(root, "carameli--x-0806", project="carameli", session="other-session")
    monkeypatch.setattr(guard.worktree, "plan_new", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    monkeypatch.setattr("sys.stdin", _stdin(payload(path=str(target), cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    err = capsys.readouterr().err
    assert "carameli--x-0806" in err and "different session" in err
    # Routed to a box of its own, and told how a sanctioned takeover looks instead.
    assert "carameli--ws-s1-0806" in err
    assert "claim carameli--x-0806 --session s1 --yes" in err


def test_a_foreign_box_edit_reuses_the_sessions_existing_box(root, monkeypatch, capsys):
    workspace = _workspace(root)
    (root / ".worktrees" / "carameli--mine-0806").mkdir(parents=True)
    (root / ".worktrees" / "leases.json").write_text(
        json.dumps(
            {
                "boxes": {
                    "carameli--x-0806": {
                        "branch": "agent/x-0806",
                        "project": "carameli",
                        "session": "other-session",
                    },
                    "carameli--mine-0806": {
                        "branch": "agent/mine-0806",
                        "project": "carameli",
                        "session": "s1",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    monkeypatch.setattr("sys.stdin", _stdin(payload(path=str(target), cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    err = capsys.readouterr().err
    assert "carameli--mine-0806" in err and "already has a box" in err


def test_a_box_leased_under_an_abbreviated_session_id_still_admits_its_session(root, monkeypatch):
    """A hand-cut `worktree.py new --session <first 8 hex>` box must keep admitting the
    session that abbreviation names -- blocking it out of its own box would be the gate
    manufacturing the very collision it exists to prevent."""
    workspace = _workspace(root)
    _lease(root, "carameli--x-0806", project="carameli", session="da11d826")
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(
                path=str(target),
                cwd=str(root),
                session="da11d826-371d-41ec-b5ee-77aabc7d119f",
            )
        ),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_an_unowned_box_is_left_alone(root, monkeypatch):
    """An adopted orphan carries no session (`worktree.py` cannot rebuild one), so there
    is no owner to defend and blocking would dead-end every box that survived a lost
    lease file."""
    workspace = _workspace(root)
    _lease(root, "carameli--x-0806", project="carameli", session="")
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    monkeypatch.setattr("sys.stdin", _stdin(payload(path=str(target), cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_a_non_box_path_under_worktrees_is_left_alone(root, monkeypatch):
    """`leases.json`, the `slugs/` directory, a stray folder: no lease, no owner, no
    block -- the status quo for everything in `.worktrees/` that is not a live box."""
    workspace = _workspace(root)
    _lease(root, "carameli--x-0806", project="carameli", session="other-session")
    target = root / ".worktrees" / "slugs" / "some-session"
    monkeypatch.setattr("sys.stdin", _stdin(payload(path=str(target), cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_the_reason_goes_to_stderr_because_stdout_is_not_surfaced(root, monkeypatch, capsys):
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    guard.main(["--workspace", str(workspace)])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


# --- helpers ----------------------------------------------------------------


class _stdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


def _workspace(root, extra=()):
    path = root / "alex-projects.code-workspace"
    path.write_text(
        json.dumps({"folders": [{"path": name} for name in [*PROJECTS, *extra]]}),
        encoding="utf-8",
    )
    return path


def _lease(root, name, **fields):
    """Record a live box, directory and all -- `live_boxes` filters on the directory."""
    (root / ".worktrees" / name).mkdir(parents=True, exist_ok=True)
    (root / ".worktrees" / "leases.json").write_text(
        json.dumps({"boxes": {name: {"branch": f"claude/{name.split('--')[1]}", **fields}}}),
        encoding="utf-8",
    )


# --- picking a reaped box back up -------------------------------------------


def _resumed_plan(root):
    """What `plan_respawn` hands back when the session's box was reaped mid-task."""
    name = "carameli--voicemail-0806"
    return guard.worktree.SpawnPlan(
        box=guard.worktree.Box(name=name, project="carameli", branch="agent/voicemail-0806"),
        path=str(root / ".worktrees" / name),
        resumed=True,
    )


def test_the_guard_asks_for_a_resume_before_it_cuts_a_new_branch(root, monkeypatch):
    """The reversion check for the whole recovery. `reconcile` reaps a box whose PR is
    still open when the disk is tight, so the session's next edit arrives here with no
    box -- and with `plan_new` in this line, it silently continued the same task on a
    second branch under a second PR. Nothing in either PR said so."""
    workspace = _workspace(root)
    asked = {}
    monkeypatch.setattr(
        guard.worktree,
        "plan_respawn",
        lambda project, ws, **kw: asked.update(project=project, **kw) or _plan(root),
    )
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert asked["project"] == "carameli"
    assert asked["session"] == "s1"


def test_a_resumed_box_says_it_already_carries_this_sessions_commits(root, monkeypatch, capsys):
    """Said out loud because the alternative is an agent assuming an empty box: a
    resumed one has an open PR, so `/ship` updates that PR rather than opening one, and
    a `git log` in it is otherwise a mystery."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _resumed_plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    err = capsys.readouterr().err
    assert "resumed agent/voicemail-0806" in err
    assert "reaped" in err and "commits are on this branch" in err


def test_a_freshly_cut_box_is_not_announced_as_a_resume(root, monkeypatch, capsys):
    """The ordinary case must stay silent about commits it does not have."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert "resumed" not in capsys.readouterr().err


# --- the harness-events ledger ------------------------------------------------


def _events(base: Path) -> list[str]:
    path = base / "logs" / "harness-events.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _blocked_edit(root, monkeypatch, target=None):
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(payload(path=str(target or root / "carameli" / "a.py"), cwd=str(root))),
    )
    return _workspace(root)


def test_a_block_that_spawns_lands_on_the_ledger(root, monkeypatch, ledger_root, capsys):
    workspace = _blocked_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\tevent=guard-block\t" in line
    assert "\tproject=carameli\t" in line and "\tsession=s1\t" in line
    assert "\tkind=checkout\t" in line and "\toutcome=spawned\t" in line
    assert "\tbox=carameli--ws-s1-0806\t" in line
    assert "\ttarget=" in line
    capsys.readouterr()


def test_a_resumed_box_is_recorded_as_a_resume(root, monkeypatch, ledger_root, capsys):
    workspace = _blocked_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _resumed_plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\toutcome=resumed\t" in line
    assert "\tbranch=agent/voicemail-0806\t" in line
    capsys.readouterr()


def test_a_reused_box_is_recorded_as_a_reuse(root, monkeypatch, ledger_root, capsys):
    workspace = _blocked_edit(root, monkeypatch)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\tevent=guard-block\t" in line and "\toutcome=reused\t" in line
    capsys.readouterr()


def test_a_failed_spawn_is_recorded_with_its_detail(root, monkeypatch, ledger_root, capsys):
    workspace = _blocked_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (False, ["boom"]))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\tevent=guard-spawn-failed\t" in line
    assert "\tdetail=boom" in line
    capsys.readouterr()


def test_a_spawn_that_raises_is_recorded_with_the_exception(root, monkeypatch, ledger_root, capsys):
    workspace = _blocked_edit(root, monkeypatch)

    def explode(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(guard.worktree, "plan_respawn", explode)
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\tevent=guard-spawn-failed\t" in line
    assert "RuntimeError: kaput" in line
    capsys.readouterr()


def test_a_foreign_box_block_is_recorded_with_its_kind(root, monkeypatch, ledger_root, capsys):
    workspace = _workspace(root)
    _lease(root, "carameli--x-0806", project="carameli", session="other-session")
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    monkeypatch.setattr("sys.stdin", _stdin(payload(path=str(target), cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\tkind=foreign-box\t" in line
    capsys.readouterr()


def test_an_allowed_edit_leaves_no_ledger_line(root, monkeypatch, ledger_root):
    """The ledger records what the harness did to an agent; the silent majority of
    calls it waves through must stay off it, or grepping it means wading."""
    workspace = _workspace(root)
    _lease(root, "carameli--x-0806", project="carameli", session="s1")
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    monkeypatch.setattr("sys.stdin", _stdin(payload(path=str(target), cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert _events(ledger_root) == []


def test_a_missing_ledger_module_never_changes_the_decision(root, monkeypatch, capsys):
    """The guarded import is the guard's own safety: an unhandled exception in a
    PreToolUse hook exits non-2, which Claude Code treats as non-blocking -- the edit
    would PROCEED onto the home branch. Diagnostics must degrade to silence instead."""
    workspace = _blocked_edit(root, monkeypatch)
    monkeypatch.setattr(guard, "harness_events", None)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    capsys.readouterr()
