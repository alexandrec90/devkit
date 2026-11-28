#!/usr/bin/env python3
"""devkit's own dashboard: the fix pass, its fixers, the ledger, and every scheduled job.

Served by the dashboard gateway (`dashboards.py`) like any project's, from the entry
`devkit.dashboards.devkit` in `projects.toml`, so it is up only while someone looks at it
and is redeployed when `main` moves. It replaced the taskbar tray, which could say one
line per job; a page can say what each one did.

What it reads, all of it already on this machine, and nothing it writes but its cache:

| section | source |
| --- | --- |
| fix passes | `logs/fix-pass.history.jsonl`, one line per pass (`fix-pass.HISTORY`) |
| fixer sessions | Claude Code transcripts whose first prompt opens with `ROLE_OPENING` |
| issues fixed / registered | the harness-events ledger (`harness_triage.load`) |
| scheduled jobs | the scheduler (`schedule_health`), judged by `schedule_health.problems` |
| job runs | `logs/job-runs.jsonl`, one line per `log-wrap.py` run (`record_run`) |
| collectors | `collectors.status_rows` |

**No verdict is made here.** Which job is broken is `schedule_health.problems`', which
collector is `collectors.row`'s, and whether the fixers are stuck is what the pass wrote
down; this only adds how loud each is, as the tray did, so the page and the fix pass
cannot disagree about the same machine.

A fixer's tokens are counted once per API response (`fix-pass-supervise.output_tokens`
says why), and transcripts are summarised once per size and mtime into
`logs/devkit-dashboard.sessions.json`, so a reload reads only what changed. Codex
fixers are not counted: their transcripts carry no per-response usage to dedupe on.

    devkit_dashboard.py --port N    # serve it: what the gateway runs on a wake
    devkit_dashboard.py --once      # the same state as JSON, on stdout

Stdlib only, as a script the gateway's console interpreter runs must be. Tested in
`tests/test_devkit_dashboard.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collectors
import harness_triage
import schedule_health

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE = Path(__file__).resolve().with_name("devkit_dashboard.html")
HOST = "127.0.0.1"

FIX_HISTORY = Path("logs") / "fix-pass.history.jsonl"
FIX_RECORD = Path("logs") / "fix-pass.log"
JOB_RUNS = Path("logs") / "job-runs.jsonl"
SESSION_CACHE = Path("logs") / "devkit-dashboard.sessions.json"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# How far back the charts and lists reach, and how often the state is re-read: the
# scheduler query takes a second or two, and the page polls every minute.
DAYS = 14
CACHE_SECONDS = 30
RUNS_SHOWN = 12
PASSES_SHOWN = 48
LISTED = 40
# A transcript left untouched this long is not read at all: the fixers it holds are
# past every chart.
SESSION_WINDOW_DAYS = 30
# A transcript whose first prompt has not come by this record is no fixer's.
HEAD_RECORDS = 40

# `fix_prompts.ROLE`'s first sentence, which opens every fixer's first prompt. Spelled
# here so the page imports none of the pass; a test holds the two together.
ROLE_OPENING = "You are a fixer session, dispatched by the fix pass."
# Where `ROLE` ends and the prompt's own task begins.
ROLE_CLOSING = "this prompt and that file win."

OK, WARN, FAIL = "ok", "warn", "fail"
RANK = {OK: 0, WARN: 1, FAIL: 2}
# Substrings that make a `schedule_health` line red rather than amber; a test holds each
# to a line that module can write.
FAIL_MARKERS = ("last run failed", "disabled")
# Passes in a row owed a fixer with none sent, at work or waiting, before the fixers
# read amber: three hours at the half-hourly schedule.
STUCK_PASSES = 6
DISPATCH = "dispatch"
# What `fix_loop.person_steps` puts between a step and the command that retires it.
RESOLVE_TAIL = " -- then: "

# `log-wrap.py --always "<label>"` in a registered command line: how a scheduler row is
# joined to the runs `log-wrap.py` recorded under that label.
LABEL = re.compile(r'--always\s+"([^"]+)"')
# `log-wrap.failure_message`: "unattended task 'Scheduled: X' failed".
FAILED_TASK = re.compile(r"task ['\"](.+)['\"] failed")
WORKTREE_SPLIT = re.compile(r"[\\/](?:\.claude[\\/]worktrees|\.worktrees)[\\/]")
BOX_DIR = re.compile(r"[\\/]\.worktrees[\\/]")


# --- reading --------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict]:
    """Every object line of `path`, oldest first; empty when it is absent or unreadable."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def parse_stamp(raw: object) -> _dt.datetime | None:
    """An ISO stamp as an aware UTC time; naive ones are taken as UTC. None for junk."""
    text = str(raw or "").strip().replace("Z", "+00:00")
    try:
        when = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)


