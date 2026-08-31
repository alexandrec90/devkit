"""Tests for the host half of a box's teardown.

Every one of these is about a box a live process is still running out of, which is the
state that produced the husks of 2026-08-30 — see the module docstring. The eviction is
asserted as *argv* against a fake `run`, the way `test_worktree.py` asserts the reap
steps: killing a process for real to check that we kill processes is not a test anyone
can afford to have go wrong.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from support import box_teardown


def test_the_holders_query_names_the_box_as_a_directory_prefix():
    """A sibling box whose name merely starts the same way must not match.

    `carameli--x-0830` is a prefix of `carameli--x-0830-2`, and the comparison is a
    `StartsWith`, so without the trailing separator a reap of the first would kill the
    second's dev server. Lowercased on both sides because the script lowercases what it
    compares against.
    """
    script = box_teardown.holders_script(Path("C:/ws/.worktrees/Demo--X-0806"))
    assert "$needle = 'c:\\ws\\.worktrees\\demo--x-0806\\'" in script
    assert "Get-Process" in script


def test_parse_holders_drops_noise_and_never_names_this_process():
    """The rows are the kill list, so anything unparseable has to fall out of it -- and
    this process above all: `reconcile` runs the reap, and a python whose own executable
    is a box's `.venv` interpreter is exactly the shape the query looks for."""
    text = f"4242|node\nAdd-Type warning\n{os.getpid()}|python\n|nameless\nxyz|node\n"
    assert box_teardown.parse_holders(text) == [(4242, "node")]


def test_holders_are_empty_when_powershell_cannot_be_asked():
    """Same contract as every other probe here: "could not ask" is never "kill nothing
    is holding it", because the caller's next move on an empty list is to retry the
    delete and report the real error rather than to claim it evicted something."""

    def refuse(*args, **kwargs):
        raise OSError("powershell is not on PATH")

    assert box_teardown.box_holders(Path("C:/ws/.worktrees/demo--x-0806"), run=refuse) == []


