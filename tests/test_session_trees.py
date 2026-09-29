"""Tests for `scripts/session_trees.py` -- reaping a merged `.claude/worktrees` tree.

The ledger report this answers: a fixer tree's compose project (`carameli-fix-nightly-
0919`, 1.3 GB) outlived its merged PR by days, because `worktree.py reconcile` reaps only
the box tier and nothing else ever removed a session tree. Every test here pins a way a
tree could hold the only copy of something, and so must be kept.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from support import load_script

st = load_script("scripts/session_trees.py")

QUIET = st.QUIET_HOURS * 3600 + 1


def done(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def porcelain(*trees: tuple[str, str, str, bool]) -> str:
    blocks = []
    for path, branch, head, locked in trees:
        lines = [f"worktree {path}", f"HEAD {head}", f"branch refs/heads/{branch}"]
        blocks.append("\n".join([*lines, *(["locked"] if locked else [])]))
    return "\n\n".join(blocks) + "\n"


def tree(tmp_path: Path, name: str = "fix-nightly-0919", **kw):
    path = tmp_path / "carameli" / ".claude" / "worktrees" / name
    return st.Tree(
        path=path,
        branch=kw.get("branch", f"agent/{name}"),
        head=kw.get("head", "abc"),
        locked=kw.get("locked", False),
    )


# --- reading git and gh -------------------------------------------------------------


def test_the_porcelain_is_read_with_branch_head_and_lock():
    text = porcelain(("C:/w/carameli", "master", "111", False), ("C:/w/t", "agent/x", "222", True))
    found = st.parse_trees(text)
    assert [(t.path.name, t.branch, t.head, t.locked) for t in found] == [
        ("carameli", "master", "111", False),
        ("t", "agent/x", "222", True),
    ]
    assert st.parse_trees("worktree C:/w/d\nHEAD 333\ndetached\n")[0].branch == ""


def test_merged_heads_keeps_only_well_formed_rows():
    payload = json.dumps([{"headRefName": "a", "headRefOid": "1"}, {"headRefName": "b"}, "x"])
    assert st.merged_heads(payload) == {"a": "1"}
    assert st.merged_heads("not json") == {}


def test_the_compose_name_is_the_trees_own(tmp_path):
    assert st.compose_name(tmp_path) == ""
    (tmp_path / ".env").write_text('X=1\nCOMPOSE_PROJECT_NAME="carameli-fix-0919"\n', "utf-8")
    assert st.compose_name(tmp_path) == "carameli-fix-0919"


def test_generated_files_and_line_ending_churn_are_noise_not_work(tmp_path):
    def run(argv):
        if "status" in argv:
            return done(out="?? AGENTS.md\n M .secrets.baseline\n M app.py\n?? notes.txt\n")
        return done(0 if argv[-1] == ".secrets.baseline" else 1)  # `diff --quiet`

    work, noise = st.changes(tmp_path, run)
    assert work == ["app.py", "notes.txt"]
    assert noise == ["AGENTS.md", ".secrets.baseline"]
    assert st.changes(tmp_path, lambda argv: done(128)) is None


# --- the verdict --------------------------------------------------------------------


def test_a_clean_quiet_tree_whose_pr_merged_at_its_head_is_reaped(tmp_path):
    one = tree(tmp_path)
    assert st.verdict(one, tmp_path / "carameli", False, {one.branch: "abc"}, QUIET) == ""


def test_every_reason_to_keep_a_tree_keeps_it(tmp_path):
    checkout = tmp_path / "carameli"
    one = tree(tmp_path)
    merged = {one.branch: "abc"}
    kept = {
        "the checkout itself": st.verdict(
            st.Tree(checkout, "master", "abc", False), checkout, False, merged, QUIET
        ),
        "a box": st.verdict(
            st.Tree(tmp_path / ".worktrees" / "carameli--x", one.branch, "abc", False),
            checkout,
            False,
            merged,
            QUIET,
        ),
        "locked": st.verdict(tree(tmp_path, locked=True), checkout, False, merged, QUIET),
        "recently used": st.verdict(one, checkout, False, merged, 60),
        "unknown activity": st.verdict(one, checkout, False, merged, None),
        "unreadable status": st.verdict(one, checkout, None, merged, QUIET),
        "dirty": st.verdict(one, checkout, True, merged, QUIET),
        "detached": st.verdict(tree(tmp_path, branch=""), checkout, False, merged, QUIET),
        "no merged PR": st.verdict(one, checkout, False, {}, QUIET),
        "commits after the merge": st.verdict(one, checkout, False, {one.branch: "def"}, QUIET),
    }
    assert all(kept.values()), [name for name, why in kept.items() if not why]


# --- acting -------------------------------------------------------------------------


def test_the_reap_downs_the_trees_own_stack_then_removes_it_without_force(tmp_path):
    one = tree(tmp_path)
    one.path.mkdir(parents=True)
    env = "COMPOSE_PROJECT_NAME=carameli-fix-nightly-0919\n"  # pragma: allowlist secret - a compose project name, not a credential
    (one.path / ".env").write_text(env, "utf-8")
    (one.path / "AGENTS.md").write_text("generated", "utf-8")
    calls: list[list[str]] = []
    error = st.reap(
        one, tmp_path / "carameli", lambda argv: calls.append(argv) or done(), ["AGENTS.md"]
    )
    assert error == ""
    assert not (one.path / "AGENTS.md").exists()
    assert calls[0] == ["docker", "compose", "-p", "carameli-fix-nightly-0919", "down", "-v"]
    assert calls[1][-3:] == ["worktree", "remove", str(one.path)]
    assert "--force" not in calls[1]


def test_a_compose_name_equal_to_the_checkouts_is_never_downed(tmp_path):
    """That name is the static checkout's stack -- its volumes hold the dev database."""
    one = tree(tmp_path)
    one.path.mkdir(parents=True)
    (one.path / ".env").write_text("COMPOSE_PROJECT_NAME=carameli\n", "utf-8")
    calls: list[list[str]] = []
    st.reap(one, tmp_path / "carameli", lambda argv: calls.append(argv) or done())
    assert not any(argv[0] == "docker" for argv in calls)


