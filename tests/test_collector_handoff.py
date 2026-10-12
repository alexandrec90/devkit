"""`collector_handoff.py`: running a project's handoff command and reading what it said.

The properties pinned are the ones a move depends on:

- `pending`'s answer is its stdout alone -- `uv`'s chatter on stderr is never a marker;
- "cannot tell" (exit 2, or no answer in time) is told apart from a broken command;
- both ends name a machine the same way, however the person typed it;
- the verbs are run with the argv, the checkout and the bound the contract names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from support import load_script

handoff = load_script("scripts/collector_handoff.py")
config = handoff.config

IBKR = config.Collector("ibkr_trader", "app", handoff=("uv", "run", "python", "scripts/handoff.py"))


# --- naming a machine ---------------------------------------------------------------


def test_this_machine_is_named_as_it_files_ledger_events(monkeypatch):
    monkeypatch.setenv("DEVKIT_HOST", "DESKTOP-B9FC8VP")
    assert handoff.this_host() == "desktop-b9fc8vp" == handoff.harness_events.host_name()


@pytest.mark.parametrize(
    ("typed", "key"),
    [
        ("DESKTOP-ML5F3PJ", "desktop-ml5f3pj"),
        ("  desktop-ml5f3pj ", "desktop-ml5f3pj"),
        ("desktop-ml5f3pj.tail1234.ts.net", "desktop-ml5f3pj"),
        ("", ""),
        (".", ""),
    ],
)
def test_a_typed_host_is_spelled_as_that_machine_spells_itself(typed, key):
    assert handoff.host_key(typed) == key


# --- what a run said ---------------------------------------------------------------


def test_a_summary_is_the_code_and_the_first_thing_said_errors_first():
    assert handoff.Said(1, "progress\n", "\nboto3 missing\nmore").summary() == (
        "exit 1: boto3 missing"
    )
    assert handoff.Said(1, "only stdout").summary() == "exit 1: only stdout"
    assert handoff.Said(124).summary() == "exit 124"


def test_the_tail_is_the_last_lines_of_both_streams():
    said = handoff.Said(1, "a\nb", "c\nd")
    assert said.tail(3) == ["b", "c", "d"]


MARKER = {"state": "pending", "from": "desktop-b9fc8vp", "to": "desktop-ml5f3pj", "key": "k"}


@pytest.mark.parametrize(
    ("said", "state"),
    [
        (handoff.Said(0, ""), handoff.NONE),
        (handoff.Said(0, "  \n", "Installed 3 packages in 2s"), handoff.NONE),
        (handoff.Said(0, json.dumps(MARKER) + "\n", "uv: resolved"), handoff.WAITING),
        (handoff.Said(2, "", "store unreachable"), handoff.UNREADABLE),
        (handoff.Said(124, "", "timed out after 120s"), handoff.UNREADABLE),
        (handoff.Said(1, "", "Traceback ..."), handoff.BROKEN),
        (handoff.Said(127, "", "uv is not on PATH"), handoff.BROKEN),
        (handoff.Said(0, "not json"), handoff.BROKEN),
        (handoff.Said(0, "[1, 2]"), handoff.BROKEN),
    ],
)
def test_pending_is_read_off_stdout_alone(said, state):
    assert handoff.read_pending(said).state == state


def test_a_waiting_marker_names_where_it_came_from():
    pending = handoff.read_pending(handoff.Said(0, json.dumps(MARKER)))
    assert pending.marker == MARKER and pending.source == "desktop-b9fc8vp"
    assert handoff.read_pending(handoff.Said(0, "{}")).source == "another machine"
    assert handoff.Pending(handoff.WAITING, {"from": 3}).source == "another machine"
    assert handoff.Pending(handoff.NONE).said == handoff.Said(0)


# --- running it ----------------------------------------------------------------------


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_run_keeps_stdout_apart_from_stderr_and_runs_frozen(tmp_path):
    code = "import os, sys; print(os.environ['UV_FROZEN']); print('chatter', file=sys.stderr)"
    said = handoff.run(python(code), tmp_path, 60)
    assert (said.code, said.out.strip(), said.err.strip()) == (0, "1", "chatter")


def test_a_run_is_from_the_checkout_and_returns_its_code(tmp_path):
    said = handoff.run(python("import os, sys; print(os.getcwd()); sys.exit(3)"), tmp_path, 60)
    assert said.code == 3 and Path(said.out.strip()) == tmp_path


def test_a_run_that_outlives_its_bound_is_ended_and_says_so(tmp_path):
    said = handoff.run(python("import time; time.sleep(60)"), tmp_path, 1)
    assert said.code == handoff.collector_tasks.TIMED_OUT
    assert "timed out after 1s" in said.err


def test_a_program_that_is_not_there_is_a_code_not_a_raise(tmp_path):
    said = handoff.run(["no-such-handoff-program-x"], tmp_path, 10)
    assert said.code == 127 and "not on PATH" in said.err


def test_a_checkout_that_is_not_there_is_a_code_not_a_raise(tmp_path):
    said = handoff.run(python("pass"), tmp_path / "missing", 10)
    assert said.code in (126, 127)


def test_an_echoed_run_captures_nothing(tmp_path):
    said = handoff.run(python("print('to the terminal')"), tmp_path, 60, echo=True)
    assert said == handoff.Said(0, "", "")


# A module loaded by path is opaque to mypy, so its class is a base only through `Any`.
HandoffBase: Any = handoff.Handoff


class Recorded(HandoffBase):
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def run(self, command, cwd, timeout, echo=False):
        self.calls.append((list(command), cwd, timeout, echo))
        return self.answer


def test_export_is_from_the_checkout_under_its_long_bound_where_a_person_can_watch(tmp_path):
    runner = Recorded(handoff.Said(0))
    assert runner.export(IBKR, tmp_path, "desktop-b9fc8vp", "desktop-ml5f3pj").code == 0
    assert runner.calls == [
        (
            [*IBKR.handoff, "export", "--from", "desktop-b9fc8vp", "--to", "desktop-ml5f3pj"],
            tmp_path,
            handoff.EXPORT_TIMEOUT,
            True,
        )
    ]


def test_pending_asks_for_one_host_under_its_short_bound(tmp_path):
    runner = Recorded(handoff.Said(0, json.dumps(MARKER)))
    assert runner.pending(IBKR, tmp_path, "desktop-ml5f3pj").state == handoff.WAITING
    assert runner.calls == [
        ([*IBKR.handoff, "pending", "--host", "desktop-ml5f3pj"], tmp_path, 120, False)
    ]
    assert handoff.PENDING_TIMEOUT == 120


def test_restore_is_the_import_verb_captured_under_its_own_bound(tmp_path):
    runner = Recorded(handoff.Said(1, "", "sha256 mismatch"))
    assert runner.restore(IBKR, tmp_path, "desktop-ml5f3pj").code == 1
    assert runner.calls == [
        (
            [*IBKR.handoff, "import", "--host", "desktop-ml5f3pj"],
            tmp_path,
            handoff.IMPORT_TIMEOUT,
            False,
        )
    ]


def test_argv_appends_the_verb_and_its_flags_to_the_declared_prefix():
    assert handoff.argv(IBKR, "pending", "--host", "h") == [
        "uv",
        "run",
        "python",
        "scripts/handoff.py",
        "pending",
        "--host",
        "h",
    ]


# --- this machine's record of what it sent ---------------------------------------------


def test_an_outgoing_record_round_trips_and_is_forgotten_by_project(tmp_path):
    path = tmp_path / "logs" / "handoffs.json"
    assert handoff.load_outgoing(path) == {}
    handoff.record_outgoing(path, "ibkr_trader", "desktop-ml5f3pj", 100.0)
    handoff.record_outgoing(path, "other", "desktop-ml5f3pj", 200.0)
    assert handoff.load_outgoing(path)["ibkr_trader"] == {"to": "desktop-ml5f3pj", "at": 100.0}
    handoff.forget_outgoing(path, "ibkr_trader")
    handoff.forget_outgoing(path, "never-sent")
    assert set(handoff.load_outgoing(path)) == {"other"}


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[1]",
        '{"p": "desktop"}',
        '{"p": {"to": 3, "at": 1}}',
        '{"p": {"to": "h", "at": "noon"}}',
    ],
)
def test_an_unreadable_outgoing_record_is_none(tmp_path, body):
    path = tmp_path / "handoffs.json"
    path.write_text(body, encoding="utf-8")
    assert handoff.load_outgoing(path) == {}


def test_save_outgoing_writes_what_load_reads(tmp_path):
    path = tmp_path / "deep" / "handoffs.json"
    handoff.save_outgoing(path, {"p": {"to": "h", "at": 1.5}})
    assert handoff.load_outgoing(path) == {"p": {"to": "h", "at": 1.5}}
