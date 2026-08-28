"""Tests for `scripts/reclaim.py`.

The script's side effects are deleting files and stopping containers, so the two things
worth guarding are the **order** it does them in and the **gate** that stops it doing
them at all. Both were real failures before they were rules:

  - reconcile run while the disk is under its own floor destroys boxes whose PR is still
    open (11 -> 2 on 2026-08-20), so `reconcile_is_safe` is asserted at, above and below
    the floor, and on an unreadable volume;
  - dry-run is the default, so a mis-click cannot delete anything. Every destructive
    path is asserted to be a no-op without `--yes`, because the cost of that assertion
    being wrong is somebody's `%TEMP%` mid-build.

The `docker stop`-not-`down` choice is pinned for the same reason it is pinned in
`test_docker_maint.py`: this runs from a one-click task, and `down` discards anything not
on a named volume.

Two more properties joined that list on 2026-08-27, after a run that stopped every
container, printed an error from its reconcile child and left the machine with its stacks
down and a zero-byte "passed" artifact:

  - **what it stopped, it restarts** -- including out of a `finally`, so a crash or a
    Ctrl-C mid-run still leaves the machine as it was found. `--leave-stopped` is the
    only way past it;
  - **a failing step is the exit code.** `run_reconcile` discarded the child's, which is
    what let `log-wrap.py` empty the artifact over a visibly failed run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import load_script

reclaim = load_script("scripts/reclaim.py")


# --- the reconcile ordering gate -----------------------------------------------


def test_a_roomy_disk_lets_reconcile_run():
    safe, why = reclaim.reconcile_is_safe(45.0, floor_gb=20.0)
    assert safe
    assert "45.0 GB free" in why


def test_a_disk_under_the_floor_blocks_reconcile():
    safe, why = reclaim.reconcile_is_safe(12.0, floor_gb=20.0)
    assert not safe
    assert "12.0 GB free" in why
    assert "still open" in why


def test_exactly_at_the_floor_blocks_reconcile():
    """`worktree.under_pressure` treats the floor as inclusive; this must agree with it,
    or the one disk level that matters most is the one they disagree about."""
    safe, _ = reclaim.reconcile_is_safe(20.0, floor_gb=20.0)
    assert not safe


def test_an_unreadable_volume_blocks_reconcile():
    safe, why = reclaim.reconcile_is_safe(-1.0, floor_gb=20.0)
    assert not safe
    assert "could not be read" in why


def test_the_default_floor_matches_the_one_worktree_reaps_at():
    """The two constants live in different files, so assert the equality rather than
    trusting the comment that says they agree."""
    worktree = load_script("scripts/worktree.py")
    assert reclaim.DEFAULT_RECONCILE_FLOOR_GB == worktree.DEFAULT_MIN_FREE_GB


# --- the sweep -----------------------------------------------------------------


def test_sweep_targets_are_all_under_the_given_temp(tmp_path):
    targets = reclaim.sweep_targets(tmp_path, "someone")
    assert targets
    for target in targets:
        assert tmp_path in target.path.parents
        assert target.why, f"{target.label} must say why it is disposable"


def test_the_pytest_tree_is_named_for_the_current_user(tmp_path):
    targets = reclaim.sweep_targets(tmp_path, "alex")
    assert any(t.path.name == "pytest-of-alex" for t in targets)


def test_dir_size_sums_a_tree(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.bin").write_bytes(b"x" * 100)
    (tmp_path / "two.bin").write_bytes(b"y" * 50)
    assert reclaim.dir_size(tmp_path) == 150


def test_dir_size_of_a_missing_tree_is_zero(tmp_path):
    assert reclaim.dir_size(tmp_path / "nope") == 0


def test_stale_files_respects_the_age_cutoff(tmp_path):
    old = tmp_path / "old.log"
    new = tmp_path / "new.log"
    old.write_bytes(b"o")
    new.write_bytes(b"n")
    import os

    os.utime(old, (0, 0))
    stale = reclaim.stale_files(tmp_path, min_age_days=3.0)
    assert stale == [old]


def test_stale_files_never_descends(tmp_path):
    """A recursive age sweep of %TEMP% would half-empty a cache tree whose owner is
    still running, which is worse than leaving it full."""
    nested = tmp_path / "somecache"
    nested.mkdir()
    buried = nested / "old.bin"
    buried.write_bytes(b"x")
    import os

    os.utime(buried, (0, 0))
    assert reclaim.stale_files(tmp_path, min_age_days=1.0) == []


def test_sweep_without_yes_deletes_nothing(tmp_path, capsys):
    diag = tmp_path / "DiagOutputDir"
    diag.mkdir()
    victim = diag / "trace.etl"
    victim.write_bytes(b"z" * 2048)
    freed = reclaim.sweep(reclaim.sweep_targets(tmp_path, "u"), tmp_path, 3.0, apply=False)
    assert victim.exists(), "dry run must not delete"
    assert freed == 2048, "but it must still report what it would have freed"


def test_sweep_with_yes_deletes(tmp_path):
    diag = tmp_path / "DiagOutputDir"
    diag.mkdir()
    victim = diag / "trace.etl"
    victim.write_bytes(b"z" * 2048)
    reclaim.sweep(reclaim.sweep_targets(tmp_path, "u"), tmp_path, 3.0, apply=True)
    assert not victim.exists()


def test_sweep_honours_a_targets_own_age_gate(tmp_path):
    """`SweepTarget.min_age_days` was declared and never read, so a caller that set it got
    a full delete and no way to notice. Nothing sets it yet -- which is exactly when the
    field is worth pinning, because the first caller to set it will trust it."""
    import os

    tree = tmp_path / "gated"
    tree.mkdir()
    old, new = tree / "old.bin", tree / "new.bin"
    old.write_bytes(b"x" * 400)
    new.write_bytes(b"y" * 100)
    os.utime(old, (0, 0))
    target = reclaim.SweepTarget("gated", tree, "why", min_age_days=1.0)
    freed = reclaim.sweep([target], tmp_path, 3.0, apply=True)
    assert not old.exists()
    assert new.exists(), "a file inside the age gate must survive"
    assert freed == 400, "and must not be counted as freed"


def test_an_age_gate_of_zero_means_no_gate_at_all(tmp_path):
    """Regression: spelled as `mtime < now` it is a race against Windows's file-timestamp
    granularity, and a file written milliseconds earlier survived a delete-everything
    target. It failed `test_sweep_with_yes_deletes` -- the existing test -- which is the
    only reason anyone saw it."""
    tree = tmp_path / "DiagOutputDir"
    tree.mkdir()
    victim = tree / "written-just-now.etl"
    victim.write_bytes(b"z" * 64)
    reclaim.sweep(reclaim.sweep_targets(tmp_path, "u"), tmp_path, 3.0, apply=True)
    assert not victim.exists()


def test_an_applied_sweep_counts_what_went_not_what_was_there(tmp_path):
    """The figure under `--yes` is re-measured. Reporting the optimistic size would put GB
    in the total that are still on the volume, under the same banner as a `free disk`
    delta that disagrees with it."""
    diag = tmp_path / "DiagOutputDir"
    diag.mkdir()
    (diag / "a.etl").write_bytes(b"z" * 1024)
    freed = reclaim.sweep(reclaim.sweep_targets(tmp_path, "u"), tmp_path, 3.0, apply=True)
    assert freed == 1024
    assert reclaim.dir_size(diag) == 0


# --- superseded versions: the half no reboot returns ---------------------------


def _aged(path: Path, days: float) -> Path:
    import os

    stamp = 1_000_000_000.0
    os.utime(path, (stamp - days * 86400, stamp - days * 86400))
    return path


NOW = 1_000_000_000.0


def test_cache_targets_sit_under_the_profile_and_say_why(tmp_path):
    targets = reclaim.cache_targets(tmp_path)
    assert targets
    for target in targets:
        assert tmp_path in target.path.parents
        assert target.why


def test_all_but_live_keeps_only_what_the_pointer_names(tmp_path):
    for name in ("v1", "v2", "live"):
        (tmp_path / name).mkdir()
    dead = reclaim.all_but_live(tmp_path, tmp_path / "live")
    assert sorted(p.name for p in dead) == ["v1", "v2"]


def test_a_pointer_that_cannot_be_resolved_deletes_nothing(tmp_path):
    """Fails closed. An unresolvable `current` means the install is mid-update or broken,
    which is the worst possible moment to delete every sibling it has."""
    (tmp_path / "v1").mkdir()
    assert reclaim.all_but_live(tmp_path, tmp_path / "current") == []


def test_a_pointer_outside_the_root_deletes_nothing(tmp_path):
    """A keeper that is not one of the candidates means the layout is not what this rule
    was written for, and `[p for p in candidates if p != keeper]` would be all of them."""
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / "v1").mkdir()
    (tmp_path / "elsewhere").mkdir()
    assert reclaim.all_but_live(tmp_path / "releases", tmp_path / "elsewhere") == []


def test_all_but_newest_keeps_the_newest_and_gates_on_age(tmp_path):
    fresh = _aged(_mkdir(tmp_path / "2.1.247"), days=0.5)
    recent = _aged(_mkdir(tmp_path / "2.1.246"), days=1.0)
    old = _aged(_mkdir(tmp_path / "2.1.237"), days=30.0)
    dead = reclaim.all_but_newest(tmp_path, keep=1, min_age_days=3.0, now=NOW)
    assert dead == [old]
    assert fresh.exists() and recent.exists()


def test_all_but_newest_can_keep_more_than_one(tmp_path):
    for days in (1.0, 10.0, 20.0, 30.0):
        _aged(_mkdir(tmp_path / f"v{days:g}"), days=days)
    dead = reclaim.all_but_newest(tmp_path, keep=2, min_age_days=3.0, now=NOW)
    assert sorted(p.name for p in dead) == ["v20", "v30"]


def test_superseded_revisions_keeps_the_newest_of_each_product(tmp_path):
    """Grouping is the whole rule. The newest *directory* under ms-playwright was a
    firefox build, so a plain keep-newest would have deleted the chromium every checkout
    runs."""
    for name in ("chromium-1208", "chromium-1223", "chromium-1228"):
        _aged(_mkdir(tmp_path / name), days=30.0)
    for name in ("firefox-1509", "chromium_headless_shell-1228"):
        _aged(_mkdir(tmp_path / name), days=30.0)
    dead = sorted(p.name for p in reclaim.superseded_revisions(tmp_path, 3.0, NOW))
    assert dead == ["chromium-1208", "chromium-1223"]


def test_a_directory_whose_name_does_not_parse_is_left_alone(tmp_path):
    """`.links` is playwright's own registry. Anything unparsed is a guess, and this rule
    does not guess."""
    _aged(_mkdir(tmp_path / ".links"), days=90.0)
    _aged(_mkdir(tmp_path / "hand-made"), days=90.0)
    _aged(_mkdir(tmp_path / "chromium-1"), days=90.0)
    _aged(_mkdir(tmp_path / "chromium-2"), days=90.0)
    dead = [p.name for p in reclaim.superseded_revisions(tmp_path, 3.0, NOW)]
    assert dead == ["chromium-1"]


def test_orphaned_installs_are_the_dot_prefixed_ones_only(tmp_path):
    _aged(_mkdir(tmp_path / ".7ad0a680-ecfc-4e47-af2f-9e73ecba9493"), days=90.0)
    installed = _aged(_mkdir(tmp_path / "ms-python.python-2026.1.0"), days=90.0)
    dead = reclaim.orphaned_installs(tmp_path, 3.0, NOW)
    assert [p.name for p in dead] == [".7ad0a680-ecfc-4e47-af2f-9e73ecba9493"]
    assert installed.exists()


def test_a_fresh_orphan_is_left_for_the_install_that_may_still_own_it(tmp_path):
    _aged(_mkdir(tmp_path / ".in-flight"), days=0.1)
    assert reclaim.orphaned_installs(tmp_path, 3.0, NOW) == []


def test_superseded_trees_needs_no_filesystem_and_still_explains_itself(tmp_path):
    """Pure: an empty profile yields every group, each empty, each with its reason. That
    is what lets the set be asserted at all -- the rules underneath it are what touch
    disk."""
    groups = reclaim.superseded_trees(tmp_path, min_age_days=3.0, now=NOW)
    assert len(groups) == 5
    for group in groups:
        assert group.paths == ()
        assert group.why, f"{group.label} must say why the older copies are dead"


def test_purge_trees_without_yes_removes_nothing_but_still_reports(tmp_path):
    victim = _mkdir(tmp_path / "v1")
    (victim / "big.bin").write_bytes(b"x" * 2048)
    assert reclaim.purge_trees((victim,), apply=False) == 2048
    assert victim.exists()


def test_purge_trees_with_yes_removes_the_whole_directory(tmp_path):
    victim = _mkdir(tmp_path / "v1")
    (victim / "nested").mkdir()
    (victim / "nested" / "big.bin").write_bytes(b"x" * 2048)
    assert reclaim.purge_trees((victim,), apply=True) == 2048
    assert not victim.exists()


def test_purge_trees_counts_only_what_actually_went(tmp_path, monkeypatch):
    """A locked file inside a superseded build must neither abort the groups after it nor
    be counted as freed. `rmtree(ignore_errors=True)` covers the first; the re-measure is
    the only thing covering the second."""
    victim = _mkdir(tmp_path / "v1")
    (victim / "stuck.bin").write_bytes(b"x" * 4096)
    monkeypatch.setattr(reclaim.shutil, "rmtree", lambda *a, **kw: None)
    assert reclaim.purge_trees((victim,), apply=True) == 0


def test_sweep_versions_says_so_when_nothing_is_superseded(tmp_path, capsys):
    group = reclaim.Disposable("codex releases", (), "why")
    assert reclaim.sweep_versions([group], apply=False) == 0
    assert "nothing superseded" in capsys.readouterr().out


def test_sweep_versions_names_the_group_and_its_reason(tmp_path, capsys):
    victim = _mkdir(tmp_path / "v1")
    (victim / "a.bin").write_bytes(b"x" * 512)
    group = reclaim.Disposable("codex releases", (victim,), "current names the live one")
    reclaim.sweep_versions([group], apply=False)
    out = capsys.readouterr().out
    assert "codex releases" in out
    assert "current names the live one" in out
    assert "1 dir(s)" in out


# --- what it cannot reclaim ----------------------------------------------------


def test_protected_staging_reports_what_is_there_largest_first(tmp_path):
    big = _mkdir(tmp_path / "$GetCurrent")
    (big / "media.esd").write_bytes(b"x" * 4096)
    small = _mkdir(tmp_path / "Windows.old")
    (small / "leftover.bin").write_bytes(b"y" * 16)
    found = reclaim.protected_staging(tmp_path)
    assert [p.name for p, _ in found] == ["$GetCurrent", "Windows.old"]
    assert found[0][1] == 4096


def test_a_drive_with_no_staging_reports_nothing(tmp_path):
    assert reclaim.protected_staging(tmp_path) == []
    assert reclaim.staging_verdict(tmp_path) == []


def test_the_staging_verdict_names_the_one_remedy_that_works(tmp_path):
    """5.67 GB, larger than everything the rest of the run could free put together, and
    the only section whose whole value is admitting the script cannot do it."""
    staged = _mkdir(tmp_path / "$GetCurrent")
    (staged / "media.esd").write_bytes(b"x" * 2048)
    body = "\n".join(reclaim.staging_verdict(tmp_path))
    assert "$GetCurrent" in body
    assert "cleanmgr" in body
    assert "not this script's to delete" in body


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- containers ----------------------------------------------------------------


class _Proc:
    """The bit of `CompletedProcess` `docker()` reads."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _docker_spy(monkeypatch, result=None, raises=None):
    """Record every docker argv, answering with `result` or raising `raises`."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if raises is not None:
            raise raises
        return result if result is not None else _Proc()

    monkeypatch.setattr(subprocess, "run", fake)
    return calls


def test_stop_stacks_uses_stop_and_never_down(monkeypatch):
    calls = _docker_spy(monkeypatch)
    reclaim.stop_stacks(["a", "b"], apply=True)
    assert calls == [["docker", "stop", "a", "b"]]
    assert not any("down" in c for c in calls[0])


def test_stop_stacks_without_yes_runs_nothing(monkeypatch):
    calls = _docker_spy(monkeypatch)
    reclaim.stop_stacks(["a"], apply=False)
    assert calls == []


def test_stop_stacks_hands_back_what_has_to_be_restarted(monkeypatch):
    _docker_spy(monkeypatch)
    stopped, failed = reclaim.stop_stacks(["a", "b"], apply=True)
    assert stopped == ["a", "b"]
    assert failed is None


def test_a_failed_stop_still_names_everything_that_was_up(monkeypatch, capsys):
    """`docker stop a b c` is not atomic: an error usually means some prefix went down,
    so the pessimistic list is the safe one -- `docker start` on a container that never
    stopped is a no-op, while forgetting one leaves it down for good."""
    _docker_spy(monkeypatch, result=_Proc(1, stderr="Error response from daemon: nope\n"))
    stopped, failed = reclaim.stop_stacks(["a", "b"], apply=True)
    assert stopped == ["a", "b"]
    assert failed == "docker stop: Error response from daemon: nope"
    assert "[warn]" in capsys.readouterr().out


def test_a_wedged_engine_is_a_line_not_a_traceback(monkeypatch):
    """The failure that left the machine with its stacks down: `TimeoutExpired` came out
    of `docker stop` and killed the run before anything could put them back."""
    _docker_spy(monkeypatch, raises=subprocess.TimeoutExpired(cmd="docker", timeout=300))
    ok, detail = reclaim.docker(["stop", "a"], timeout=300)
    assert ok is False
    assert "did not return within 300s" in detail


def test_docker_that_is_not_installed_is_also_a_line(monkeypatch):
    _docker_spy(monkeypatch, raises=FileNotFoundError())
    ok, detail = reclaim.docker(["start", "a"], timeout=30)
    assert (ok, detail) == (False, "docker is not on PATH")


def test_restart_stacks_starts_exactly_what_was_stopped(monkeypatch):
    calls = _docker_spy(monkeypatch)
    assert reclaim.restart_stacks(["a", "b"], apply=True) is None
    assert calls == [["docker", "start", "a", "b"]]


def test_restart_stacks_without_yes_runs_nothing(monkeypatch):
    calls = _docker_spy(monkeypatch)
    reclaim.restart_stacks(["a"], apply=False)
    assert calls == []


def test_an_engine_that_went_away_names_the_script_that_can_fix_it(monkeypatch, capsys):
    _docker_spy(monkeypatch, result=_Proc(1, stderr="failed to connect to the docker API\n"))
    failed = reclaim.restart_stacks(["a"], apply=True)
    assert failed == "docker start: failed to connect to the docker API"
    assert "restart-engine" in capsys.readouterr().out


def test_stop_stacks_with_nothing_up_is_quiet(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest_fail())
    reclaim.stop_stacks([], apply=True)
    assert "nothing to stop" in capsys.readouterr().out


def pytest_fail():
    raise AssertionError("must not run docker with no containers")


# --- the memory report ---------------------------------------------------------


def test_tasklist_rows_aggregate_by_image():
    rows = [
        ["chrome.exe", "1", "Console", "1", "100,000 K"],
        ["chrome.exe", "2", "Console", "1", "50,000 K"],
        ["code.exe", "3", "Console", "1", "200,000 K"],
    ]
    assert reclaim.aggregate_tasklist(rows) == [
        ("code.exe", 1, 200000),
        ("chrome.exe", 2, 150000),
    ]


def test_tasklist_rows_that_do_not_parse_are_skipped():
    rows = [
        ["good.exe", "1", "Console", "1", "10 K"],
        ["bad.exe", "2", "Console", "1", "N/A"],
        ["short.exe", "3"],
    ]
    assert reclaim.aggregate_tasklist(rows) == [("good.exe", 1, 10)]


def test_a_healthy_commit_says_so_and_names_nobody(monkeypatch):
    monkeypatch.setattr(reclaim, "top_memory_holders", lambda *a, **kw: [("x.exe", 1, 1)])
    lines = reclaim.memory_verdict(reclaim.Snapshot(100.0, 8.0, 10.0, 32.0))
    assert len(lines) == 1
    assert "healthy" in lines[0]


def test_a_near_limit_commit_names_the_holders_and_admits_it_cannot_help(monkeypatch):
    """The reclaim frees disk and CPU, never committed memory. Saying so is the whole
    value of the section -- a silent success would read as 'this is fixed now'."""
    monkeypatch.setattr(
        reclaim, "top_memory_holders", lambda *a, **kw: [("chrome.exe", 37, 2 * 1024 * 1024)]
    )
    lines = reclaim.memory_verdict(reclaim.Snapshot(100.0, 0.3, 33.2, 34.5))
    body = "\n".join(lines)
    assert "near the limit" in body
    assert "does not free committed memory" in body
    assert "chrome.exe" in body and "37 processes" in body


def test_the_windows_api_is_reached_without_a_bare_attribute_access():
    """`ctypes.windll` spelled directly cannot be green on both platforms at once: mypy
    resolves it against the platform it runs on, so it is an `[attr-defined]` error on the
    Linux CI runner and the `type: ignore` that fixes it there is an unused-ignore error
    on the Windows desktop. The obvious workaround, `getattr(ctypes, "windll")`, is worse
    than useless -- ruff's B009 autofix rewrites it back at commit time, which is exactly
    how this reddened the gate once. Assert the surviving spelling, not the intent."""
    source = Path(reclaim.__file__).read_text(encoding="utf-8")
    assert 'sys.modules["ctypes"].windll' in source
    assert "getattr(ctypes" not in source, "ruff B009 will rewrite this at commit time"


def test_snapshot_reads_this_machine():
    """The ctypes call is stubbed everywhere else in this file, so without this nothing
    would notice it had stopped working."""
    snap = reclaim.snapshot()
    assert snap.free_gb > 0
    if snap.limit_gb > 0:  # a platform without GlobalMemoryStatusEx reports -1.0
        assert snap.committed_gb > 0
        assert snap.committed_gb <= snap.limit_gb


def test_an_unreadable_memory_status_reports_that(monkeypatch):
    lines = reclaim.memory_verdict(reclaim.Snapshot(10.0, -1.0, -1.0, -1.0))
    assert lines == ["  memory: could not be read"]


# --- what the run reports ------------------------------------------------------


def _isolate(monkeypatch, tmp_path, free_gb):
    """Point `main` at a scratch %TEMP% and stub everything that touches the machine.

    `home` is in that list and the reason is sharper than isolation: without it a
    `main(["--yes"])` test deletes the developer's own superseded playwright browsers and
    VS Code caches while the suite runs, and passes.
    """
    monkeypatch.setattr(reclaim, "tempdir", lambda: str(tmp_path))
    monkeypatch.setattr(reclaim, "home", lambda: tmp_path)
    monkeypatch.setattr(reclaim, "staging_verdict", lambda *a, **kw: [])
    monkeypatch.setattr(reclaim, "username", lambda: "u")
    monkeypatch.setattr(reclaim, "running_container_names", lambda: [])
    monkeypatch.setattr(reclaim, "top_memory_holders", lambda *a, **kw: [])
    monkeypatch.setattr(reclaim, "run_reconcile", lambda *a, **kw: None)
    monkeypatch.setattr(
        reclaim, "snapshot", lambda *a, **kw: reclaim.Snapshot(free_gb, 8.0, 10.0, 32.0)
    )


def test_a_dry_run_never_reports_a_disk_delta(monkeypatch, tmp_path, capsys):
    """Nothing was deleted, so any movement between the two readings belongs to some
    other process on the machine. Printing it as `before -> after` under this script's
    own "Done" banner claims a result it did not produce -- which is exactly how the
    first run read, on a machine where a build happened to finish mid-run."""
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    assert reclaim.main([]) == 0
    out = capsys.readouterr().out
    assert "->" not in out
    assert "unchanged" in out
    assert "would free" in out
    assert "Nothing was changed" in out


def test_an_applied_run_does_report_the_delta(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    assert reclaim.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "24.0 -> 24.0 GB" in out
    assert "Nothing was changed" not in out


def test_keep_stacks_says_what_it_is_giving_up(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    monkeypatch.setattr(reclaim, "running_container_names", lambda: pytest_fail())
    reclaim.main(["--keep-stacks"])
    assert "bind-mount spin stays" in capsys.readouterr().out


def test_a_run_reports_the_superseded_versions_section(monkeypatch, tmp_path, capsys):
    """The 2026-08-27 gap, end to end: the machine was 21 GB from full with every %TEMP%
    tree already clear, and a run that only reported those found nothing to say."""
    _isolate(monkeypatch, tmp_path, free_gb=21.0)
    releases = tmp_path / ".codex" / "packages" / "standalone" / "releases"
    _mkdir(releases / "0.149.0")
    (releases / "0.149.0" / "codex.exe").write_bytes(b"x" * 4096)
    _mkdir(releases / "0.150.1")
    (tmp_path / ".codex" / "packages" / "standalone" / "current").mkdir()
    monkeypatch.setattr(
        reclaim,
        "superseded_trees",
        lambda *a, **kw: [
            reclaim.Disposable("superseded codex releases", (releases / "0.149.0",), "why")
        ],
    )
    assert reclaim.main([]) == 0
    out = capsys.readouterr().out
    assert "superseded tool versions" in out
    assert "no reboot returns" in out
    assert "superseded codex releases" in out
    assert (releases / "0.149.0").exists(), "dry run must not delete a version directory"


def test_the_run_reaches_the_profile_only_through_the_home_seam(monkeypatch, tmp_path, capsys):
    """Reversion check for the stub above: with `home` un-stubbed this run would sweep the
    developer's own profile. Assert `main` asks for it rather than calling `Path.home`."""
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    asked = []
    monkeypatch.setattr(reclaim, "home", lambda: asked.append(1) or tmp_path)
    reclaim.main([])
    assert asked, "main must resolve the profile through home()"