def test_a_refused_removal_is_reported_with_gits_words(tmp_path):
    one = tree(tmp_path)
    error = st.reap(one, tmp_path / "carameli", lambda argv: done(1, err="contains modified"))
    assert "refused" in error and "contains modified" in error


def _remover(error: str = ""):
    removed: list[Path] = []

    def remove(path: Path) -> tuple[str, list[str]]:
        removed.append(path)
        return error, []

    return removed, remove


def test_a_removal_the_filesystem_refused_partway_is_finished(tmp_path):
    """devkit, 2026-09-29: git dropped the `.git` link and the registration, then a
    `.venv` file failed with `Invalid argument` -- a husk no later pass would name."""
    one = tree(tmp_path)
    (one.path / ".venv").mkdir(parents=True)
    calls: list[list[str]] = []
    said = "error: failed to delete 'C:/x/fix-0928': Invalid argument"
    removed, remove = _remover()

    def run(argv):
        calls.append(argv)
        return done(255, err=said) if "remove" in argv else done()

    assert st.reap(one, tmp_path / "carameli", run, remove=remove) == ""
    assert removed == [one.path]
    assert calls[-1][-2:] == ["worktree", "prune"]


def test_a_finish_that_fails_too_names_both_failures(tmp_path):
    one = tree(tmp_path)
    one.path.mkdir(parents=True)
    _removed, remove = _remover("x.pyd: Access is denied")
    run = lambda argv: done(255, err="Invalid argument") if "remove" in argv else done()
    error = st.reap(one, tmp_path / "carameli", run, remove=remove)
    assert "Invalid argument" in error and "x.pyd: Access is denied" in error


def test_a_dirty_tree_refusal_is_never_finished_by_hand(tmp_path):
    one = tree(tmp_path)
    one.path.mkdir(parents=True)
    (one.path / ".git").write_text("gitdir: elsewhere", "utf-8")
    removed, remove = _remover()
    run = lambda argv: done(128, err="contains modified or untracked files, use --force")
    error = st.reap(one, tmp_path / "carameli", run, remove=remove)
    assert "refused" in error and removed == []


def _husks(tmp_path):
    checkout = tmp_path / "carameli"
    root = checkout / ".claude" / "worktrees"
    (root / "husk-0928" / ".venv").mkdir(parents=True)
    (root / "busy-0929").mkdir()
    (root / "fix-nightly-0919").mkdir()  # registered: its own reap decides it
    (root / "hand-cut").mkdir()
    (root / "hand-cut" / ".git").write_text("gitdir: elsewhere", "utf-8")
    listed = [st.Tree(checkout, "master", "000", False), tree(tmp_path)]
    return checkout, root, listed


