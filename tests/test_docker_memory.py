"""`docker_memory.py`: the VM's budget, and what Windows says of a silent VM.

The samples are the real lines read on 2026-10-09, the day Docker's 4 GB VM ran out of
memory and booted eight times while the collectors pass named nothing but the restart.
"""

from __future__ import annotations

import datetime as dt
import os

from support import load_script

memory = load_script("scripts/docker_memory.py")

GIB, MIB = memory.GIB, memory.MIB

# The request row Docker Desktop's backend logged for the 19:32 UTC kill, and its answer.
OOM_REQUEST = (
    "[2026-10-09T19:32:33.837887400Z][com.docker.backend.exe.ipc] (39669d41-13) "
    "0ebafcc3-BackendAPI S<-C f2539b76-init POST /analytics/track/oom-kills"
)
OOM_ANSWER = (
    "[2026-10-09T19:32:33.837887400Z][com.docker.backend.exe.ipc] (39669d41-13) "
    "0ebafcc3-BackendAPI S->C f2539b76-init POST /analytics/track/oom-kills (0s): 1"
)
PING = (
    "[2026-10-09T19:30:02.332735900Z][com.docker.backend.exe.ipc][W] (db525023-17727) "
    'bb08c092-stats C<-S ConnectionClosed GET /ping (0s): Get "http://ipc/ping": context deadline exceeded'
)
KILLED_AT = dt.datetime(2026, 10, 9, 19, 32, 33, tzinfo=dt.timezone.utc).timestamp()


# --- the budget ----------------------------------------------------------------------


def test_an_unlimited_container_is_a_problem_named_by_its_label():
    problems = memory.budget([("ibkr_trader: `app`", 0), ("ibkr_trader: `db`", 768 * MIB)], 4 * GIB)
    assert [p.line.split(" -- ")[0] for p in problems] == ["ibkr_trader: `app` has no memory limit"]


def test_limits_that_fit_under_the_headroom_are_no_problem():
    """The 2026-10-09 fix's numbers: app, db and two 512m collectors in a 3.83 GiB VM."""
    capped = [("a", 1536 * MIB), ("b", 768 * MIB), ("c", 512 * MIB), ("d", 512 * MIB)]
    assert memory.budget(capped, int(3.827 * GIB)) == []


def test_limits_past_the_headroom_are_one_problem_with_the_numbers_apart():
    """The numbers go in `detail`, so the failure line is the same on every pass and
    the ledger files one group for it however the limits move."""
    problems = memory.budget([("a", 3 * GIB), ("b", 768 * MIB)], 4 * GIB)
    assert len(problems) == 1 and isinstance(problems[0], memory.Problem)
    assert "GiB" not in problems[0].line
    assert (
        problems[0].detail
        == "limits total 3.8 GiB; the VM has 4.0 GiB, less 0.5 GiB for the engine itself"
    )


def test_limits_exactly_at_the_headroom_still_fit():
    assert memory.budget([("a", 3 * GIB), ("b", 512 * MIB)], 4 * GIB) == []


def test_the_sum_is_not_judged_while_any_limit_is_missing_or_unknown():
    assert len(memory.budget([("a", 0), ("b", 8 * GIB)], 4 * GIB)) == 1, "only the unlimited one"
    assert memory.budget([("a", None), ("b", 8 * GIB)], 4 * GIB) == []


def test_an_unknown_vm_size_judges_only_the_missing_limits():
    assert memory.budget([("a", 8 * GIB)], None) == []
    assert len(memory.budget([("a", 0)], None)) == 1


def test_nothing_kept_up_is_no_problem():
    assert memory.budget([], 4 * GIB) == []


# --- reading the engine --------------------------------------------------------------


def test_inspect_rows_are_read_as_full_ids_and_byte_limits():
    text = "abc123full\t1610612736\ndef456full\t0\n\nmalformed\n"
    assert memory.parse_limits(text) == {"abc123full": 1536 * MIB, "def456full": 0}


def test_a_short_id_finds_its_full_ids_limit():
    limits = {"abc123full": 0, "def456full": 512 * MIB}
    assert memory.limit_of("def456", limits) == 512 * MIB
    assert memory.limit_of("abc123", limits) == 0
    assert memory.limit_of("zzz", limits) is None


def test_a_zero_or_unreadable_vm_size_is_no_answer():
    """2026-10-09: over a 500 from the API, `docker info` printed `0` and exited 0."""
    assert memory.capacity("4109926400\n") == 4109926400
    assert memory.capacity("0\n") is None
    assert memory.capacity("") is None
    assert memory.capacity("<no value>") is None


