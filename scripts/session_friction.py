#!/usr/bin/env python3
"""Read every agent session on this machine for turns the harness wasted, and file them.

The fix pass read gates, and sessions were told to *report* what the harness did to
them. Nothing read the reports: they were prose in a transcript, so a refusal, a
missing interpreter or a poll loop cost the same turns in the next session and the
one after. This closes that loop without asking any session to cooperate -- it reads
the transcripts themselves (`session_transcripts.py`), from where the last pass stopped.

Each detector is narrow on purpose, because every finding costs a devkit session a
verification: a failing test is the work, not friction, so an ordinary non-zero exit
is never filed on its own. What is filed is what the harness could have prevented --
a guard refusal, a tool or interpreter that is not there, a wait loop, a full suite
where a targeted run was asked for, `--no-verify`, the same failing command three times,
and the user saying the session did something wrong. A detector that turns out noisy
is itself a finding to fix here: on its first week of transcripts, before calibration,
four in five of its findings were a file that merely quoted an error.

Only sessions whose working directory is under the workspace root are read. The
cursor (`CURSOR_NAME`, beside the dispatch ledger) keeps a byte offset and a line count
per transcript, so each line is read once; a transcript first seen is read only if it
was written in the last `LOOKBACK`, so adopting this does not file a month at once.

Tested in `tests/test_session_friction.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_findings
import session_transcripts as st

Event = st.Event
harness_events = fix_findings.harness_events

CURSOR_NAME = "friction-cursor.json"
LOOKBACK = _dt.timedelta(days=3)
# A repeated failure: the same command failing this many times in one session.
REPEATS = 3
SNIPPET = 90

# Text a failed tool call carries when something outside the work refused or was
# missing. Each pattern names the class it files under; the first match wins.
RESULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "isolation-guard",
        re.compile(
            r"session is isolated in the worktree|too complex to verify|"
            r"cannot be shown not to be git|names git in a form",
            re.I,
        ),
    ),
    ("blocked-call", re.compile(r"<tool_use_error>Blocked:|requires approval", re.I)),
    ("user-rejected", re.compile(r"doesn't want to proceed with this tool use", re.I)),
    (
        "environment",
        re.compile(
            r"No module named|ModuleNotFoundError|is not recognized as an internal or external|"
            r"command not found|Python was not found|exit code 9009|"
            r"No such file or directory[^\n]{0,80}\.venv|"
            # Git Bash converting a revision path: `ambiguous argument 'origin\master;x'`.
            r"ambiguous argument '[^'\n]*\\[^'\n]*'",
            re.I,
        ),
    ),
    # A session's own patch script refusing its own edit -- the Bash tool had dropped a
    # backslash on the way in, three times in the first supervised run.
    ("patch-failed", re.compile(r'File "<stdin>", line \d+[\s\S]{0,600}?AssertionError')),
)

# Commands that are friction whatever they return.
COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("no-verify", re.compile(r"\bgit\b[^\n]*--no-verify")),
    # In command position, so a heredoc's text is not a poll; and not `gh pr checks
    # --watch`, which `.claude/rules/engineering.md` prescribes as the one blocking wait.
    # Nor one settle right before that same `--watch`, which returns at once when a push
    # has no checks yet: one call, not a loop (44a5ff19).
    (
        "poll",
        re.compile(
            r"(?:^|&&|\|\||;)\s*(?:sleep\s+\d{2,}\b(?![^\n]*\bgh\s+pr\s+checks\b[^\n]*--watch)|"
            r"until\b[^\n]*;\s*do\b[^\n]*\bsleep\b)",
            re.M,
        ),
    ),
    # The push gate is the PR gate's whole suite, run locally: the gate's job, not a session's.
    # In command position, so a grep or sed that merely names the file is not it (30d0035d).
    (
        "full-suite",
        re.compile(
            r"(?:^|&&?|\|\|?|;)\s*(?:&\s*)?(?:\S*python\S*\s+)?\S*run_push_gate\.py\b|"
            r"\bpre-commit\s+run\b[^\n]*(?:devkit-push-gate|--hook-stage[\s=]+pre-push)",
            re.M,
        ),
    ),
    # A file written or patched through a shell heredoc: Claude Code's Bash tool collapses
    # backslashes in one. Retired three times as "use Write/Edit" and back each time,
    # since only the sessions that noticed the damage reported it -- so every write is.
    (
        "heredoc-write",
        re.compile(
            r"(?:^|[;&|]\s*)(?:cat\s[^\n<]*>|tee\s)[^\n<]*<<|"
            r"<<-?\s*['\"]?\w+['\"]?\n(?=[\s\S]*?(?:\.write_text\(|\.write\(|open\([^)\n]*,\s*(?:mode\s*=\s*)?['\"][wa]))",
            re.M,
        ),
    ),
)

# A command reading back text the harness wrote, which quotes the very patterns above:
# the triage log, the ledger, a transcript, a friction file. Its output is never friction.
READS_HARNESS_TEXT = re.compile(
    r"harness-triage\.log|harness-events[^\s'\"]*\.log|\.jsonl\b|friction[^\s'\"/\\]*\.md"
)
# A command whose output is read for an environment failure even when it exited 0.
SEES_ENVIRONMENT = re.compile(r"(?:^|[;&|]\s*)(?:\S*python\S*\s+-m\s+pytest|pytest|git)\b", re.M)

# The opening message of a session the fix pass dispatched: every prompt's finish line.
DISPATCHED = "the fix pass commits, pushes"
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})

# A test run in command position -- not `pytest` inside a heredoc's source -- whose
# arguments name nothing narrower than the suite: where a targeted run was asked for.
TEST_RUN = re.compile(
    r"(?:^|&&?|\|\|?|;)\s*(?:&\s*)?(?:\S*python\S*\s+-m\s+pytest|\S*python\S*\s+\S*run-tests\.py|"
    r"uv\s+run\s+pytest|pytest)(?=\s|$)(?P<rest>[^|;&>\n]*)",
    re.M,
)
# A command that changes what the next test run sees without touching a file: a service
# started, dependencies installed, a database created, the branch moved. A run after one
# of these reads something new, so it is not a rerun (eda7aed7: a carameli session
# started its db, then brought compose up, between three runs of one test file).
ENVIRONMENT_CHANGE = re.compile(
    r"(?:^|&&?|\|\|?|;)\s*(?:"
    r"docker\s+(?:compose\s+)?(?:up|start|restart|run|build|create)\b|"
    r"(?:npm|pnpm|yarn)\s+(?:ci|install|i)\b|"
    r"(?:\S*python\S*\s+-m\s+)?pip\s+install\b|uv\s+(?:sync|pip|venv|lock)\b|"
    r"psql\b|createdb\b|\S*python\S*\s+\S*(?:bootstrap|worktree)\.py\b|"
    r"git\s+(?:checkout|switch|merge|pull|reset|rebase|cherry-pick|apply|stash)\b|"
    r"sed\s+-i\b"
    r")",
    re.M,
)
NARROWING_FLAGS = frozenset(
    {"--changed", "--target", "-k", "--lf", "--last-failed", "--help", "-h"}
    | {"--co", "--collect-only", "--version", "--sf"}
)
# Roots that are a whole suite: devkit's own, and the vendored tier every project runs.
SUITE_ROOTS = frozenset(
    {"tests", "tests/", ".", "./", "scripts/hooks/tests", "scripts/hooks/tests/"}
)
# `$p`, `${files[@]}`, `$env:T`, `%TARGET%`, PowerShell's splat `@t`: an argument the
# shell fills in, which the detector cannot see, so it reads as narrowing, not as nothing.
SHELL_VARIABLE = re.compile(r"\$\{?[A-Za-z_]|%[A-Za-z_]\w*%|^@[A-Za-z_]\w*$")

# The user telling a session it went wrong -- the most expensive friction there is, and
# the one no tool result carries. Skipped on a session's opening message, which is the
# task, and on anything the harness injected (`<command-name>`, pasted blocks).
FRUSTRATION = re.compile(
    r"\bwhy (?:did|do|does|is|are|would) (?:you|it|this|the)\b|keeps? happening|\bi hate\b|"
    r"\b(?:you|agents?|it|they) shouldn'?t\b|\bstop (?:doing|asking)\b|dead end|what prompted|"
    r"\bstill (?:broken|failing)\b",
    re.I,
)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A dispatched session's last words handing a choice to someone who is not there. The
# ledger sweep ended "the last group needs your decision" with its pick marked
# "(Recommended)" -- a recommendation is the decision, so this is always a lost session.
HANDED_BACK = re.compile(
    r"\bneeds? your (?:decision|call|input|approval|go-ahead)\b|\byour call\b|\bup to you\b|"
    r"\b(?:let me know|tell me) (?:which|if|whether|how)\b|\b(?:do|would) you (?:want|like|prefer)\b|"
    r"\b(?:should|shall) I\b|\bwant me to\b",
    re.I,
)


# --- the detectors ------------------------------------------------------------------------


def normalize(text: str) -> str:
    """The part of a message that stays the same when the defect recurs."""
    text = ANSI.sub("", text)
    text = re.sub(r"[A-Za-z]:[\\/][^\s'\"]*?worktrees[\\/][^\s\\/'\"]+", "<tree>", text)
    text = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", text)
    text = re.sub(r"\d{2,}", "N", text)
    return " ".join(text.split())


def _snippet(text: str, pattern: re.Pattern[str]) -> str:
    """From the match on: what grouped recurrences share, not the context before it."""
    text = ANSI.sub("", text)
    found = pattern.search(text)
    start = found.start() if found else 0
    return normalize(text[start : start + SNIPPET])


def full_suite(rest: str) -> bool:
    """A test command's arguments name no file, directory below the suite, or selector."""
    for token in rest.split():
        word = token.strip("'\"")
        if word.split("=", 1)[0] in NARROWING_FLAGS:
            return False
        if word.startswith("-") or word in SUITE_ROOTS:
            continue
        if "/" in word or "\\" in word or "::" in word or word.endswith(".py"):
            return False
        if SHELL_VARIABLE.search(word):
            return False
    return True