def test_a_husk_is_an_unregistered_tier_directory_with_no_git_link(tmp_path):
    checkout, root, listed = _husks(tmp_path)
    assert st.husks(checkout, listed) == [root / "busy-0929", root / "husk-0928"]
    assert st.husks(tmp_path / "no-tier-here", []) == []


def test_sweep_husks_removes_a_quiet_husk_and_keeps_a_busy_one(tmp_path):
    checkout, root, listed = _husks(tmp_path)
    idle = lambda path: 60 if path.name == "busy-0929" else QUIET
    said: list[str] = []
    removed, remove = _remover()
    assert st.sweep_husks(checkout, listed, False, said.append, idle, remove) == 0
    assert said == ["session tree carameli:husk-0928: a husk a removal left behind -- would remove"]
    assert removed == []
    said.clear()
    assert st.sweep_husks(checkout, listed, True, said.append, idle, remove) == 0
    assert removed == [root / "husk-0928"]
    assert said == ["session tree carameli:husk-0928: a husk a removal left behind -- removed"]
    _removed, refused = _remover("Access is denied")
    said.clear()
    assert st.sweep_husks(checkout, listed, True, said.append, lambda p: None, refused) == 0
    assert st.sweep_husks(checkout, listed, True, said.append, idle, refused) == 1
    assert said == ["session tree carameli:husk-0928: could not remove its husk: Access is denied"]


def test_the_checkout_sweep_counts_husks_even_with_no_session_tree(tmp_path, monkeypatch):
    checkout = tmp_path / "carameli"
    (checkout / ".claude" / "worktrees" / "husk").mkdir(parents=True)
    monkeypatch.setattr(st.box_teardown, "force_remove_box", lambda path: ("denied", []))
    run = lambda argv: done(out=porcelain((str(checkout), "master", "000", False)))
    said: list[str] = []
    gh = lambda path: lambda *args: done(1)
    assert st.sweep_checkout(checkout, True, said.append, run, gh, lambda p: QUIET) == 1
    assert said == ["session tree carameli:husk: could not remove its husk: denied"]


def _admin_only(monkeypatch, elevated: bool = False) -> None:
    """The carameli husk of 2026-09-29: a `.pytest_cache` only Administrators may open."""
    monkeypatch.setattr(st.wt_profile, "is_elevated", lambda: elevated)
    monkeypatch.setattr(st.box_teardown, "unopenable", lambda path: [str(path / ".pytest_cache")])
    monkeypatch.setattr(
        st.box_teardown, "force_remove_box", lambda path: (".pytest_cache: Access is denied", [])
    )


def test_admin_only_answers_only_an_unelevated_pass_over_an_unopenable_entry(tmp_path, monkeypatch):
    husk = tmp_path / "husk"
    husk.mkdir()
    _admin_only(monkeypatch)
    assert st.admin_only(tmp_path / "gone") == ""
    why = st.admin_only(husk)
    assert why.startswith("only an administrator can remove it") and str(husk) in why
    monkeypatch.setattr(st.box_teardown, "unopenable", lambda path: ["a", "b", "c"])
    assert "left a (+2 more)" in st.admin_only(husk)
    monkeypatch.setattr(st.box_teardown, "unopenable", lambda path: [])
    assert st.admin_only(husk) == ""
    _admin_only(monkeypatch, elevated=True)
    assert st.admin_only(husk) == ""


def test_a_husk_only_an_administrator_can_delete_is_named_not_failed(tmp_path, monkeypatch):
    """carameli, 2026-09-29: an elevated session's pytest left a `.pytest_cache` whose ACL
    grants Administrators alone. The unelevated reap failed on it every run, so the ledger
    sent a fixer after it every run, and no fixer -- unelevated by design -- could act.
    The line now names the one command that clears it and who must run it."""
    checkout = tmp_path / "carameli"
    husk = checkout / ".claude" / "worktrees" / "declarative-finding-sky"
    husk.mkdir(parents=True)
    _admin_only(monkeypatch)
    run = lambda argv: done(out=porcelain((str(checkout), "master", "000", False)))
    said: list[str] = []
    gh = lambda path: lambda *args: done(1)
    assert st.sweep_checkout(checkout, True, said.append, run, gh, lambda p: QUIET) == 0
    assert len(said) == 1
    assert "only an administrator can remove it" in said[0]
    assert "elevated shell" in said[0] and str(husk) in said[0]
    assert str(husk / ".pytest_cache") in said[0]


