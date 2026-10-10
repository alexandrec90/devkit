"""Tests for the host half of a box's teardown.

Every one of these is about a box a live process is still running out of, which is the
state that produced the husks of 2026-08-30 — see the module docstring. The eviction is
asserted as *argv* against a fake `run`, the way `test_worktree.py` asserts the reap
steps: killing a process for real to check that we kill processes is not a test anyone
can afford to have go wrong.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import _ctypes
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


def test_an_entry_gone_by_the_retry_is_deleted_not_failed(tmp_path, monkeypatch):
    """A delete Windows refused because the entry was already *delete-pending* -- git's
    own `worktree remove` had marked it and a handle was still open -- finds it gone by
    the time the hook retries. That is the outcome the delete wanted, not a failure:
    reap-stale filed `fix-harness-ledger-1007-4` as "could not be removed" over
    `The system cannot find the file specified` on a tree that was no longer on disk
    (ad9e1a06)."""
    box_dir = tmp_path / "demo--x-0806"

    def pending_rmtree(target, onexc=None, **_kwargs):
        onexc(os.rmdir, str(target), PermissionError(13, "Access is denied"))

    monkeypatch.setattr(shutil, "rmtree", pending_rmtree)
    assert box_teardown.remove_tree_longpath(box_dir) == ""


def test_delete_refused_tells_the_filesystem_from_git():
    """A tree gone after the filesystem refused git is reaped; one gone after git itself
    refused is not, so the two spellings must stay apart."""
    assert box_teardown.delete_refused("error: failed to delete 'C:/x': Permission denied")
    assert not box_teardown.delete_refused("fatal: 'C:/x' contains modified or untracked files")


def test_a_tree_already_gone_is_removed(tmp_path):
    """The root itself missing -- a concurrent reaper, or a delete-pending directory
    that closed between the caller's check and this call -- raises from `rmtree` before
    any hook runs. Nothing is left to delete, so nothing failed."""
    assert box_teardown.remove_tree_longpath(tmp_path / "gone") == ""


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


def _released_after(box_dir: Path, monkeypatch, refusals: int) -> list[float]:
    """Deny the box's `_yaml.pyd` for its first `refusals` deletes, with nothing evictable.

    Each `remove_tree_longpath` asks twice -- `rmtree`, then its `onexc` retry by path.

    fc85393e: reconcile's reap of roguelike's merged box died on that file, held by a
    handle no holder query named, and the file opened exclusively minutes later.
    """
    (box_dir / "yaml").mkdir(parents=True)
    (box_dir / "yaml" / "_yaml.pyd").write_text("native", encoding="utf-8")
    left = {"refusals": refusals}
    real_unlink = os.unlink

    def held(path, *args, **kwargs):
        if str(path).endswith("_yaml.pyd") and left["refusals"] > 0:
            left["refusals"] -= 1
            raise PermissionError(13, "Access is denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", held)
    monkeypatch.setattr(box_teardown, "evict_box_holders", lambda path, run=None: [])
    return []


def test_force_remove_retries_a_held_file_nobody_could_be_evicted_for(tmp_path, monkeypatch):
    box_dir = tmp_path / "roguelike--devkit-upgrade-v0-11-37-1001"
    slept = _released_after(box_dir, monkeypatch, refusals=4)
    error, notes = box_teardown.force_remove_box(box_dir, sleep=slept.append)
    assert (error, notes) == ("", [])
    assert not box_dir.exists()
    assert slept == list(box_teardown.RELEASE_PAUSES[:2])


def test_force_remove_reports_a_hold_that_outlasts_every_pause(tmp_path, monkeypatch):
    box_dir = tmp_path / "roguelike--devkit-upgrade-v0-11-37-1001"
    slept = _released_after(box_dir, monkeypatch, refusals=99)
    error, notes = box_teardown.force_remove_box(box_dir, sleep=slept.append)
    assert "_yaml.pyd" in error and "Access is denied" in error and notes == []
    assert slept == list(box_teardown.RELEASE_PAUSES)


def test_force_remove_waits_on_nothing_only_an_administrator_could_delete(tmp_path, monkeypatch):
    """No pause cures an ACL, and the scheduled reap meets five such trees every run."""
    box_dir = tmp_path / "declarative-finding-sky"
    _released_after(box_dir, monkeypatch, refusals=99)
    monkeypatch.setattr(box_teardown, "unopenable", lambda path: [str(path / ".pytest_cache")])
    error, _notes = box_teardown.force_remove_box(box_dir, sleep=pytest_fail)
    assert "Access is denied" in error


def _linked_from_a_cache(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """`(box, cache file, trash)`: a box whose `.venv` DLL is a hard link of a cache file.

    61d79c1d / e044f8ff: uv links a package's files out of its cache into every `.venv`,
    so data-lake's merged session tree held `pyarrow/arrow.dll` as one of twelve links --
    and social-scraper's long-running `scrape` had mapped the same file through its own.
    Windows refuses to unlink *any* link of a mapped image, and no holder sits under the
    box to evict, so the reap failed every run. The refusal is injected so the contract
    is checked on both CI platforms; the test after this one is the real one.
    """
    cache = tmp_path / "uv-cache" / "arrow.dll"
    cache.parent.mkdir()
    cache.write_text("native", encoding="utf-8")
    box_dir = tmp_path / "social-scraper-connector-1003"
    linked = box_dir / ".venv" / "pyarrow" / "arrow.dll"
    linked.parent.mkdir(parents=True)
    os.link(cache, linked)
    real_unlink = os.unlink

    def mapped_elsewhere(path, *args, **kwargs):
        # POSIX `rmtree` unlinks a bare name under a `dir_fd`, which no other unlink here
        # passes, so that spelling is the box's link too; the retry and the trash go by path.
        in_box = kwargs.get("dir_fd") is not None or box_dir.name in str(path)
        if in_box and str(path).endswith("arrow.dll"):
            raise PermissionError(13, "Access is denied")
        return real_unlink(path, *args, **kwargs)

    trash = tmp_path / "trash"
    monkeypatch.setattr(os, "unlink", mapped_elsewhere)
    monkeypatch.setattr(box_teardown, "evict_box_holders", lambda path, run=None: [])
    monkeypatch.setattr(box_teardown, "trash_dir", lambda: trash)
    return box_dir, cache, trash


def test_a_link_mapped_through_another_path_is_set_aside_after_the_eviction(tmp_path, monkeypatch):
    """The tree's link is moved out of it, which Windows allows of a mapped image; the
    file itself lives on through its other links. Only once the eviction has had its
    turn: the first delete sets nothing aside, so a dev server running out of the box is
    still killed rather than left running a moved file."""
    box_dir, cache, trash = _linked_from_a_cache(tmp_path, monkeypatch)
    slept: list[float] = []
    error, notes = box_teardown.force_remove_box(box_dir, sleep=slept.append)
    assert (error, notes) == ("", [])
    assert not box_dir.exists()
    assert cache.read_text(encoding="utf-8") == "native"
    assert [p.name.endswith("-arrow.dll") for p in trash.iterdir()] == [True]
    assert slept == [box_teardown.RELEASE_PAUSES[0]]


def test_a_file_with_no_other_link_is_never_set_aside(tmp_path, monkeypatch):
    """Its only copy is the one in the box, so a holder of it is under the box too -- the
    eviction's to kill -- and moving it out would leave that holder running."""
    box_dir = tmp_path / "roguelike--devkit-upgrade-v0-11-37-1001"
    slept = _released_after(box_dir, monkeypatch, refusals=99)
    monkeypatch.setattr(box_teardown, "trash_dir", lambda: tmp_path / "trash")
    error, _notes = box_teardown.force_remove_box(box_dir, sleep=slept.append)
    assert "_yaml.pyd" in error
    assert not (tmp_path / "trash").exists()