# --- leaving the machine as it was found ---------------------------------------


def _with_stacks(monkeypatch, tmp_path, names=("api", "db")):
    """An isolated run whose machine has `names` up, recording every restart."""
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    monkeypatch.setattr(reclaim, "running_container_names", lambda: list(names))
    monkeypatch.setattr(reclaim, "stop_stacks", lambda n, apply: (list(n), None))
    restarted = []
    monkeypatch.setattr(
        reclaim, "restart_stacks", lambda n, apply: restarted.append(list(n)) or None
    )
    return restarted


def test_restore_puts_back_what_was_stopped(monkeypatch):
    """`restore` is the one decision the happy path and the `finally` share. It is tested
    directly as well as through `main` because a second copy of the `--leave-stopped`
    test is exactly how the two paths would come to disagree."""
    seen = []
    monkeypatch.setattr(reclaim, "restart_stacks", lambda n, apply: seen.append((list(n), apply)))
    assert reclaim.restore(["a"], apply=True, leave_stopped=False) is None
    assert seen == [(["a"], True)]


def test_restore_declines_when_the_caller_wants_the_cpu(monkeypatch, capsys):
    monkeypatch.setattr(reclaim, "restart_stacks", lambda n, apply: pytest_fail())
    assert reclaim.restore(["a"], apply=True, leave_stopped=True) is None
    assert "they stay down" in capsys.readouterr().out


