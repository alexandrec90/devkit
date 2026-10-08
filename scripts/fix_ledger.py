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

Three things an entry can say beyond "sent": that it is old enough to look at again
(`RESEND_AFTER`, because a session that died can leave nothing but its entry), that the
pass found its session dead (`mark_dead`, which frees it at once; `mark_interrupted`
when a restart killed it, which also takes the send back), and that the session
reported itself blocked (`mark_blocked`).

And one thing the entries say together: how many sessions one *problem* has had
(`problem_key`, the key less its commit). A failure that survives `ATTEMPTS` fixers
unchanged is escalated -- a finding on the harness-defect ledger, which the devkit
session takes over (`fix_budget.budget`) -- never parked for a person. Every reader here
takes a `since`: the stamp that escalation was resolved at. Entries before it are
history, so a problem whose escalation is closed gets fresh fixers.

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
# record saying "already dispatched". After this long the pass looks again, under
# `ATTEMPTS`; a *blocked* entry never expires. Longer than any fixer runs, and short
# enough that a dead one costs an afternoon rather than a day.
RESEND_AFTER = _dt.timedelta(hours=6)

# How many sessions one *problem* gets in its life: the same failures on the same
# target under the same action, at whatever commit they were seen. The second is the
# retry after a fix that did not take; a third at an unchanged signature is a fixer
# that cannot, and the one outcome worth refusing. A signature that changed is
# progress -- some failures went, or new ones came -- and is a new problem, so the
# limit is on repeating a failure rather than on the target. Counted over the life of
# the ledger, not a day, so a PR flipping between two signatures does not buy more.
ATTEMPTS = 2
# A problem with no evidence cannot show progress: its one session is all it gets.
BLIND_ATTEMPTS = 1


# --- the keys -------------------------------------------------------------------------


def failure_key(failure: fix_plan.Failure) -> str:
    """What one dispatch is remembered as: the failure, at the commit it was seen on."""
    digest = hashlib.sha256("\n".join(failure.signature).encode("utf-8")).hexdigest()
    # A nightly is keyed at its base's tip when that is known: a re-run of an old red
    # run is made once per tip, and made again once the tip moves.
    at = failure.tip or failure.sha or failure.run_id or "?"
    return f"{failure.kind}:{failure.project}:{target(failure)}:{at}:{digest[:KEY_DIGEST]}"


def target(failure: fix_plan.Failure) -> str:
    """The key's third field, which says *what* is red: a PR's number, or for a refused
    commit -- which has none -- its branch. git refuses a `:` in a ref name, so a branch
    cannot shift the fields after it.

    Every refused commit used to be `0`, so one project's refusals sharing a hook's
    line were one problem: social-scraper's happy-drifting-sprout was refused by
    detect-secrets on 2026-10-08, and was filed fixers-exhausted with no fixer ever sent
    at it, its `ATTEMPTS` spent on 10-03 and 10-05 at other trees' refusals (787d0750).
    """
    if failure.kind == fix_plan.COMMIT and failure.head:
        return failure.head
    return str(failure.number)


def key_kind(key: str) -> str:
    """The kind a ledger key names: its first field (`UPSTREAM` for a folded group)."""
    return key.split(":", 1)[0]


def is_upstream(key: str) -> bool:
    """Whether a key records a devkit session: a folded group (`upstream:<n>:<digest>`),
    or one failure sent upstream (`<failure key>:upstream`) -- which is what a sweep of
    the ledger alone is, and the shape a prefix test missed, so three sweeps ran at once."""
    return key_kind(key) == fix_plan.UPSTREAM or key.endswith(f":{fix_plan.UPSTREAM}")


def decision_key(decision: fix_plan.Decision) -> str:
    """An upstream decision is one dispatch for the whole group, so one key.

    Any member re-observed at a new sha changes the key: a consumer whose PR was
    re-pushed and is still red under the same signature is a reason to look again.

    A single failure is keyed under the **action** as well, because what the pass
    decides can change while the failure does not -- which is how a dispatch this pass
    got wrong becomes unrepeatable. devkit #381 was recorded at its head sha as an
    upstream session; nobody was going to push to a conflicted PR, so without the
    action in the key the corrected pass would read its own bad dispatch as reason
    enough never to send the resolver. Anything that reads a target reads the first three
    fields, so the suffix leaves the daily budget per PR exactly where it was.
    """
    keys = sorted(failure_key(f) for f in decision.failures)
    if len(keys) == 1:
        return f"{keys[0]}:{decision.action}"
    digest = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()
    return f"{fix_plan.UPSTREAM}:{len(keys)}:{digest[:KEY_DIGEST]}"


