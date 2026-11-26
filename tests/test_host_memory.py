"""`scripts/host_memory.py`: the free memory the fix pass checks before a session."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_send
import host_memory

MEMINFO = "MemTotal:       16303440 kB\nMemFree:          812344 kB\nMemAvailable:    6970112 kB\n"


def test_linux_reads_available_not_free():
    """`MemFree` leaves out the page cache the kernel gives back on demand."""
    assert host_memory.meminfo_mb(MEMINFO) == 6806
    assert host_memory.meminfo_mb("MemTotal: 1 kB\n") is None
    assert host_memory.meminfo_mb("MemAvailable:\n") is None


def test_a_session_needs_its_own_size_above_the_floor():
    floor = host_memory.FLOOR_MB + host_memory.SESSION_MB
    assert host_memory.room_for_session(floor)
    assert not host_memory.room_for_session(floor - 1)


def test_a_caller_may_ask_about_a_lower_floor():
    fix = host_memory.FIX_FLOOR_MB
    assert fix < host_memory.FLOOR_MB
    assert host_memory.room_for_session(fix + host_memory.SESSION_MB, fix)
    assert not host_memory.room_for_session(fix + host_memory.SESSION_MB - 1, fix)
    assert not host_memory.room_for_session(fix + host_memory.SESSION_MB)


MB = 1024 * 1024


def test_windows_room_is_the_lower_of_physical_and_commit():
    """2026-11-25: 1.0 GB physical, 1.4 GB commit, a game running. Commit past its limit
    fails an allocation outright; physical past its limit pages. Either can be lower."""
    assert host_memory.windows_room(1022 * MB, 1459 * MB) == 1022
    assert host_memory.windows_room(6000 * MB, 700 * MB) == 700, "a full commit charge"
    assert host_memory.windows_room(0, 4000 * MB) == 0


def test_a_probe_that_failed_guards_nothing():
    assert host_memory.room_for_session(None)


def test_this_machine_answers_with_a_plausible_figure():
    """The one probe a stub cannot vouch for: the platform call on the machine running it."""
    free = host_memory.available_mb()
    assert free is None or 0 < free < 10_000_000


def test_the_fix_pass_floor_is_the_module_one():
    assert fix_send.MEMORY_FLOOR_MB == host_memory.FIX_FLOOR_MB


def test_a_probe_that_fails_reads_as_no_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(host_memory.sys, "platform", "linux")
    monkeypatch.setattr(host_memory, "MEMINFO", tmp_path / "missing")
    assert host_memory.available_mb() is None
    (tmp_path / "meminfo").write_text("MemAvailable: lots kB\n", encoding="utf-8")
    monkeypatch.setattr(host_memory, "MEMINFO", tmp_path / "meminfo")
    assert host_memory.available_mb() is None
    (tmp_path / "meminfo").write_text(MEMINFO, encoding="utf-8")
    assert host_memory.available_mb() == 6806