def test_restore_with_nothing_stopped_says_so(monkeypatch, capsys):
    monkeypatch.setattr(reclaim, "restart_stacks", lambda n, apply: pytest_fail())
    assert reclaim.restore([], apply=True, leave_stopped=False) is None
    assert "nothing to put back" in capsys.readouterr().out


def test_restore_hands_back_the_failure_line(monkeypatch):
    monkeypatch.setattr(reclaim, "restart_stacks", lambda n, apply: "docker start: nope")
    assert reclaim.restore(["a"], apply=True, leave_stopped=False) == "docker start: nope"


def test_a_run_puts_back_every_container_it_stopped(monkeypatch, tmp_path, capsys):
    """The whole point: this is the one-click answer to a slow machine, not a decision to
    end the working day. It left every stack down until 2026-08-27."""
    restarted = _with_stacks(monkeypatch, tmp_path)
    assert reclaim.main(["--yes"]) == 0
    assert restarted == [["api", "db"]]
    assert "putting the containers back" in capsys.readouterr().out


def test_a_crash_mid_run_still_puts_them_back(monkeypatch, tmp_path, capsys):
    """Reversion check for the `finally`. Restoring only on the happy path is the same
    bug in a smaller window: the run that reported an error is exactly the run that left
    the stacks down, because it never reached the end."""
    restarted = _with_stacks(monkeypatch, tmp_path)
    monkeypatch.setattr(reclaim, "run_reconcile", lambda *a, **kw: 1 / 0)
    try:
        reclaim.main(["--yes"])
    except ZeroDivisionError:
        pass
    assert restarted == [["api", "db"]]
    assert "interrupted" in capsys.readouterr().out


