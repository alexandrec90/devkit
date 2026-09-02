#!/usr/bin/env python3
"""Report where a Claude Code session's tokens actually go.

Raw token counts mislead in two directions, and this script exists because both
mistakes were made by hand first:

1. **Cache pricing.** ~94% of billed input is *cache reads*, billed at 0.1x,
   while cache *writes* bill at 1.25x (5-minute TTL) or 2.0x (1-hour TTL) --
   12.5x to 20x a read. Counting raw input tokens therefore makes re-sent
   context look like ~98% of spend when priced it is nearer 40%, and makes
   output look like a rounding error when it is closer to a third.

2. **Transcript shape.** Claude Code writes one JSONL record per content block
   and repeats the entire ``usage`` object on each one. Summing records
   multiplies a batched turn by its tool-call count -- observed 869 records
   against 210 real API calls on one session, a 4x overstatement concentrated
   in exactly the sessions that batch well. :func:`api_calls` dedupes on
   ``requestId``; :func:`naive_call_count` is kept only so the test suite can
   assert the two disagree.

The output ranks tools by *lifetime* cost, which is the number that changes
behaviour: a tool result is written to cache once and then re-read on every
later call in the session, so an early Read costs far more than a late one of
the same size.

Usage::

    python scripts/token-audit.py                  # this machine's busiest sessions
    python scripts/token-audit.py --project NAME   # one project's transcripts
    python scripts/token-audit.py --ttl 1h --top 20
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter

# Prices relative to a fresh input token.
PRICE_FRESH = 1.0
PRICE_CACHE_READ = 0.1
PRICE_OUTPUT = 5.0
WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}

# A transcript below this size is a stub -- a resumed session's header, or a
# session that never got a reply -- and only adds noise to the ranking.
MIN_TRANSCRIPT_BYTES = 50_000

PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"
DEFAULT_LOG = pathlib.Path(__file__).resolve().parent.parent / "logs" / "token-audit.log"


def iter_records(path):
    """Yield each parseable JSON record from a transcript.

    Transcripts are appended to live, so a truncated final line is normal
    rather than a corruption worth reporting.
    """
    with pathlib.Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def request_key(record):
    """The identity of the API response a record belongs to."""
    message = record.get("message") or {}
    return record.get("requestId") or message.get("id")


def api_calls(records):
    """Yield ``(key, usage)`` once per API response, not once per record.

    This is the whole point of the module: see the note on transcript shape in
    the module docstring.
    """
    seen = set()
    for record in records:
        if record.get("type") != "assistant":
            continue
        usage = (record.get("message") or {}).get("usage")
        if not usage:
            continue
        key = request_key(record)
        if key in seen:
            continue
        seen.add(key)
        yield key, usage


def naive_call_count(records):
    """Count records carrying usage, double-counting batched turns.

    Deliberately wrong, and kept so ``test_token_audit`` can assert that the
    deduped count differs -- a regression here is silent otherwise, because
    both numbers look plausible.
    """
    return sum(
        1
        for record in records
        if record.get("type") == "assistant" and (record.get("message") or {}).get("usage")
    )


def token_totals(records):
    """Sum billed tokens by kind across a transcript's API calls."""
    totals = {"fresh": 0, "cache_write": 0, "cache_read": 0, "output": 0, "calls": 0}
    for _, usage in api_calls(records):
        totals["calls"] += 1
        totals["fresh"] += usage.get("input_tokens", 0)
        totals["cache_write"] += usage.get("cache_creation_input_tokens", 0)
        totals["cache_read"] += usage.get("cache_read_input_tokens", 0)
        totals["output"] += usage.get("output_tokens", 0)
    return totals


def cost_units(totals, ttl="5m"):
    """Price a token total in units of one fresh input token."""
    write = WRITE_MULTIPLIER[ttl]
    return {
        "fresh": totals["fresh"] * PRICE_FRESH,
        "cache_write": totals["cache_write"] * write,
        "cache_read": totals["cache_read"] * PRICE_CACHE_READ,
        "output": totals["output"] * PRICE_OUTPUT,
    }


def estimate_tokens(content):
    """Rough token count for a tool result payload.

    Four bytes per token is the standard approximation; this ranks tools
    against each other rather than reconciling to a bill, so the constant
    cancels.
    """
    if content is None:
        return 0
    if not isinstance(content, str):
        content = json.dumps(content)
    return len(content) // 4


