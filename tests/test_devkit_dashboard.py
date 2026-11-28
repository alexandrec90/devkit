"""`devkit_dashboard.py`: devkit's own dashboard, which replaced the taskbar tray.

What is pinned here is that **no verdict is made on the page**: a job's level comes from
`schedule_health`'s lines, a collector's from `collectors`, the fixers' from what the
pass wrote. The couplings that would otherwise rot silently -- the pass's spellings this
module copies rather than imports -- are each held to their source.
"""

from __future__ import annotations

import datetime as _dt
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from support import load_script

dash = load_script("scripts/devkit_dashboard.py")
fix_cycle = load_script("scripts/fix_cycle.py")
fix_loop = load_script("scripts/fix_loop.py")
fix_prompts = load_script("scripts/fix_prompts.py")
schedule_health = load_script("scripts/schedule_health.py")
triage = load_script("scripts/harness_triage.py")

NOW = _dt.datetime(2026, 11, 27, 18, 0, tzinfo=_dt.UTC)


# --- the spellings it copies ------------------------------------------------------------


def test_the_fixer_opening_and_closing_are_the_pass_role():
    """A fixer is recognised by its first prompt; a reworded `ROLE` would hide them all."""
    assert fix_prompts.ROLE.startswith(dash.ROLE_OPENING)
    assert fix_prompts.ROLE.endswith(dash.ROLE_CLOSING)


def test_the_pass_spellings_are_the_pass_own():
    assert dash.RESOLVE_TAIL == fix_loop.PERSON_RESOLVE_TAIL
    assert dash.DISPATCH == fix_cycle.DISPATCH
    assert {dash.collectors.OK, dash.collectors.WARN, dash.collectors.FAIL} == set(dash.RANK)


def test_every_fail_marker_still_matches_something_schedule_health_can_say():
    source = Path(schedule_health.__file__).read_text(encoding="utf-8")
    for marker in dash.FAIL_MARKERS:
        assert marker in source, f"{marker!r} is gone from schedule_health.py"


def test_the_failed_task_pattern_reads_the_message_log_wrap_writes():
    lw = load_script("scripts/log-wrap.py")
    message = lw.failure_message("Scheduled: Collectors", True)
    assert dash.FAILED_TASK.search(message).group(1) == "Scheduled: Collectors"


def test_the_label_is_read_off_the_command_devkit_schtasks_registers():
    schtasks = load_script("scripts/devkit_schtasks.py")
    arguments = schtasks.logged("Scheduled: Reap Stale", "pythonw.exe", "x.py", "C:/devkit")
    assert dash.label_of(f"C:/py/pythonw.exe {arguments}") == "Scheduled: Reap Stale"
    assert dash.label_of("pythonw.exe tray.py") == ""


# --- reading ------------------------------------------------------------------------------


