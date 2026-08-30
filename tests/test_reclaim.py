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
    # Stubbed for the same reason as `home`: the root it asks about is the *real*
    # workspace, so a `main(["--yes"])` test would write a rule into Windows Search.
    monkeypatch.setattr(reclaim, "run_search_scope", lambda *a, **kw: None)
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


# --- the search indexer --------------------------------------------------------


def test_boxes_dir_name_agrees_with_sweep():
    """`workspace_root` recognises a box by the directory it sits in; sweep owns that
    name. A silent rename there would make every box-launched run ask the indexer about
    `.worktrees/` instead of the workspace."""
    sweep = load_script("scripts/sweep.py")
    assert reclaim.BOXES_DIR_NAME == sweep.BOXES_DIR_NAME


def test_workspace_root_is_the_parent_of_a_checkout(tmp_path):
    assert reclaim.workspace_root(tmp_path / "vs_code" / "devkit") == tmp_path / "vs_code"


def test_workspace_root_climbs_out_of_a_box(tmp_path):
    box = tmp_path / "vs_code" / reclaim.BOXES_DIR_NAME / "devkit--something-0829"
    assert reclaim.workspace_root(box) == tmp_path / "vs_code"


def test_the_scope_script_names_the_root_as_a_directory_url():
    script = reclaim.search_scope_script(Path(r"C:\Users\u\Desktop\vs_code"), exclude=False)
    assert r"Run('C:\Users\u\Desktop\vs_code\', $false)" in script
    assert "namespace WS" in script
    assert "__CSHARP__" not in script and "__ROOT__" not in script


def test_the_scope_script_carries_the_exclude_flag_and_escapes_quotes():
    script = reclaim.search_scope_script(Path("C:\\it's\\here\\"), exclude=True)
    assert "Run('C:\\it''s\\here\\', $true)" in script


_REPORT = """\
backlog: 1565851
indexing: file:C:/Users/u/Desktop/vs_code/.worktrees/x/.venv/Lib/site-packages/a.pyc
in-scope: 1
excluded: 1
"""


def test_parse_index_report_reads_every_line():
    report = reclaim.parse_index_report(_REPORT)
    assert report == reclaim.IndexReport(
        backlog=1565851,
        indexing="file:C:/Users/u/Desktop/vs_code/.worktrees/x/.venv/Lib/site-packages/a.pyc",
        in_scope=True,
        excluded=True,
    )


def test_parse_index_report_without_the_exclusion_line_is_not_excluded():
    """Also the idle-indexer shape: an empty `indexing:` must not swallow the line
    after it as the URL, which `\\s*` did on the first cut."""
    report = reclaim.parse_index_report("backlog: 0\nindexing:\nin-scope: 0\n")
    assert report == reclaim.IndexReport(0, "", False, False)


def test_a_dotnet_exception_is_no_report_at_all():
    """A script that compiled and then threw prints the exception, not the lines; that
    has to read as "could not be asked", never as a healthy zero backlog."""
    assert reclaim.parse_index_report("Exception calling Run: Class not registered") is None
    assert reclaim.parse_index_report("") is None


def test_the_verdict_when_the_indexer_cannot_be_asked():
    lines = reclaim.index_verdict(None, Path("W"), apply=False)
    assert lines == ["  windows search could not be asked -- indexer off, or not Windows"]


def test_the_verdict_for_a_root_already_out_of_scope():
    report = reclaim.IndexReport(12, "file:x", in_scope=False, excluded=False)
    lines = reclaim.index_verdict(report, Path("W"), apply=False)
    assert lines[0] == "  backlog 12 item(s) queued"
    assert lines[1] == "  indexing now: file:x"
    assert "outside the crawl scope -- healthy" in lines[2]


def test_the_dry_run_verdict_says_what_it_costs_and_what_yes_does():
    report = reclaim.IndexReport(1_565_851, "", in_scope=True, excluded=False)
    lines = reclaim.index_verdict(report, Path("W"), apply=False)
    assert "backlog 1,565,851" in lines[0]
    assert "IN the crawl scope" in lines[1]
    assert ".venv and node_modules" in lines[1]
    assert "--yes excludes the root" in lines[2]


