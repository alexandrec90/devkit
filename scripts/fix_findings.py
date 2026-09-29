#!/usr/bin/env python3
"""The one place a fix pass puts what it could not turn green: the harness-defect ledger.

The pass's contract is that every observation ends in one of three states -- green, in
flight with a deadline, or a finding on the ledger -- and never in a record line a person
has to notice. The ledger is the right sink because something already drains it: the
devkit session `fix_backlog.py` sends at the open backlog, which may fix the harness,
the pass itself, or the project a fixer failed on.

- `Finding` is one such outcome. `kind` leads the detail, since the ledger groups by
  detail (`harness_triage.Item.signature`), so one kind recurring is one group.
- `record_all` writes only what is not already open under the same signature: a pass
  every half hour would otherwise file the same stuck push 48 times a day.
- `Journal.step` runs one step of the pass and turns an exception into a finding, so
  one broken step costs that step and not the pass -- the crash that stops every later
  step is the one failure a self-correcting loop cannot report.
- `escalation` reads back what became of a problem the pass escalated: open (the devkit
  session has it) or resolved at some stamp, after which the problem gets fresh fixers.

Tested in `tests/test_fix_findings.py`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_triage as triage

harness_events = triage.harness_events

FINDING = "fix-pass-finding"
FRICTION = "session-friction"

# Where a finding's long evidence goes (a traceback, a transcript excerpt): a ledger value
# is capped at a few hundred characters, and the tail of a traceback is the part that says
# why. Under the devkit checkout's ignored `logs/`, named by content so a recurrence
# overwrites rather than accumulates.
EVIDENCE_DIR = Path("logs") / "findings"

# What a step of the pass can raise that is a defect in it rather than a reason to stop
# the process: every built-in family a bug surfaces as, and a subprocess timing out. Named
# rather than `Exception`, so an interrupt still stops the pass; the pass adds its own
# error classes (`Journal.errors`), which derive from `Exception` directly.
STEP_ERRORS: tuple[type[Exception], ...] = (
    ArithmeticError,
    AssertionError,
    AttributeError,
    ImportError,
    LookupError,
    NameError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    subprocess.SubprocessError,
)


@dataclass(frozen=True)
class Finding:
    kind: str  # what went wrong, one slug: `push-failed`, `fixers-exhausted`, ...
    project: str  # the repo it is about; the pass itself is `devkit`
    detail: str  # one line saying what, stable across recurrences
    key: str = ""  # the dispatch problem it concerns, when there is one
    evidence: str = ""  # a path, a URL, or `transcript#L12`
    command: str = ""
    event: str = FINDING
    agent: str = ""  # the runtime a harvested transcript ran under; "" for the pass
    # A branch whose merge settles it: the finding is filed already resolved against it,
    # and `fix_verify` reopens it if that branch never lands. Not a ledger field.
    settles_with: str = ""

    @property
    def headline(self) -> str:
        return f"{self.kind}: {self.detail}"

    def at(self, evidence: str) -> Finding:
        """The same finding pointing at `evidence` instead, when there is any."""
        return replace(self, evidence=evidence) if evidence else self

    def fields(self) -> tuple[tuple[str, object], ...]:
        pairs: list[tuple[str, object]] = [("project", self.project), ("detail", self.headline)]
        pairs += [(name, value) for name, value in self._optional() if value]
        return tuple(pairs)

    def _optional(self) -> tuple[tuple[str, str], ...]:
        return (
            ("key", self.key),
            ("evidence", self.evidence),
            ("command", self.command),
            ("agent", self.agent),
        )


def signature(finding: Finding) -> tuple[str, str, str, str]:
    """The ledger's own grouping key for this finding, computed by the ledger's parser."""
    fields = finding.fields()
    if not finding.agent:
        fields = (*fields, ("agent", harness_events.agent_name()))
    line = harness_events.event_line("0", finding.event, fields)
    parsed = triage.parse_line(line)
    return parsed.signature if parsed else (finding.event, "", finding.project, finding.headline)


def fresh(findings: Iterable[Finding], items: list[triage.Item]) -> list[Finding]:
    """The findings with no open ledger item of the same signature, each once."""
    events = (FINDING, FRICTION)
    seen = {item.signature for item in triage.open_items(items, events)}
    kept: list[Finding] = []
    for finding in findings:
        sig = signature(finding)
        if sig not in seen:
            seen.add(sig)
            kept.append(finding)
    return kept


def record_all(
    findings: Iterable[Finding], items: list[triage.Item], devkit_dir: Path
) -> list[Finding]:
    """Append every fresh finding to the ledger under `devkit_dir`; what was written."""
    written = fresh(findings, items)
    for finding in written:
        harness_events.record(finding.event, finding.fields(), root=devkit_dir)
    settle(written, devkit_dir)
    return written


def settle(written: list[Finding], devkit_dir: Path) -> list[str]:
    """Resolve each just-filed finding that names a branch, against that branch.

    A user correcting a session is filed as friction, and in the first supervised run
    seven of eight such rows had been fixed by the very session corrected, on the branch
    it then shipped; a fixer's own friction line saying "fixed on this branch" is the same
    case (`fix_reports.fixed_here`). Resolved-pending-merge rather than dropped: `fix_verify` reopens the
    row if the branch never becomes a merged PR, so the ones nobody fixed come back.
    """
    pending = [f for f in written if f.settles_with]
    if not pending:
        return []
    open_now = triage.open_items(triage.load(devkit_dir), (FINDING, FRICTION))
    refs: list[str] = []
    for finding in pending:
        sig = signature(finding)
        ids = [item.id for item in open_now if item.signature == sig]
        if ids:
            note = f"the session it concerns went on to ship {finding.settles_with}"
            refs += triage.resolve(ids[:1], note, pr=finding.settles_with, root=devkit_dir)
    return refs


# git's dubious-ownership refusal, by any of the lines it prints: the refusal, the owner,
# and the `safe.directory` hint a truncated detail may keep alone. It quotes the tree's
# path and a ship's detail leads with the tree's branch, so each tree an elevated session
# cut filed a group of its own -- two roguelike ships in one pass (936fd498, 91fa12f2)
# while the fix for the first, #467, was pending and would have held them all.
OWNERSHIP_MARKS = ("dubious ownership", "is owned by:", "add safe.directory")
OWNERSHIP = "git refused a tree another account owns (detected dubious ownership)"


def by_cause(project: str, detail: str) -> str:
    """`detail`, or one naming only its cause when that cause is one whose text names
    the tree it happened in; `project` stays, since the ledger groups by it anyway."""
    if any(mark in detail for mark in OWNERSHIP_MARKS):
        return f"{project}: {OWNERSHIP}"
    return detail


def file(journal: Journal | None, kind: str, project: str, detail: str, evidence: str = "") -> None:
    """Add one finding to `journal`, when the caller has one to add it to.

    A detail `by_cause` folds is kept whole as the evidence when there is no other."""
    if journal is None:
        return
    stable = by_cause(project, detail)
    if stable != detail and not evidence:
        evidence = evidence_file(detail, journal.devkit_dir, kind)
    journal.add(Finding(kind, project, stable, evidence=evidence))


def evidence_file(text: str, devkit_dir: Path, stem: str) -> str:
    """Keep a long piece of evidence beside the ledger; its path, or "" if unwritable."""
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:10]
    path = devkit_dir / EVIDENCE_DIR / f"{stem}-{digest}.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return ""
    return str(path)


def kept(artifact: Path, devkit_dir: Path, stem: str) -> str:
    """A copy of `artifact` kept beside the ledger, for an artifact its job rewrites.

    `logs/installers.log` is rewritten by every `installers.py` run -- each pass, the
    daily job, any `status` -- so a finding citing it cited a file that was gone within
    the half hour (55655d1a). The copy is what the finding names; the artifact's own path
    when it cannot be read or copied, which is no worse than citing it was."""
    try:
        text = artifact.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return str(artifact)
    return evidence_file(text, devkit_dir, stem) or str(artifact)


@dataclass
class Journal:
    """What one pass found, gathered as it goes, and the steps that raised."""

    devkit_dir: Path
    findings: list[Finding] = field(default_factory=list)
    crashed: list[str] = field(default_factory=list)
    errors: tuple[type[Exception], ...] = STEP_ERRORS

    def add(self, *found: Finding) -> None:
        self.findings.extend(found)

    def step(self, name: str, fn: Callable[..., object], *args, default=None, **kwargs):
        """`fn(*args, **kwargs)`, or `default` with a finding when it raises."""
        try:
            return fn(*args, **kwargs)
        except self.errors as exc:
            trace = traceback.format_exc()
            where = evidence_file(trace, self.devkit_dir, f"step-{name}")
            self.crashed.append(name)
            self.add(
                Finding(
                    "pass-step-crashed",
                    "devkit",
                    f"fix-pass step {name!r} raised {type(exc).__name__}: {exc}",
                    evidence=where,
                )
            )
            return default


# --- what became of an escalation ------------------------------------------------------


@dataclass(frozen=True)
class Escalation:
    open: bool  # a finding about this problem is still on the ledger, unresolved
    resolved_at: str  # the newest resolution of one, ISO; "" when none was ever resolved


NOT_ESCALATED = Escalation(False, "")


def escalation(problem: str, items: list[triage.Item]) -> Escalation:
    """What the ledger says about findings filed under `problem` (their `key=`)."""
    mine = {i.id for i in items if i.event == FINDING and i.fields.get("key") == problem}
    if not mine:
        return Escalation(False, "")
    verdicts = triage.verdicts(items)
    still_open = any(verdicts.get(ref, ("", ""))[0] != triage.RESOLVED_EVENT for ref in mine)
    stamps = [
        verdicts[ref][1]
        for ref in mine
        if ref in verdicts and verdicts[ref][0] == triage.RESOLVED_EVENT
    ]
    return Escalation(still_open, max(stamps, default=""))


def after(stamp: str, when: str) -> bool:
    """Whether ISO `when` is later than ISO `stamp`; everything is after an empty stamp."""
    if not stamp:
        return True
    try:
        return _dt.datetime.fromisoformat(when) > _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return when > stamp
