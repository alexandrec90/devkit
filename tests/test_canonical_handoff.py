"""`scripts/canonical_handoff.py`: an edit of the registry leaves the static checkout for
a task branch, and the checkout is left clean on its default branch."""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import load_script

canonical_handoff = load_script("scripts/canonical_handoff.py")


def checkout(path: Path) -> Path:
    """A committed repo on `main` holding `projects.toml`, judged by no hook."""
    path.mkdir(parents=True)
    registry = path / "projects.toml"
    registry.write_text('projects = ["a"]\n', encoding="utf-8", newline="\n")
    for args in (
        ("init", "--quiet", "-b", "main"),
        ("config", "core.hooksPath", str(path / ".nohooks")),
        ("config", "user.email", "t@e.st"),
        ("config", "user.name", "t"),
        ("add", "-A"),
        ("commit", "--quiet", "-m", "init"),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return registry


def git_out(path: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return done.stdout.strip()


def test_the_edit_moves_to_a_task_branch_and_the_checkout_is_put_back(tmp_path):
    registry = checkout(tmp_path / "devkit")
    registry.write_text("projects = []\n", encoding="utf-8", newline="\n")

    cut = canonical_handoff.hand_off(
        registry, "unplug-carameli", "Unplug carameli\n", carry=lambda tree: "and more\n"
    )

    assert cut.branch.startswith("agent/unplug-carameli-") and cut.said == ""
    # What `fix-pass-watchdog.self_update` asks before it fast-forwards.
    assert git_out(registry.parent, "status", "--porcelain", "--untracked-files=no") == ""
    assert git_out(registry.parent, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert cut.tree is not None
    assert git_out(cut.tree, "rev-parse", "--abbrev-ref", "HEAD") == cut.branch
    assert (cut.tree / "projects.toml").read_text(encoding="utf-8") == "projects = []\n"
    intent = (cut.tree / "logs" / "ship-intent.md").read_text(encoding="utf-8")
    assert intent == "Unplug carameli\nand more\n"


def test_nothing_is_handed_off_without_an_edit_or_off_the_default_branch(tmp_path):
    registry = checkout(tmp_path / "devkit")
    assert canonical_handoff.hand_off(registry, "x", "X\n") == canonical_handoff.Cut()
    subprocess.run(
        ["git", "-C", str(registry.parent), "checkout", "--quiet", "-b", "agent/x-1111"],
        check=True,
    )
    registry.write_text("projects = []\n", encoding="utf-8", newline="\n")
    assert canonical_handoff.hand_off(registry, "x", "X\n") == canonical_handoff.Cut()
    assert canonical_handoff.dirty(registry), "an edit on a branch already stays on it"


def test_a_cut_that_fails_says_why_and_leaves_the_edit(tmp_path):
    registry = checkout(tmp_path / "devkit")
    registry.write_text("projects = []\n", encoding="utf-8", newline="\n")
    real = subprocess.run

    def runner(argv, **kw):
        if "worktree" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: no room\n")
        return real(argv, **kw)

    cut = canonical_handoff.hand_off(registry, "x", "X\n", runner)
    assert cut.said.endswith("fatal: no room") and cut.tree is None
    assert canonical_handoff.dirty(registry)


def test_git_in_runs_git_in_the_checkout_and_never_raises(tmp_path):
    registry = checkout(tmp_path / "devkit")
    git = canonical_handoff.git_in(registry.parent)
    assert git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert git("no-such-verb").returncode != 0


def _commit_beside(registry: Path, name: str, text: str) -> Path:
    """Commit `name` holding `text` beside `registry`, in its checkout."""
    path = registry.parent / name
    path.write_text(text, encoding="utf-8", newline="\n")
    for args in (("add", name), ("commit", "--quiet", "-m", f"add {name}")):
        subprocess.run(["git", "-C", str(registry.parent), *args], check=True, capture_output=True)
    return path


def test_a_file_the_same_edit_wrote_moves_onto_the_same_branch(tmp_path):
    """A sibling the same edit wrote lands in the one commit with the registry."""
    registry = checkout(tmp_path / "devkit")
    ports = _commit_beside(registry, "ports.toml", "[slots]\na = 1\n")
    registry.write_text('projects = ["a", "b"]\n', encoding="utf-8", newline="\n")
    ports.write_text("[slots]\na = 1\nb = 2\n", encoding="utf-8", newline="\n")

    cut = canonical_handoff.hand_off(registry, "plug-b", "Plug b\n", also=("ports.toml",))

    assert cut.tree is not None and cut.said == ""
    assert git_out(registry.parent, "status", "--porcelain", "--untracked-files=no") == ""
    assert ports.read_text(encoding="utf-8") == "[slots]\na = 1\n"
    assert (cut.tree / "ports.toml").read_text(encoding="utf-8") == "[slots]\na = 1\nb = 2\n"
    changed = sorted(git_out(cut.tree, "diff", "--name-only").splitlines())
    assert changed == ["ports.toml", "projects.toml"]


def test_an_edit_of_the_sibling_alone_is_still_handed_off(tmp_path):
    registry = checkout(tmp_path / "devkit")
    ports = _commit_beside(registry, "ports.toml", "[slots]\n")
    ports.write_text("[slots]\nb = 2\n", encoding="utf-8", newline="\n")

    cut = canonical_handoff.hand_off(registry, "x", "X\n", also=("ports.toml",))

    assert cut.tree is not None
    assert git_out(cut.tree, "diff", "--name-only") == "ports.toml"
    assert git_out(registry.parent, "status", "--porcelain", "--untracked-files=no") == ""


def test_a_sibling_that_is_absent_or_unedited_is_skipped(tmp_path):
    registry = checkout(tmp_path / "devkit")
    registry.write_text("projects = []\n", encoding="utf-8", newline="\n")

    cut = canonical_handoff.hand_off(registry, "x", "X\n", also=("ports.toml",))

    assert cut.tree is not None
    assert git_out(cut.tree, "diff", "--name-only") == "projects.toml"
    assert not (registry.parent / "ports.toml").exists()


def test_a_path_git_cannot_read_is_not_dirty(tmp_path):
    (tmp_path / "projects.toml").write_text("projects = []\n", encoding="utf-8")
    assert not canonical_handoff.dirty(tmp_path / "projects.toml")