def test_a_link_that_cannot_be_moved_keeps_its_failure(tmp_path, monkeypatch):
    """A trash on another volume cannot take a rename, and the reap must still say so."""
    box_dir, _cache, _trash = _linked_from_a_cache(tmp_path, monkeypatch)

    def cross_volume(src, dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", cross_volume)
    error, _notes = box_teardown.force_remove_box(box_dir, sleep=lambda _s: None)
    assert "arrow.dll" in error and "Access is denied" in error


def test_the_trash_is_this_users_own_temp_directory(monkeypatch, tmp_path):
    """Per user, so an unelevated reap can always write it, and outside every tree, so
    no husk sweep or `git status` ever counts what is in it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert box_teardown.trash_dir() == tmp_path / "devkit-reap-trash"


def test_the_trash_is_emptied_of_what_nothing_maps_any_more(tmp_path, monkeypatch):
    box_dir, _cache, trash = _linked_from_a_cache(tmp_path, monkeypatch)
    trash.mkdir()
    (trash / "0f-old.dll").write_text("released since", encoding="utf-8")
    box_teardown.force_remove_box(box_dir, sleep=lambda _s: None)
    assert [p.name.endswith("-arrow.dll") for p in trash.iterdir()] == [True]


def test_a_library_another_link_has_loaded_no_longer_keeps_the_box(tmp_path, monkeypatch):
    """The real refusal: this process loads a native library through the cache's link,
    as social-scraper's `scrape` did, and the box's link is the one being deleted.

    Run on both platforms rather than skipped on one: POSIX unlinks a mapped file
    without complaint, so there it holds trivially, and on Windows it is the refusal of
    61d79c1d itself."""
    library = Path(_ctypes.__file__)
    cache = tmp_path / "uv-cache" / library.name
    cache.parent.mkdir()
    shutil.copy(library, cache)
    box_dir = tmp_path / "social-scraper-connector-1003"
    (box_dir / ".venv").mkdir(parents=True)
    os.link(cache, box_dir / ".venv" / library.name)
    monkeypatch.setattr(box_teardown, "evict_box_holders", lambda path, run=None: [])
    monkeypatch.setattr(box_teardown, "trash_dir", lambda: tmp_path / "trash")
    handle = ctypes.CDLL(str(cache))._handle
    try:
        error, _notes = box_teardown.force_remove_box(box_dir, sleep=lambda _s: None)
    finally:
        (getattr(_ctypes, "FreeLibrary", None) or _ctypes.dlclose)(handle)
    assert error == ""
    assert not box_dir.exists()


def test_engine_unreachable_reads_each_platforms_spelling_and_nothing_else():
    assert box_teardown.engine_unreachable(
        "failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; "
        "check if the path is correct and if the daemon is running: "
        "open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified."
    )
    assert box_teardown.engine_unreachable(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    )
    assert not box_teardown.engine_unreachable("Error response from daemon: volume is in use")
    assert not box_teardown.engine_unreachable("")


def test_a_wedged_engines_ping_is_unreachable_but_a_500_elsewhere_is_not():
    """2026-10-08: Docker Desktop answered, and its engine's ping was a 500 for hours."""
    route = "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine"
    said = "request returned 500 Internal Server Error for API route and version {}, check"
    assert box_teardown.engine_unreachable(said.format(f"{route}/_ping"))
    assert not box_teardown.engine_unreachable(said.format(f"{route}/v1.55/containers/x/stop"))


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
    assert box_teardown.fallback_applies(box_dir, "failed to delete 'x': Invalid argument")
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


def test_unopenable_names_each_directory_this_process_may_not_list(tmp_path):
    """carameli, 2026-09-29: an elevated session's pytest left a `.pytest_cache` whose ACL
    grants Administrators alone, so the unelevated reap could not even list it, and failed
    on it every run. The walk names such a directory, without the long-path prefix, and
    only for a refusal: a directory that vanished mid-walk is not one.

    The refusal is injected because neither CI platform can make one portably: Windows
    ignores `chmod`, and a POSIX runner may be root."""
    husk = tmp_path / "declarative-finding-sky"
    husk.mkdir()
    cache = "\\\\?\\C:\\ws\\declarative-finding-sky\\.pytest_cache"

    def walk(top, onerror):
        assert "declarative-finding-sky" in top
        onerror(PermissionError(13, "Access is denied", cache))
        onerror(FileNotFoundError(2, "gone", "C:\\ws\\declarative-finding-sky\\tmp"))
        yield top, [], []

    assert box_teardown.unopenable(husk, walk=walk) == [
        "C:\\ws\\declarative-finding-sky\\.pytest_cache"
    ]
    assert box_teardown.unopenable(husk) == []


BOX = "C:\\ws\\.worktrees\\carameli--up-0929"


def claude_stop(answer: int = 0):
    """A fake `run` recording each `claude stop`, answering `answer`."""
    seen: list[list[str]] = []

    def run(argv, **_kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, answer, "", "")

    return run, seen


def test_vacate_stops_an_idle_background_session_in_the_box_and_waits_for_it():
    """94e52397: the finished fixer's process stood in the box's root, so the reap
    deleted every file and died on the empty directory."""
    run, seen = claude_stop()
    waited: list[float] = []
    sessions = [
        {"kind": "background", "state": "done", "cwd": BOX, "id": "2b4a8c0e", "pid": 1},
        {"kind": "background", "state": "done", "cwd": BOX + "-2", "id": "sibling", "pid": 2},
    ]
    occupants, notes = box_teardown.vacate(Path(BOX), sessions, run, waited.append)
    assert occupants == [] and seen == [["claude", "stop", "2b4a8c0e"]]
    assert notes == ["stopped idle background session 2b4a8c0e still in the box"]
    assert waited == [box_teardown.HOLDER_RELEASE_SECONDS]


def test_vacate_stops_nothing_while_a_session_is_working_or_a_person_is_in_the_box():
    run, seen = claude_stop()
    for occupant in (
        {"kind": "background", "state": "working", "cwd": BOX + "\\app", "id": "b2", "pid": 2},
        {"kind": "interactive", "status": "idle", "cwd": BOX.lower().replace("\\", "/")},
    ):
        idle = {"kind": "background", "state": "done", "cwd": BOX, "id": "a1", "pid": 1}
        occupants, notes = box_teardown.vacate(Path(BOX), [idle, occupant], run, pytest_fail)
        assert len(occupants) == 1 and notes == []
    assert seen == []


def test_a_session_that_would_not_stop_is_an_occupant():
    run, _seen = claude_stop(answer=1)
    session = {"kind": "background", "state": "blocked", "cwd": BOX, "id": "a1", "pid": 1}
    occupants, notes = box_teardown.vacate(Path(BOX), [session], run, pytest_fail)
    assert occupants == ["background session a1 (blocked)"] and notes == []


def test_a_listed_session_with_no_process_neither_holds_the_box_nor_is_stopped():
    """d95eb347: c476bac3 stayed listed with no `pid` after its process went, and
    `claude stop` could not confirm stopping it. Read as an occupant, it would hold its
    box forever; asked to stop, it refuses. It stands in nothing, so it is neither."""
    run, seen = claude_stop(answer=1)
    for state in ("blocked", "working"):
        gone = {"kind": "background", "state": state, "cwd": BOX, "id": "c476bac3"}
        assert box_teardown.vacate(Path(BOX), [gone], run, pytest_fail) == ([], [])
    assert seen == []


def test_an_empty_box_is_vacated_without_a_word():
    run, seen = claude_stop()
    assert box_teardown.vacate(Path(BOX), [], run, pytest_fail) == ([], [])
    assert seen == []


def pytest_fail(_seconds: float) -> None:
    raise AssertionError("waited with nothing stopped")