def test_evicting_holders_kills_each_process_tree():
    """`/T` is the load-bearing flag: on Windows the dev server is a `node` child of an
    `npm.cmd` wrapper, and killing the wrapper alone leaves the child holding the file."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "powershell":
            return subprocess.CompletedProcess(cmd, 0, "4242|node\n8484|esbuild\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    killed = box_teardown.evict_box_holders(Path("C:/ws/.worktrees/demo--x-0806"), run=fake_run)
    assert killed == ["node (4242)", "esbuild (8484)"]
    assert ["taskkill", "/T", "/F", "/PID", "4242"] in calls
    assert ["taskkill", "/T", "/F", "/PID", "8484"] in calls


def test_a_locked_file_no_longer_costs_the_rest_of_the_tree(tmp_path, monkeypatch):
    """The `onexc` hook re-raised what it could not fix, which aborted `rmtree` at the
    first such entry -- so one mapped `.node` left every sibling and every parent on
    disk, and the caller was handed that one path as though it were all that remained.
    Everything deletable goes now, and the file itself leads the report because `rmtree`
    is depth-first."""
    box_dir = tmp_path / "demo--x-0806"
    (box_dir / "nested").mkdir(parents=True)
    (box_dir / "nested" / "binding.node").write_text("native", encoding="utf-8")
    (box_dir / "ordinary.txt").write_text("disposable", encoding="utf-8")

    real_unlink = os.unlink

    def stubborn(path, *args, **kwargs):
        if str(path).endswith("binding.node"):
            raise PermissionError(13, "Access is denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", stubborn)
    error = box_teardown.remove_tree_longpath(box_dir)
    assert "binding.node" in error.splitlines()[0]
    assert not (box_dir / "ordinary.txt").exists(), "one locked file stopped the whole walk"


def test_a_dir_fd_flavoured_callback_is_recorded_rather_than_replayed(tmp_path, monkeypatch):
    """The hook must delete by path, not replay the callable `rmtree` handed it.

    On POSIX `rmtree` walks with directory file descriptors, so that callable is
    `os.open`/`os.unlink`/`os.rmdir` still owed a `dir_fd` and a bare name. Replaying it
    with a full path raised `TypeError: open() missing required argument 'flags'` —
    not an `OSError`, so it went straight past the hook's `except` and aborted the very
    walk the hook exists to keep going. Windows takes `rmtree`'s path-based branch and
    never sees it; the first CI run did, on three tests at once.

    Simulated rather than skipped on Windows: the contract that broke is what `rmtree`
    passes `onexc`, and the fake passes exactly that.
    """
    box_dir = tmp_path / "demo--x-0806"
    (box_dir / "nested").mkdir(parents=True)
    (box_dir / "nested" / "binding.node").write_text("native", encoding="utf-8")

    def fd_walking_rmtree(target, onexc=None, **_kwargs):
        onexc(os.open, str(Path(target) / "nested"), PermissionError(13, "Access is denied"))

    monkeypatch.setattr(shutil, "rmtree", fd_walking_rmtree)
    error = box_teardown.remove_tree_longpath(box_dir)
    assert "nested" in error


def test_the_hook_leaves_the_directories_it_touched_traversable(tmp_path, monkeypatch):
    """Clearing the read-only bit must not cost read and execute.

    `chmod(S_IWRITE)` is the Windows spelling of "stop refusing this delete", but the
    constant is `0o200`, and on POSIX assigning it takes a directory's read and execute
    bits away. Every entry under a directory the hook had touched then failed for a
    reason with nothing to do with the lock that got us here — including the assertions
    of the test above it, which could no longer stat what they were checking was gone.
    """
    box_dir = tmp_path / "demo--x-0806"
    (box_dir / "nested").mkdir(parents=True)
    (box_dir / "nested" / "binding.node").write_text("native", encoding="utf-8")

    real_unlink = os.unlink

    def stubborn(path, *args, **kwargs):
        if str(path).endswith("binding.node"):
            raise PermissionError(13, "Access is denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", stubborn)
    box_teardown.remove_tree_longpath(box_dir)
    assert os.listdir(box_dir / "nested") == ["binding.node"]


def test_force_remove_asks_for_an_eviction_only_after_a_delete_has_failed(tmp_path):
    """The cost is the reason: enumerating every process's loaded modules takes a second
    or two, and a reap that succeeded — which is nearly all of them — must not pay it."""
    box_dir = tmp_path / "demo--x-0806"
    box_dir.mkdir()
    asked: list[Path] = []

    def fake_run(cmd, **kwargs):
        asked.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    error, notes = box_teardown.force_remove_box(box_dir, run=fake_run)
    assert (error, notes, asked) == ("", [], [])
    assert not box_dir.exists()


def test_force_remove_kills_the_holder_and_retries_the_delete(tmp_path, monkeypatch):
    """The whole point, in one call: the first delete is denied, what holds the file is
    killed, and the *retry* is what frees the box. Without the retry the eviction would
    be a note about a box that is still there."""
    box_dir = tmp_path / "demo--x-0806"
    (box_dir / "nested").mkdir(parents=True)
    (box_dir / "nested" / "binding.node").write_text("native", encoding="utf-8")

    released = {"now": False}
    real_unlink = os.unlink

    def stubborn(path, *args, **kwargs):
        if str(path).endswith("binding.node") and not released["now"]:
            raise PermissionError(13, "Access is denied")
        return real_unlink(path, *args, **kwargs)

    def fake_evict(path, run=subprocess.run):
        released["now"] = True
        return ["node (4242)"]

    slept: list[float] = []
    monkeypatch.setattr(os, "unlink", stubborn)
    monkeypatch.setattr(box_teardown, "evict_box_holders", fake_evict)
    error, notes = box_teardown.force_remove_box(box_dir, sleep=slept.append)
    assert error == ""
    assert notes == ["killed 1 process(es) still running out of the box: node (4242)"]
    assert not box_dir.exists()
    # Windows releases the image section when the killed process is reaped, not when
    # taskkill returns, so the pause between the kill and the retry is load-bearing.
    assert slept == [box_teardown.HOLDER_RELEASE_SECONDS]


def test_an_access_denied_delete_is_a_filesystem_failure_not_a_dirty_refusal(tmp_path):
    """The widened predicate, at its own level. `Access is denied` is git's report of a
    Win32 delete that failed, which the fallback exists for; the dirty-tree refusal is
    git declining to destroy work, which it must never finish.

    The box carries a live `.git` on purpose: without it the husk clause answers yes to
    everything and the assertion below would hold against a predicate that never learned
    the new spellings at all.
    """
    box_dir = tmp_path / "ws" / ".worktrees" / "demo--x-0806"
    box_dir.mkdir(parents=True)
    (box_dir / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert box_teardown.fallback_applies(box_dir, "failed to delete: Access is denied")
    assert box_teardown.fallback_applies(box_dir, "failed to unlink: Permission denied")
    assert not box_teardown.fallback_applies(
        box_dir, "fatal: contains modified or untracked files, use --force to delete it"
    )


def test_a_husk_is_reapable_however_the_removal_worded_its_failure(tmp_path):
    """A directory whose `.git` is gone is one a previous removal died partway through,
    and no `git worktree remove` can ever succeed on it again — so the fallback is the
    only thing that can clear it, whatever git said this time."""
    husk = tmp_path / "ws" / ".worktrees" / "demo--x-0806"
    husk.mkdir(parents=True)
    assert box_teardown.fallback_applies(husk, "fatal: 'demo--x-0806' is not a working tree")