def test_read_jsonl_skips_what_is_not_an_object_line(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text('{"a": 1}\nnot json\n[1]\n{"b": 2}\n', encoding="utf-8")
    assert dash.read_jsonl(path) == [{"a": 1}, {"b": 2}]
    assert dash.read_jsonl(tmp_path / "absent") == []


def test_a_stamp_is_utc_whatever_its_spelling():
    assert dash.parse_stamp("2026-11-27T10:00:00Z") == _dt.datetime(
        2026, 11, 27, 10, tzinfo=_dt.UTC
    )
    assert dash.parse_stamp("2026-11-27T10:00:00").tzinfo is not None
    assert dash.parse_stamp("junk") is None and dash.parse_stamp(None) is None


def test_the_day_axis_ends_today():
    axis = dash.day_axis(NOW, 3)
    assert axis == ["2026-11-25", "2026-11-26", "2026-11-27"]


def test_a_day_is_the_utc_date():
    late = _dt.datetime(2026, 11, 27, 23, 30, tzinfo=_dt.timezone(_dt.timedelta(hours=-5)))
    assert dash.day_of(late) == "2026-11-28"


# --- the fix pass -------------------------------------------------------------------------


def pass_row(when="2026-11-27T17:30:04+00:00", **fields):
    row = {"when": when, "mode": "dispatch", "harness": "clean", "sent": [], "waiting": {}}
    row.update({"working": [], "person": [], "owed": 0, **fields})
    return row


def test_no_history_says_no_pass_has_run():
    assert dash.fixers_verdict([]) == (dash.WARN, "no fix pass has run on this machine")


def test_a_red_harness_holds_sends_and_says_why():
    level, said = dash.fixers_verdict([pass_row(harness=["2 harness failure(s) open"])])
    assert level == dash.WARN and "2 harness failure(s) open" in said


def test_owed_with_nothing_sent_for_six_passes_is_amber_and_five_is_not():
    stuck = [pass_row(owed=1)] * dash.STUCK_PASSES
    assert dash.fixers_verdict(stuck)[0] == dash.WARN
    assert dash.fixers_verdict(stuck[1:])[0] == dash.OK


def test_a_fixer_at_work_or_a_reasoned_wait_is_not_stuck():
    for busy in ({"working": ["devkit x"]}, {"waiting": {"devkit #1": "held"}}):
        assert dash.fixers_verdict([pass_row(owed=1, **busy)] * dash.STUCK_PASSES)[0] == dash.OK


def test_history_written_before_the_owed_fields_is_never_judged_stuck():
    old = {"when": "2026-10-20T00:00:00+00:00", "mode": "dispatch", "harness": "clean"}
    assert dash.fixers_verdict([old] * dash.STUCK_PASSES) == (dash.OK, "clean")


def test_a_pass_that_sends_nothing_by_its_mode_says_so():
    assert dash.fixers_verdict([pass_row(mode="plan")]) == (
        dash.OK,
        "mode=plan: the pass sends nothing",
    )


def test_groups_awaiting_one_step_are_one_step_that_counts_them():
    billing = "settle the GitHub account's Actions billing"
    steps = [
        f"{billing} -- then: python scripts/harness_triage.py --resolve-like {i}" for i in "ab"
    ]
    found = dash.person_steps([pass_row(person=[*steps, "reboot"])])
    assert [(s["step"], s["count"]) for s in found] == [(billing, 2), ("reboot", 1)]
    assert found[0]["then"].startswith("python scripts/harness_triage.py")
    assert dash.person_steps([]) == []


def test_passes_are_counted_per_day_and_listed_newest_first():
    history = [
        pass_row("2026-11-26T10:00:00+00:00", sent=["a -- dispatch", "b -- upstream"], filed=1),
        pass_row("2026-11-27T10:00:00+00:00"),
        pass_row("2026-11-27T10:30:00+00:00", filed=2, backlog=3),
        pass_row("2026-10-01T10:00:00+00:00"),  # past the axis: in no day
    ]
    view = dash.passes_view(history, NOW, days=2)
    assert view["days"] == [
        {"day": "2026-11-26", "passes": 1, "sends": 2, "filed": 1},
        {"day": "2026-11-27", "passes": 2, "sends": 0, "filed": 2},
    ]
    assert [row["when"] for row in view["recent"]][0] == "2026-10-01T10:00:00+00:00"
    assert view["last_sent"] == "2026-11-26T10:00:00+00:00"
    assert view["state"] == dash.OK


def test_the_page_reads_the_line_the_pass_writes():
    account = fix_cycle.Account(
        fix_cycle.DISPATCH,
        fix_cycle.harness_state({}, True, []),
        sent=("devkit ledger -- upstream",),
        working=("devkit agent/fix-x",),
        person=("run `claude auth login` on this machine",),
        owed=1,
    )
    row = json.loads(fix_cycle.history_line(account, NOW))
    view = dash.passes_view([row], NOW)
    assert view["person"][0]["step"] == account.person[0]
    assert view["last"]["sent"] == ["devkit ledger -- upstream"]
    assert view["last"]["working"] == 1 and view["last"]["owed"] == 1
    assert view["last"]["harness"] == []


# --- fixer sessions -----------------------------------------------------------------------


def transcript(
    prompt: str, *, cwd: str = r"C:\ws\devkit\.claude\worktrees\fix-x-1127"
) -> list[dict]:
    """A fixer's transcript as Claude Code writes it: the usage repeated per block."""
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 1000,
        "output_tokens": 50,
    }
    return [
        {"type": "custom-title", "customTitle": "devkit/fix-x-1127"},
        {
            "type": "user",
            "cwd": cwd,
            "gitBranch": "agent/fix-x-1127",
            "timestamp": "2026-11-27T10:00:00.000Z",
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "requestId": "req1",
            "timestamp": "2026-11-27T10:01:00.000Z",
            "message": {
                "id": "m1",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [{"type": "tool_use", "id": "t1"}],
            },
        },
        {
            "type": "assistant",
            "requestId": "req1",
            "message": {"id": "m1", "usage": usage, "content": [{"type": "tool_use", "id": "t2"}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True}]
            },
        },
        {
            "type": "assistant",
            "requestId": "req2",
            "timestamp": "2026-11-27T10:30:00.000Z",
            "message": {"id": "m2", "usage": {**usage, "output_tokens": 7}, "content": []},
        },
    ]


