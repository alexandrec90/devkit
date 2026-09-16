"""`installer_cli.py`: the `--status` / `--uninstall` verbs all thirteen installers share.

The properties here are the ones the five hand-written copies got wrong before this module
existed -- an absent task read as a failure, and a verb that could not express its own dry
run -- so each test says which failure it pins.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import load_script

cli = load_script("scripts/installer_cli.py")


def _scripted(answers: dict[str, tuple[int, str]]):
    """A runner keyed by the schtasks verb, recording what it was asked and in what order."""
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        code, out = answers.get(argv[1].lower(), (0, ""))
        return subprocess.CompletedProcess(list(argv), code, out, "")

    return run, calls


def test_the_delete_is_forced_because_nothing_can_answer_a_prompt():
    assert cli.uninstall_argv("devkit-x") == ["schtasks", "/Delete", "/TN", "devkit-x", "/F"]


def test_the_status_query_is_the_human_readable_one():
    assert cli.query_argv("devkit-x") == ["schtasks", "/Query", "/TN", "devkit-x"]


def test_remove_deletes_a_registered_task():
    run, calls = _scripted({"/query": (0, "devkit-x"), "/delete": (0, "SUCCESS")})
    ok, message = cli.remove("devkit-x", run)
    assert ok and "SUCCESS" in message
    assert [argv[1] for argv in calls] == ["/Query", "/Delete"]


def test_remove_treats_an_absent_task_as_already_removed():
    """The goal is a state, not a change. `installers.py uninstall` on a half-provisioned
    machine must not report a failure per job it had already removed -- the operator could
    not then tell those from the ones that genuinely broke."""
    run, calls = _scripted({"/query": (1, "")})
    ok, message = cli.remove("devkit-x", run)
    assert ok and "was not registered" in message
    assert [argv[1] for argv in calls] == ["/Query"], "it deleted something it had not found"


def test_remove_does_not_read_the_delete_error_text():
    """`schtasks` localises its messages, so absence is established by the query. A string
    match here would report every removal as failed on a non-English machine."""
    run, _calls = _scripted({"/query": (0, "devkit-x"), "/delete": (1, "ACCESS DENIED")})
    ok, message = cli.remove("devkit-x", run)
    assert not ok and "ACCESS DENIED" in message


def test_query_or_remove_declines_when_neither_mode_was_asked_for():
    """None is the caller's signal to carry on into --check/--yes."""
    run, _calls = _scripted({})
    assert (
        cli.query_or_remove("devkit-x", status=False, uninstall=False, apply=True, run=run) is None
    )


def test_query_or_remove_is_dry_until_yes():
    run, calls = _scripted({})
    code, message = cli.query_or_remove(
        "devkit-x", status=False, uninstall=True, apply=False, run=run
    )
    assert code == 0 and "Would run" in message and "--yes" in message
    assert calls == [], "a dry run reached the scheduler"


def test_query_or_remove_uninstalls_with_yes():
    run, calls = _scripted({"/query": (0, "devkit-x"), "/delete": (0, "SUCCESS")})
    code, _message = cli.query_or_remove(
        "devkit-x", status=False, uninstall=True, apply=True, run=run
    )
    assert code == 0
    assert [argv[1] for argv in calls] == ["/Query", "/Delete"]


def test_query_or_remove_reports_a_missing_task_in_status():
    run, _calls = _scripted({"/query": (1, "")})
    code, message = cli.query_or_remove(
        "devkit-x", status=True, uninstall=False, apply=False, run=run
    )
    assert code == 1 and "no scheduled task called devkit-x" in message


# --- answer: the branch an installer's main delegates wholesale ------------------


def test_answer_declines_when_no_verb_was_asked_for():
    """None is what tells `main` to carry on into --check/--yes, so this is the path every
    ordinary invocation takes."""
    run, calls = _scripted({})
    assert (
        cli.answer("devkit-x", status=False, uninstall=False, apply=False, run=run, windows=True)
        is None
    )
    assert calls == []


def test_answer_is_silent_and_green_off_windows(capsys):
    """A POSIX machine running devkit is supported, and there is no scheduler there to ask
    -- reporting that as a failure would make every consumer's run red for the platform."""
    run, calls = _scripted({})
    assert (
        cli.answer("devkit-x", status=True, uninstall=False, apply=False, run=run, windows=False)
        == 0
    )
    assert "Windows-only" in capsys.readouterr().out
    assert calls == [], "it asked a scheduler that is not there"


def test_answer_prints_a_failure_on_stderr(capsys):
    run, _calls = _scripted({"/query": (1, "")})
    assert (
        cli.answer("devkit-x", status=True, uninstall=False, apply=False, run=run, windows=True)
        == 1
    )
    captured = capsys.readouterr()
    assert "no scheduled task called devkit-x" in captured.err
    assert captured.out == "", "a failure went to stdout"


def test_answer_prints_success_on_stdout_first(capsys):
    run, _calls = _scripted({"/query": (0, "devkit-x")})
    assert (
        cli.answer("devkit-x", status=True, uninstall=False, apply=False, run=run, windows=True)
        == 0
    )
    captured = capsys.readouterr()
    assert "devkit-x" in captured.out
    assert captured.err == ""


# --- which installers a `--only` names ------------------------------------------


def test_short_name_is_what_a_person_ticks():
    """The file name is the stable identifier -- `TASK_NAME` is not, because two
    installers register no job at all -- but nobody wants `install-` and `.py` in a
    picker, and the workspace checklist is spelled in these."""
    assert cli.short_name(Path("scripts/install-reap-schedule.py")) == "reap-schedule"
    assert cli.short_name(Path("scripts/install-tray.py")) == "tray"


def test_parse_only_reads_nothing_ticked_as_everything():
    """A `multiPick` the operator dismissed sends "", and a run that silently did nothing
    would be read as "there was nothing to do"."""
    assert cli.parse_only("") == []
    assert cli.parse_only(" tray , reap-schedule ,, ") == ["tray", "reap-schedule"]


def test_select_with_no_ticks_is_everything():
    scripts = [Path("scripts/install-alpha.py"), Path("scripts/install-beta.py")]
    assert cli.select(scripts, []) == (scripts, [])


def test_select_accepts_a_tick_spelled_short_or_long():
    scripts = [Path("scripts/install-alpha.py")]
    for spelling in ("alpha", "install-alpha", "install-alpha.py"):
        chosen, missing = cli.select(scripts, [spelling])
        assert chosen == scripts and missing == [], spelling


def test_select_returns_a_tick_that_matched_nothing():
    """Five boxes ticked, four run, and a report that reads as a complete success is the
    exact failure a checklist is supposed to prevent."""
    chosen, missing = cli.select([Path("scripts/install-alpha.py")], ["alpha", "ghost"])
    assert [p.name for p in chosen] == ["install-alpha.py"]
    assert missing == ["ghost"]


def test_uninstall_order_takes_the_maintainer_first():
    """It registers the job whose `maintain` pass re-registers everything else, so taking
    it last would have the next logon quietly undo the uninstall -- indistinguishable, to
    the operator, from an uninstall that never worked."""
    scripts = [
        Path("scripts/install-zed.py"),
        Path(f"scripts/{cli.MAINTAINER}"),
        Path("scripts/install-alpha.py"),
    ]
    order = [p.name for p in cli.uninstall_order(scripts)]
    assert order[0] == cli.MAINTAINER
    assert order[1:] == ["install-alpha.py", "install-zed.py"], "the rest lost their order"
