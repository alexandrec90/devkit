"""Tests for the PreToolUse hook that spawns a box instead of refusing an edit.

The decision half is what matters and it is pure: `redirect_decision` gets a path, a
cwd, and the registry, and says whether this edit needs its own box. Everything it
returns None for is a call some other part of the harness already owns, so each of
those is a named test — a hook that fires on the ordinary project session would
cut a worktree per edit rather than one per (session, project).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from support import REPO_ROOT, devkit_project, load_script

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
    """Isolate process-global hook state and redirect the ledger into tmp.

    `guard.LEDGER_ROOT` resolves to the checkout the hook lives in -- during a test
    run, the real one -- and many tests here drive blocking flows, each of which
    appends a ledger line. Without this, a green run would salt the workspace's actual
    `logs/harness-events.log` with phantom `guard-spawn-failed` events, which is
    exactly the class `workspace-status.py` surfaces for triage.

    The Stop hook can run this suite as a descendant of `codex-hook-adapter.py`, whose
    marker is meaningful to the hook process but is not a default scenario for these
    unit tests. Tests of the adapter branch set it explicitly; every other case must
    start from the ordinary Claude response contract regardless of its parent process.
    """
    base = tmp_path / "ledger"
    monkeypatch.delenv(guard.ADAPTER_ENV, raising=False)
    monkeypatch.setattr(guard, "LEDGER_ROOT", base)
    return base


def payload(
    tool: str = "Edit",
    path: str = "",
    cwd: str = "",
    session: str = "s1",
    key: str = "file_path",
    **arguments,
) -> str:
    return json.dumps(
        {
            "tool_name": tool,
            "tool_input": {key: path, **arguments},
            "cwd": cwd,
            "session_id": session,
        }
    )


def guidance(capsys) -> str:
    """The text the hook put in front of the agent, whichever way it routed the edit.

    Both outcomes deliver the same prose; only the channel differs, because only one of
    them is a block. A refusal writes it to stderr, which Claude Code surfaces as the
    hook error; a re-aim carries it as `additionalContext` in the structured response on
    stdout. Nearly every assertion below is about the prose rather than the channel, so
    they read it through here -- the two tests that ARE about the channel read `capsys`
    directly, and are named for it.
    """
    captured = capsys.readouterr()
    if not captured.out.strip():
        return captured.err
    return json.loads(captured.out)["hookSpecificOutput"]["additionalContext"]


def response(capsys) -> dict:
    """The `hookSpecificOutput` object the hook wrote to stdout."""
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]


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


def test_the_deny_message_says_how_to_report_the_block_as_wrong():
    """The block is where the reporting channel has to be named, because it is the only
    place both runtimes reach.

    The instruction to file a harness defect lives in `.claude/rules/engineering.md`,
    and Codex reads straight past `.claude/rules/`: no `CLAUDE.md` names the reporter,
    `sync-codex-context.py` mirrors only `.claude/skills/`, and the adapter discards
    SessionStart output. The ledger measured the consequence -- 1176 rows on
    2026-08-28, of which Codex had written seven guard-blocks and *zero* reports. An
    agent that cannot see the channel routes around the gate instead, which is exactly
    what the guardrail asks it not to do.
    """
    message = guard.deny_message("carameli", "a.py", "/ws/.worktrees/b", "b", [])
    assert "report-harness-defect.py" in message


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


def test_the_routed_path_is_absolute_from_a_relative_workspace(root, monkeypatch, capsys):
    """The path is the actionable part, and it is now also the argument the tool is
    about to be called with -- which cannot be relative to a cwd this hook happens to
    have been invoked from and the tool does not necessarily share."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    guard.main(["--workspace", workspace.name])
    assert response(capsys)["updatedInput"]["file_path"] == str(
        root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    )


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


def test_an_in_checkout_edit_on_a_home_branch_is_re_aimed_and_says_why(root, monkeypatch, capsys):
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
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = guidance(capsys)
    assert "parked on 'master', a home branch" in said
    assert "not inside" not in said


def test_the_message_names_the_branch_the_decision_was_made_on(root, monkeypatch, capsys):
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

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = guidance(capsys)
    assert "claude/dual-vendor-status-audit-0813" in said
    assert "home branch" not in said


def test_a_session_reuses_the_box_it_already_has(root, monkeypatch, capsys):
    """One box per (session, project). Without this the hook cuts a worktree per edit,
    which would be worse than the problem it solves."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = guidance(capsys)
    assert "carameli--ws-s1-0806" in said
    # "a box has been spawned" on the fortieth edit is simply untrue, and a message
    # that misdescribes what happened is how an agent concludes it is looping.
    assert "already has a box" in said
    assert "has been spawned" not in said


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

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert seen == {"timeout": guard.SPAWN_TIMEOUT, "provision": False}
    assert "provision carameli--ws-s1-0806 --yes" in guidance(capsys)


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

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
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


def test_an_edit_into_another_sessions_box_is_routed_toward_its_own(root, monkeypatch, capsys):
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
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = guidance(capsys)
    assert "carameli--x-0806" in said and "different session" in said
    # Routed to a box of its own, and told how a sanctioned takeover looks instead.
    assert "carameli--ws-s1-0806" in said
    assert "claim carameli--x-0806 --session s1 --yes" in said


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
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = guidance(capsys)
    assert "carameli--mine-0806" in said and "already has a box" in said


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


def test_a_refusal_puts_its_reason_on_stderr_because_a_blocked_hooks_stdout_is_dropped(
    root, monkeypatch, capsys
):
    """The two channels are not interchangeable. Claude Code reads an exit-2 hook's
    stderr and discards its stdout, so a fallback block that wrote JSON would refuse the
    edit and say nothing about why."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setenv(guard.ADAPTER_ENV, "codex")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


def test_a_re_aim_puts_its_response_on_stdout_because_that_is_where_the_rewrite_is_read(
    root, monkeypatch, capsys
):
    """And the mirror image: `updatedInput` is only read off a rc-0 hook's stdout, so an
    allow whose reason went to stderr would re-aim the edit and explain nothing."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["hookSpecificOutput"]["updatedInput"]


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
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
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
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = guidance(capsys)
    assert "resumed agent/voicemail-0806" in said
    assert "reaped" in said and "commits are on this branch" in said


def test_a_freshly_cut_box_is_not_announced_as_a_resume(root, monkeypatch, capsys):
    """The ordinary case must stay silent about commits it does not have."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert "resumed" not in guidance(capsys)


# --- re-aiming the call, and the four ways it falls back to a refusal ----------


def test_the_edit_is_re_aimed_rather_than_refused(root, monkeypatch, capsys):
    """The whole point of the change. A refusal costs a failed tool call, the agent's
    re-issue of the same arguments -- for a `Write`, the entire file content a second
    time -- and a block message in the transcript on every one of them. `updatedInput`
    rewrites the arguments the tool is called with, so the edit lands in the box on the
    first attempt and the prose arrives as context rather than as an error."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = response(capsys)
    assert said["hookEventName"] == "PreToolUse"
    assert said["updatedInput"]["file_path"] == str(
        root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    )
    assert said["additionalContext"]


def test_the_re_aim_names_no_permission_decision(root, monkeypatch, capsys):
    """The reversion check for the one line that makes any of this work. Claude Code
    applies `updatedInput` only when the same object sets no `permissionDecision` --
    adding an explicit `"allow"` for symmetry, which reads as harmless, silently drops
    the rewrite and lands the edit on the home branch."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert "permissionDecision" not in response(capsys)


def test_the_re_aim_keeps_every_other_argument(root, monkeypatch, capsys):
    """`updatedInput` replaces the arguments wholesale rather than merging into them,
    so anything not copied across is dropped -- a `Write` would arrive with no content
    and truncate the file it was re-aimed at."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(
                tool="Write",
                path=str(root / "carameli" / "a.py"),
                cwd=str(root),
                content="print('hi')\n",
            )
        ),
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert response(capsys)["updatedInput"]["content"] == "print('hi')\n"


def test_the_re_aim_writes_the_path_back_under_the_key_it_was_read_from(root, monkeypatch, capsys):
    """An unrecognised key is not an error: Claude Code logs the mismatch as
    `permission_updated_input_invalid` and calls the tool with the ORIGINAL arguments,
    so guessing `file_path` for a `NotebookEdit` is a silent landing on the home
    branch."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(
                tool="NotebookEdit",
                path=str(root / "carameli" / "a.ipynb"),
                cwd=str(root),
                key="notebook_path",
            )
        ),
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    arguments = response(capsys)["updatedInput"]
    assert "file_path" not in arguments
    assert arguments["notebook_path"] == str(
        root / ".worktrees" / "carameli--ws-s1-0806" / "a.ipynb"
    )


def test_the_re_aim_says_the_original_path_will_now_disagree(root, monkeypatch, capsys):
    """The cost of re-aiming rather than refusing: the agent asked to edit one file and
    a different one changed. Left unsaid, its next `Read` of the path it named returns
    the file without the change, and the obvious reading of that is that the write
    failed."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = guidance(capsys)
    assert "Nothing was written to carameli" in said
    assert "contradict" in said


def test_a_tool_whose_arguments_the_guard_cannot_re_aim_is_still_refused(root, monkeypatch, capsys):
    """`MUTATING_TOOLS` is deliberately wider than `REWRITABLE_TOOLS`: it carries
    Codex's `apply_patch` and `create_file`, whose argument shapes this hook does not
    model. Rewriting a shape you are guessing at is the one failure mode with no
    symptom -- the tool is called with something it accepts and the guard reports
    success."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(payload(tool="apply_patch", path=str(root / "carameli" / "a.py"), cwd=str(root))),
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert "not re-aimed automatically" in capsys.readouterr().err