# What a command-shaped class is filed as. Stable on purpose: the ledger groups by the
# detail, and 33 full-suite runs spelled 33 ways were 33 groups for one habit. The
# command itself rides in the finding's `command` field.
COMMAND_DETAIL = {
    "full-suite": "a session ran a whole test suite where a targeted run was asked for",
    "poll": "a session waited in a sleep or until loop",
    "no-verify": "a session committed with --no-verify",
    "heredoc-write": "a session wrote a file through a shell heredoc, which the Bash tool mangles",
}


def _command_classes(command: str) -> Iterator[tuple[str, str]]:
    for cls, pattern in COMMAND_PATTERNS:
        if pattern.search(command):
            yield cls, COMMAND_DETAIL[cls]
    if any(full_suite(run.group("rest") or "") for run in TEST_RUN.finditer(command)):
        yield "full-suite", COMMAND_DETAIL["full-suite"]


def _result_class(text: str) -> tuple[str, str]:
    """The class and snippet a failed call's output files under; `("", "")` for none."""
    for cls, pattern in RESULT_PATTERNS:
        if pattern.search(text):
            return cls, _snippet(text, pattern)
    return "", ""


def _complaint(event: Event, spoken_before: int) -> str:
    """What the user objected to, or "" -- never the opening message, which is the task."""
    if spoken_before == 0 or event.text.lstrip().startswith("<"):
        return ""
    return _snippet(event.text, FRUSTRATION) if FRUSTRATION.search(event.text) else ""