def test_a_ctrl_c_mid_run_still_puts_them_back(monkeypatch, tmp_path):
    """`KeyboardInterrupt` is not an `Exception`, so an `except Exception` here would
    pass every test above and still strand the machine on the one interruption a user
    actually performs."""
    restarted = _with_stacks(monkeypatch, tmp_path)

    def interrupt(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(reclaim, "run_reconcile", interrupt)
    try:
        reclaim.main(["--yes"])
    except KeyboardInterrupt:
        pass
    assert restarted == [["api", "db"]]


def test_leave_stopped_keeps_the_cpu_back(monkeypatch, tmp_path, capsys):
    restarted = _with_stacks(monkeypatch, tmp_path)
    assert reclaim.main(["--yes", "--leave-stopped"]) == 0
    assert restarted == []
    assert "they stay down" in capsys.readouterr().out


def test_keep_stacks_has_nothing_to_put_back(monkeypatch, tmp_path):
    """It never stopped anything, so the restore must not start something the user had
    deliberately left down."""
    restarted = _with_stacks(monkeypatch, tmp_path)
    assert reclaim.main(["--yes", "--keep-stacks"]) == 0
    assert restarted == []


# --- a failing step is the exit code -------------------------------------------


def test_a_failed_reconcile_is_reported_rather_than_swallowed(monkeypatch, tmp_path, capsys):
    """`log-wrap.py` empties the artifact on a pass, so exiting 0 over a child that
    printed an error left a zero-byte file as the only record of the run."""
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    monkeypatch.setattr(reclaim, "run_reconcile", lambda *a, **kw: "reconcile exited 1")
    assert reclaim.main(["--yes"]) == 1
    assert "[failed] reconcile exited 1" in capsys.readouterr().out


def test_a_failed_stop_is_reported_too(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    monkeypatch.setattr(reclaim, "running_container_names", lambda: ["api"])
    monkeypatch.setattr(reclaim, "stop_stacks", lambda n, apply: (list(n), "docker stop: nope"))
    monkeypatch.setattr(reclaim, "restart_stacks", lambda n, apply: None)
    assert reclaim.main(["--yes"]) == 1
    assert "[failed] docker stop: nope" in capsys.readouterr().out


def test_run_reconcile_reports_a_nonzero_child(monkeypatch, tmp_path):
    script = tmp_path / "scripts" / "worktree.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc(returncode=3))
    assert reclaim.run_reconcile(tmp_path, apply=True) == "reconcile exited 3"


def test_run_reconcile_is_quiet_about_a_clean_child(monkeypatch, tmp_path):
    script = tmp_path / "scripts" / "worktree.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc(returncode=0))
    assert reclaim.run_reconcile(tmp_path, apply=True) is None


def test_a_reconcile_that_never_returns_does_not_kill_the_run(monkeypatch, tmp_path):
    script = tmp_path / "scripts" / "worktree.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    monkeypatch.setattr(
        subprocess, "run", _raiser(subprocess.TimeoutExpired(cmd="worktree", timeout=1800))
    )
    assert reclaim.run_reconcile(tmp_path, apply=True) == "reconcile timed out"


def _raiser(exc):
    def raise_it(*a, **kw):
        raise exc

    return raise_it


def test_a_full_disk_skips_reconcile_rather_than_reaping_open_prs(monkeypatch, tmp_path, capsys):
    """The ordering lesson, asserted end to end: below the floor the run still sweeps,
    and still declines to hand `worktree.py reconcile` a disk it would reclaim boxes on."""
    _isolate(monkeypatch, tmp_path, free_gb=12.0)
    monkeypatch.setattr(reclaim, "run_reconcile", lambda *a, **kw: pytest_fail())
    reclaim.main(["--yes"])
    assert "skipping reconcile" in capsys.readouterr().out