def test_under_a_hook_adapter_the_edit_is_refused_rather_than_re_aimed(root, monkeypatch, capsys):
    """`codex-hook-adapter.py` passes a rc-0 hook's stdout through verbatim, so whether a
    re-aim takes effect is decided entirely by the other runtime. If it does not, the
    response reads as a bare allow and the edit lands on the home branch -- the exact
    outcome this hook exists to prevent, arriving silently.

    Note this is caution, not a known absence: Codex's PreToolUse schema *does* carry
    `updatedInput` (`scripts/hooks/codex-hook-schema.json`). Nobody has yet watched a
    live Codex session honour one, and this hook's asymmetry says an unverified rewrite
    is worse than a needless block. An earlier version of this docstring asserted Codex
    had no such member, which was a guess stated as a fact -- the same guess that got
    the adapter's member classification wrong."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setenv(guard.ADAPTER_ENV, "codex")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert guard.ADAPTER_ENV in capsys.readouterr().err


def test_the_adapter_seam_is_spelled_the_same_way_in_both_files(root):
    """The two cannot import each other -- the adapter is vendored into every project
    and this hook is devkit's alone -- so the constant is duplicated, and a rename on
    either side turns the fallback above off with nothing red. This is the only tree
    where both files exist, so it is the only place the pair can be compared."""
    adapter = Path(guard.__file__).resolve().parent / "hooks" / "codex-hook-adapter.py"
    assert f'ADAPTER_ENV = "{guard.ADAPTER_ENV}"' in adapter.read_text(encoding="utf-8")


def test_an_old_string_the_box_copy_does_not_have_is_refused(root, monkeypatch, capsys):
    """The box holds `origin/<default>`'s copy of the file; the agent read the
    checkout's, which may be ahead of it. Re-aiming an `Edit` whose `old_string` is not
    in the box copy turns a clear block into `String to replace not found`, reported
    against a path the agent never named and cannot account for."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    box_copy = root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    box_copy.parent.mkdir(parents=True, exist_ok=True)
    box_copy.write_text("what origin has\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(
                path=str(root / "carameli" / "a.py"),
                cwd=str(root),
                old_string="what the checkout has",
                new_string="something else",
            )
        ),
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert "not re-aimed automatically" in capsys.readouterr().err


def test_an_old_string_the_box_copy_does_have_is_re_aimed(root, monkeypatch, capsys):
    """The check is narrow on purpose: it refuses only the edits that would fail, not
    every `Edit`. A box copy that already carries the text is the ordinary case."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    box_copy = root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    box_copy.parent.mkdir(parents=True, exist_ok=True)
    box_copy.write_text("what origin has\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(
                path=str(root / "carameli" / "a.py"),
                cwd=str(root),
                old_string="what origin has",
                new_string="something else",
            )
        ),
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert response(capsys)["updatedInput"]["new_string"] == "something else"


def test_a_multi_edit_is_judged_by_every_one_of_its_edits(root, monkeypatch, capsys):
    """`MultiEdit` applies its edits in sequence and fails the whole call on the first
    `old_string` it cannot find, so one absent string makes the re-aim unsafe however
    many of the others are present."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    box_copy = root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    box_copy.parent.mkdir(parents=True, exist_ok=True)
    box_copy.write_text("first\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(
                tool="MultiEdit",
                path=str(root / "carameli" / "a.py"),
                cwd=str(root),
                edits=[
                    {"old_string": "first", "new_string": "1"},
                    {"old_string": "second", "new_string": "2"},
                ],
            )
        ),
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert "not re-aimed automatically" in capsys.readouterr().err


def test_a_box_copy_that_does_not_exist_yet_is_refused_rather_than_guessed_at(
    root, monkeypatch, capsys
):
    """A fresh box has only tracked files, and an `Edit` naming an `old_string` for a
    file that is not there cannot succeed. Refusing says so; re-aiming produces a file
    -not-found against the box path instead."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(
                path=str(root / "carameli" / "a.py"),
                cwd=str(root),
                old_string="anything",
                new_string="else",
            )
        ),
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert "not re-aimed automatically" in capsys.readouterr().err


def test_a_refused_re_aim_still_carries_the_whole_way_out(root, monkeypatch, capsys):
    """A fallback block is the old behaviour and has to stay as actionable as it was:
    the box path, the provision command and the ship instructions, plus the one new
    sentence saying why this particular call was not re-aimed."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setenv(guard.ADAPTER_ENV, "codex")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    err = capsys.readouterr().err
    assert str(root / ".worktrees" / "carameli--ws-s1-0806" / "a.py") in err
    assert "provision carameli--ws-s1-0806 --yes" in err
    assert "Do NOT reap" in err


def test_a_refused_re_aim_is_recorded_as_a_block_and_not_as_a_route(
    root, monkeypatch, ledger_root, capsys
):
    """The ledger is read by grep, so the two outcomes have to stay distinguishable:
    a route costs the agent nothing and is background, while a block is a failed tool
    call and is the thing worth triaging."""
    workspace = _guarded_edit(root, monkeypatch)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setenv(guard.ADAPTER_ENV, "codex")

    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\tevent=guard-block\t" in line
    capsys.readouterr()


# --- the harness-events ledger ------------------------------------------------


def _events(base: Path) -> list[str]:
    path = base / "logs" / "harness-events.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _guarded_edit(root, monkeypatch, target=None):
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(payload(path=str(target or root / "carameli" / "a.py"), cwd=str(root))),
    )
    return _workspace(root)


def test_a_route_that_spawns_lands_on_the_ledger(root, monkeypatch, ledger_root, capsys):
    workspace = _guarded_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    (line,) = _events(ledger_root)
    assert "\tevent=guard-route\t" in line
    assert "\tproject=carameli\t" in line and "\tsession=s1\t" in line
    assert "\tkind=checkout\t" in line and "\toutcome=spawned\t" in line
    assert "\tbox=carameli--ws-s1-0806\t" in line
    assert "\ttarget=" in line
    capsys.readouterr()


def test_a_resumed_box_is_recorded_as_a_resume(root, monkeypatch, ledger_root, capsys):
    workspace = _guarded_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _resumed_plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    (line,) = _events(ledger_root)
    assert "\toutcome=resumed\t" in line
    assert "\tbranch=agent/voicemail-0806\t" in line
    capsys.readouterr()


def test_a_reused_box_is_recorded_as_a_reuse(root, monkeypatch, ledger_root, capsys):
    workspace = _guarded_edit(root, monkeypatch)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    (line,) = _events(ledger_root)
    assert "\tevent=guard-route\t" in line and "\toutcome=reused\t" in line
    capsys.readouterr()


def test_a_failed_spawn_is_recorded_with_its_detail(root, monkeypatch, ledger_root, capsys):
    workspace = _guarded_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (False, ["boom"]))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\tevent=guard-spawn-failed\t" in line
    assert "\tdetail=boom" in line
    capsys.readouterr()


def test_a_spawn_that_raises_is_recorded_with_the_exception(root, monkeypatch, ledger_root, capsys):
    workspace = _guarded_edit(root, monkeypatch)

    def explode(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(guard.worktree, "plan_respawn", explode)
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    (line,) = _events(ledger_root)
    assert "\tevent=guard-spawn-failed\t" in line
    assert "RuntimeError: kaput" in line
    capsys.readouterr()


# --- the two registrations of this hook race for one box -----------------------
#
# It is registered in the user's `settings.json` and in the project's, and Claude Code
# fires both on the same tool call. They plan the same box for the same session at the
# same instant; the loser's `git worktree add` dies on the branch the winner has just
# created. On 2026-08-23 an agent was handed both halves at once -- "Spawning a box for
# it failed: ... a branch named 'agent/pr-merged-0823' already exists" as the error, and
# "the edit was applied there instead" as the additionalContext -- with nothing written
# anywhere. The context reads as authoritative, and an agent that believes it goes on to
# edit a file it thinks already carries its change, or ships an empty branch.


def _lost_the_race(root, name="carameli--ws-s1-0806", session="s1"):
    """An `apply_new` that fails the way the losing process really fails.

    The lease appearing *during* the call is the part that matters: the winner
    registers its box between this process's plan and its `worktree add`, which is
    exactly why the answer is available by the time the failure is handled.
    """

    def apply_new(plan, ws, timeout=300.0, provision=True):
        _lease(root, name, project="carameli", session=session)
        return False, [
            "FAILED at `git worktree add --no-track -b agent/ws-s1-0806 ...`: fatal: "
            "a branch named 'agent/ws-s1-0806' already exists"
        ]

    return apply_new


def test_a_spawn_that_lost_the_race_routes_into_the_winners_box(root, monkeypatch, capsys):
    """The regression. Blocking here contradicted the other process's own response."""
    workspace = _guarded_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", _lost_the_race(root))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    answer = response(capsys)
    box = root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    assert answer["updatedInput"]["file_path"] == str(box)
    assert "already exists" in answer["additionalContext"]  # the failure is not hidden
    assert "Cut one by hand" not in answer["additionalContext"]


def test_a_spawn_that_failed_with_no_box_anywhere_still_blocks(root, monkeypatch, capsys):
    """The reversion check for the recovery: it must not become an allow. Nothing was
    cut, so there is nowhere to aim, and letting the edit through puts it on the home
    branch -- the one outcome this hook exists to prevent."""
    workspace = _guarded_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (False, ["boom"]))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    said = guidance(capsys)
    assert "Spawning a box for it failed: boom" in said
    assert "the edit was applied there instead" not in said