@dataclass
class _Session:
    """What `detect` accumulates over one session's events."""

    found: dict[tuple[str, str], Event] = field(default_factory=dict)
    calls: dict[str, str] = field(default_factory=dict)
    failures: dict[str, list[Event]] = field(default_factory=dict)
    spoken: int = 0
    dispatched: bool = False  # the fix pass sent it, so nobody is there to answer
    # Commands run since the last edit, by what runs before any pipe: the same run again
    # with only its `| tail` changed read nothing new -- five times in one session.
    unchanged: dict[str, int] = field(default_factory=dict)
    last_said: Event | None = None  # the agent's latest text: how the session ended
    # line -> every class noted there: what `outdated` re-judges a filed row by, since
    # `found` keeps only each class's first event.
    at: dict[int, set[str]] = field(default_factory=dict)

    def note(self, cls: str, what: str, event: Event) -> None:
        """Keep the first event of each `(cls, what)`; an empty `what` is no finding."""
        if cls and what:
            self.found.setdefault((cls, what), event)
            self.at.setdefault(event.line, set()).add(cls)

    def user(self, event: Event) -> None:
        if self.spoken == 0:
            self.dispatched = DISPATCHED in event.text
        self.note("user-frustration", _complaint(event, self.spoken), event)
        self.spoken += 1

    def call(self, event: Event) -> None:
        self.calls[event.call_id] = event.command
        if event.tool != "AskUserQuestion":
            self.last_said = None  # it went on working: that text was not how it ended
        for cls, what in _command_classes(event.command):
            self.note(cls, what, event)
        if event.tool == "AskUserQuestion" and self.dispatched:
            self.note(
                "asked-user", "a dispatched session asked a question nobody would answer", event
            )
        if event.tool in EDIT_TOOLS or ENVIRONMENT_CHANGE.search(event.command):
            self.unchanged.clear()
        elif TEST_RUN.search(event.command):
            # Only a test run: reading `git status` thrice between edits is not waste.
            self._rerun(event)

    def say(self, event: Event) -> None:
        self.last_said = event

    def ending(self) -> None:
        """Judge the last thing the agent said, once the session's events are read."""
        said = self.last_said
        if self.dispatched and said and HANDED_BACK.search(said.text):
            self.note("handed-back", "a dispatched session ended on a decision for nobody", said)

    def _rerun(self, event: Event) -> None:
        key = normalize(event.command.split("|", 1)[0])[:SNIPPET]
        self.unchanged[key] = self.unchanged.get(key, 0) + 1
        if self.unchanged[key] == REPEATS:
            self.note(
                "rerun-unchanged", "a session re-ran the same tests with no edit between", event
            )

    def succeeded(self, event: Event) -> None:
        """A call that exited 0 still failed if it was a test run or a git call reporting
        a missing environment: `| tail` hides pytest's exit code."""
        command = self.calls.get(event.call_id, "")
        if not SEES_ENVIRONMENT.search(command) or READS_HARNESS_TEXT.search(command):
            return
        cls, what = _result_class(event.text)
        if cls == "environment":
            self.note(cls, what, replace(event, command=command))

    def failed(self, event: Event) -> None:
        """A failed call only: a file that merely quotes an error is not one."""
        command = self.calls.get(event.call_id, "")
        event = replace(event, command=command)
        if not READS_HARNESS_TEXT.search(command):
            self.note(*_result_class(event.text), event)
        # A test run failing again is the work of fixing it, not a wasted retry.
        if command and not TEST_RUN.search(command):
            self.failures.setdefault(normalize(command)[:SNIPPET], []).append(event)