def test_the_applied_verdict_reports_the_exclusion_that_took():
    report = reclaim.IndexReport(1, "", in_scope=True, excluded=True)
    lines = reclaim.index_verdict(report, Path("W"), apply=True)
    assert "excluded now, children overridden" in lines[1]


def test_the_applied_verdict_admits_an_exclusion_that_did_not_take():
    report = reclaim.IndexReport(1, "", in_scope=True, excluded=False)
    lines = reclaim.index_verdict(report, Path("W"), apply=True)
    assert "did not take" in lines[1]


def test_run_search_scope_writes_a_ps1_and_hands_its_stdout_to_the_parser(monkeypatch, tmp_path):
    monkeypatch.setattr(reclaim, "tempdir", lambda: str(tmp_path))
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["script"] = Path(cmd[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=_REPORT, stderr="")

    monkeypatch.setattr(reclaim.subprocess, "run", fake_run)
    report = reclaim.run_search_scope(Path("C:\\w"), apply=True)
    assert report is not None and report.backlog == 1565851
    assert seen["cmd"][0] == "powershell" and "-File" in seen["cmd"]
    assert "-NonInteractive" in seen["cmd"]
    assert "Run('C:\\w\\', $true)" in seen["script"]
    assert not list(tmp_path.iterdir()), "the throwaway .ps1 must not outlive the call"


def test_run_search_scope_without_powershell_is_none_not_a_traceback(monkeypatch, tmp_path):
    monkeypatch.setattr(reclaim, "tempdir", lambda: str(tmp_path))

    def missing(*a, **kw):
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(reclaim.subprocess, "run", missing)
    assert reclaim.run_search_scope(Path("C:\\w"), apply=False) is None
    assert not list(tmp_path.iterdir())


def test_run_search_scope_treats_a_failing_script_as_unasked(monkeypatch, tmp_path):
    monkeypatch.setattr(reclaim, "tempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        reclaim.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, stdout="backlog: 0\nin-scope: 0", stderr="boom"
        ),
    )
    assert reclaim.run_search_scope(Path("C:\\w"), apply=False) is None


def test_the_run_asks_the_indexer_about_the_workspace_and_prints_the_verdict(
    monkeypatch, tmp_path, capsys
):
    """Reversion check for the whole section: `main` has to ask, about the workspace this
    devkit belongs to, with `apply` matching `--yes` -- and print what it heard."""
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    asked = []
    monkeypatch.setattr(reclaim, "devkit_dir", lambda: tmp_path / "ws" / "devkit")
    monkeypatch.setattr(
        reclaim,
        "run_search_scope",
        lambda root, apply: (
            asked.append((root, apply)) or reclaim.IndexReport(7, "", in_scope=True, excluded=False)
        ),
    )
    assert reclaim.main([]) == 0
    assert asked == [(tmp_path / "ws", False)]
    out = capsys.readouterr().out
    assert "search indexer" in out
    assert "backlog 7" in out
    assert "IN the crawl scope" in out


