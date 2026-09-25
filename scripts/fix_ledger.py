#!/usr/bin/env python3
"""The dispatch ledger: what was sent, at which commit, and what came of it.

`fix_plan.py` decides what one red thing gets; this remembers what was already done
about it, which is what makes a second click -- or the next half-hourly pass -- safe.
Every dispatch is recorded under a key that names the failure *and the commit it was
observed on* (the PR head sha, or the run id for a scheduled workflow), so the pass
sends nothing at a failure an agent is already on, and a fix that pushed a new sha is
a new key when it is still red. The user chose click-only over a scheduled dispatch
precisely because a session costs real money and a loop that spent one silently would
be the worst outcome here.

Two things an entry can say beyond "sent": that it is old enough to look at again
(`RESEND_AFTER`, because a session that died leaves nothing but its entry -- once,
`MAX_SENDS`, because a second death is a pattern and not an accident), and that the
session reported itself blocked (`mark_blocked`, which never expires, because the
session did report and what it reported needs a person).

Stdlib plus `fix_plan`. Tested in `tests/test_fix_ledger.py`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_plan

# The ledger's file name; the pass puts it beside the box tier's lease file.
LEDGER_NAME = "dispatch.json"

# Keys are the sha256 of the signature, cut to this many hex digits: enough that two
# signatures on one machine cannot collide, short enough to read in a log line.
KEY_DIGEST = 12

# How long a recorded dispatch stops the same one being made again. A session that
# died -- on a permission prompt nobody answered, on a crash -- leaves nothing but its
# ledger entry, and an entry that never expired held a red PR for a week with the
# record saying "already dispatched". After this long the pass looks again, under the
# same daily caps; a *blocked* entry never expires.
RESEND_AFTER = _dt.timedelta(hours=24)

# How many sessions one key gets in its life: the dispatch and one re-send. The entry
# counts them, so a failure whose session dies every day does not buy a session every
# day; past this it reads "needs a human" until the failure moves to a new sha.
MAX_SENDS = 2


# --- the keys -------------------------------------------------------------------------


def failure_key(failure: fix_plan.Failure) -> str:
    """What one dispatch is remembered as: the failure, at the commit it was seen on."""
    digest = hashlib.sha256("\n".join(failure.signature).encode("utf-8")).hexdigest()
    at = failure.sha or failure.run_id or "?"
    return f"{failure.kind}:{failure.project}:{failure.number}:{at}:{digest[:KEY_DIGEST]}"


def key_kind(key: str) -> str:
    """The kind a ledger key names: its first field (`UPSTREAM` for a folded group)."""
    return key.split(":", 1)[0]


def decision_key(decision: fix_plan.Decision) -> str:
    """An upstream decision is one dispatch for the whole group, so one key.

    Any member re-observed at a new sha changes the key: a consumer whose PR was
    re-pushed and is still red under the same signature is a reason to look again.

    A single failure is keyed under the **action** as well, because what the pass
    decides can change while the failure does not -- which is how a dispatch this pass
    got wrong becomes unrepeatable. devkit #381 was recorded at its head sha as an
    upstream session; nobody was going to push to a conflicted PR, so without the
    action in the key the corrected pass would read its own bad dispatch as reason
    enough never to send the resolver. `fix_cycle.target_of` reads the first three
    fields, so the suffix leaves the daily budget per PR exactly where it was.
    """
    keys = sorted(failure_key(f) for f in decision.failures)
    if len(keys) == 1:
        return f"{keys[0]}:{decision.action}"
    digest = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()
    return f"{fix_plan.UPSTREAM}:{len(keys)}:{digest[:KEY_DIGEST]}"


# --- the file -------------------------------------------------------------------------


def read_ledger(path: Path) -> dict[str, dict]:
    """The recorded dispatches. Unreadable is empty: a corrupt ledger must not block."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write(path: Path, ledger: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record(path: Path, key: str, note: str, now: _dt.datetime | None = None) -> None:
    """One more session sent under `key`: the newest time, and how many so far."""
    ledger = read_ledger(path)
    when = (now or _dt.datetime.now(_dt.UTC)).isoformat(timespec="seconds")
    ledger[key] = {"when": when, "what": note, "sent": sends(ledger.get(key)) + 1}
    _write(path, ledger)


def sends(entry: object) -> int:
    """Sessions an entry has had; an entry written before the count was kept had one."""
    if not isinstance(entry, dict):
        return 0
    try:
        return max(1, int(entry.get("sent", 1)))
    except (TypeError, ValueError):
        return 1


def mark_blocked(path: Path, key: str, reason: str) -> bool:
    """A dispatched session reported it could not finish: keep its entry, with why.

    Only an entry the ledger already has -- a stamp naming a key no dispatch made is
    noise, not a report. Returns whether one was marked.
    """
    ledger = read_ledger(path)
    entry = ledger.get(key)
    if not isinstance(entry, dict):
        return False
    entry["blocked"] = reason.strip()
    _write(path, ledger)
    return True


# --- what the ledger says about a decision ---------------------------------------------


def blocked_reason(decision: fix_plan.Decision, ledger: dict[str, dict]) -> str:
    """What the session sent at this decision said was in the way, or "" when nothing."""
    entry = ledger.get(decision_key(decision))
    return str(entry.get("blocked", "")) if isinstance(entry, dict) else ""


def already_sent(
    decision: fix_plan.Decision, ledger: dict[str, dict], now: _dt.datetime | None = None
) -> str:
    """When this exact dispatch was already made, or "" when it is new.

    With a clock, an entry older than `RESEND_AFTER` no longer counts unless the session
    reported itself blocked or the key has had its `MAX_SENDS`: a session that died
    left nothing else, and one re-send bounds what that can cost. Without one, every
    entry stands -- the click-only caller's `--redo` is the override there.
    """
    entry = ledger.get(decision_key(decision))
    if not isinstance(entry, dict):
        return ""
    when = str(entry.get("when", "?"))
    if sends(entry) >= MAX_SENDS:
        return f"{when}, its {sends(entry)} sessions spent -- needs a human"
    if now is None or entry.get("blocked"):
        return when
    try:
        sent_at = _dt.datetime.fromisoformat(when)
    except ValueError:
        return when
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=_dt.UTC)
    return "" if now - sent_at >= RESEND_AFTER else when


def render(decisions: Iterable[fix_plan.Decision], ledger: dict[str, dict]) -> str:
    """The plan, for the terminal: what will be sent, what was already, what is skipped."""
    lines = []
    for decision in decisions:
        names = ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)
        sent = already_sent(decision, ledger)
        if decision.action == fix_plan.SKIP:
            lines.append(f"skip     {names} -- {decision.note}")
        elif decision.action == fix_plan.HOLD:
            lines.append(f"held     {names} -- {decision.note}")
        elif sent:
            lines.append(f"sent     {names} -- already dispatched at {sent} (--redo to send again)")
        else:
            lines.append(f"{decision.action:8} {names} -- {decision.note}")
    return "\n".join(lines) or "nothing is red"