def problem_key(decision: fix_plan.Decision) -> str:
    """`decision_key` without the commit: what `ATTEMPTS` counts sessions against.

    A fixer that pushed and left the same tests red produced a new sha, so a new
    `decision_key`, and used to read as a fresh failure; under this key it is the
    same problem again, which is exactly the loop to stop.
    """
    keys = sorted(_drop_commit(failure_key(f)) for f in decision.failures)
    if len(keys) == 1:
        return f"{keys[0]}:{decision.action}"
    digest = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()
    return f"{fix_plan.UPSTREAM}:{len(keys)}:{digest[:KEY_DIGEST]}:problem"


def _drop_commit(key: str) -> str:
    """`kind:project:number:<sha>:digest[:action]` less its fourth field."""
    parts = key.split(":")
    return ":".join(parts[:3] + parts[4:]) if len(parts) >= 5 else key


def problem_of(key: str, entry: object) -> str:
    """The problem an entry was recorded under; derived from a single-failure key for
    an entry written before `record` kept it, and "" for a group, which cannot be."""
    if isinstance(entry, dict) and entry.get("problem"):
        return str(entry["problem"])
    return _drop_commit(key) if len(key.split(":")) == 6 else ""


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


def record(
    path: Path,
    key: str,
    note: str,
    now: _dt.datetime | None = None,
    problem: str = "",
    source: str = "",
) -> None:
    """One more session sent under `key`: the newest time, how many so far, and the
    `problem_key` it counts against when the caller knows it.

    A `source` (`fix_plan.DEPENDABOT`) is a family of sends with a daily cap: its entries
    keep every send's time (`AT_KEEP` of them), because `when` is only the newest and a
    key re-sent after `RESEND_AFTER` is two sends in one day.
    """
    ledger = read_ledger(path)
    old = ledger.get(key)
    when = (now or _dt.datetime.now(_dt.UTC)).isoformat(timespec="seconds")
    entry: dict = {"when": when, "what": note, "sent": sends(old) + 1}
    if problem:
        entry["problem"] = problem
    if source:
        earlier = old.get("at", []) if isinstance(old, dict) else []
        entry["source"] = source
        entry["at"] = [*(earlier if isinstance(earlier, list) else []), when][-AT_KEEP:]
    ledger[key] = entry
    _write(path, ledger)


# How many send times a sourced entry keeps: more than one key can be sent in any day.
AT_KEEP = 8


def sent_since(ledger: dict[str, dict], source: str, since: _dt.datetime) -> int:
    """How many sessions of `source` the ledger records at or after `since`."""
    count = 0
    for entry in ledger.values():
        if not isinstance(entry, dict) or entry.get("source") != source:
            continue
        stamps = entry.get("at")
        for stamp in stamps if isinstance(stamps, list) else []:
            try:
                at = _dt.datetime.fromisoformat(str(stamp))
            except ValueError:
                continue
            count += (at if at.tzinfo else at.replace(tzinfo=_dt.UTC)) >= since
    return count


def sends(entry: object) -> int:
    """Sessions an entry has had; an entry written before the count was kept had one,
    and one whose only session a restart took back (`mark_interrupted`) has none."""
    if not isinstance(entry, dict):
        return 0
    try:
        return max(0, int(entry.get("sent", 1)))
    except (TypeError, ValueError):
        return 1


def mark_blocked(path: Path, key: str, reason: str) -> bool:
    """A dispatched session reported it could not finish: keep its entry, with why.

    Only an entry the ledger already has -- a stamp naming a key no dispatch made is
    noise, not a report. Returns whether one was marked.
    """
    return _mark(path, key, "blocked", reason)


def mark_dead(path: Path, key: str, reason: str) -> bool:
    """The session sent under `key` ended without an outcome, or never started.

    Frees the key for a re-send on the next pass rather than after `RESEND_AFTER`; the
    re-send still counts against the problem's `ATTEMPTS`. Returns whether one was marked.
    """
    return _mark(path, key, "dead", reason)


