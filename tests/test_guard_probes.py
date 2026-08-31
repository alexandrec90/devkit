"""Tests for the git probes `worktree-guard.py` routes an edit by.

These ask a real repo on disk rather than a stub: `check-ignore` and `status` are the
oracles under test, so stubbing git here would test nothing but the stub. The guard's
own tests inject `exempt` instead, because at that level the question is what the
decision does with the answer, not how the answer is obtained.

Every probe fails closed, and each has a named test for it: an unanswerable probe must
route the edit into a box, never let it through.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import REPO_ROOT, guard_probes


def make_repo(path: Path, gitignore: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(path)], check=True, capture_output=True)
    (path / ".gitignore").write_text(gitignore, encoding="utf-8")
    return path


def make_repo_with_a_commit(path: Path, name: str = "app/main.py") -> Path:
    """A repo with one tracked, committed file.

    `git status` is the oracle for `path_is_modified`, so an empty repo would answer
    "clean" for the wrong reason. `core.hooksPath` is pinned at a directory that does not
    exist because this machine installs a global branch policy as a hooks path, and a
    fixture repo must not be judged by it.
    """
    make_repo(path)
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original\n", encoding="utf-8")
    git = ["git", "-C", str(path), "-c", f"core.hooksPath={path / '.nohooks'}"]
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [*git, "-c", "user.email=t@e.st", "-c", "user.name=t", "commit", "--quiet", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return path


# --- git-ignored paths ------------------------------------------------------


def test_path_is_ignored_reads_the_projects_own_gitignore(tmp_path):
    """What is ignored is per-project and already written down, which is why this asks
    git rather than carrying a hard-coded list of names."""
    repo = make_repo(tmp_path / "carameli", ".env\n.env.*\nlogs/\n")
    assert guard_probes.path_is_ignored(repo, repo / ".env") is True
    assert guard_probes.path_is_ignored(repo, repo / ".env.local") is True
    assert guard_probes.path_is_ignored(repo, repo / "logs" / "runtime.log") is True


def test_path_is_ignored_is_false_for_an_ordinary_source_file(tmp_path):
    repo = make_repo(tmp_path / "carameli", "*.md\n")
    assert guard_probes.path_is_ignored(repo, repo / "app" / "main.py") is False


def test_a_tracked_file_matching_an_ignore_rule_is_not_ignored(tmp_path):
    """`check-ignore` consults the index (no `--no-index`) on purpose: tracked is
    tracked, and an edit to a tracked file lands on the home branch however its name
    reads -- which is the case the hook exists for."""
    repo = make_repo(tmp_path / "carameli", "*.md\n")
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", "README.md"], check=True, capture_output=True
    )
    assert guard_probes.path_is_ignored(repo, repo / "README.md") is False


def test_path_is_ignored_fails_closed_when_git_will_not_answer(tmp_path):
    """A directory that is not a repo stands in for every way the probe can fail. A hook
    that cannot read the repo must not start letting edits through on the strength of a
    failed subprocess -- it routes them, as it always did."""
    assert guard_probes.path_is_ignored(tmp_path, tmp_path / ".env") is False


# --- paths the human already left dirty -------------------------------------


def test_path_is_modified_sees_a_staged_change(tmp_path):
    """The case that earned this probe: the user's in-browser editor wrote carameli's
    `layoutConfig.ts` and the change was staged on `master` before any agent touched
    it."""
    repo = make_repo_with_a_commit(tmp_path / "carameli")
    (repo / "app" / "main.py").write_text("edited\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    assert guard_probes.path_is_modified(repo, repo / "app" / "main.py") is True


def test_path_is_modified_sees_an_unstaged_change(tmp_path):
    """Staged or not is not the question -- "is this already the human's WIP" is."""
    repo = make_repo_with_a_commit(tmp_path / "carameli")
    (repo / "app" / "main.py").write_text("edited\n", encoding="utf-8")
    assert guard_probes.path_is_modified(repo, repo / "app" / "main.py") is True


def test_path_is_modified_is_false_for_a_clean_tracked_file(tmp_path):
    repo = make_repo_with_a_commit(tmp_path / "carameli")
    assert guard_probes.path_is_modified(repo, repo / "app" / "main.py") is False


def test_path_is_modified_ignores_an_untracked_file(tmp_path):
    """`--untracked-files=no`: the agent's own `Write` can create one of these, so
    treating it as pre-existing WIP would widen the exemption to cover the guard's own
    escapees."""
    repo = make_repo_with_a_commit(tmp_path / "carameli")
    (repo / "app" / "new.py").write_text("fresh\n", encoding="utf-8")
    assert guard_probes.path_is_modified(repo, repo / "app" / "new.py") is False


def test_path_is_modified_is_per_path_not_per_checkout(tmp_path):
    """One file the human left dirty does not license a write anywhere else in the
    tree."""
    repo = make_repo_with_a_commit(tmp_path / "carameli")
    (repo / "app" / "other.py").write_text("also tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            f"core.hooksPath={repo / '.nohooks'}",
            "-c",
            "user.email=t@e.st",
            "-c",
            "user.name=t",
            "commit",
            "--quiet",
            "-m",
            "second",
        ],
        check=True,
        capture_output=True,
    )
    (repo / "app" / "main.py").write_text("edited\n", encoding="utf-8")
    assert guard_probes.path_is_modified(repo, repo / "app" / "main.py") is True
    assert guard_probes.path_is_modified(repo, repo / "app" / "other.py") is False


def test_path_is_modified_fails_closed_when_git_will_not_answer(tmp_path):
    assert guard_probes.path_is_modified(tmp_path, tmp_path / "app" / "main.py") is False


# --- the two together -------------------------------------------------------


def test_path_is_exempt_covers_both_reasons(tmp_path):
    repo = make_repo_with_a_commit(tmp_path / "carameli")
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / "app" / "main.py").write_text("edited\n", encoding="utf-8")
    assert guard_probes.path_is_exempt(repo, repo / ".env") is True
    assert guard_probes.path_is_exempt(repo, repo / "app" / "main.py") is True


def test_path_is_exempt_is_false_for_a_clean_tracked_file(tmp_path):
    """The population the guard exists for: nothing about this path says a box would
    protect nothing, so it gets one."""
    repo = make_repo_with_a_commit(tmp_path / "carameli")
    assert guard_probes.path_is_exempt(repo, repo / "app" / "main.py") is False


def test_git_never_raises_when_the_directory_is_not_a_repo(tmp_path):
    """The shared runner both probes fail closed on top of: it returns a non-zero result
    rather than raising, so each caller decides what silence means."""
    assert guard_probes.git(tmp_path, "status", "--porcelain").returncode != 0


def test_git_decodes_utf8_rather_than_the_platform_codec():
    """`text=True` alone decodes git's output through cp1252 on this machine, and a byte
    it cannot map -- in a branch name, a path, a commit subject -- raises inside
    subprocess's reader thread, past this helper's `check=False`. That crash would land
    in a PreToolUse hook, which is every edit in the workspace.

    Asserted on the source because the failure is in the arguments, not in the return:
    a stub git emitting a bad byte would prove nothing about the real call's keywords.
    The vendored half of this ratchet is
    `test_every_capture_in_a_vendored_hook_declares_its_codec`; these probes are
    devkit-only, so they need their own.
    """
    source = (REPO_ROOT / "scripts" / "guard_probes.py").read_text(encoding="utf-8")
    call = source[source.index("def git(") :]
    call = call[: call.index("\n\n\n")]
    assert 'encoding="utf-8"' in call and 'errors="replace"' in call, (
        "guard_probes.git must name its codec: encoding='utf-8', errors='replace'"
    )
