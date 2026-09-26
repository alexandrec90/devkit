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