def test_an_exclusion_that_did_not_take_fails_the_run(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    monkeypatch.setattr(
        reclaim,
        "run_search_scope",
        lambda root, apply: reclaim.IndexReport(7, "", in_scope=True, excluded=False),
    )
    assert reclaim.main(["--yes"]) == 1
    assert "exclusion did not take" in capsys.readouterr().out


# --- the package caches --------------------------------------------------------


def test_the_npm_cache_is_a_target_and_is_age_gated(tmp_path):
    """A box's `npm ci` may be mid-flight in another session, and cacache writes the
    tarball before the index entry that names it -- so today's files stay."""
    npm = [t for t in reclaim.cache_targets(tmp_path) if t.label == "npm cache"]
    assert len(npm) == 1
    assert npm[0].path == tmp_path / "AppData" / "Local" / "npm-cache"
    assert npm[0].min_age_days == reclaim.DEFAULT_MIN_AGE_DAYS
    assert npm[0].why


def test_uv_cache_sits_under_the_profile(tmp_path):
    assert reclaim.uv_cache(tmp_path) == tmp_path / "AppData" / "Local" / "uv" / "cache"


def test_an_empty_uv_cache_runs_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(reclaim, "run_tool", lambda *a, **kw: pytest_fail())
    assert reclaim.clear_uv_cache(tmp_path / "uv" / "cache", apply=True) == 0
    assert "nothing to clear" in capsys.readouterr().out


def test_a_dry_run_reports_the_uv_cache_and_runs_nothing(monkeypatch, tmp_path, capsys):
    cache = tmp_path / "cache"
    _mkdir(cache)
    (cache / "wheel").write_bytes(b"x" * 2048)
    monkeypatch.setattr(reclaim, "run_tool", lambda *a, **kw: pytest_fail())
    assert reclaim.clear_uv_cache(cache, apply=False) == 2048
    assert "hard-linked into venvs" in capsys.readouterr().out
    assert (cache / "wheel").exists()


def test_an_applied_uv_clean_uses_uvs_own_verb_and_the_volume_delta(monkeypatch, tmp_path):
    """The tree's size is not the figure: a hard-linked wheel frees nothing. Assert the
    freed number is the free-space delta, and that the deleter is `uv cache clean`."""
    cache = tmp_path / "cache"
    _mkdir(cache)
    (cache / "wheel").write_bytes(b"x" * 2048)
    ran = []
    free = iter([1_000, 1_512])
    monkeypatch.setattr(reclaim, "run_tool", lambda cmd, timeout: ran.append(cmd) or (True, ""))
    monkeypatch.setattr(reclaim, "free_bytes", lambda p: next(free))
    assert reclaim.clear_uv_cache(cache, apply=True) == 512
    assert ran == [["uv", "cache", "clean"]]


def test_a_volume_that_got_fuller_meanwhile_reads_as_zero_freed(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    _mkdir(cache)
    (cache / "wheel").write_bytes(b"x")
    free = iter([2_000, 1_000])
    monkeypatch.setattr(reclaim, "run_tool", lambda cmd, timeout: (True, ""))
    monkeypatch.setattr(reclaim, "free_bytes", lambda p: next(free))
    assert reclaim.clear_uv_cache(cache, apply=True) == 0


def test_uv_that_is_not_installed_is_a_warning_not_a_crash(monkeypatch, tmp_path, capsys):
    cache = tmp_path / "cache"
    _mkdir(cache)
    (cache / "wheel").write_bytes(b"x")
    monkeypatch.setattr(reclaim, "run_tool", lambda cmd, timeout: (False, "uv is not on PATH"))
    assert reclaim.clear_uv_cache(cache, apply=True) == 0
    assert "[warn] uv is not on PATH" in capsys.readouterr().out


def test_free_bytes_of_an_unreadable_path_is_zero(tmp_path):
    assert reclaim.free_bytes(tmp_path / "nowhere") == 0
    assert reclaim.free_bytes(tmp_path) > 0


def test_run_tool_reports_a_missing_tool_by_name(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(reclaim.subprocess, "run", missing)
    assert reclaim.run_tool(["nope", "x"], timeout=1) == (False, "nope is not on PATH")


def test_run_tool_hands_back_the_first_line_a_failing_tool_said(monkeypatch):
    monkeypatch.setattr(
        reclaim.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, stdout="", stderr="bad\nworse"),
    )
    assert reclaim.run_tool(["t", "v"], timeout=1) == (False, "bad")


def test_the_run_reports_the_uv_cache_under_the_temp_section(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    cache = reclaim.uv_cache(tmp_path)
    _mkdir(cache)
    (cache / "wheel").write_bytes(b"x" * 4096)
    monkeypatch.setattr(reclaim, "run_tool", lambda *a, **kw: pytest_fail())
    assert reclaim.main([]) == 0
    assert "uv cache" in capsys.readouterr().out
    assert (cache / "wheel").exists()