def day_of(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).date().isoformat()


def day_axis(now: _dt.datetime, days: int = DAYS) -> list[str]:
    """The last `days` UTC dates, oldest first, ending today."""
    today = now.astimezone(_dt.timezone.utc).date()
    return [(today - _dt.timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def _iso(when: _dt.datetime | None) -> str:
    return when.isoformat(timespec="seconds") if when else ""


# --- fix passes -----------------------------------------------------------------------


def _stuck(row: dict) -> bool:
    owed = row.get("owed")
    if not isinstance(owed, int) or "working" not in row:
        return False  # written before the pass recorded either: nothing to judge by
    return owed > 0 and not (row.get("sent") or row.get("working") or row.get("waiting"))


def fixers_verdict(history: list[dict]) -> tuple[str, str]:
    """`(level, sentence)` on the fixers, by the newest pass: amber when sends are held
    or a fixer has been owed and unsent for `STUCK_PASSES` passes."""
    if not history:
        return WARN, "no fix pass has run on this machine"
    last = history[-1]
    reasons = harness_reasons(last)
    if reasons:
        return WARN, f"harness red, project sends held: {'; '.join(reasons)}"
    recent = history[-STUCK_PASSES:]
    if len(recent) == STUCK_PASSES and all(_stuck(row) for row in recent):
        return WARN, f"owed a fixer and none sent for {STUCK_PASSES} passes"
    if last.get("mode", "") != DISPATCH:
        return OK, f"mode={last.get('mode', '')}: the pass sends nothing"
    return OK, "clean"


def person_steps(history: list[dict]) -> list[dict]:
    """Each distinct step the newest pass awaits a person for, with how many ledger groups
    await it and the command that retires it."""
    found: dict[str, dict] = {}
    for step in (history[-1].get("person") or []) if history else []:
        said, _, then = str(step).partition(RESOLVE_TAIL)
        entry = found.setdefault(said, {"step": said, "then": then, "count": 0})
        entry["count"] += 1
    return list(found.values())


def count(row: dict, key: str) -> int:
    """`row[key]` when it is a count, else 0: older history lines lack most fields."""
    value = row.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def harness_reasons(row: dict) -> list[str]:
    """Why the pass held project sends, or [] for a clean harness."""
    harness = row.get("harness", "clean")
    if isinstance(harness, list):
        return [str(reason) for reason in harness]
    return [] if harness == "clean" else [str(harness)]


def pass_row(row: dict) -> dict:
    """One history line as the page lists it."""
    return {
        "when": str(row.get("when", "")),
        "mode": str(row.get("mode", "")),
        "sent": [str(s) for s in row.get("sent") or []],
        **{key: count(row, key) for key in ("filed", "backlog", "owed", "held")},
        "working": len(row.get("working") or []),
        "waiting": len(row.get("waiting") or {}),
        "harness": harness_reasons(row),
        "release": str(row.get("release", "")),
    }


def passes_view(history: list[dict], now: _dt.datetime, days: int = DAYS) -> dict:
    """The fix pass's recent passes, its per-day counts, and its verdict on the fixers."""
    axis = day_axis(now, days)
    per_day = {day: {"passes": 0, "sends": 0, "filed": 0} for day in axis}
    for row in history:
        when = parse_stamp(row.get("when"))
        bucket = per_day.get(day_of(when)) if when else None
        if bucket is None:
            continue
        bucket["passes"] += 1
        bucket["sends"] += len(row.get("sent") or [])
        bucket["filed"] += count(row, "filed")
    level, said = fixers_verdict(history)
    last = pass_row(history[-1]) if history else None
    sends = [row for row in history if row.get("sent")]
    return {
        "state": level,
        "verdict": said,
        "last": last,
        "last_sent": str(sends[-1].get("when", "")) if sends else "",
        "waiting": dict(history[-1].get("waiting") or {}) if history else {},
        "person": person_steps(history),
        "recent": [pass_row(row) for row in reversed(history[-PASSES_SHOWN:])],
        "days": [{"day": day, **per_day[day]} for day in axis],
        "record": FIX_RECORD.as_posix(),
    }


# --- fixer sessions -------------------------------------------------------------------


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def task_of(prompt: str) -> str:
    """The fixer's task: its first prompt with `ROLE` taken off, cut to a sentence or two."""
    body = prompt.split(ROLE_CLOSING, 1)[-1].strip()
    return body[:240] + ("…" if len(body) > 240 else "")


def place_of(cwd: str) -> tuple[str, str]:
    """`(project, tree)` of a session's working directory: the checkout a worktree hangs
    off, and the tree's own name; a checkout itself is its own tree."""
    parts = WORKTREE_SPLIT.split(cwd, 1)
    checkout = re.split(r"[\\/]", parts[0].rstrip("\\/"))[-1]
    if len(parts) == 1:
        return checkout, checkout
    tree = re.split(r"[\\/]", parts[1])[0]
    # A box hangs off the workspace, not a checkout, and is named `<project>--<task>`.
    if BOX_DIR.search(cwd) and "--" in tree:
        return tree.split("--", 1)[0], tree
    return checkout, tree


@dataclass
class Tally:
    """What a transcript has said so far: its usage per API response, its tool calls, its
    failed tool results, and the facts its records carry about the session."""

    usage: dict[str, dict] = field(default_factory=dict)
    calls: set[str] = field(default_factory=set)
    failed: int = 0
    info: dict[str, str] = field(
        default_factory=lambda: dict.fromkeys(
            ("title", "cwd", "branch", "model", "first", "last"), ""
        )
    )

    def note(self, row: dict) -> None:
        """The session facts any record may carry: its title, cwd, branch and clock."""
        info = self.info
        info["title"] = str(row.get("customTitle") or info["title"])
        info["cwd"] = info["cwd"] or str(row.get("cwd") or "")
        info["branch"] = info["branch"] or str(row.get("gitBranch") or "")
        stamp = row.get("timestamp")
        if isinstance(stamp, str) and stamp:
            info["first"] = info["first"] or stamp
            info["last"] = stamp

    def take(self, row: dict, message: dict) -> None:
        """One record after the first prompt. Usage is kept per API response
        (`requestId`, else the message id), the last copy winning, because Claude Code
        repeats a response's usage on every block it writes."""
        blocks = [b for b in message.get("content") or [] if isinstance(b, dict)]
        if row.get("type") == "assistant":
            self.info["model"] = str(message.get("model") or self.info["model"])
            key = str(row.get("requestId") or message.get("id") or "")
            if key and isinstance(message.get("usage"), dict):
                self.usage[key] = message["usage"]
            self.calls.update(str(b.get("id", "")) for b in blocks if b.get("type") == "tool_use")
        elif row.get("type") == "user" and isinstance(message.get("content"), list):
            self.failed += sum(
                1 for b in blocks if b.get("type") == "tool_result" and b.get("is_error")
            )

    def total(self, name: str) -> int:
        return sum(u[name] for u in self.usage.values() if isinstance(u.get(name), int))

    def session(self, prompt: str) -> dict:
        """The fixer session this tally describes, its first prompt being `prompt`."""
        first, last = parse_stamp(self.info["first"]), parse_stamp(self.info["last"])
        project, tree = place_of(self.info["cwd"])
        return {
            "title": self.info["title"],
            "project": project,
            "tree": tree,
            "branch": self.info["branch"],
            "model": self.info["model"],
            "started": _iso(first),
            "ended": _iso(last),
            "minutes": round((last - first).total_seconds() / 60, 1) if first and last else 0,
            "task": task_of(prompt),
            "input": self.total("input_tokens"),
            "cache_write": self.total("cache_creation_input_tokens"),
            "cache_read": self.total("cache_read_input_tokens"),
            "output": self.total("output_tokens"),
            "responses": len(self.usage),
            "calls": len(self.calls),
            "failed_calls": self.failed,
        }


def summarise(records: Iterable[dict]) -> dict | None:
    """One fixer session from its transcript records, or None when it is no fixer's: its
    first prompt, which comes by `HEAD_RECORDS`, opens with `ROLE_OPENING`."""
    tally = Tally()
    prompt = ""
    for n, row in enumerate(records):
        if not prompt and n >= HEAD_RECORDS:
            return None
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        tally.note(row)
        if prompt:
            tally.take(row, message)
        elif row.get("type") == "user" and not row.get("isSidechain"):
            prompt = _text(message.get("content"))
            if not prompt.startswith(ROLE_OPENING):
                return None
    return tally.session(prompt) if prompt else None


def _records(path: Path) -> Iterable[dict]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def read_session(path: Path) -> dict | None:
    """`summarise` of one transcript; None for one that is no fixer's or unreadable."""
    try:
        found = summarise(_records(path))
    except OSError:
        return None
    if found is not None:
        found["session"] = path.stem
    return found


def fixer_sessions(
    root: Path,
    cache_path: Path,
    now: _dt.datetime,
    read: Callable[[Path], dict | None] = read_session,
    window_days: int = SESSION_WINDOW_DAYS,
) -> list[dict]:
    """Every fixer session whose transcript changed within `window_days`, newest first.

    A transcript is read again only when its size or mtime moved; the summary, a fixer's
    or None for any other session, is kept in `cache_path` between reads."""
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    cache = cache if isinstance(cache, dict) else {}
    horizon = now.timestamp() - window_days * 86400
    kept: dict[str, Any] = {}
    found = []
    for path in sorted(root.glob("*/*.jsonl")) if root.is_dir() else []:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < horizon:
            continue
        key = [stat.st_size, stat.st_mtime_ns]
        entry = cache.get(str(path))
        if isinstance(entry, dict) and entry.get("key") == key:
            summary = entry.get("session")
        else:
            summary = read(path)
        kept[str(path)] = {"key": key, "session": summary}
        if summary:
            found.append(summary)
    # Best-effort: a cache that cannot be written only means the next read reads again.
    with contextlib.suppress(OSError):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(kept), encoding="utf-8")
    return sorted(found, key=lambda s: s.get("started", ""), reverse=True)


def sessions_view(sessions: list[dict], resolutions: list[dict], now: _dt.datetime) -> dict:
    """The fixers, each with the ledger items its branch resolved, and tokens per day."""
    axis = day_axis(now)
    per_day = {day: {"sessions": 0, "output": 0, "input": 0} for day in axis}
    listed = []
    for session in sessions:
        tree = session.get("tree") or ""
        fixed = [r for r in resolutions if tree and tree in r.get("pr", "")]
        listed.append({**session, "resolved": len(fixed)})
        started = parse_stamp(session.get("started"))
        bucket = per_day.get(day_of(started)) if started else None
        if bucket is not None:
            bucket["sessions"] += 1
            bucket["output"] += session.get("output", 0)
            bucket["input"] += (
                session.get("input", 0)
                + session.get("cache_write", 0)
                + session.get("cache_read", 0)
            )
    return {"sessions": listed[: LISTED * 2], "days": [{"day": d, **per_day[d]} for d in axis]}


# --- the ledger -----------------------------------------------------------------------


def _ledger_row(item: harness_triage.Item) -> dict:
    return {
        "id": item.id,
        "when": item.stamp,
        "event": item.event,
        "project": item.project,
        "detail": item.detail[:300],
        "cause": item.fields.get("cause", "")[:200],
        "pr": item.fields.get("pr", ""),
        "note": item.fields.get("note", "")[:300],
        "ref": item.fields.get("ref", ""),
    }


def ledger_view(items: list[harness_triage.Item], now: _dt.datetime, days: int = DAYS) -> dict:
    """Issues registered (every triage event) and fixed (every resolution) per day, the
    newest of each, and what is open now by kind."""
    axis = day_axis(now, days)
    per_day = {day: {"registered": 0, "resolved": 0} for day in axis}
    registered, resolved = [], []
    for item in items:
        when = parse_stamp(item.stamp)
        bucket = per_day.get(day_of(when)) if when else None
        if item.event == harness_triage.RESOLVED_EVENT:
            resolved.append(item)
            if bucket is not None:
                bucket["resolved"] += 1
        elif item.event in harness_triage.TRIAGE_EVENTS:
            registered.append(item)
            if bucket is not None:
                bucket["registered"] += 1
    open_now = harness_triage.open_items(items)
    by_kind: dict[str, int] = {}
    for item in open_now:
        by_kind[item.event] = by_kind.get(item.event, 0) + 1
    return {
        "days": [{"day": d, **per_day[d]} for d in axis],
        "registered": [_ledger_row(i) for i in reversed(registered[-LISTED:])],
        "resolved": [_ledger_row(i) for i in reversed(resolved[-LISTED:])],
        "open": [_ledger_row(i) for i in open_now[:LISTED]],
        "open_count": len(open_now),
        "open_by_kind": by_kind,
    }


def job_failures(items: list[harness_triage.Item]) -> dict[str, list[dict]]:
    """`{log-wrap label: its failed-run rows, oldest first}`, with whether each is open."""
    done = harness_triage.resolved_refs(items)
    found: dict[str, list[dict]] = {}
    for item in items:
        if item.event != "scheduled-job-failed":
            continue
        match = FAILED_TASK.search(item.fields.get("message", ""))
        if match:
            found.setdefault(match.group(1), []).append(
                {**_ledger_row(item), "open": item.id not in done}
            )
    return found


# --- scheduled jobs -------------------------------------------------------------------


def severity(line: str) -> str:
    lowered = line.lower()
    return FAIL if any(marker in lowered for marker in FAIL_MARKERS) else WARN


def named_in(line: str, known: frozenset[str]) -> str:
    """The job a `schedule_health` line is about: every line is `<task>: <what>`."""
    name, sep, _rest = line.partition(": ")
    return name if sep and (name.startswith(schedule_health.PREFIX) or name in known) else ""


def verdicts(lines: list[str], known: frozenset[str]) -> dict[str, tuple[str, str]]:
    """`{job: (worst level, its line)}` over `schedule_health.problems`' lines."""
    worst: dict[str, tuple[str, str]] = {}
    for line in lines:
        name = named_in(line, known)
        if not name:
            continue
        level = severity(line)
        if name not in worst or RANK[level] > RANK[worst[name][0]]:
            worst[name] = (level, line.partition(": ")[2])
    return worst


def label_of(command: str) -> str:
    match = LABEL.search(command)
    return match.group(1) if match else ""


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


@dataclass(frozen=True)
class Machine:
    """What this machine's scheduler and collectors said, as `jobs_view` judges it."""

    jobs: list[schedule_health.Job]
    lines: list[str]
    deliberate: frozenset[str] = field(default_factory=frozenset)
    # `collectors.scheduled_tasks`: {task name: its log} for each scheduled collector.
    tasks: dict[str, str] = field(default_factory=dict)
    # `collectors.status_rows`: (row name, level, detail) for each container collector.
    collector_rows: list[tuple[str, str, str]] = field(default_factory=list)


def job_view(
    job: schedule_health.Job,
    verdict: tuple[str, str],
    runs: list[dict],
    failures: list[dict],
    now: _dt.datetime,
    tasks: dict[str, str] | None = None,
) -> dict:
    """One scheduled task: the scheduler's view, its last runs, and its failed runs. A
    scheduled collector (one of `tasks`) is shown under its collector name and log."""
    tasks = tasks or {}
    week = now - _dt.timedelta(days=7)
    recent = [f for f in failures if (parse_stamp(f["when"]) or now) >= week]
    seconds = [float(r["seconds"]) for r in runs if isinstance(r.get("seconds"), int | float)]
    interval = job.interval
    collector = job.name in tasks
    return {
        "name": collectors.ROW_PREFIX + job.name if collector else job.name,
        "kind": "collector" if collector else "job",
        "artifact": tasks.get(job.name) or schedule_health.ARTIFACTS.get(job.name, ""),
        "state": verdict[0],
        "detail": verdict[1],
        "enabled": job.enabled,
        "status": job.status,
        "last_run": job.last_run.isoformat(timespec="seconds") if job.last_run else "",
        "next_run": job.next_run.isoformat(timespec="seconds") if job.next_run else "",
        "last_result": job.last_result,
        "every_minutes": round(interval.total_seconds() / 60) if interval else None,
        "event_only": job.event_only,
        "label": label_of(job.command),
        "runs": list(reversed(runs[-RUNS_SHOWN:])),
        "median_seconds": round(_median(seconds), 1),
        "failures_7d": len(recent),
        "open_failures": sum(1 for f in failures if f.get("open")),
        "last_failure": failures[-1] if failures else None,
    }


def collector_row(name: str, level: str, detail: str) -> dict:
    """A collector row no scheduler task stands behind."""
    return {
        "name": name,
        "kind": "collector",
        "state": level,
        "detail": detail,
        "artifact": collectors.ARTIFACT.as_posix(),
        "runs": [],
    }


def jobs_view(
    machine: Machine, runs: list[dict], failures: dict[str, list[dict]], now: _dt.datetime
) -> list[dict]:
    """Every scheduled job and collector row, worst first then by name.

    Every job appears, not only the unhealthy ones, and one stood down on purpose is green
    saying so; a scheduled collector this machine should run whose task the scheduler
    lacks is red, since a row that simply vanished would read as nothing to report."""
    known = frozenset(job.name for job in machine.jobs)
    judged = verdicts(machine.lines, known)
    by_label: dict[str, list[dict]] = {}
    for row in runs:
        by_label.setdefault(str(row.get("task", "")), []).append(row)
    found = []
    for job in machine.jobs:
        quiet = "off on purpose (harness-switch)" if job.name in machine.deliberate else ""
        label = label_of(job.command)
        verdict = judged.get(job.name, (OK, quiet))
        found.append(
            job_view(
                job, verdict, by_label.get(label, []), failures.get(label, []), now, machine.tasks
            )
        )
    unscheduled = "not scheduled on this machine -- `collectors.py maintain` registers it"
    found += [
        collector_row(collectors.ROW_PREFIX + name, FAIL, unscheduled)
        for name in sorted(machine.tasks)
        if name not in known
    ]
    found += [collector_row(*row) for row in machine.collector_rows]
    return sorted(found, key=lambda item: (-RANK[item["state"]], item["name"]))


# --- the whole page -------------------------------------------------------------------


def _section(build: Callable[[], Any]) -> Any:
    """`build()`, or `{"error": why}`: one source that cannot be read must not blank the
    page, and the section that failed says so rather than showing nothing wrong."""
    try:
        return build()
    except Exception as exc:  # shown on the page, which is its whole report
        return {"error": f"{type(exc).__name__}: {exc}"}


def _jobs(root: Path, failures: dict[str, list[dict]], now: _dt.datetime) -> list[dict]:
    tasks = collectors.scheduled_tasks(root)
    jobs = schedule_health.query(also=frozenset(tasks))
    deliberate = schedule_health.stood_down()
    lines = schedule_health.problems(
        jobs,
        None,
        deliberate,
        artifacts={**schedule_health.ARTIFACTS, **tasks},
        awake=schedule_health.awake_local(),
    )
    machine = Machine(jobs, lines, deliberate, tasks, collectors.status_rows(root))
    return jobs_view(machine, read_jsonl(root / JOB_RUNS), failures, now)


def overall(state: dict) -> str:
    """The page's colour: the worst job, the fixers, and red for a person's step."""
    levels = [OK]
    jobs = state.get("jobs")
    if isinstance(jobs, list):
        levels += [job["state"] for job in jobs]
    passes = state.get("passes")
    if isinstance(passes, dict) and "state" in passes:
        levels.append(passes["state"])
        levels += [FAIL] if passes.get("person") else []
    return max(levels, key=lambda level: RANK[level])


def snapshot(root: Path = REPO_ROOT, now: _dt.datetime | None = None) -> dict:
    """Everything the page draws, read fresh."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    items = _section(lambda: harness_triage.load(root))
    items = items if isinstance(items, list) else []
    ledger = _section(lambda: ledger_view(items, now))
    resolutions = ledger.get("resolved", []) if isinstance(ledger, dict) else []
    failures = job_failures(items)
    state = {
        "generated": _iso(now),
        "host": socket.gethostname(),
        "passes": _section(lambda: passes_view(read_jsonl(root / FIX_HISTORY), now)),
        "fixers": _section(
            lambda: sessions_view(
                fixer_sessions(CLAUDE_PROJECTS, root / SESSION_CACHE, now), resolutions, now
            )
        ),
        "ledger": ledger,
        "jobs": _section(lambda: _jobs(root, failures, now)),
    }
    state["overall"] = overall(state)
    return state


# --- serving --------------------------------------------------------------------------


class State:
    """`snapshot`, re-read at most every `CACHE_SECONDS` however many requests ask."""

    def __init__(self, read: Callable[[], dict] = snapshot, ttl: float = CACHE_SECONDS):
        self.read, self.ttl = read, ttl
        self.lock = threading.Lock()
        self.value: dict | None = None
        self.at = 0.0

    def get(self, fresh: bool = False) -> dict:
        with self.lock:
            value = self.value
            if fresh or value is None or time.monotonic() - self.at > self.ttl:
                value = self.value = self.read()
                self.at = time.monotonic()
            return value


def handler(state: State, page: Path = PAGE) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the name http.server dispatches on
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page.read_bytes())
            elif path == "/api/state":
                body = json.dumps(state.get(fresh="fresh=1" in self.path)).encode("utf-8")
                self._send(200, "application/json", body)
            elif path == "/healthz":
                self._send(200, "text/plain", b"ok")
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, kind: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return  # the gateway keeps the dashboard's output; a line per poll is noise

    return Handler


def watch_parent(interval: float = 5.0) -> None:
    """End this process once the gateway that started it is gone. On Windows its job
    object does that; elsewhere an orphan is re-parented, which is what this watches."""
    if os.name == "nt":
        return
    parent = os.getppid()

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(interval)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="serve on 127.0.0.1:PORT")
    parser.add_argument("--once", action="store_true", help="print the state as JSON and exit")
    args = parser.parse_args(argv)
    if args.once or not args.port:
        print(json.dumps(snapshot(), indent=2))
        return 0
    watch_parent()
    server = ThreadingHTTPServer((HOST, args.port), handler(State()))
    print(f"devkit dashboard on http://{HOST}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
