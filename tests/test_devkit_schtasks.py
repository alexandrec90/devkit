"""Tests for the scheduled-task document builder.

The document is the artifact, not the argv, and these tests exist because the failure
it prevents is invisible: a task registered with the wrong power settings looks
completely healthy in `schtasks /Query` and simply does not run. Nothing goes red, no
log is written, and the first symptom is whatever the job was supposed to prevent
happening for a week.

`test_the_settings_block_is_in_schema_order` is the reversion check for the whole
module. Task Scheduler validates `<Settings>` as a sequence rather than a set, so a
plausible-looking reordering is rejected at registration time -- on the installing
machine, which is the one place a unit test cannot reach.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import load_script

schtasks = load_script("scripts/devkit_schtasks.py")

# The order Windows itself accepted when this document shape was registered and read
# back. Not a preference -- a schema sequence.
SETTINGS_ORDER = (
    "MultipleInstancesPolicy",
    "DisallowStartIfOnBatteries",
    "StopIfGoingOnBatteries",
    "AllowHardTerminate",
    "StartWhenAvailable",
    "RunOnlyIfNetworkAvailable",
    "IdleSettings",
    "AllowStartOnDemand",
    "Enabled",
    "Hidden",
    "RunOnlyIfIdle",
    "WakeToRun",
    "ExecutionTimeLimit",
    "Priority",
)


def document(**kwargs) -> str:
    return schtasks.task_xml(
        r"C:\py\pythonw.exe", "script.py --all", schtasks.repeating_trigger(15), **kwargs
    )


# --- the three settings this module exists for -------------------------------


def test_a_job_runs_on_battery():
    """`schtasks.exe` cannot express this at all, which is why the module exists: the
    default skips every fire while the laptop is unplugged."""
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in document()


def test_unplugging_does_not_kill_a_running_job():
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in document()


def test_a_missed_run_is_caught_up():
    """Without this a daily 03:00 job loses the entire day every night the lid is shut,
    and reports nothing at all about the days it skipped."""
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in document()


def settings_block(body: str) -> str:
    """Just the `<Settings>` element.

    Scoped rather than searched whole because `<Enabled>` is a legal child of both
    `<Settings>` and a trigger, and the trigger's copy comes first in the document --
    an order assertion over the whole string compares the wrong element and fails on a
    correct document.
    """
    start = body.index("<Settings>")
    return body[start : body.index("</Settings>", start)]


def test_the_settings_block_is_in_schema_order():
    """Task Scheduler validates the sequence, so a reordering fails at registration --
    on the installing machine, where no test here can catch it."""
    body = settings_block(document())
    found = [name for name in SETTINGS_ORDER if f"<{name}>" in body]
    positions = [body.index(f"<{name}>") for name in found]
    assert found == list(SETTINGS_ORDER)
    assert positions == sorted(positions)


def test_a_job_runs_where_it_was_told_to():
    """A scheduled task's cwd is `system32`, so a job that resolves `logs/` from the cwd
    -- which every runner following the failure-artifact rule does -- writes its report
    into a Windows system directory. `<WorkingDirectory>` is what makes an unattended
    job's artifact land where anyone can read it."""
    assert "<WorkingDirectory>C:\\ws\\devkit</WorkingDirectory>" in document(
        working_dir=r"C:\ws\devkit"
    )


def test_a_job_that_names_no_directory_emits_no_element():
    """Absent, not empty: an empty `<WorkingDirectory>` is not the same as omitting it,
    and the two jobs that predate this parameter pass absolute paths instead."""
    assert "<WorkingDirectory>" not in document()


def test_the_exec_block_is_in_schema_order():
    """`<Exec>` children are a sequence like `<Settings>`. `<WorkingDirectory>` before
    `<Arguments>` is rejected at registration, on the installing machine, where nothing
    in this suite can reach it."""
    body = document(working_dir=r"C:\ws\devkit")
    order = [body.index(f"<{name}>") for name in ("Command", "Arguments", "WorkingDirectory")]
    assert order == sorted(order)


def test_a_working_directory_with_an_ampersand_is_escaped_like_every_other_path():
    assert "C:\\ws\\a &amp; b" in document(working_dir=r"C:\ws\a & b")


def test_a_wedged_run_cannot_suppress_every_later_one_forever():
    """`IgnoreNew` plus the three-day default limit means one hang silently disables the
    job for three days -- the exact failure shape being fixed."""
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in document()
    assert "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>" in document()
    assert "<ExecutionTimeLimit>PT2H</ExecutionTimeLimit>" in document(time_limit="PT2H")


def test_a_sleeping_laptop_is_not_woken():
    """Catching up on the next wake is strictly better than waking at 03:00 to open
    dependency PRs."""
    assert "<WakeToRun>false</WakeToRun>" in document()


# --- triggers ----------------------------------------------------------------


def test_a_repeating_trigger_carries_no_duration():
    """The element is optional and absent means forever; any value present is a
    stopping point, so a plausible `P1D` turns the job off after a day."""
    trigger = schtasks.repeating_trigger(15)
    assert "<Interval>PT15M</Interval>" in trigger
    assert "<Duration>" not in trigger


def test_a_repeating_trigger_starts_in_the_past():
    """A start boundary in the future means the first repetition is not due yet, which
    reads as a task that was installed and never ran."""
    assert "<StartBoundary>2020-01-01T00:00:00</StartBoundary>" in schtasks.repeating_trigger(15)


def test_a_daily_trigger_fires_once_a_day_at_the_time_given():
    trigger = schtasks.daily_trigger("03:00")
    assert "T03:00:00</StartBoundary>" in trigger
    assert "<DaysInterval>1</DaysInterval>" in trigger


# --- the mechanics that fail confusingly -------------------------------------


def test_the_file_is_written_as_utf16(tmp_path):
    """`schtasks /XML` honours the declared encoding, and a UTF-8 file claiming UTF-16
    is rejected with a parse error naming neither."""
    path = schtasks.write_task_file(document(), tmp_path)
    assert path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
    assert path.read_text(encoding="utf-16").startswith("<?xml")


def test_a_path_with_an_ampersand_produces_valid_xml():
    """A generator that can emit invalid XML is one you cannot trust with a path you
    did not choose."""
    body = schtasks.task_xml(r"C:\a&b\py.exe", "--x", schtasks.repeating_trigger(15))
    assert "C:\\a&amp;b\\py.exe" in body
    assert "&b\\py" not in body.replace("&amp;", "&amp;")


def test_registration_replaces_rather_than_erroring_on_a_second_run():
    """Re-running an installer after the checkout moves is the natural thing to do, and
    is what keeps these idempotent."""
    argv = schtasks.register_argv("devkit-x", Path("t.xml"))
    assert argv[:2] == ["schtasks", "/Create"]
    assert "/XML" in argv and "/F" in argv


def test_the_task_file_is_removed_even_when_registration_fails(tmp_path, monkeypatch):
    """It holds a full command line; leaving copies in the temp directory reads as a
    leak the first time anyone looks."""
    seen: list[Path] = []

    def runner(argv):
        seen.append(Path(argv[argv.index("/XML") + 1]))
        return subprocess.CompletedProcess(list(argv), 1, "", "denied")

    monkeypatch.setattr(schtasks.tempfile, "gettempdir", lambda: str(tmp_path))
    ok, message = schtasks.register("devkit-x", document(), runner)

    assert ok is False
    assert "denied" in message
    assert seen and not seen[0].exists()