def test_a_fixer_session_counts_each_response_once():
    found = dash.summarise(transcript(f"{fix_prompts.ROLE} Fix the red test in devkit."))
    assert (found["input"], found["cache_write"], found["cache_read"]) == (20, 200, 2000)
    assert found["output"] == 57 and found["responses"] == 2
    assert found["calls"] == 2 and found["failed_calls"] == 1
    assert found["minutes"] == 30.0 and found["started"] == "2026-11-27T10:00:00+00:00"
    assert (found["project"], found["tree"]) == ("devkit", "fix-x-1127")
    assert found["task"] == "Fix the red test in devkit."
    assert found["model"] == "claude-opus-5-5" and found["title"] == "devkit/fix-x-1127"


def test_a_session_that_is_no_fixer_is_none():
    assert dash.summarise(transcript("please add a button")) is None


def test_a_transcript_with_no_prompt_by_its_head_is_none():
    assert dash.summarise([{"type": "mode"}] * (dash.HEAD_RECORDS + 1)) is None
    assert dash.summarise([]) is None


def test_a_long_task_is_cut_with_a_mark():
    task = dash.task_of(f"{fix_prompts.ROLE} " + "x" * 400)
    assert len(task) == 241 and task.endswith("…")


@pytest.mark.parametrize(
    ("cwd", "place"),
    [
        (r"C:\ws\devkit\.claude\worktrees\fix-a", ("devkit", "fix-a")),
        ("/ws/carameli/.claude/worktrees/b/sub", ("carameli", "b")),
        (r"C:\ws\.worktrees\ibkr_trader--fix-c", ("ibkr_trader", "ibkr_trader--fix-c")),
        (r"C:\ws\d2-trader", ("d2-trader", "d2-trader")),
    ],
)
def test_the_place_is_the_checkout_a_tree_hangs_off(cwd, place):
    assert dash.place_of(cwd) == place


