"""Tests for scripts/token-audit.py.

The regression this suite exists for is :func:`api_calls` deduping on
``requestId``. Claude Code writes one transcript record per content block and
repeats the whole ``usage`` object on each, so a batched turn is counted once
per tool call unless it is deduped -- an overstatement that is invisible
because both the wrong and the right number look plausible. ``test_dedupe_*``
pins it from both sides: the deduped count is right, and the naive count is
demonstrably different on the same input.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support import load_script

audit = load_script("scripts/token-audit.py")


# --------------------------------------------------------------------------
# transcript fixtures
# --------------------------------------------------------------------------


def usage(fresh=0, write=0, read=0, out=0):
    return {
        "input_tokens": fresh,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "output_tokens": out,
    }


def assistant(req, msg_id, blocks, use=None):
    record = {"type": "assistant", "requestId": req, "message": {"id": msg_id, "content": blocks}}
    if use is not None:
        record["message"]["usage"] = use
    return record


def tool_use(tid, name):
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def tool_result(tid, content):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]},
    }


def batched_turn(req, msg_id, tools, use):
    """One API response split across one record per tool block, usage repeated.

    This is the real on-disk shape, and the reason the module exists.
    """
    return [assistant(req, msg_id, [tool_use(tid, name)], use) for tid, name in tools]


def write_transcript(tmp_path, records, name="session.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# api_calls -- the regression
# --------------------------------------------------------------------------


def test_dedupe_counts_one_call_per_response():
    records = batched_turn(
        "req1", "msg1", [("t1", "Read"), ("t2", "Read"), ("t3", "Grep")], usage(read=1000)
    )
    assert len(list(audit.api_calls(records))) == 1


def test_dedupe_disagrees_with_naive_count():
    """The naive count must differ, or the dedupe is asserting nothing."""
    records = batched_turn("req1", "msg1", [("t1", "Read"), ("t2", "Grep")], usage(read=1000))
    assert audit.naive_call_count(records) == 2
    assert len(list(audit.api_calls(records))) == 1


def test_dedupe_keeps_distinct_responses_apart():
    records = batched_turn("req1", "msg1", [("t1", "Read"), ("t2", "Read")], usage(read=100))
    records += batched_turn("req2", "msg2", [("t3", "Read")], usage(read=200))
    assert [u["cache_read_input_tokens"] for _, u in audit.api_calls(records)] == [100, 200]


def test_dedupe_falls_back_to_message_id_when_request_id_absent():
    a = {"type": "assistant", "message": {"id": "msg1", "content": [], "usage": usage(read=5)}}
    assert len(list(audit.api_calls([a, dict(a)]))) == 1


def test_records_without_usage_are_not_calls():
    records = [assistant("req1", "msg1", [tool_use("t1", "Read")])]  # streaming stub, no usage
    assert list(audit.api_calls(records)) == []


def test_user_records_are_never_calls():
    assert list(audit.api_calls([tool_result("t1", "x")])) == []


# --------------------------------------------------------------------------
# request_key -- which API response a record belongs to
# --------------------------------------------------------------------------


def test_request_key_prefers_request_id():
    record = assistant("req1", "msg1", [], usage(read=1))
    assert audit.request_key(record) == "req1"


def test_request_key_falls_back_to_message_id():
    record = {"type": "assistant", "message": {"id": "msg1", "content": []}}
    assert audit.request_key(record) == "msg1"


def test_request_key_is_none_when_neither_is_present():
    """Records with no identity must not all collapse onto one key."""
    assert audit.request_key({"type": "assistant"}) is None


def test_request_key_groups_the_split_records_of_one_response():
    records = batched_turn("req1", "msg1", [("t1", "Read"), ("t2", "Grep")], usage(read=1))
    assert {audit.request_key(r) for r in records} == {"req1"}


# --------------------------------------------------------------------------
# token_totals / cost_units
# --------------------------------------------------------------------------


def test_token_totals_counts_batched_turn_once():
    records = batched_turn(
        "req1", "msg1", [("t1", "Read"), ("t2", "Read")], usage(fresh=10, write=20, read=30, out=40)
    )
    assert audit.token_totals(records) == {
        "fresh": 10,
        "cache_write": 20,
        "cache_read": 30,
        "output": 40,
        "calls": 1,
    }


def test_token_totals_on_empty_transcript():
    assert audit.token_totals([])["calls"] == 0


def test_cost_units_prices_each_kind():
    totals = {"fresh": 100, "cache_write": 100, "cache_read": 100, "output": 100, "calls": 1}
    costs = audit.cost_units(totals, ttl="5m")
    assert costs == {"fresh": 100.0, "cache_write": 125.0, "cache_read": 10.0, "output": 500.0}


def test_cost_units_hour_ttl_writes_cost_more():
    totals = {"fresh": 0, "cache_write": 100, "cache_read": 0, "output": 0, "calls": 1}
    assert (
        audit.cost_units(totals, "1h")["cache_write"]
        > audit.cost_units(totals, "5m")["cache_write"]
    )


def test_cache_read_is_an_order_of_magnitude_under_fresh():
    """The whole point of the repricing: re-sent context is discounted 10x."""
    totals = {"fresh": 1000, "cache_write": 0, "cache_read": 1000, "output": 0, "calls": 1}
    costs = audit.cost_units(totals)
    assert costs["cache_read"] * 10 == pytest.approx(costs["fresh"])


def test_cost_units_rejects_unknown_ttl():
    with pytest.raises(KeyError):
        audit.cost_units(
            {"fresh": 0, "cache_write": 0, "cache_read": 0, "output": 0, "calls": 0}, "7d"
        )


# --------------------------------------------------------------------------
# estimate_tokens
# --------------------------------------------------------------------------


def test_estimate_tokens_none_is_zero():
    assert audit.estimate_tokens(None) == 0


def test_estimate_tokens_string():
    assert audit.estimate_tokens("a" * 400) == 100


def test_estimate_tokens_serialises_structures():
    assert audit.estimate_tokens([{"type": "text", "text": "x" * 400}]) > 100


# --------------------------------------------------------------------------
# tool_payload_units -- the lifetime model
# --------------------------------------------------------------------------


def _session_with_result_at(position, total_calls, payload):
    """A transcript whose single tool result lands at call ``position``."""
    records = []
    for i in range(1, total_calls + 1):
        if i == position:
            records.append(
                assistant(f"req{i}", f"msg{i}", [tool_use("t1", "Read")], usage(read=10))
            )
            records.append(tool_result("t1", payload))
        else:
            records.append(assistant(f"req{i}", f"msg{i}", [], usage(read=10)))
    return records


def test_register_tool_uses_maps_ids_to_tools():
    tool_of = {}
    audit.register_tool_uses({"content": [tool_use("t1", "Read"), tool_use("t2", "Grep")]}, tool_of)
    assert tool_of == {"t1": "Read", "t2": "Grep"}


def test_register_tool_uses_ignores_non_tool_blocks():
    tool_of = {}
    audit.register_tool_uses({"content": [{"type": "text", "text": "hi"}, "not-a-dict"]}, tool_of)
    assert tool_of == {}


def test_iter_results_yields_tool_and_token_count():
    message = {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 400}]}
    assert list(audit.iter_results(message, {"t1": "Read"})) == [("Read", 100)]


def test_iter_results_skips_unattributable_results():
    """A resumed session references tool_use ids from before the transcript."""
    message = {"content": [{"type": "tool_result", "tool_use_id": "gone", "content": "x"}]}
    assert list(audit.iter_results(message, {})) == []


def test_iter_results_ignores_non_result_blocks():
    assert list(audit.iter_results({"content": [{"type": "text", "text": "hi"}]}, {})) == []


def test_result_units_is_one_write_plus_remaining_reads():
    assert audit.result_units(1000, entered_at=1, total_calls=3) == pytest.approx(
        1000 * 1.25 + 1000 * 0.1 * 2
    )


def test_result_units_never_charges_negative_reads():
    """A result recorded after the last counted call must not go negative."""
    assert audit.result_units(1000, entered_at=9, total_calls=3) == pytest.approx(1000 * 1.25)


def test_result_units_honours_the_ttl():
    assert audit.result_units(100, 1, 1, "1h") > audit.result_units(100, 1, 1, "5m")


def test_early_result_costs_more_than_late_result():
    """Position matters as much as size -- the finding the report is built on."""
    payload = "x" * 4000
    early, _ = audit.tool_payload_units(_session_with_result_at(1, 10, payload))
    late, _ = audit.tool_payload_units(_session_with_result_at(9, 10, payload))
    assert early["Read"] > late["Read"]


def test_lifetime_cost_is_one_write_plus_each_later_read():
    payload = "x" * 4000  # 1000 tokens
    units, counts = audit.tool_payload_units(_session_with_result_at(1, 3, payload), ttl="5m")
    # written once at 1.25x, then re-read on the 2 remaining calls at 0.1x
    assert units["Read"] == pytest.approx(1000 * 1.25 + 1000 * 0.1 * 2)
    assert counts["Read"] == 1


def test_last_call_result_is_written_but_never_re_read():
    payload = "x" * 4000
    units, _ = audit.tool_payload_units(_session_with_result_at(3, 3, payload))
    assert units["Read"] == pytest.approx(1000 * 1.25)


def test_payload_attributed_to_the_right_tool():
    records = batched_turn("req1", "msg1", [("t1", "Read"), ("t2", "Grep")], usage(read=10))
    records.append(tool_result("t1", "x" * 4000))
    records.append(tool_result("t2", "y" * 400))
    units, counts = audit.tool_payload_units(records)
    assert units["Read"] > units["Grep"]
    assert counts == {"Read": 1, "Grep": 1}


def test_orphan_tool_result_is_ignored():
    """A result whose tool_use is outside the transcript cannot be attributed."""
    units, counts = audit.tool_payload_units([tool_result("unknown", "x" * 4000)])
    assert units == {} and counts == {}


# --------------------------------------------------------------------------
# iter_records / find_transcripts -- I/O edges
# --------------------------------------------------------------------------


def test_iter_records_skips_truncated_final_line(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type":"user"}\n{"type":"assis', encoding="utf-8")
    assert [r["type"] for r in audit.iter_records(path)] == ["user"]


def test_iter_records_skips_blank_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('\n\n{"type":"user"}\n\n', encoding="utf-8")
    assert len(list(audit.iter_records(path))) == 1


def test_iter_records_survives_undecodable_bytes(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"type":"user","x":"\xff\xfe"}\n')
    assert len(list(audit.iter_records(path))) == 1


def test_find_transcripts_missing_project_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "PROJECTS_DIR", tmp_path / "nope")
    assert audit.find_transcripts() == []


def test_find_transcripts_skips_stubs(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "PROJECTS_DIR", tmp_path)
    (tmp_path / "small.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "big.jsonl").write_text("{}\n" + "x" * 60_000, encoding="utf-8")
    assert [p.name for p in audit.find_transcripts()] == ["big.jsonl"]


# --------------------------------------------------------------------------
# audit / format_report / main
# --------------------------------------------------------------------------


def test_audit_skips_single_call_sessions(tmp_path):
    path = write_transcript(
        tmp_path, batched_turn("req1", "msg1", [("t1", "Read")], usage(read=10))
    )
    assert audit.audit([path])["sessions"] == []


def test_audit_reports_record_inflation(tmp_path):
    records = batched_turn("req1", "msg1", [("t1", "Read"), ("t2", "Read")], usage(read=10))
    records += batched_turn("req2", "msg2", [("t3", "Read"), ("t4", "Read")], usage(read=20))
    path = write_transcript(tmp_path, records)
    session = audit.audit([path])["sessions"][0]
    assert session["calls"] == 2 and session["naive_calls"] == 4


def test_format_report_renders_every_section(tmp_path):
    records = batched_turn(
        "req1", "msg1", [("t1", "Read")], usage(fresh=5, write=50, read=500, out=10)
    )
    records.append(tool_result("t1", "x" * 4000))
    records += batched_turn("req2", "msg2", [("t2", "Grep")], usage(read=900, out=20))
    path = write_transcript(tmp_path, records)
    report = audit.format_report(audit.audit([path]))
    assert "cache_read" in report
    assert "tool result payload" in report
    assert "busiest sessions" in report
    assert "Read" in report


def test_main_writes_the_log_artifact(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(audit, "PROJECTS_DIR", tmp_path)
    records = batched_turn("req1", "msg1", [("t1", "Read")], usage(read=500, out=10))
    records.append(tool_result("t1", "x" * 4000))
    records += batched_turn("req2", "msg2", [("t2", "Read")], usage(read=900))
    body = "\n".join(json.dumps(r) for r in records)
    (tmp_path / "s.jsonl").write_text(body + "\n" + " " * 60_000, encoding="utf-8")

    log = tmp_path / "logs" / "token-audit.log"
    assert audit.main(["--log", str(log)]) == 0
    assert log.exists() and "cache_read" in log.read_text(encoding="utf-8")
    assert "written to" in capsys.readouterr().out


def test_main_reports_no_transcripts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(audit, "PROJECTS_DIR", tmp_path / "empty")
    assert audit.main(["--log", str(tmp_path / "x.log")]) == 1
    assert "no transcripts" in capsys.readouterr().err