def mark_interrupted(path: Path, key: str, reason: str) -> bool:
    """The session sent under `key` was killed by the machine restarting.

    Frees the key as `mark_dead` does, and takes the send back: nothing a fixer did is
    measured by a power-off, and counted, it spent a blind problem's one attempt and
    escalated two conflicts (#480, #482) nobody had tried. Returns whether one was marked.
    """
    if not _mark(path, key, "dead", reason):
        return False
    ledger = read_ledger(path)
    ledger[key]["sent"] = max(0, sends(ledger[key]) - 1)
    _write(path, ledger)
    return True


def _mark(path: Path, key: str, field: str, reason: str) -> bool:
    ledger = read_ledger(path)
    entry = ledger.get(key)
    if not isinstance(entry, dict) or entry.get(field):
        return False
    entry[field] = reason.strip()
    _write(path, ledger)
    return True


def _since(entry: object, since: str) -> bool:
    """The entry was written after `since` (ISO); every entry is, when `since` is empty."""
    if not since:
        return True
    when = str(entry.get("when", "")) if isinstance(entry, dict) else ""
    try:
        return _dt.datetime.fromisoformat(when) > _dt.datetime.fromisoformat(since)
    except ValueError:
        return when > since


# --- what the ledger says about a decision ---------------------------------------------


def blocked_reason(decision: fix_plan.Decision, ledger: dict[str, dict], since: str = "") -> str:
    """What a session sent at this decision -- or at the same problem on an earlier
    commit, after `since` -- said was in the way, or "" when nothing.

    The problem half is what keeps a report standing after an unrelated push: the
    blocker a fixer named does not go away because the sha moved. It goes away when the
    finding it was escalated as is resolved, which is the `since` the caller passes.
    """
    entry = ledger.get(decision_key(decision))
    if isinstance(entry, dict) and entry.get("blocked") and _since(entry, since):
        return str(entry["blocked"])
    problem = problem_key(decision)
    for key, other in ledger.items():
        if (
            isinstance(other, dict)
            and other.get("blocked")
            and problem_of(key, other) == problem
            and _since(other, since)
        ):
            return str(other["blocked"])
    return ""


def attempts(decision: fix_plan.Decision, ledger: dict[str, dict], since: str = "") -> int:
    """Sessions sent at this decision's problem, at any commit, after `since`."""
    problem = problem_key(decision)
    return sum(
        sends(entry)
        for key, entry in ledger.items()
        if problem_of(key, entry) == problem and _since(entry, since)
    )


def last_sent(decision: fix_plan.Decision, ledger: dict[str, dict]) -> str:
    """The newest `when` any entry of this problem carries, ISO; "" when none."""
    problem = problem_key(decision)
    stamps = (
        str(entry.get("when", ""))
        for key, entry in ledger.items()
        if isinstance(entry, dict) and problem_of(key, entry) == problem
    )
    return max(stamps, default="")


def moved_on(decision: fix_plan.Decision, ledger: dict[str, dict]) -> bool:
    """Every session this problem has had was sent at a commit other than its head now.

    The head moved under each of them, so each did something. Counted over the life of
    the ledger, like `attempts`, not over a day: a conflict resolved yesterday and back
    today is the base moving again. A folded upstream key names no one commit and never
    counts as moved.
    """
    parts = decision_key(decision).split(":")
    if parts[0] == fix_plan.UPSTREAM or len(parts) < 4:
        return False
    problem = problem_key(decision)
    shas = [
        other.split(":")[3]
        for other, entry in ledger.items()
        if problem_of(other, entry) == problem and len(other.split(":")) >= 4
    ]
    return bool(shas) and parts[3] not in shas


def already_sent(
    decision: fix_plan.Decision,
    ledger: dict[str, dict],
    now: _dt.datetime | None = None,
    since: str = "",
) -> str:
    """When this exact dispatch was already made, or "" when it is new.

    With a clock, an entry older than `RESEND_AFTER` no longer counts, and one the pass
    found dead counts not at all: a session that died left nothing else. How often that
    may happen is `ATTEMPTS`' business, not this function's. An entry from before
    `since` is history. Without a clock every entry stands -- the click-only caller's
    `--redo` is the override there.
    """
    entry = ledger.get(decision_key(decision))
    if not isinstance(entry, dict) or not _since(entry, since):
        return ""
    when = str(entry.get("when", "?"))
    if now is None or entry.get("blocked"):
        return when
    if entry.get("dead"):
        return ""
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