# --- a silent VM, read from Windows ----------------------------------------------------


def test_the_oom_kill_request_is_counted_once_and_nothing_else_is():
    text = "\n".join([PING, OOM_REQUEST, OOM_ANSWER])
    assert memory.oom_kills(text, 0.0) == [KILLED_AT]


def test_a_kill_before_the_lookback_is_not_counted():
    assert memory.oom_kills(OOM_REQUEST, KILLED_AT + 1) == []
    assert memory.oom_kills(OOM_REQUEST, KILLED_AT) == [KILLED_AT]


def test_a_row_with_an_unreadable_time_is_skipped():
    assert memory.oom_kills(OOM_REQUEST.replace("2026-10-09T19:32:33", "not-a-time"), 0.0) == []


def test_parse_when_reads_nanoseconds_and_refuses_a_time_with_no_zone():
    assert memory.parse_when("2026-10-09T19:32:33.837887400Z") == KILLED_AT
    assert memory.parse_when("2026-10-09T19:32:33") is None
    assert memory.parse_when("garbage") is None


def test_backend_logs_read_the_live_file_and_its_rotations_written_since(tmp_path):
    (tmp_path / "com.docker.backend.exe.log").write_text("live\n", encoding="utf-8")
    rotated = tmp_path / "com.docker.backend.exe.log.20261009-154935.772"
    rotated.write_text(OOM_REQUEST + "\n", encoding="utf-8")
    old = tmp_path / "com.docker.backend.exe.log.20261001-000000.000"
    old.write_text("old\n", encoding="utf-8")
    os.utime(old, (1000.0, 1000.0))
    (tmp_path / "monitor.log").write_text("other\n", encoding="utf-8")
    text = memory.backend_logs(tmp_path, 2000.0)
    assert "live" in text and "oom-kills" in text
    assert "old" not in text and "other" not in text


def test_no_log_directory_is_no_text(tmp_path):
    assert memory.backend_logs(tmp_path / "absent", 0.0) == ""


def test_the_wslconfig_cap_is_read_from_the_wsl2_section_in_binary_units():
    text = "[wsl2]\n# 16 GB box\nmemory=4GB\nprocessors=4\nswap=2GB\n"
    assert memory.wsl_cap(text) == 4 * GIB
    assert memory.wsl_cap("[wsl2]\nmemory = 4096MB\n") == 4096 * MIB
    assert memory.wsl_cap("[experimental]\nmemory=4GB\n") is None
    assert memory.wsl_cap("[wsl2]\nprocessors=4\n") is None


def test_the_vm_working_set_is_read_whatever_the_locale_separates_thousands_with():
    assert memory.vm_working_set('"vmmem","11480","Services","0","3,937,576 K"\n') == 3937576 * 1024
    assert memory.vm_working_set('"vmmemWSL","1","Services","0","3.937.576 K"\n') == 3937576 * 1024


def test_no_vm_process_is_no_working_set():
    info = "INFO: No tasks are running which match the specified criteria.\n"
    assert memory.vm_working_set(info) is None
    assert memory.vm_working_set('"Code.exe","1","Console","1","200,000 K"\n') is None


def test_a_kill_is_evidence_by_itself():
    said = memory.silent_vm_evidence([KILLED_AT], None, None)
    assert said == "Docker Desktop logged 1 OOM kill(s) inside its VM, the last at 19:32 UTC"


def test_a_vm_near_its_cap_is_evidence_and_one_well_under_it_is_not():
    """3.75 GiB of 4 was the frozen VM on 2026-10-09: under the cap itself, since the
    kernel reserves part of it, which is why the bar is `NEAR_CAP` and not 100%."""
    assert (
        memory.silent_vm_evidence([], int(3.75 * GIB), 4 * GIB)
        == "the VM holds 3.8 GiB of its 4.0 GiB cap"
    )
    assert memory.silent_vm_evidence([], 2 * GIB, 4 * GIB) == ""


def test_an_unknown_cap_or_working_set_says_nothing_of_either():
    assert memory.silent_vm_evidence([], 4 * GIB, None) == ""
    assert memory.silent_vm_evidence([], None, 4 * GIB) == ""


def test_both_signals_are_said_together():
    said = memory.silent_vm_evidence([KILLED_AT], int(3.9 * GIB), 4 * GIB)
    assert "OOM kill" in said and "; the VM holds" in said


def test_the_log_directory_follows_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert memory.log_dir() == tmp_path / "Docker/log/host"
    assert memory.gib(1.5 * GIB) == "1.5 GiB"