def _read(events: Iterable[Event]) -> _Session:
    """Every event through the per-event detectors, in order."""
    session = _Session()
    handlers = {"user": session.user, "call": session.call, "say": session.say}
    for event in events:
        if event.kind == "result":
            (session.failed if event.error else session.succeeded)(event)
        elif event.kind in handlers:
            handlers[event.kind](event)
    return session


def detect(events: Iterable[Event]) -> list[tuple[str, str, Event]]:
    """`(class, what, event)` for every friction in one session's events, each once."""
    session = _read(events)
    for what, runs in session.failures.items():
        if len(runs) >= REPEATS:
            session.note("repeat-failure", f"x{len(runs)} {what}", runs[-1])
    session.ending()
    return [(cls, what, event) for (cls, what), event in session.found.items()]


# --- the harvest --------------------------------------------------------------------------


def session_findings(
    path: Path, chunk: st.Chunk, cwd: str, workspace_root: Path
) -> list[fix_findings.Finding]:
    """What one transcript's new rows show, as findings; none for a session outside the workspace."""
    if not _under(cwd, workspace_root):
        return []
    agent = "codex" if st.is_codex(path) else "claude"
    project = harness_events.project_name(Path(cwd))
    return [
        fix_findings.Finding(
            cls,
            project,
            what,
            evidence=f"{path}#L{event.line}",
            command=_said(cls, event, cwd),
            event=fix_findings.FRICTION,
            agent=agent,
        )
        for cls, what, event in detect(st.events(path, chunk.rows))
    ]


def _said(cls: str, event: Event, cwd: str) -> str:
    """What rides in a finding's `command`: the call, or a complaint whole with the tree
    it was said in -- the detail is a snippet, and a sweep parsed a 5,672-line transcript
    to read one, then spent 5 calls learning that session had its fix open already."""
    if cls != "user-frustration":
        return event.command[:300]
    return f"said in {cwd}: {' '.join(ANSI.sub('', event.text).split())[:1500]}"


def _under(cwd: str, root: Path) -> bool:
    if not cwd:
        return False
    try:
        Path(cwd).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


# --- re-judging what was filed ------------------------------------------------------------

# The classes a whole session decides -- a repeat, a rerun, how it ended, who was there to
# answer. Every other class is one event's alone, so today's detectors can re-judge it.
WHOLE_SESSION = frozenset(
    {"repeat-failure", "rerun-unchanged", "handed-back", "asked-user", "user-frustration"}
)


