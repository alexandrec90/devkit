"""`scripts/canonical_handoff.py`: an edit of the canonical copy leaves the static
checkout for a task branch, and the checkout is left clean on its default branch."""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import load_script

canonical_handoff = load_script("scripts/canonical_handoff.py")


def checkout(path: Path) -> Path:
    """A committed repo on `main` holding `workspace.jsonc`, judged by no hook."""
    path.mkdir(parents=True)
    canonical = path / "workspace.jsonc"
    canonical.write_text("{}\n", encoding="utf-8", newline="\n")
    for args in (
        ("init", "--quiet", "-b", "main"),
        ("config", "core.hooksPath", str(path / ".nohooks")),
        ("config", "user.email", "t@e.st"),
        ("config", "user.name", "t"),
        ("add", "-A"),
        ("commit", "--quiet", "-m", "init"),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return canonical


def git_out(path: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return done.stdout.strip()


def test_the_edit_moves_to_a_task_branch_and_the_checkout_is_put_back(tmp_path):
    canonical = checkout(tmp_path / "devkit")
    canonical.write_text('{"folders": []}\n', encoding="utf-8", newline="\n")

    cut = canonical_handoff.hand_off(
        canonical, "unplug-carameli", "Unplug carameli\n", carry=lambda tree: "and more\n"
    )

    assert cut.branch.startswith("agent/unplug-carameli-") and cut.said == ""
    # What `fix-pass-watchdog.self_update` asks before it fast-forwards.
    assert git_out(canonical.parent, "status", "--porcelain", "--untracked-files=no") == ""
    assert git_out(canonical.parent, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert cut.tree is not None
    assert git_out(cut.tree, "rev-parse", "--abbrev-ref", "HEAD") == cut.branch
    assert (cut.tree / "workspace.jsonc").read_text(encoding="utf-8") == '{"folders": []}\n'
    intent = (cut.tree / "logs" / "ship-intent.md").read_text(encoding="utf-8")
    assert intent == "Unplug carameli\nand more\n"


def test_nothing_is_handed_off_without_an_edit_or_off_the_default_branch(tmp_path):
    canonical = checkout(tmp_path / "devkit")
    assert canonical_handoff.hand_off(canonical, "x", "X\n") == canonical_handoff.Cut()
    subprocess.run(
        ["git", "-C", str(canonical.parent), "checkout", "--quiet", "-b", "agent/x-1111"],
        check=True,
    )
    canonical.write_text("{ }\n", encoding="utf-8", newline="\n")
    assert canonical_handoff.hand_off(canonical, "x", "X\n") == canonical_handoff.Cut()
    assert canonical_handoff.dirty(canonical), "an edit on a branch already stays on it"


def test_a_cut_that_fails_says_why_and_leaves_the_edit(tmp_path):
    canonical = checkout(tmp_path / "devkit")
    canonical.write_text("{ }\n", encoding="utf-8", newline="\n")
    real = subprocess.run

    def runner(argv, **kw):
        if "worktree" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: no room\n")
        return real(argv, **kw)

    cut = canonical_handoff.hand_off(canonical, "x", "X\n", runner)
    assert cut.said.endswith("fatal: no room") and cut.tree is None
    assert canonical_handoff.dirty(canonical)


def test_git_in_runs_git_in_the_checkout_and_never_raises(tmp_path):
    canonical = checkout(tmp_path / "devkit")
    git = canonical_handoff.git_in(canonical.parent)
    assert git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert git("no-such-verb").returncode != 0


def test_a_path_git_cannot_read_is_not_dirty(tmp_path):
    (tmp_path / "workspace.jsonc").write_text("{}", encoding="utf-8")
    assert not canonical_handoff.dirty(tmp_path / "workspace.jsonc")