def tool_payload_units(records, ttl="5m"):
    """Lifetime cost of each tool's results: one write plus every later re-read.

    A result entering context at call *i* of *n* is written once and then read
    back on each of the remaining ``n - i`` calls, which is why position in the
    session matters as much as payload size.
    """
    records = list(records)
    write = WRITE_MULTIPLIER[ttl]
    total_calls = sum(1 for _ in api_calls(records))

    units = Counter()
    counts = Counter()
    tool_of = {}
    seen = set()
    index = 0

    for record in records:
        message = record.get("message") or {}
        if record.get("type") == "assistant":
            key = request_key(record)
            if message.get("usage") and key not in seen:
                seen.add(key)
                index += 1
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_of[block.get("id")] = block.get("name")
        elif record.get("type") == "user":
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool = tool_of.get(block.get("tool_use_id"))
                if tool is None:
                    continue
                tokens = estimate_tokens(block.get("content"))
                remaining = max(0, total_calls - index)
                units[tool] += tokens * write + tokens * PRICE_CACHE_READ * remaining
                counts[tool] += 1

    return units, counts


def find_transcripts(project=None, min_bytes=MIN_TRANSCRIPT_BYTES, limit=12):
    """Most recently modified transcripts, largest-session-first ranking aside."""
    root = PROJECTS_DIR / project if project else PROJECTS_DIR
    if not root.exists():
        return []
    paths = [p for p in root.glob("**/*.jsonl") if p.stat().st_size >= min_bytes]
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[:limit]


def audit(paths, ttl="5m", top=10):
    """Aggregate a set of transcripts into the report's underlying numbers."""
    totals = {"fresh": 0, "cache_write": 0, "cache_read": 0, "output": 0, "calls": 0}
    units = Counter()
    counts = Counter()
    sessions = []

    for path in paths:
        records = list(iter_records(path))
        session_totals = token_totals(records)
        if session_totals["calls"] < 2:
            continue
        for key in totals:
            totals[key] += session_totals[key]
        session_units, session_counts = tool_payload_units(records, ttl=ttl)
        units.update(session_units)
        counts.update(session_counts)
        sessions.append(
            {
                "name": path.stem[:8],
                "calls": session_totals["calls"],
                "naive_calls": naive_call_count(records),
                "units": sum(cost_units(session_totals, ttl).values()),
            }
        )

    sessions.sort(key=lambda s: s["units"], reverse=True)
    return {
        "totals": totals,
        "costs": cost_units(totals, ttl),
        "tool_units": units,
        "tool_counts": counts,
        "sessions": sessions,
        "ttl": ttl,
        "top": top,
    }


def format_report(result):
    """Render an audit as the text written to the log and the terminal."""
    costs = result["costs"]
    totals = result["totals"]
    grand = sum(costs.values()) or 1
    lines = []

    lines.append(f"token audit -- {len(result['sessions'])} sessions, ttl={result['ttl']}")
    lines.append(
        f"{totals['calls']:,} API calls, {sum(s['naive_calls'] for s in result['sessions']):,} transcript records"
    )
    lines.append("")
    lines.append(f"{'':<14}{'tokens':>16}{'cost units':>16}{'share':>9}")
    for key in ("cache_read", "cache_write", "output", "fresh"):
        token_key = key if key != "output" else "output"
        lines.append(
            f"{key:<14}{totals[token_key]:>16,}{costs[key]:>16,.0f}{100 * costs[key] / grand:>8.1f}%"
        )
    lines.append(f"{'TOTAL':<14}{'':>16}{grand:>16,.0f}")
    lines.append("")

    lines.append("tool result payload -- written once, re-read for the rest of the session")
    lines.append(f"{'tool':<16}{'calls':>8}{'cost units':>16}{'units/call':>14}{'share':>9}")
    for tool, value in result["tool_units"].most_common(result["top"]):
        calls = result["tool_counts"][tool]
        lines.append(
            f"{tool:<16}{calls:>8,}{value:>16,.0f}{value / max(calls, 1):>14,.0f}"
            f"{100 * value / grand:>8.1f}%"
        )
    payload = sum(result["tool_units"].values())
    lines.append(f"{'TOTAL':<16}{'':>8}{payload:>16,.0f}{'':>14}{100 * payload / grand:>8.1f}%")
    lines.append("")

    lines.append("busiest sessions (cost units)")
    for session in result["sessions"][: result["top"]]:
        inflation = session["naive_calls"] / max(session["calls"], 1)
        lines.append(
            f"  {session['name']:<10}{session['calls']:>6} calls"
            f"{session['units']:>16,.0f}   records/calls {inflation:.1f}x"
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", help="project directory under ~/.claude/projects")
    parser.add_argument("--ttl", choices=sorted(WRITE_MULTIPLIER), default="5m")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--limit", type=int, default=12, help="transcripts to read")
    parser.add_argument("--log", type=pathlib.Path, default=DEFAULT_LOG)
    args = parser.parse_args(argv)

    paths = find_transcripts(args.project, limit=args.limit)
    if not paths:
        print("no transcripts found", file=sys.stderr)
        return 1

    report = format_report(audit(paths, ttl=args.ttl, top=args.top))
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.log.write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\nwritten to {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