def test_an_elevated_pass_that_still_fails_is_a_failure(tmp_path, monkeypatch):
    checkout = tmp_path / "carameli"
    (checkout / ".claude" / "worktrees" / "husk").mkdir(parents=True)
    _admin_only(monkeypatch, elevated=True)
    run = lambda argv: done(out=porcelain((str(checkout), "master", "000", False)))
    said: list[str] = []
    gh = lambda path: lambda *args: done(1)
    assert st.sweep_checkout(checkout, True, said.append, run, gh, lambda p: QUIET) == 1
    assert "could not remove its husk" in said[0]


def test_a_reap_only_an_administrator_can_finish_is_named_not_failed(tmp_path, monkeypatch):
    checkout, _run, gh, _calls = _checkout(tmp_path)
    one = tree(tmp_path)
    one.path.mkdir(parents=True)

    def run(argv):
        if argv[-2:] == ["list", "--porcelain"]:
            return _run(argv)
        return done(255, err="Access is denied") if "remove" in argv else done()

    _admin_only(monkeypatch)
    said: list[str] = []
    assert st.sweep_checkout(checkout, True, said.append, run, gh, lambda p: QUIET) == 0
    assert len(said) == 1 and "only an administrator can remove it" in said[0]


def _checkout(tmp_path, head: str = "abc"):
    checkout = tmp_path / "carameli"
    one = tree(tmp_path, head=head)
    listing = porcelain(
        (str(checkout), "master", "000", False), (str(one.path), one.branch, head, False)
    )
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        if argv[-2:] == ["list", "--porcelain"]:
            return done(out=listing)
        return done()

    gh = lambda path: (
        lambda *args: done(out=json.dumps([{"headRefName": one.branch, "headRefOid": "abc"}]))
    )
    return checkout, run, gh, calls


def test_status_names_what_it_would_reap_and_touches_nothing(tmp_path):
    checkout, run, gh, calls = _checkout(tmp_path)
    said: list[str] = []
    assert st.sweep_checkout(checkout, False, said.append, run, gh, lambda p: QUIET) == 0
    assert said == [
        "session tree carameli:fix-nightly-0919: its PR merged at this HEAD -- would reap"
    ]
    assert not any("remove" in argv for argv in calls)


def test_apply_reaps_and_a_kept_tree_is_not_mentioned(tmp_path):
    checkout, run, gh, calls = _checkout(tmp_path)
    said: list[str] = []
    assert st.sweep_checkout(checkout, True, said.append, run, gh, lambda p: QUIET) == 0
    assert said[0].endswith("-- reaped")
    assert any(argv[-3:-1] == ["worktree", "remove"] for argv in calls)
    checkout, run, gh, _calls = _checkout(tmp_path, head="newer")
    said.clear()
    assert st.sweep_checkout(checkout, True, said.append, run, gh, lambda p: QUIET) == 0
    assert said == []


def test_an_unreadable_pr_list_keeps_everything_and_says_so(tmp_path):
    checkout, run, _gh, calls = _checkout(tmp_path)
    said: list[str] = []
    gh = lambda path: lambda *args: done(1)
    assert st.sweep_checkout(checkout, True, said.append, run, gh, lambda p: QUIET) == 0
    assert "could not be read" in said[0]
    assert not any("remove" in argv for argv in calls)


def test_idle_seconds_reads_the_transcript_store(tmp_path):
    store = tmp_path / "store"
    target = tmp_path / "w"
    (store / st.rc_machine.slug(target)).mkdir(parents=True)
    transcript = store / st.rc_machine.slug(target) / "s.jsonl"
    transcript.write_text("{}", "utf-8")
    mtime = transcript.stat().st_mtime
    assert st.idle_seconds(target, store, now=mtime + 10) == 10
    assert st.idle_seconds(target, tmp_path / "missing") is None


def test_sweep_workspace_walks_each_listed_checkout_and_survives_a_missing_file(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    assert st.sweep_workspace(workspace, False, print) == 0
    workspace.write_text('{"folders": [{"path": "not-a-repo"}]}', "utf-8")
    (tmp_path / "not-a-repo").mkdir()
    said: list[str] = []
    assert st.sweep_workspace(workspace, True, said.append) == 0
    assert said == []