def write_transcript(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_a_transcript_is_read_again_only_once_it_changed(tmp_path):
    root = tmp_path / "projects"
    path = write_transcript(root / "slug" / "s1.jsonl", transcript(f"{fix_prompts.ROLE} Fix."))
    other = write_transcript(root / "slug" / "s2.jsonl", transcript("not a fixer"))
    cache = tmp_path / "cache.json"
    read = []

    def counting(p):
        read.append(p.name)
        return dash.read_session(p)

    first = dash.fixer_sessions(root, cache, _dt.datetime.now(_dt.UTC), read=counting)
    again = dash.fixer_sessions(root, cache, _dt.datetime.now(_dt.UTC), read=counting)
    assert [s["session"] for s in first] == ["s1"] and again == first
    assert sorted(read) == ["s1.jsonl", "s2.jsonl"], "the second read came from the cache"
    write_transcript(path, transcript(f"{fix_prompts.ROLE} Fix it again."))
    dash.fixer_sessions(root, cache, _dt.datetime.now(_dt.UTC), read=counting)
    assert read[-1] == "s1.jsonl" and other.name not in read[2:]


def test_a_transcript_past_the_window_is_not_read(tmp_path):
    root = tmp_path / "projects"
    write_transcript(root / "slug" / "s1.jsonl", transcript(f"{fix_prompts.ROLE} Fix."))
    later = _dt.datetime.now(_dt.UTC) + _dt.timedelta(days=dash.SESSION_WINDOW_DAYS + 1)
    assert dash.fixer_sessions(root, tmp_path / "c.json", later) == []
    assert dash.fixer_sessions(tmp_path / "absent", tmp_path / "c.json", later) == []


def test_a_session_is_credited_with_what_its_branch_resolved():
    session = {
        "tree": "fix-x-1127",
        "started": "2026-11-27T10:00:00+00:00",
        "output": 5,
        "input": 1,
        "cache_write": 2,
        "cache_read": 3,
    }
    resolutions = [{"pr": "agent/fix-x-1127-2"}, {"pr": "agent/other"}, {"pr": ""}]
    view = dash.sessions_view([session], resolutions, NOW)
    assert view["sessions"][0]["resolved"] == 1
    assert view["days"][-1] == {"day": "2026-11-27", "sessions": 1, "output": 5, "input": 6}


# --- the ledger ---------------------------------------------------------------------------


def items(*lines: str) -> list:
    return [triage.parse_line(line) for line in lines]


def test_issues_registered_and_resolved_are_counted_per_day():
    found = items(
        "2026-11-26T10:00:00+00:00\tevent=fix-pass-finding\tproject=devkit\tdetail=a",
        "2026-11-27T10:00:00+00:00\tevent=session-friction\tproject=devkit\tdetail=b",
    )
    found += items(
        f"2026-11-27T11:00:00+00:00\tevent=triage-resolved\tref={found[0].id}\tpr=agent/fix-a"
        "\tnote=fixed"
    )
    view = dash.ledger_view(found, NOW, days=2)
    assert view["days"] == [
        {"day": "2026-11-26", "registered": 1, "resolved": 0},
        {"day": "2026-11-27", "registered": 1, "resolved": 1},
    ]
    assert [row["detail"] for row in view["open"]] == ["b"]
    assert view["open_count"] == 1 and view["open_by_kind"] == {"session-friction": 1}
    assert view["resolved"][0]["pr"] == "agent/fix-a"
    assert [row["detail"] for row in view["registered"]] == ["b", "a"]


def test_a_job_failure_is_filed_under_its_label_with_whether_it_is_open():
    found = items(
        "2026-11-27T10:00:00+00:00\tevent=scheduled-job-failed\t"
        "message=unattended task 'Scheduled: Prune' failed\tcause=disk full",
        "2026-11-27T10:05:00+00:00\tevent=session-friction\tdetail=x",
    )
    found += items(f"2026-11-27T11:00:00+00:00\tevent=triage-resolved\tref={found[0].id}")
    failures = dash.job_failures(found)
    assert list(failures) == ["Scheduled: Prune"]
    assert failures["Scheduled: Prune"][0]["cause"] == "disk full"
    assert failures["Scheduled: Prune"][0]["open"] is False


# --- scheduled jobs -----------------------------------------------------------------------


def test_a_line_names_its_job_and_says_how_loud_it_is():
    known = frozenset({"social-scraper"})
    assert dash.named_in("devkit-a: last run failed (exit 2) at 04:45", known) == "devkit-a"
    assert dash.named_in("social-scraper: disabled", known) == "social-scraper"
    assert dash.named_in("something else: happened", known) == ""
    assert dash.severity("devkit-a: last run failed (exit 1)") == dash.FAIL
    assert dash.severity("devkit-a: has not run since 04:30 (3 intervals ago)") == dash.WARN


def test_a_job_keeps_its_worst_line():
    worst = dash.verdicts(
        ["devkit-a: has not run since x", "devkit-a: last run failed (exit 1)"], frozenset()
    )
    assert worst == {"devkit-a": (dash.FAIL, "last run failed (exit 1)")}


def job(name, command="", **fields):
    return schedule_health.Job(name, fields.pop("enabled", True), 0, command=command, **fields)


def test_every_job_is_shown_joined_to_its_runs_and_failures():
    command = 'pythonw.exe "C:/d/scripts/log-wrap.py" --always "Scheduled: A" -- "python.exe" a.py'
    runs = [
        {"task": "Scheduled: A", "started": f"2026-11-27T0{n}:00:00+00:00", "seconds": n, "exit": 0}
        for n in range(1, 4)
    ] + [{"task": "Other", "seconds": 9}]
    failures = {"Scheduled: A": [{"when": "2026-11-26T00:00:00+00:00", "open": True}]}
    machine = dash.Machine(
        [
            job(
                "devkit-a",
                command,
                last_run=_dt.datetime(2026, 11, 27, 3),
                next_run=_dt.datetime(2026, 11, 27, 3, 15),
            ),
            job("devkit-b"),
            job("devkit-off"),
        ],
        ["devkit-b: last run failed (exit 1)"],
        frozenset({"devkit-off"}),
    )
    found = dash.jobs_view(machine, runs, failures, NOW)
    assert [(j["name"], j["state"]) for j in found] == [
        ("devkit-b", dash.FAIL),
        ("devkit-a", dash.OK),
        ("devkit-off", dash.OK),
    ]
    a = found[1]
    assert [r["seconds"] for r in a["runs"]] == [3, 2, 1], "newest first, its own runs only"
    assert a["median_seconds"] == 2 and a["every_minutes"] == 15 and a["label"] == "Scheduled: A"
    assert a["failures_7d"] == 1 and a["open_failures"] == 1
    assert a["artifact"] == ""
    assert found[2]["detail"] == "off on purpose (harness-switch)"


def test_a_job_view_counts_only_this_week_s_failures():
    failures = [
        {"when": "2026-11-01T00:00:00+00:00", "open": True},
        {"when": "2026-11-27T00:00:00+00:00", "open": False},
    ]
    view = dash.job_view(job("devkit-a", enabled=False), (dash.FAIL, "x"), [], failures, NOW)
    assert (view["failures_7d"], view["open_failures"]) == (1, 1)
    assert view["last_failure"] == failures[-1] and view["enabled"] is False
    assert view["every_minutes"] is None and view["median_seconds"] == 0.0
    assert (view["kind"], view["name"]) == ("job", "devkit-a")


def test_collectors_ride_along_under_their_own_names():
    tasks = {"social-scraper": "logs/collectors/social-scraper.log", "gone": "logs/x.log"}
    machine = dash.Machine(
        [job("social-scraper")],
        [],
        tasks=tasks,
        collector_rows=[("collector: ibkr_trader", dash.WARN, "starting")],
    )
    found = dash.jobs_view(machine, [], {}, NOW)
    by_name = {j["name"]: j for j in found}
    assert by_name["collector: gone"]["state"] == dash.FAIL
    assert by_name["collector: social-scraper"]["artifact"] == tasks["social-scraper"]
    assert by_name["collector: social-scraper"]["kind"] == "collector"
    assert by_name["collector: ibkr_trader"]["detail"] == "starting"
    assert found[0]["name"] == "collector: gone"
    assert dash.collector_row("x", dash.OK, "")["artifact"] == dash.collectors.ARTIFACT.as_posix()


def test_a_count_is_an_int_and_never_a_bool():
    assert dash.count({"a": 3, "b": True, "c": "4"}, "a") == 3
    assert dash.count({"b": True}, "b") == 0 and dash.count({}, "x") == 0


def test_the_harness_reasons_are_a_list_whatever_the_line_wrote():
    assert dash.harness_reasons({"harness": "clean"}) == []
    assert dash.harness_reasons({}) == []
    assert dash.harness_reasons({"harness": ["a", "b"]}) == ["a", "b"]
    assert dash.harness_reasons({"harness": "red"}) == ["red"]
    assert dash.pass_row({"harness": []})["harness"] == []


def test_a_tally_keeps_the_last_copy_of_a_response_and_the_first_cwd():
    tally = dash.Tally()
    tally.note({"cwd": "a", "timestamp": "2026-11-27T10:00:00Z"})
    tally.note({"cwd": "b", "timestamp": "2026-11-27T10:05:00Z"})
    assert tally.info["cwd"] == "a" and tally.info["last"] == "2026-11-27T10:05:00Z"
    row = {"type": "assistant", "requestId": "r"}
    tally.take(row, {"usage": {"output_tokens": 1}})
    tally.take(row, {"usage": {"output_tokens": 4}})
    assert tally.total("output_tokens") == 4
    assert tally.session("x")["responses"] == 1


# --- the page -----------------------------------------------------------------------------


def test_the_page_is_as_red_as_its_worst_part():
    assert dash.overall({}) == dash.OK
    assert dash.overall({"jobs": [{"state": dash.WARN}], "passes": {"state": dash.OK}}) == dash.WARN
    assert dash.overall({"jobs": [], "passes": {"state": dash.OK, "person": [{}]}}) == dash.FAIL
    assert dash.overall({"jobs": {"error": "x"}, "passes": {"error": "y"}}) == dash.OK


def test_a_section_that_cannot_be_read_says_so_and_spares_the_rest():
    def broken():
        raise OSError("no scheduler")

    assert dash._section(broken) == {"error": "OSError: no scheduler"}
    assert dash._section(lambda: [1]) == [1]


def test_the_snapshot_assembles_every_section(tmp_path, monkeypatch):
    history = tmp_path / dash.FIX_HISTORY
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps(pass_row()) + "\n", encoding="utf-8")
    monkeypatch.setattr(dash, "CLAUDE_PROJECTS", tmp_path / "none")
    monkeypatch.setattr(dash.harness_triage, "load", lambda root: [])
    monkeypatch.setattr(dash.collectors, "scheduled_tasks", lambda root: {})
    monkeypatch.setattr(dash.collectors, "status_rows", lambda root: [])
    monkeypatch.setattr(dash.schedule_health, "query", lambda also=frozenset(): [job("devkit-a")])
    monkeypatch.setattr(dash.schedule_health, "stood_down", lambda: frozenset())
    monkeypatch.setattr(dash.schedule_health, "awake_local", lambda: None)
    monkeypatch.setattr(dash.schedule_health, "problems", lambda *a, **k: [])
    state = dash.snapshot(tmp_path, NOW)
    assert state["overall"] == dash.OK and state["generated"] == "2026-11-27T18:00:00+00:00"
    assert [j["name"] for j in state["jobs"]] == ["devkit-a"]
    assert state["passes"]["last"]["mode"] == "dispatch"
    assert state["fixers"]["sessions"] == [] and state["ledger"]["open_count"] == 0
    json.dumps(state)  # what the page is handed must serialise