def test_a_lost_race_is_recorded_as_raced_rather_than_as_a_failure(
    root, monkeypatch, ledger_root, capsys
):
    """`workspace-status.py` surfaces `guard-spawn-failed` for triage. A race that
    recovered is not a defect anyone needs to look at, and filing it as one buries the
    spawns that really did fail."""
    workspace = _guarded_edit(root, monkeypatch)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", _lost_the_race(root))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    (line,) = _events(ledger_root)
    assert "\tevent=guard-spawn-raced\t" in line
    assert "\tbox=carameli--ws-s1-0806\t" in line and "\toutcome=raced\t" in line
    assert "already exists" in line
    capsys.readouterr()


def test_the_box_cut_while_this_hook_waited_is_found_by_re_reading(root, monkeypatch, capsys):
    """`main` reads the leases before the spawn lock is waited on, so its snapshot is
    the one mapping that cannot answer 'does this session have a box yet'. Re-reading
    under the lock is what turns the second process's spawn into a plain reuse."""
    workspace = _guarded_edit(root, monkeypatch)
    live = guard.worktree.live_boxes
    reads = []

    def live_boxes(base):
        reads.append(base)
        return {} if len(reads) == 1 else live(base)  # the winner registers after read 1

    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(guard.worktree, "live_boxes", live_boxes)
    spawned = []
    monkeypatch.setattr(
        guard.worktree, "plan_respawn", lambda *a, **k: spawned.append(a) or _plan(root)
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert spawned == []
    assert "already has a box" in response(capsys)["additionalContext"]


def test_a_spawn_waits_for_the_lock_the_other_registration_holds(root, monkeypatch, capsys):
    """Waiting is the primary fix; the recovery above is what happens when the wait
    runs out. Left unlocked, both processes reach `git worktree add` and one of them
    always loses."""
    workspace = _guarded_edit(root, monkeypatch)
    monkeypatch.setattr(guard, "SPAWN_LOCK_WAIT", 0.2)
    held = root / ".worktrees" / guard.worktree.spawn_lock_name("carameli--s1")
    held.mkdir(parents=True)
    waited = []
    monkeypatch.setattr(
        guard.worktree, "plan_respawn", lambda *a, **k: waited.append(True) or _plan(root)
    )
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert waited == [True]  # it waited its turn, then went ahead anyway
    assert held.is_dir()  # and left the other holder's lock alone
    capsys.readouterr()


def test_spawn_and_route_blocks_on_its_own_when_no_box_can_be_cut(root, monkeypatch, capsys):
    """The spawn half runs under the lock its caller holds, so it is worth being able
    to drive -- and to fail -- without one."""
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (False, ["boom"]))
    call = json.loads(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    code = guard.spawn_and_route(
        "carameli", "a.py", _workspace(root), root, "s1", call, locked=False
    )
    assert code == guard.EXIT_BLOCK
    assert "boom" in guidance(capsys)


def test_after_failed_spawn_routes_into_whatever_box_now_exists(root, capsys):
    """The recovery reads as a decision of its own, so it is driven as one: given a
    failure and a box for this session, it aims the edit there rather than at the
    checkout. The block half is `..._with_no_box_anywhere_still_blocks` above."""
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    call = json.loads(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    code = guard.after_failed_spawn(
        call, "carameli", "a.py", root, "s1", "fatal: a branch named 'x' already exists"
    )
    assert code == guard.EXIT_ALLOW
    answer = response(capsys)
    assert answer["updatedInput"]["file_path"] == str(
        root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    )
    assert "already exists" in answer["additionalContext"]


def test_current_boxes_falls_back_to_the_snapshot_it_was_given(root, monkeypatch):
    """An unreadable lease file must not turn a re-read into a second box: the caller's
    snapshot is stale at worst, while `{}` would spawn one this session already has."""

    def unreadable(_base):
        raise OSError("leases.json is being replaced")

    monkeypatch.setattr(guard.worktree, "live_boxes", unreadable)
    snapshot = {"carameli--ws-s1-0806": object()}
    assert guard.current_boxes(root, snapshot) is snapshot


def test_a_foreign_box_route_is_recorded_with_its_kind(root, monkeypatch, ledger_root, capsys):
    workspace = _workspace(root)
    _lease(root, "carameli--x-0806", project="carameli", session="other-session")
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    monkeypatch.setattr("sys.stdin", _stdin(payload(path=str(target), cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
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
    """The guarded import is the guard's own safety, and the re-aim raises the stakes
    rather than lowering them: an unhandled exception in a PreToolUse hook exits non-2
    with nothing on stdout, so there is no `updatedInput` and the edit PROCEEDS at the
    path it named -- onto the home branch. Diagnostics must degrade to silence."""
    workspace = _guarded_edit(root, monkeypatch)
    monkeypatch.setattr(guard, "harness_events", None)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert response(capsys)["updatedInput"]


# --- the shell tier ---------------------------------------------------------
#
# Editor calls were the guard's whole scope until Claude Code's bypass-permissions mode
# began telling sessions, in text they cannot tell apart from their operator's, to make
# file changes with `sed`, heredocs or short scripts rather than with Edit and Write.
# That turned a quiet blind side into a route: the hook matched `^(Edit|Write|...)$`, so
# a `sed -i` onto a checkout's home branch was not merely unguarded but *recommended*.
# Every test here is about a command line, and the ones that assert an ALLOW are the
# load-bearing half -- a tier that reads shell is one false positive away from being
# switched off again, which is how the capped-Bash gate was lost.


def shell_payload(command: str, cwd: str = "", session: str = "s1", tool: str = "Bash") -> str:
    return json.dumps(
        {
            "tool_name": tool,
            "tool_input": {"command": command},
            "cwd": cwd,
            "session_id": session,
        }
    )


@pytest.mark.parametrize(
    "command, expected",
    [
        # redirection, which is also the whole heredoc route: `cat > x <<EOF` is this
        ("cat > devkit/a.py", ["devkit/a.py"]),
        ("cat >devkit/a.py", ["devkit/a.py"]),
        ("cat >> devkit/a.py", ["devkit/a.py"]),
        ("cat > devkit/a.py <<'EOF'", ["devkit/a.py"]),
        ("python x.py 2> devkit/a.log", ["devkit/a.log"]),
        ('echo x > "C:\\ws\\devkit\\a.py"', ["C:\\ws\\devkit\\a.py"]),
        # the named verbs
        ("sed -i 's/a/b/' devkit/a.py", ["s/a/b/", "devkit/a.py"]),
        ("sed -i.bak s/a/b/ devkit/a.py", ["s/a/b/", "devkit/a.py"]),
        ("tee devkit/a.py", ["devkit/a.py"]),
        ("cat x | tee -a devkit/a.py", ["devkit/a.py"]),
        ("rm devkit/a.py", ["devkit/a.py"]),
        ("touch devkit/a.py", ["devkit/a.py"]),
        ("dd if=/dev/zero of=devkit/a.bin", ["devkit/a.bin"]),
        # copy-shaped: the last operand is the destination, the rest are reads
        ("cp devkit/a.py /elsewhere/b", ["/elsewhere/b"]),
        ("mv old.py devkit/a.py", ["devkit/a.py"]),
        # wrappers standing in front of the real verb
        ("sudo tee devkit/a.py", ["devkit/a.py"]),
        ("FOO=1 tee devkit/a.py", ["devkit/a.py"]),
        # PowerShell has its own tool in this harness, and its own spelling
        ("Set-Content -Path devkit/a.py -Value x", ["devkit/a.py"]),
        ("Copy-Item -Path a -Destination devkit/a.py", ["devkit/a.py"]),
        ("remove-item devkit/a.py", ["devkit/a.py"]),
        (
            "Remove-Item devkit/a.py -ErrorAction SilentlyContinue",
            ["devkit/a.py"],
        ),
        # more than one, in the order the command names them
        ("touch devkit/a.py && rm devkit/b.py", ["devkit/a.py", "devkit/b.py"]),
    ],
)
def test_shell_write_targets_reads_the_spellings_that_write(command, expected):
    assert guard.shell_write_targets(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "cat devkit/a.py",
        "sed -n '1,5p' devkit/a.py",  # -n reads; only -i writes
        "grep -rn thing devkit/",
        "python -m pytest tests/ -q",
        "git diff --stat devkit/a.py",
        "python x.py 2>&1",  # a descriptor duplication names no file
        "wc -l devkit/a.py",
        # PowerShell providers are process state, not paths in the current checkout.
        "$env:DEVKIT_HOOK_ADAPTER = 'codex'; "
        "Remove-Item Env:DEVKIT_HOOK_ADAPTER -ErrorAction SilentlyContinue",
        "",
    ],
)
def test_a_command_that_writes_nothing_names_no_target(command):
    """The half that keeps this tier alive. `enforce-capped-bash.py` had to be reversed
    because 46% of its blocks were its own false positives, and it earned them by trying
    to model the shell; this one recognises verbs and allows everything else, so a read
    has to stay invisible even when its argument is a checkout's file."""
    assert guard.shell_write_targets(command) == []


@pytest.mark.parametrize(
    "command",
    [
        # every one of these was blocked as a write in a single session on 2026-08-24,
        # and each block cut a box that `reconcile` reaped as "never used" minutes later
        "awk -F'\\t' '$1 > \"2026-08-24T01:53\"' logs/harness-events.log",
        'grep -n "shlex|REDIRECT|redirect|\'>\'|\\">>\\"" scripts/worktree-guard.py',
        "grep -rn '>>>>>>>' devkit/frontend/src",
        'python -c "print([l for l in xs if l[:16] >= cut])"',
        "python - <<'PY'\nafter = [x for x in xs if x[:16] >= cut]\nPY",
        "python - <<'PY'\ndef f(x: int) -> None:\n    return None\nPY",
        # a heredoc body is program text; only its marker line can carry a redirection
        "cat <<'EOF'\nsee devkit/a.py > devkit/b.py for the rewrite\nEOF",
    ],
)
def test_a_greater_than_that_is_not_a_redirection_names_no_target(command):
    """The claim this replaced -- "an invented candidate resolves to no checkout" -- was
    false because an invented candidate is *relative*, and a relative path resolves
    against the cwd, which in a guarded session is the checkout being guarded. So a quote
    character was named as the file the block was protecting.

    Reversion check: restore the `REDIRECT` regex and every case here fails, each naming
    the punctuation it read as a path."""
    assert guard.shell_write_targets(command) == []


@pytest.mark.parametrize(
    "command, expected",
    [
        # respecting quotes must not cost a detection: the target itself may be quoted
        ('echo x > "devkit/a b.py"', ["devkit/a b.py"]),
        ("echo x > 'devkit/a.py'", ["devkit/a.py"]),
        ('echo x > "C:\\ws\\devkit\\a.py"', ["C:\\ws\\devkit\\a.py"]),
        # the marker line of a heredoc is a command line like any other
        ("cat > devkit/a.py <<'EOF'\nbody > not-a-file\nEOF", ["devkit/a.py"]),
        # a write after a quoted argument still reads
        ("grep -n '>' devkit/a.py && tee devkit/b.py", ["devkit/b.py"]),
    ],
)
def test_quote_awareness_costs_no_detection(command, expected):
    assert guard.shell_write_targets(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        # the two that blocked a Codex session on 2026-08-24, verbatim in shape: a
        # conflict-marker search, whose alternation carries both a `|` and a `>`
        "git merge --no-edit origin/main; git status --short; "
        'rg -n "^(<<<<<<<|=======|>>>>>>>)" scripts tests',
        'rg -n -C 30 "^(<<<<<<<|=======|>>>>>>>)" "$box\\scripts\\preview-task.py"',
        # the same shape without the `>`-run, which is enough on its own
        'grep -n "a|b>c" devkit/a.py',
        "awk -F'|' '$1 > \"x\"' logs/harness-events.log",
        # a separator inside quotes is not a separator, whichever one it is
        'grep -n "a;b>c" devkit/a.py',
        'grep -n "a&&b>c" devkit/a.py',
    ],
)
def test_a_separator_inside_quotes_does_not_split_the_statement(command):
    """Splitting at a quoted separator hands the scanner a fragment whose quote is
    already open, so the `>` the quote covered reads as a redirection and the rest of
    the fragment reads as its target: `rg -n "^(<<<<<<<|=======|>>>>>>>)" scripts tests`
    was blocked as a write to `) scripts tests`, and the box that block cut was reaped
    as never used minutes later.

    Reversion check: restore the `STATEMENTS` regex and every case here fails, each
    naming the tail of a quoted pattern as the file the guard is protecting."""
    assert guard.shell_write_targets(command) == []


@pytest.mark.parametrize(
    "command, expected",
    [
        # the old splitter lost this one: split at the quoted `|`, the redirection ends
        # up in a fragment whose quote is open, and a quoted span names no target
        ("git log --format='%h|%s' > devkit/a.log", ["devkit/a.log"]),
        # and it must still see every write a real separator does divide
        ("cat x | tee devkit/a.py", ["devkit/a.py"]),
        ('grep -n "a|b" x; tee devkit/a.py', ["devkit/a.py"]),
        ("rg 'a|b' x && rm devkit/a.py", ["devkit/a.py"]),
    ],
)
def test_quote_aware_splitting_costs_no_detection(command, expected):
    assert guard.shell_write_targets(command) == expected


@pytest.mark.parametrize(
    "command, expected",
    [
        ("a && b", ["a ", " b"]),
        ("a || b", ["a ", " b"]),
        ("a; b", ["a", " b"]),
        ("a\nb", ["a", "b"]),
        ("a | b", ["a ", " b"]),
        ("a & b", ["a ", " b"]),
        # a descriptor duplication is not a background `&`, which is what the regex
        # this replaced spelled `&(?!\d)`
        ("python x.py 2>&1", ["python x.py 2>&1"]),
        # quoted separators of every kind stay inside their statement
        ("rg 'a|b;c&&d' x", ["rg 'a|b;c&&d' x"]),
        ('rg "a|b" x', ['rg "a|b" x']),
        # an unbalanced quote swallows the rest: it loses detections, and losing a
        # detection allows, which is the direction this tier is willing to be wrong in
        ('echo "x | tee devkit/a.py', ['echo "x | tee devkit/a.py']),
    ],
)
def test_split_statements_cuts_at_unquoted_separators_only(command, expected):
    assert guard.split_statements(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        'S=/scratch/x.py; printf a > "$S"',
        "tee $OUT",
        "echo x > %TEMP%\\a.py",
        "cp a ${DEST}",
    ],
)
def test_an_unexpanded_variable_is_not_a_path(command):
    """The shell has not substituted yet, so `$S` arrives as a relative word and resolves
    against the cwd -- the checkout. Blocked a write to a scratch directory that way."""
    assert guard.shell_write_targets(command) == []


def test_strip_heredocs_keeps_the_marker_line_and_drops_the_body():
    """The marker line carries the redirection; the body carries the false positives."""
    command = "cat > devkit/a.py <<'EOF'\na >= b\nEOF\ntee devkit/b.py"
    assert guard.strip_heredocs(command) == "cat > devkit/a.py <<'EOF'\ntee devkit/b.py"


def test_an_unterminated_heredoc_swallows_the_rest_like_a_shell_does():
    assert guard.strip_heredocs("cat <<EOF\na\nb") == "cat <<EOF"


def test_redirect_targets_reads_a_descriptor_duplication_as_no_file():
    assert guard.redirect_targets("python x.py 2>&1") == []


@pytest.mark.parametrize(
    "tokens, expected",
    [
        # A duplication names no file, so it consumes nothing after it.
        (["cp", "a", "b", "2>&1"], ["cp", "a", "b"]),
        (["cp", "a", "b", "1>&2"], ["cp", "a", "b"]),
        (["cp", "a", "b", ">&2"], ["cp", "a", "b"]),
        # Detached: the operator alone, whose target is the *next* token.
        (["cp", "a", "b", ">", "log"], ["cp", "a", "b"]),
        (["cp", "a", "b", "2>", "err.txt"], ["cp", "a", "b"]),
        (["cat", "<", "in.txt"], ["cat"]),
        # Attached: the target rides in the same token.
        (["cp", "a", "b", ">log"], ["cp", "a", "b"]),
        (["cp", "a", "b", "2>>out.txt"], ["cp", "a", "b"]),
        # Nothing to strip leaves the list alone, including a `>` inside a word.
        (["cp", "a", "b"], ["cp", "a", "b"]),
        (["echo", "a->b"], ["echo", "a->b"]),
    ],
)
def test_strip_redirections_drops_the_operator_and_its_target_together(tokens, expected):
    """`strip_redirections` is what keeps a redirection out of the operand list.

    Named directly rather than only through `shell_write_targets`, because the three
    token shapes it separates -- duplication, detached, attached -- are the whole of its
    job, and a caller-level test only ever exercises the two that a `cp` happens to hit.
    """
    assert guard.strip_redirections(tokens) == expected


@pytest.mark.parametrize(
    "command, expected",
    [
        # The reported false positive: `2>&1` arrived as an ordinary operand, and `cp`
        # takes its last one, so the block named a path nobody wrote.
        ("cp devkit/a.ts devkit/b.ts 2>&1 | tail -c 200", ["devkit/b.ts"]),
        ("cp devkit/a.ts devkit/b.ts 1>&2", ["devkit/b.ts"]),
        # The quieter half: with any trailing redirection the real destination of a
        # `SHELL_WRITE_LAST` verb was never measured at all.
        ("cp devkit/a.ts devkit/b.ts > log", ["log", "devkit/b.ts"]),
        ("cp devkit/a.ts devkit/b.ts >log", ["log", "devkit/b.ts"]),
        ("mv devkit/a.ts devkit/b.ts >> log", ["log", "devkit/b.ts"]),
        ("cp devkit/a.ts devkit/b.ts 2> err.txt", ["err.txt", "devkit/b.ts"]),
        # An input redirection is not a write, and its operand is not a target.
        ("cat < in.txt > devkit/out.txt", ["devkit/out.txt"]),
        # A write-all verb keeps its own operands and gains no phantom one.
        ("tee devkit/out.txt 2>&1", ["devkit/out.txt"]),
    ],
)
def test_a_redirection_is_never_read_as_an_operand(command, expected):
    """`redirect_targets` walked the text and read what a statement redirects to; nothing
    took those words back out of the token list, so the operand loop saw them too.

    Both directions were wrong. `2>&1` became a relative path that resolved into the
    checkout and blocked with "an edit to 2>&1 would land on it" -- unre-issuable, and
    the reported defect. And because `cp`/`mv` are judged on `operands[-1]`, a trailing
    redirection displaced the real destination, so the write this tier exists to catch
    went unmeasured. It failed closed only because both spellings happen to be relative.
    """
    assert guard.shell_write_targets(command) == expected


def test_a_redirection_does_not_displace_the_ref_of_a_branch_move():
    """`switch_targets` runs the same operand loop, so it had the same hole: a trailing
    `2>&1` was a candidate ref. Stripping in one helper is what keeps the two tiers
    agreeing about what an operand is."""
    assert guard.switch_targets("git switch -c agent/x 2>&1") == [("", "agent/x")]


def test_an_interpreter_script_is_the_documented_gap():
    """`python -c` computes its target at runtime and names it nowhere in the argv, so no
    argv scan can see it. Pinned as a test rather than left implicit: a silent gap in a
    guard reads as coverage, and whoever widens this tier should find the limit written
    where closing it will show up as a failure."""
    assert guard.shell_write_targets("python -c \"open('devkit/a.py','w').write('x')\"") == []


def test_an_unbalanced_quote_falls_back_to_whitespace_splitting():
    """`shlex` raises on it, and an unhandled raise in a PreToolUse hook exits non-2 --
    which Claude Code reports as a non-blocking error and lets the write PROCEED."""
    assert "devkit/a.py" in guard.shell_write_targets("tee devkit/a.py 'unclosed")


def test_shell_tokens_keeps_windows_backslashes():
    """`posix=True` eats them, and every path this hook judges on this machine has them."""
    assert guard.shell_tokens(r"tee C:\ws\devkit\a.py") == ["tee", r"C:\ws\devkit\a.py"]


def test_guarded_targets_reads_a_command_for_a_shell_tool_and_a_path_for_an_editor():
    edit = json.loads(payload(path="/ws/carameli/a.py"))
    assert guard.guarded_targets(edit) == ["/ws/carameli/a.py"]
    assert guard.guarded_targets(json.loads(shell_payload("tee /ws/carameli/a.py"))) == [
        "/ws/carameli/a.py"
    ]
    assert guard.guarded_targets(json.loads(shell_payload("cat /ws/carameli/a.py"))) == []


def test_a_shell_write_onto_a_home_branch_is_routed_to_the_box(root, monkeypatch, capsys):
    """The reversion check for the whole tier: take `Bash` back out of `MUTATING_TOOLS`
    and this write lands on carameli's home branch with nothing under it."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload(f"sed -i s/a/b/ {root / 'carameli' / 'a.py'}", cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    said = guidance(capsys)
    assert "carameli--ws-s1-0806" in said
    assert "shell command, not an editor call" in said


def test_a_shell_write_is_blocked_rather_than_re_aimed(root, monkeypatch, capsys):
    """The rewrite replaces a path *argument*, and a command line has none. Re-aiming one
    would mean editing the command text -- guessing at quoting, at a heredoc body, at
    which of `cp`'s operands moved -- and a rewrite this hook gets wrong lands the write
    on the home branch while reporting success."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload(f"tee {root / 'carameli' / 'a.py'}", cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # no updatedInput anywhere near this
    assert "not re-aimed automatically" in captured.err


def test_a_command_that_writes_nothing_never_reads_the_registry(root, monkeypatch):
    """Widening the matcher put this hook on every Bash call in the session, not on the
    handful that edit files. The overwhelming majority write nothing, and what they must
    cost is a payload parse -- not a workspace read and a project list per `grep`."""
    workspace = _workspace(root)
    monkeypatch.setattr(
        guard.devkit_project,
        "known_projects",
        lambda *a, **k: pytest.fail("a read-only command must not reach the registry"),
    )
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload(f"grep -n foo {root / 'carameli' / 'a.py'}", cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_the_shell_note_quotes_the_word_the_command_actually_used():
    """The note's whole job is to be greppable against the command the agent just sent.
    So it repeats the target *as written* -- an absolute Windows path, a relative one, a
    quoted one -- rather than the resolved path the guard reasoned about, which appears
    nowhere in the command line and so cannot be the word anyone replaces."""
    for target in (r"C:\repo\app\a.py", "app/a.py", "'a file.py'"):
        note = guard.shell_note(target)
        assert f"`{target}`" in note
        assert "shell command, not an editor call" in note
        assert "not re-aimed automatically" in note


def test_a_shell_command_that_only_reads_never_spawns(root, monkeypatch, capsys):
    """A guard that cut a box per `cat` would be switched off within the hour."""
    workspace = _workspace(root)
    monkeypatch.setattr(
        guard.worktree,
        "plan_respawn",
        lambda *a, **k: pytest.fail("a read must not spawn a box"),
    )
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload(f"cat {root / 'carameli' / 'a.py'}", cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    assert capsys.readouterr().out.strip() == ""


def test_a_shell_write_outside_every_checkout_is_left_alone(root, monkeypatch):
    workspace = _workspace(root)
    monkeypatch.setattr(
        guard.worktree,
        "plan_respawn",
        lambda *a, **k: pytest.fail("nothing outside a checkout has a branch to protect"),
    )
    monkeypatch.setattr("sys.stdin", _stdin(shell_payload("cat x > /dev/null", cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_a_shell_write_into_the_sessions_own_box_is_left_alone(root, monkeypatch):
    """The commonest command in a guarded session, once the routing has worked."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        guard.worktree,
        "plan_respawn",
        lambda *a, **k: pytest.fail("the write is already where it belongs"),
    )
    box = root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    monkeypatch.setattr("sys.stdin", _stdin(shell_payload(f"tee {box}", cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_the_first_guarded_target_decides_the_whole_command(root, monkeypatch, capsys):
    """There is no partial outcome for a command line: it runs or it does not. So a
    command that writes somewhere harmless *and* onto a home branch is routed on the
    second, and the message names the word that did it."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    command = f"touch /elsewhere/scratch && touch {root / 'carameli' / 'a.py'}"
    monkeypatch.setattr("sys.stdin", _stdin(shell_payload(command, cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert str(root / "carameli" / "a.py") in guidance(capsys)


def test_a_routed_shell_command_is_recorded_as_one(root, monkeypatch, ledger_root, capsys):
    """`kind` is what tells a triage pass whether the shell tier is earning its keep or
    manufacturing false positives, and the two cannot be told apart after the fact."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload(f"rm {root / 'carameli' / 'a.py'}", cwd=str(root))),
    )
    guard.main(["--workspace", str(workspace)])
    capsys.readouterr()
    assert any("kind=shell" in line for line in _events(ledger_root))


# --- the patch tier ---------------------------------------------------------
#
# Codex edits files with `apply_patch`, which reaches this hook as a tool named
# `apply_patch` carrying an envelope under `command` -- the same key `Bash` uses. That
# name was in `MUTATING_TOOLS` from the beginning and every one of its writes was allowed
# anyway: `guarded_targets` read the path keys, found none, returned nothing, and `main`
# exits ALLOW on an empty target list. So the tier was wired, matched, judged and blind,
# which is the only failure shape this hook can have that reports nothing.
#
# It ran that way until 2026-08-24, when a Codex session edited carameli's static checkout
# through seventeen `apply_patch` calls, committed them onto `feat/comic-bubble-text-input`
# and pushed it. The tests below are the reversion check: revert `PATCH_TOOLS` out of
# `guarded_targets` and the first two pass ALLOW where they now demand a box.


def patch_payload(patch: str, cwd: str = "", session: str = "s1", key: str = "command") -> str:
    return json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": {key: patch},
            "cwd": cwd,
            "session_id": session,
        }
    )


def envelope(*body: str) -> str:
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"])


@pytest.mark.parametrize(
    "body, expected",
    [
        (["*** Add File: devkit/a.py", "+x = 1"], ["devkit/a.py"]),
        (["*** Update File: devkit/a.py", "@@", "-x = 1", "+x = 2"], ["devkit/a.py"]),
        (["*** Delete File: devkit/a.py"], ["devkit/a.py"]),
        # a rename names both: the old file is rewritten, the new one is created
        (
            ["*** Update File: devkit/a.py", "*** Move to: devkit/b.py", "@@", "+x"],
            ["devkit/a.py", "devkit/b.py"],
        ),
        # several stanzas, in the order the envelope names them
        (
            ["*** Add File: devkit/a.py", "+x", "*** Add File: devkit/b.py", "+y"],
            ["devkit/a.py", "devkit/b.py"],
        ),
        # the same file twice is one target, so a block names it once
        (
            ["*** Delete File: devkit/a.py", "*** Add File: devkit/a.py", "+x"],
            ["devkit/a.py"],
        ),
        # absolute Windows paths are what Codex actually sends
        ([r"*** Add File: C:\ws\devkit\a.py", "+x"], [r"C:\ws\devkit\a.py"]),
    ],
)
def test_patch_targets_reads_every_header_that_names_a_file(body, expected):
    assert guard.patch_targets(envelope(*body)) == expected


def test_a_patch_body_is_content_and_not_a_header():
    """The one way this parser could be talked into a false positive: a patch that adds a
    file whose own content is a patch. Content lines carry a `+`/`-`/space prefix, so the
    header test is anchored at column zero and the inner envelope is just text."""
    inner = envelope("*** Add File: devkit/victim.py", "+x")
    body = ["*** Add File: devkit/outer.md", *[f"+{line}" for line in inner.splitlines()]]
    assert guard.patch_targets(envelope(*body)) == ["devkit/outer.md"]


def test_a_context_line_quoting_a_header_is_not_a_header():
    body = ["*** Update File: devkit/a.py", "@@", " *** Add File: devkit/ghost.py", "+x"]
    assert guard.patch_targets(envelope(*body)) == ["devkit/a.py"]


@pytest.mark.parametrize("patch", ["", "*** Begin Patch\n*** End Patch", "not a patch at all"])
def test_a_patch_that_names_no_file_yields_no_targets(patch):
    assert guard.patch_targets(patch) == []


def test_patch_body_reads_the_key_codex_actually_uses():
    """`command` first, because that is the observed one -- Codex normalises its editor
    call to the same key its shell tool uses. The others are defensive."""
    for key in ("command", "patch", "input"):
        assert "Add File" in guard.patch_body(
            json.loads(patch_payload(envelope("*** Add File: a.py", "+x"), key=key))
        )
    assert guard.patch_body({"tool_input": {"file_path": "a.py"}}) == ""


def test_guarded_targets_reads_the_envelope_for_a_patch_tool():
    call = json.loads(patch_payload(envelope("*** Add File: /ws/carameli/a.py", "+x")))
    assert guard.guarded_targets(call) == ["/ws/carameli/a.py"]


def test_a_patch_tool_still_falls_back_to_a_path_argument():
    """`apply_patch` is a name two harnesses could spell differently. A payload carrying a
    plain path keeps working, because this tier's failure mode is a silent allow."""
    call = json.loads(payload(tool="apply_patch", path="/ws/carameli/a.py", key="path"))
    assert guard.guarded_targets(call) == ["/ws/carameli/a.py"]


def test_a_patch_onto_a_home_branch_is_routed_to_the_box(root, monkeypatch, capsys):
    """The reversion check for the whole tier, and for the carameli incident above."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            patch_payload(
                envelope(f"*** Add File: {root / 'carameli' / 'a.py'}", "+x"), cwd=str(root)
            )
        ),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    said = guidance(capsys)
    assert "carameli--ws-s1-0806" in said
    assert "apply_patch envelope, not a path argument" in said


def test_a_patch_is_blocked_rather_than_re_aimed(root, monkeypatch, capsys):
    """Same reason a shell write is: the rewrite replaces a path *argument*, and the path
    here is inside the envelope. Editing it would mean rewriting the patch."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            patch_payload(
                envelope(f"*** Update File: {root / 'carameli' / 'a.py'}", "@@", "+x"),
                cwd=str(root),
            )
        ),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "not re-aimed automatically" in captured.err


def test_the_patch_note_quotes_the_header_the_envelope_actually_used():
    for target in (r"C:\repo\app\a.py", "app/a.py"):
        note = guard.patch_note(target)
        assert f"`{target}`" in note
        assert "apply_patch envelope, not a path argument" in note


def test_a_patch_into_the_sessions_own_box_is_left_alone(root, monkeypatch):
    """What every Codex edit looks like once the routing has worked."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        guard.worktree,
        "plan_respawn",
        lambda *a, **k: pytest.fail("the patch is already where it belongs"),
    )
    box = root / ".worktrees" / "carameli--ws-s1-0806" / "a.py"
    monkeypatch.setattr(
        "sys.stdin", _stdin(patch_payload(envelope(f"*** Add File: {box}", "+x"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_a_patch_outside_every_checkout_is_left_alone(root, monkeypatch):
    workspace = _workspace(root)
    monkeypatch.setattr(
        guard.worktree,
        "plan_respawn",
        lambda *a, **k: pytest.fail("nothing outside a checkout has a branch to protect"),
    )
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(patch_payload(envelope("*** Add File: /elsewhere/a.py", "+x"), cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_a_routed_patch_is_recorded_as_one(root, monkeypatch, ledger_root, capsys):
    """A Codex block and a Claude one are indistinguishable in the ledger otherwise, and
    this tier's whole history is that nobody could see it doing nothing."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            patch_payload(envelope(f"*** Delete File: {root / 'carameli' / 'a.py'}"), cwd=str(root))
        ),
    )
    guard.main(["--workspace", str(workspace)])
    capsys.readouterr()
    assert any("kind=patch" in line for line in _events(ledger_root))


def test_a_patch_names_no_branch_move(root, monkeypatch):
    """`switch_targets` reads `command`, and so, now, does the patch tier. An envelope
    whose content happens to contain a `git checkout` line must not be read as one."""
    workspace = _workspace(root)
    monkeypatch.setattr(
        guard.worktree,
        "live_boxes",
        lambda *a, **k: pytest.fail("a patch body is not a command line"),
    )
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            patch_payload(
                envelope("*** Add File: /elsewhere/a.sh", "+git checkout agent/x-0821"),
                cwd=str(root),
            )
        ),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_the_hook_is_wired_for_every_tool_it_judges():
    """The code half is useless without the matcher half, and they live in different
    files: `MUTATING_TOOLS` in the hook, a regex in `.claude/settings.json`. devkit runs
    the harness it ships, so its own settings are the copy this can check."""
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    matchers = [
        entry["matcher"]
        for entry in settings["hooks"]["PreToolUse"]
        if any("worktree-guard" in hook.get("command", "") for hook in entry["hooks"])
    ]
    assert matchers, "worktree-guard.py is not wired in devkit's own settings"
    for tool in guard.MUTATING_TOOLS:
        assert any(re.fullmatch(matcher, tool) for matcher in matchers), tool


# --- two guards, one call ---------------------------------------------------


def test_a_spawn_that_lost_a_race_reuses_the_box_instead_of_blocking(root, monkeypatch, capsys):
    """Found by this hook firing on itself. The guard is registered twice in a devkit
    session -- a user-level absolute path and a project-level `$CLAUDE_PROJECT_DIR` one
    resolving to the same file -- so two processes judge every call. Neither can see the
    other's box, because the loser reads the lease registry before the winner writes it;
    it then dies on `a branch named 'agent/...' already exists` and exits 2, blocking the
    call the winner had just re-aimed. The agent is handed both messages at once -- "the
    edit was applied at <box>" on stdout, "spawning a box failed" on stderr -- with the
    box on disk and the edit nowhere."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(
        guard.worktree,
        "apply_new",
        lambda *a, **k: (
            False,
            ["FAILED at `git worktree add`: a branch named ... already exists"],
        ),
    )
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW
    said = guidance(capsys)
    assert "carameli--ws-s1-0806" in said
    assert "failed" not in said.lower()


def test_a_spawn_that_failed_with_no_box_afterwards_still_blocks(root, monkeypatch, capsys):
    """The reuse above must not swallow a genuine failure: with no box on disk there is
    nothing to aim at, and allowing the edit is the outcome this hook exists to prevent."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (False, ["boom"]))
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert "boom" in capsys.readouterr().err


# --- the branch tier --------------------------------------------------------
#
# The regression suite for the incident that produced it: carameli's static checkout was
# found parked on `agent/seems-only-1-preview-time-0821`, another session's task branch,
# for two days. Every tier above had worked -- that session's edits went to a box and
# their PR was open -- and something had simply run `git checkout` in the static copy,
# which writes no file and so was invisible to all of them.
#
# As with the shell tier, the ALLOW tests are the load-bearing half: a branch tier that
# blocks `git checkout master` is one that gets switched off, and moving a checkout home
# is the repair rather than the problem.


@pytest.mark.parametrize(
    "command, expected",
    [
        ("git checkout agent/thing-0821", [("", "agent/thing-0821")]),
        ("git switch agent/thing-0821", [("", "agent/thing-0821")]),
        ("git checkout -b agent/thing-0821", [("", "agent/thing-0821")]),
        ("git checkout -B agent/thing-0821", [("", "agent/thing-0821")]),
        ("git switch -c agent/thing-0821", [("", "agent/thing-0821")]),
        ("git switch -C agent/thing-0821", [("", "agent/thing-0821")]),
        ("git checkout --orphan agent/thing-0821", [("", "agent/thing-0821")]),
        # git's own -C, which decides WHICH checkout the move lands in
        ("git -C carameli checkout agent/thing-0821", [("carameli", "agent/thing-0821")]),
        ("git -c core.x=1 checkout agent/thing-0821", [("", "agent/thing-0821")]),
        # options before the ref, and a tracking spelling
        ("git checkout --track origin/agent/thing-0821", [("", "origin/agent/thing-0821")]),
        # wrappers, exactly as the shell tier strips them
        ("sudo git checkout agent/thing-0821", [("", "agent/thing-0821")]),
        ("GIT_PAGER=cat git checkout agent/thing-0821", [("", "agent/thing-0821")]),
        # more than one statement, in the order the command names them
        (
            "git checkout master && git -C ibkr_trader switch agent/b-0821",
            [("", "master"), ("ibkr_trader", "agent/b-0821")],
        ),
    ],
)
def test_switch_targets_reads_the_moves_a_command_makes(command, expected):
    assert guard.switch_targets(command) == expected


@pytest.mark.parametrize(
    "command, expected",
    [
        # the report: a rescue branch cut inside a UI-preview copy, judged against the
        # session's cwd and refused as parking the carameli checkout
        (
            'cd "/ws/.ui-previews/carameli/master" && git switch -c agent/rescue-0827',
            [("/ws/.ui-previews/carameli/master", "agent/rescue-0827")],
        ),
        # `-C` still decides, and an absolute one ignores the move entirely
        ("cd /ws/a && git -C /ws/b checkout agent/x-0827", [("/ws/b", "agent/x-0827")]),
        # a relative `-C` hangs off the move, exactly as the shell tier rebases a path
        ("cd /ws/a && git -C sub checkout agent/x-0827", [("/ws/a/sub", "agent/x-0827")]),
        # two moves in one line: the second `cd` is the one the second git call sees
        (
            "cd /ws/a && git switch agent/x-0827 && cd /ws/b && git switch agent/y-0827",
            [("/ws/a", "agent/x-0827"), ("/ws/b", "agent/y-0827")],
        ),
        # a move this tier cannot follow leaves the base alone rather than clearing it
        ("cd /ws/a && cd - && git switch agent/x-0827", [("/ws/a", "agent/x-0827")]),
        ("cd $HOME && git switch agent/x-0827", [("", "agent/x-0827")]),
    ],
)
def test_a_switch_is_judged_where_the_cd_before_it_landed(command, expected):
    """The reversion check for the `cd` half, and it is a *false positive* that motivated
    it: `switch_targets` read git's `-C` and nothing else, so
    `cd <workspace>/.ui-previews/carameli/master && git switch -c agent/...` was judged
    against the session's own cwd and refused as parking the carameli checkout. The
    preview copy is a detached `git worktree add` that `--clean` deletes, so the block
    landed on the one move that rescues work out of it. `shell_write_targets` had tracked
    `cd` since it was written; this tier simply never did."""
    assert guard.switch_targets(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        # restores: HEAD does not move, and these are ordinary and frequent
        "git checkout -- app/a.py",
        "git checkout master -- app/a.py",
        "git checkout -p app/a.py",
        "git checkout --patch",
        # not a move at all
        "git status",
        "git log --oneline -5",
        "git branch --show-current",
        "git worktree add ../box agent/thing-0821",
        # not git
        "checkout agent/thing-0821",
        "gh pr checkout 42",
        "",
    ],
)
def test_a_command_that_moves_no_branch_names_no_switch(command):
    assert guard.switch_targets(command) == []


@pytest.mark.parametrize(
    "ref, expected",
    [
        ("agent/thing-0821", "agent/thing-0821"),
        ("claude/thing-0727", "claude/thing-0727"),
        ("codex/thing-0801", "codex/thing-0801"),
        # a detached HEAD at the remote ref is the same park, and arguably worse: the box
        # tier declines a branch git will not name, so it is quiet there too
        ("origin/agent/thing-0821", "agent/thing-0821"),
        ("refs/heads/agent/thing-0821", "agent/thing-0821"),
        ("refs/remotes/origin/agent/thing-0821", "agent/thing-0821"),
    ],
)
def test_switch_branch_recognises_a_task_branch_however_it_is_spelled(ref, expected):
    assert guard.switch_branch(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "master",
        "main",
        "develop",
        "carameli-b",  # a long-lived worktree anchor: a home branch, not a task branch
        "preview/agent-thing-0821",  # what `worktree.py preview` cuts; lives in a box
        "feature/agent-thing",  # only ONE segment is stripped, and only to a real prefix
        "abc1234",
        "HEAD",
        "",
    ],
)
def test_a_home_branch_is_never_read_as_a_task_branch(ref):
    """The allow half. Moving a checkout home is the repair for a park, so a tier that
    blocked it would have no exit -- the same defect `sweep.NEEDS_PR` had."""
    assert guard.switch_branch(ref) == ""


def test_a_checkout_moved_onto_a_task_branch_is_a_park(root):
    assert guard.switch_decision("carameli", "agent/x-0821", str(root), root, PROJECTS, {}) == (
        "checkout",
        "carameli",
        "",
    )


def test_a_move_in_the_sessions_cwd_is_judged_on_that_cwd(root):
    """`git checkout` with no `-C` lands wherever the tool call is running, which for a
    project-scoped session is the checkout itself -- the exact shape of the incident."""
    assert guard.switch_decision(
        "", "agent/x-0821", str(root / "carameli"), root, PROJECTS, {}
    ) == ("checkout", "carameli", "")


def test_a_move_outside_every_checkout_is_ordinary(root, tmp_path):
    assert guard.switch_decision("", "agent/x-0821", str(tmp_path), root, PROJECTS, {}) is None


def test_a_box_moved_off_the_branch_its_lease_records_is_blocked(root):
    """`reconcile` looks a box's PR up by the branch the registry names. A box standing
    somewhere else is invisible to the reaper as shipped work and reapable as work that
    never happened -- which destroys the worktree with the commits still in it."""
    box = guard.worktree.Box(
        name="carameli--x-0806", project="carameli", branch="agent/x-0806", session="s1"
    )
    assert guard.switch_decision(
        "",
        "agent/other-0821",
        str(root / ".worktrees" / "carameli--x-0806"),
        root,
        PROJECTS,
        {box.name: box},
    ) == ("box", "carameli--x-0806", "agent/x-0806")


def test_a_box_re_attaching_to_its_own_branch_is_ordinary(root):
    """What `worktree.py resume` leaves behind, and what re-attaching after a detached
    HEAD does. Blocking it would make the recovery unreachable from inside the box."""
    box = guard.worktree.Box(
        name="carameli--x-0806", project="carameli", branch="agent/x-0806", session="s1"
    )
    assert (
        guard.switch_decision(
            "",
            "agent/x-0806",
            str(root / ".worktrees" / "carameli--x-0806"),
            root,
            PROJECTS,
            {box.name: box},
        )
        is None
    )


def test_a_path_under_worktrees_that_is_in_no_live_box_is_ordinary(root):
    """A husk, or a stray directory. There is no lease to desync, so nothing to protect."""
    assert (
        guard.switch_decision(
            "", "agent/x-0821", str(root / ".worktrees" / "carameli--x-0806"), root, PROJECTS, {}
        )
        is None
    )


def test_the_park_message_names_the_three_things_the_move_stands_in_for():
    """This is the only block in the hook that ends in neither a box nor a path to write
    to -- nothing was going to be written -- so the whole value of the message is that
    each alternative is spelled as the command that performs it."""
    said = guard.switch_message("checkout", "carameli", "agent/x-0821")
    assert "agent/x-0821" in said and "carameli" in said
    assert "worktree.py resume" in said
    assert "worktree.py preview carameli --branch agent/x-0821" in said
    assert "log/show/diff" in said


def test_the_park_message_says_why_a_park_silences_this_very_hook():
    """The second-order effect, and the one an agent cannot see from anywhere else: once a
    checkout is parked on a live task branch, `needs_box` declines every later edit there
    as the "fix PR #42" case, so the checkout becomes unguarded space."""
    said = guard.switch_message("checkout", "carameli", "agent/x-0821")
    assert "QUIET" in said
    assert "no box and no block" in said


def test_the_box_message_explains_the_registry_rather_than_the_home_branch():
    said = guard.switch_message("box", "carameli--x-0806", "agent/other-0821", "agent/x-0806")
    assert "agent/x-0806" in said and "agent/other-0821" in said
    assert "lease registry" in said
    assert "worktree.py resume" in said


def test_a_checkout_switch_onto_a_task_branch_is_blocked(root, monkeypatch, capsys):
    """The reversion check for the whole tier: drop `switch_targets` out of `main` and
    this is once again the one act in the workspace that nothing judges."""
    workspace = _workspace(root)
    monkeypatch.setattr(
        guard.worktree,
        "plan_respawn",
        lambda *a, **k: pytest.fail("a branch move writes nothing, so it needs no box"),
    )
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload("git -C carameli checkout agent/x-0821", cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # no box, no updatedInput, nothing to re-aim
    assert "park the static checkout carameli" in captured.err


def test_moving_a_checkout_home_is_allowed_because_it_is_the_repair(root, monkeypatch):
    workspace = _workspace(root)
    monkeypatch.setattr("sys.stdin", _stdin(shell_payload("git checkout master", cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_a_branch_move_that_names_no_task_branch_never_reads_the_registry(root, monkeypatch):
    """Same bound the shell tier keeps: the tiers run on every Bash call in the session,
    so the lease registry is read only once a command names a task branch -- which is the
    case being blocked anyway."""
    workspace = _workspace(root)
    monkeypatch.setattr(
        guard.worktree,
        "live_boxes",
        lambda *a, **k: pytest.fail("an ordinary branch move must not read the lease registry"),
    )
    monkeypatch.setattr("sys.stdin", _stdin(shell_payload("git checkout main", cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_a_restore_is_not_a_park_even_from_a_task_branch(root, monkeypatch):
    """`git checkout <branch> -- <path>` restores file content and leaves HEAD where it
    is. It is ordinary, and blocking it is how a tier earns its way out of the config."""
    workspace = _workspace(root)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload("git -C carameli checkout agent/x-0821 -- app/a.py", cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_creating_a_task_branch_in_a_checkout_is_the_same_park(root, monkeypatch, capsys):
    """`branch-on-write.py` used to cut a task branch in place, and retiring it is what the
    box tier replaced. `git checkout -b` is that hook by hand: it solves the branch and
    keeps the problem, because the checkout still outlives the task."""
    workspace = _workspace(root)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload("git -C carameli switch -c agent/new-0823", cwd=str(root))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert "agent/new-0823" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload, blocked",
    [
        ({"tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x"}}, False),
        ({"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "here"}}, False),
        # the precondition the box cannot meet: it holds origin/<default>'s copy
        ({"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "gone"}}, True),
        # a tool whose arguments this hook does not know how to rewrite
        ({"tool_name": "Bash", "tool_input": {"command": "x"}}, True),
        # rewritable, but naming no path to put the box's copy under
        ({"tool_name": "Write", "tool_input": {"content": "x"}}, True),
    ],
)
def test_redirect_blocker_re_aims_only_what_the_box_can_satisfy(tmp_path, payload, blocked):
    """The predicate that decides block-vs-re-aim, and it has to fail closed: a needless
    block costs a turn and names the box, while a rewrite the runtime does not honour
    lands the edit on the home branch and reports success. So an `old_string` the box's
    copy does not contain is refused rather than re-aimed into
    `String to replace not found` at a path the agent never named."""
    destination = tmp_path / "a.py"
    destination.write_text("here it is\n", encoding="utf-8")
    assert bool(guard.redirect_blocker(payload, destination, env={})) is blocked


def test_redirect_blocker_refuses_every_call_under_the_hook_adapter(tmp_path):
    """Codex's schema carries `updatedInput`, but no live session on that runtime has been
    watched honouring it -- and a schema is not a behaviour. Same asymmetry: block."""
    destination = tmp_path / "a.py"
    destination.write_text("here\n", encoding="utf-8")
    payload = {"tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x"}}
    assert guard.redirect_blocker(payload, destination, env={}) == ""
    assert guard.redirect_blocker(payload, destination, env={guard.ADAPTER_ENV: "1"})


def test_redirect_blocker_refuses_when_the_boxs_copy_cannot_be_read(tmp_path):
    """Unreadable is not "no precondition": it is an unanswered question, and the fail-closed
    rule makes an unanswered question a block."""
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "here"}}
    assert guard.redirect_blocker(payload, tmp_path / "missing.py", env={})


def test_a_park_is_recorded_on_the_ledger_with_its_own_event(
    root, monkeypatch, ledger_root, capsys
):
    """Its own event name, not `guard-block`: a triage pass has to be able to tell a write
    that was routed to a box from a move that was refused outright, and the two have
    nothing in common but the exit code."""
    workspace = _workspace(root)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(shell_payload("git -C carameli checkout agent/x-0821", cwd=str(root))),
    )
    guard.main(["--workspace", str(workspace)])
    capsys.readouterr()
    lines = _events(ledger_root)
    assert any("guard-branch-block" in line and "branch=agent/x-0821" in line for line in lines)


def test_a_park_is_judged_before_anything_the_same_command_would_write(root, monkeypatch, capsys):
    """A command doing both is pathological, but the order is not arbitrary: the park is
    what would silence the write tier afterwards, so it is the one that has to decide."""
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    command = f"git -C carameli checkout agent/x-0821 && touch {root / 'carameli' / 'a.py'}"
    monkeypatch.setattr("sys.stdin", _stdin(shell_payload(command, cwd=str(root))))
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    assert "park the static checkout" in capsys.readouterr().err


def test_an_editor_call_is_never_read_for_a_branch_move(root, monkeypatch):
    """`switch_targets` reads a `command` key, and only a shell tool has one. An Edit whose
    payload happens to carry that word must not be parsed as a git call."""
    assert guard.switch_targets("") == []
    workspace = _workspace(root)
    monkeypatch.setattr(guard.worktree, "plan_respawn", lambda *a, **k: _plan(root))
    monkeypatch.setattr(guard.worktree, "apply_new", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            payload(
                path=str(root / "carameli" / "a.py"),
                cwd=str(root),
                command="git checkout agent/x-0821",
            )
        ),
    )
    # Routed as an ordinary edit -- re-aimed into the box, not refused as a park.
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_the_guards_git_helper_decodes_utf8_rather_than_the_platform_codec():
    """`text=True` alone decodes git's output through cp1252 on this machine, and a byte
    it cannot map -- in a branch name, a path, a commit subject -- raises inside
    subprocess's reader thread, past this helper's `check=False`. That crash would land
    in a PreToolUse hook, which is every edit in the workspace.

    Asserted on the source because the failure is in the arguments, not in the return:
    a stub git emitting a bad byte would prove nothing about the real call's keywords.
    The vendored half of this ratchet is
    `test_every_capture_in_a_vendored_hook_declares_its_codec`; the guard is
    devkit-only, so it needs its own.
    """
    source = (REPO_ROOT / "scripts" / "worktree-guard.py").read_text(encoding="utf-8")
    call = source[source.index("def _git(") :]
    call = call[: call.index("\n\n\n")]
    assert 'encoding="utf-8"' in call and 'errors="replace"' in call, (
        "worktree-guard._git must name its codec: encoding='utf-8', errors='replace'"
    )


@pytest.mark.parametrize(
    "command, expected",
    [
        # The two commands the ledger reported verbatim. Both wrote inside the directory
        # they had just moved to; both were judged against the session cwd instead and
        # matched a same-named file in a checkout.
        (
            "cd /c/Users/a/.claude/projects/C--x/memory && printf '%s\\n' x >> MEMORY.md",
            ["/c/Users/a/.claude/projects/C--x/memory/MEMORY.md"],
        ),
        (
            "cd /c/Users/a/AppData/Local/Temp/claude/s/scratchpad"
            " && gh api repos/o/r/actions/runs/1/logs > lint.zip",
            ["/c/Users/a/AppData/Local/Temp/claude/s/scratchpad/lint.zip"],
        ),
        # A relative move composes onto the base rather than replacing it.
        ("cd a && cd b && tee out.txt", ["a/b/out.txt"]),
        # A rooted move replaces it. `/b` has no drive letter, so `Path.is_absolute()`
        # answers False on Windows and only `_is_rooted` gets this right.
        ("cd /a && cd /b && tee out.txt", ["/b/out.txt"]),
        # A rooted *target* is never rebased, whatever the base is.
        ("cd /tmp && tee /etc/thing", ["/etc/thing"]),
        # No `cd`: unchanged, which is the behaviour every existing caller relies on.
        ("tee out.txt", ["out.txt"]),
        # Moves this cannot follow leave the base alone rather than guessing.
        ("cd - && tee out.txt", ["out.txt"]),
        ("cd ~ && tee out.txt", ["out.txt"]),
        ("cd $HOME && tee out.txt", ["out.txt"]),
        ("cd a b && tee out.txt", ["out.txt"]),
        # PowerShell and cmd spellings of the same move.
        ("Set-Location /a; Out-File out.txt", ["/a/out.txt"]),
        ("cd /d C:/a && tee out.txt", ["C:/a/out.txt"]),
    ],
)
def test_a_cd_earlier_in_the_command_moves_what_a_relative_write_is_relative_to(command, expected):
    """`shell_write_targets` used to hand every relative operand back as written, and the
    caller resolved it against the session cwd. A `cd` in the same command line makes that
    the wrong directory, and the guard's most-reported false positive is what follows: a
    write to `memory/MEMORY.md` after `cd`-ing there matched devkit's own `MEMORY.md` by
    basename, and a `gh api ... > lint.zip` in the scratchpad read as a write to
    `devkit/lint.zip`. Neither could be re-aimed -- "Bash arguments are not rewritable by
    this hook" -- so both were flat blocks on correct commands.
    """
    assert guard.shell_write_targets(command) == expected


def test_a_write_under_a_dot_git_directory_is_never_routed_to_a_box(root):
    """Deleting a stale `.git/index.lock` was blocked and pointed at a box path that
    cannot exist: a worktree's `.git` is a *file*, not a directory, so there is nothing
    under it to write to. The agent got through only by spelling the delete as a Python
    one-liner, which is the shape of a gate teaching people to route around it.

    Git's own state is not content: no branch is at stake, nothing is committed from it,
    and the box has nowhere to put it. `branch_of` is injected so the decline is the
    `.git` rule rather than `root`'s checkouts not being real repositories -- the same
    call one directory over must still get a box.
    """
    checkout = str(root / "carameli")
    assert (
        guard.redirect_decision(
            str(root / "carameli" / ".git" / "index.lock"),
            checkout,
            root,
            PROJECTS,
            branch_of=on_branch("master"),
        )
        is None
    )
    assert guard.redirect_decision(
        str(root / "carameli" / "app" / "index.lock"),
        checkout,
        root,
        PROJECTS,
        branch_of=on_branch("master"),
    ) == ("carameli", str(Path("app/index.lock")))


@pytest.mark.parametrize(
    "relative, internal",
    [
        (".git/index.lock", True),
        (".git/worktrees/box/HEAD", True),
        ("app/.git/config", True),
        # The `.git` *file* a worktree carries is content-shaped: it is the last part,
        # not a directory above, so it is not what this rule exempts.
        (".git", False),
        ("app/main.py", False),
        ("docs/gitignore.md", False),
    ],
)
def test_is_git_internal_names_directories_above_the_file_only(root, relative, internal):
    """The predicate on its own, because `redirect_decision` can decline for half a dozen
    other reasons and a test that only reads its `None` cannot tell which one fired."""
    assert guard._is_git_internal(root / "carameli" / relative) is internal


def test_the_hook_reads_its_payload_through_read_stdin():
    """`sys.stdin.read()` decodes through cp1252 on this machine while the harness writes
    UTF-8, and this is the hook that echoes the payload back through `updatedInput` -- so
    the corruption lands in the agent's own file. Asserted on the source: a test that fed
    a `read()` stub would pass whichever call `main` makes.
    """
    source = (REPO_ROOT / "scripts" / "worktree-guard.py").read_text(encoding="utf-8")
    body = source[source.index("\ndef main(") :]
    assert "parse_hook_input(read_stdin())" in body
    assert "sys.stdin.read()" not in body


def test_read_stdin_decodes_utf8_bytes_the_platform_codec_would_mangle():
    """The exact corruption from the report: U+2192 written as UTF-8 and read back
    through cp1252 becomes three mojibake characters, which is what reached the box."""

    class _bytes_stdin:
        def __init__(self, raw: bytes):
            self.buffer = self
            self._raw = raw

        def read(self) -> bytes:
            return self._raw

        def close(self):  # pragma: no cover - never called
            pass

    text = "a \u2192 b \u2014 c"
    raw = text.encode("utf-8")
    assert raw.decode("cp1252") != text, "pick a payload cp1252 actually mangles"
    import sys as _sys

    saved = _sys.stdin
    try:
        _sys.stdin = _bytes_stdin(raw)
        assert guard.read_stdin() == text
    finally:
        _sys.stdin = saved


def test_read_stdin_survives_a_byte_it_cannot_decode():
    """A hook that raises on PreToolUse blocks every edit in the workspace, so an
    undecodable byte must cost a character and never the turn."""

    class _bad_stdin:
        buffer = None

        def read(self) -> str:
            raise ValueError("stdin is closed")

    import sys as _sys

    saved = _sys.stdin
    try:
        _sys.stdin = _bad_stdin()
        assert guard.read_stdin() == ""
    finally:
        _sys.stdin = saved