def outdated(items: Iterable[fix_findings.triage.Item]) -> list[tuple[str, str]]:
    """`(id, why)` of every friction row today's detectors would no longer file.

    A detector fixed on the default branch left its rows open, and a sweep spent ~13 calls
    re-proving them (d677ea57). Each per-event row is re-read at its own transcript line;
    it counts as outdated only when that line still holds the call the row names -- a
    transcript first read from its end numbers from there -- so a transcript gone, a line
    moved or a whole-session class is left open rather than guessed at.
    """
    sessions: dict[str, tuple[_Session, dict[int, set[str]]] | None] = {}
    found = []
    for item in items:
        cls = item.detail.split(":", 1)[0]
        path, _, line = item.fields.get("evidence", "").rpartition("#L")
        if item.event != fix_findings.FRICTION or cls in WHOLE_SESSION or not line.isdigit():
            continue
        if path not in sessions:
            sessions[path] = _whole(Path(path))
        read = sessions[path]
        if read is None:
            continue
        session, commands = read
        if item.fields.get("command", "") not in commands.get(int(line), set()):
            continue
        if cls not in session.at.get(int(line), set()):
            found.append(
                (item.id, f"{cls} no longer fires on {path}#L{line} with today's detectors")
            )
    return found


def _whole(path: Path) -> tuple[_Session, dict[int, set[str]]] | None:
    """A transcript read whole: its detectors' verdicts, and each line's command as the
    ledger kept it (`""` for none). None when it is gone."""
    if not path.is_file():
        return None
    events = st.events(path, st.read_new(path, 0, 0).rows)
    calls: dict[str, str] = {}
    commands: dict[int, set[str]] = {}
    for event in events:
        if event.kind == "call":
            calls[event.call_id] = event.command
        command = calls.get(event.call_id, "") if event.kind in ("call", "result") else ""
        kept = harness_events.clean(command[:300], harness_events.limit_for("command"))
        commands.setdefault(event.line, set()).add(kept if command else "")
    return _read(events), commands


def _load_cursor(path: Path) -> dict[str, dict]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _start_of(path: Path, now: _dt.datetime) -> tuple[int, int]:
    """Where a transcript never seen before is read from: the top when it is recent,
    else its end. Not counting an old file's lines keeps the first pass cheap; if one is
    ever resumed, its evidence lines count from where this started reading."""
    try:
        stat = path.stat()
    except OSError:
        return 0, 0
    if now - _dt.datetime.fromtimestamp(stat.st_mtime, _dt.UTC) <= LOOKBACK:
        return 0, 0
    return stat.st_size, 0


def harvest(
    workspace_root: Path, cursor_path: Path, now: _dt.datetime, paths: list[Path] | None = None
) -> list[fix_findings.Finding]:
    """Every new friction since the cursor, across every transcript; the cursor moves on.

    The cursor keeps each transcript's working directory too, because only a Codex
    session's first line names it and a later read starts past that line.
    """
    cursor = _load_cursor(cursor_path)
    found: list[fix_findings.Finding] = []
    for path in st.transcripts() if paths is None else paths:
        seen = cursor.get(str(path))
        seen = seen if isinstance(seen, dict) else {}
        offset, line = (
            (int(seen.get("offset", 0)), int(seen.get("line", 0))) if seen else _start_of(path, now)
        )
        chunk = st.read_new(path, offset, line)
        cwd = str(seen.get("cwd", "")) or next(
            (st.cwd_of(r) for _, r in chunk.rows if st.cwd_of(r)), ""
        )
        found.extend(session_findings(path, chunk, cwd, workspace_root))
        cursor[str(path)] = {"offset": chunk.offset, "line": chunk.line, "cwd": cwd}
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(json.dumps(cursor, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return found


if __name__ == "__main__":
    # A dry run for calibrating the detectors: what a first harvest would file, with no
    # cursor written and nothing recorded.
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description="Print the friction a harvest would file.")
    parser.add_argument("--root", type=Path, required=True, help="the workspace root")
    parser.add_argument("--days", type=float, default=LOOKBACK.days)
    args = parser.parse_args()
    LOOKBACK = _dt.timedelta(days=args.days)
    with tempfile.TemporaryDirectory() as scratch:
        for finding in harvest(args.root, Path(scratch) / CURSOR_NAME, _dt.datetime.now(_dt.UTC)):
            print(f"{finding.project:14} {finding.headline[:120]}  <- {finding.evidence}")