def test_the_state_is_read_again_only_once_it_is_stale():
    reads = []
    state = dash.State(lambda: reads.append(1) or {"n": len(reads)}, ttl=3600)
    assert state.get() == {"n": 1} and state.get() == {"n": 1}
    assert state.get(fresh=True) == {"n": 2}


def test_the_server_serves_the_page_the_state_and_nothing_else(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><title>devkit</title>", encoding="utf-8")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), dash.handler(dash.State(lambda: {"ok": 1}), page)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        answers = {}
        for path in ("/", "/api/state", "/healthz", "/nope"):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=5
            )
            connection.request("GET", path)
            response = connection.getresponse()
            answers[path] = (response.status, response.read())
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
    assert answers["/"][0] == 200 and b"devkit" in answers["/"][1]
    assert json.loads(answers["/api/state"][1]) == {"ok": 1}
    assert answers["/healthz"] == (200, b"ok")
    assert answers["/nope"][0] == 404


def test_the_page_it_serves_is_there_and_asks_for_the_state():
    text = dash.PAGE.read_text(encoding="utf-8")
    assert "/api/state" in text and text.startswith("<!doctype html>")


def test_the_parent_is_watched_only_where_no_job_object_ends_the_child(monkeypatch):
    """On Windows the gateway's job object ends the dashboard with it; elsewhere an
    orphan is re-parented, and a dashboard nobody forwards to must not hold its port."""
    started = []

    class Thread:
        def __init__(self, target, daemon):
            started.append(daemon)

        def start(self):
            started.append("started")

    monkeypatch.setattr(dash.threading, "Thread", Thread)
    monkeypatch.setattr(dash.os, "name", "nt")
    dash.watch_parent()
    assert started == []
    monkeypatch.setattr(dash.os, "name", "posix")
    monkeypatch.setattr(dash.os, "getppid", lambda: 1234, raising=False)
    dash.watch_parent()
    assert started == [True, "started"]


def test_once_prints_the_state_as_json(monkeypatch, capsys):
    monkeypatch.setattr(dash, "snapshot", lambda: {"overall": "ok"})
    assert dash.main(["--once"]) == 0
    assert json.loads(capsys.readouterr().out) == {"overall": "ok"}
