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
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_findings
import session_transcripts as st
import sweep

Event = st.Event
harness_events = fix_findings.harness_events

CURSOR_NAME = "friction-cursor.json"
LOOKBACK = _dt.timedelta(days=3)
# A repeated failure: the same command failing this many times in one session.
REPEATS = 3
SNIPPET = 90

# Text a failed tool call carries when something outside the work refused or was
# missing. Each pattern names the class it files under; the first match wins.
#
# Claude Code's own worktree isolation guard ("session is isolated in the worktree",
# "too complex to verify") is deliberately absent. `.claude/rules/engineering.md` says
# no setting and nothing in devkit changes what it accepts, and already carries the
# spellings that get past it; filed here, every refusal reopened one group that each
# sweep could only retire again with that same note.
RESULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
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
# Claude Code's own refusal of a foreground `sleep`, which names the right wait in the
# same message. Absent from `blocked-call` for the isolation guard's reason, and it
# retracts its call's `poll` too: a refused sleep waited for nothing, so the one refusal
# was filed as two groups neither of which devkit could fix (9854b541, 363d8be7).
SLEEP_GUARD = re.compile(r"<tool_use_error>Blocked: sleep \d+ followed by")
# The rewrite a call that sets Git Bash's own conversion switches was measuring, beside the
# setting that stops it: the probe that put `MSYS2_ARG_CONV_EXCL` in the agent env was
# filed as the defect it measured (d71a2caf), as `BYTE_DUMP` is for a heredoc's probe.
REWRITTEN_REV = re.compile(r"^ambiguous argument", re.I)
PATH_CONVERSION_PROBE = re.compile(r"\bMSYS2_ARG_CONV_EXCL=|\bMSYS_NO_PATHCONV=")
WAIT_TOOLS = frozenset({"Monitor"})

# An odd count of any of these before a match on its line means the match is quoted.
QUOTE_MARKS = ("'", '"', "`")
# A backslash-escaped character, which opens and closes nothing: the `\"` inside a test's
# string literal is still inside it (83496ae4, a diff of this detector's own tests).
ESCAPED = re.compile(r"\\.")
# So is a match a failed assertion reports: pytest echoing a test's expected text.
ASSERTION = re.compile(r"\bassert\b|AssertionError")

# Commands that are friction whatever they return.
NO_VERIFY = re.compile(r"\bgit\b[^\n]*--no-verify")
COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("no-verify", NO_VERIFY),
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
    # since only the sessions that noticed the damage reported it -- so every write that
    # *can* be damaged is, noticed or not: `damageable_heredoc` holds it to a body with a
    # doubled backslash, the one spelling the tool collapses. One without is delivered
    # intact -- no backslash at all (b935e421) or only single ones, like the `\n` in an
    # f-string (46a1578d) -- and filing it left a sweep nothing to retire it with but
    # "no defect".
    (
        "heredoc-write",
        re.compile(
            r"(?:^|[;&|]\s*)(?:cat\s[^\n<]*>|tee\s)[^\n<]*<<|"
            r"<<-?\s*['\"]?\w+['\"]?\n(?=[\s\S]*?(?:\.write_text\(|\.write\(|open\([^)\n]*,\s*(?:mode\s*=\s*)?['\"][wa]))",
            re.M,
        ),
    ),
)
# A heredoc's body: from the line after `<<TAG` to the line that is only `TAG`, or to the
# end of an unterminated one.
HEREDOC_BODY = re.compile(
    r"<<-?\s*(['\"]?)(?P<tag>\w+)\1[^\n]*\n(?P<body>[\s\S]*?)(?:^[ \t]*(?P=tag)[ \t]*$|\Z)",
    re.M,
)
# A byte dump in command position: a heredoc read back this way in the same command is a
# measurement of what the Bash tool does to one, not a file the work depends on
# (ccde706b, the probe that established the doubled-backslash rule above).
BYTE_DUMP = re.compile(r"(?:^|[;&|]\s*)(?:od|xxd|hexdump|Format-Hex)\b", re.M)
# A directory that is a scratch repository: a `tmp*` or `temp` segment, or the shell's
# temp variable. A `--no-verify` there commits a fixture that nothing gates (7ce3ea59, a
# release repro under the job's `tmp/`); `templates/` is not one.
SCRATCH_DIR = re.compile(
    r"(?:^|[\\/])(?:tmp\w*|temp)(?:[\\/]|$)|\$(?:env:)?\{?(?:TMP|TEMP|TMPDIR)\b", re.I
)
GIT_C = re.compile(r"\bgit\s(?:[^\n]*?\s)?-C\s+(\S+)")

# A command reading back text the harness wrote, which quotes the very patterns above:
# the triage log, the ledger, a transcript, a friction file. Its output is never friction.
READS_HARNESS_TEXT = re.compile(
    r"harness-triage\.log|harness-events[^\s'\"]*\.log|\.jsonl\b|friction[^\s'\"/\\]*\.md"
)
# A command whose output is read for an environment failure even when it exited 0.
SEES_ENVIRONMENT = re.compile(r"(?:^|[;&|]\s*)(?:\S*python\S*\s+-m\s+pytest|pytest|git)\b", re.M)
# A quoted argument, whose `|`, `;` and newlines separate no statements: the `\|pytest` of
# `grep -n "full-suite\|pytest"` read as a pytest in command position, and the file it
# grepped quoted "No module named pytest" (0d422fe7).
QUOTED_ARGUMENT = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
SEPARATOR = re.compile(r"[;&|\n]")
# The statements of a command line; within one, only what leads its pipeline prints.
STATEMENT = re.compile(r"&&|\|\||[;\n]")
# Git's own voice. Everything else these subcommands print is the repository's text -- a
# diff, a blob, a log -- which quotes an error as readily as a test fixture does
# (83496ae4). Not `commit` or `push`: what they print besides is a hook's, and a hook's
# "No module named" is the environment. `status` prints only the tree's own paths, and
# leads the diff a ship step reads (5453c7ae).
GIT_DIAGNOSTIC = re.compile(r"^(?:fatal|error|warning): .*$", re.M)
GIT_READERS = frozenset(
    {"diff", "show", "log", "blame", "grep", "cat-file", "format-patch", "status"}
)
# Statements that print nothing of their own, so they leave a git read a git read.
SILENT = frozenset({"cd", "pushd", "popd"})

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
# Files every test in a suite reads: the dependency set, the lock that pins it, the
# runner's own configuration. The first test run after a change to one is targeted at
# the whole suite, because the whole suite is what the change touches -- retired as that
# four times before the detector learned it (7458ed23 a pytest `addopts`, 77e3c01d a
# library's dependency floors and its relock).
SUITE_WIDE_FILE = re.compile(
    r"(?:^|[\\/])(?:pyproject\.toml|uv\.lock|poetry\.lock|setup\.(?:cfg|py)|tox\.ini|"
    r"pytest\.ini|conftest\.py|requirements[\w.-]*\.txt|package(?:-lock)?\.json|"
    r"pnpm-lock\.yaml|yarn\.lock|(?:vitest|jest)\.config\.\w+)$"
)
# The same change made by a command: a relock, a dependency added or removed. Not
# `uv sync`, which installs what the lock already said.
SUITE_WIDE_COMMAND = re.compile(
    r"(?:^|&&?|\|\|?|;)\s*(?:(?:uv|poetry)\s+(?:lock|add|remove)|"
    r"(?:npm|pnpm|yarn)\s+(?:add|remove|uninstall))\b",
    re.M,
)
# A file a Codex `apply_patch` names, whose patch rides in the command.
PATCHED_FILE = re.compile(r"^\*\*\* (?:Update|Add) File: (.+)$", re.M)

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


def command_position(command: str) -> str:
    """`command` with every separator inside a quoted argument blanked, so a pattern
    anchored on one finds only what the shell would run. Same length, and the quoted
    text is otherwise kept: `pytest "tests/x.py"` still names its file."""
    return QUOTED_ARGUMENT.sub(lambda quoted: SEPARATOR.sub(" ", quoted.group()), command)


def runs_tests(command: str) -> bool:
    """`command` runs a test suite in command position."""
    return bool(TEST_RUN.search(command_position(command)))


def full_suite(rest: str) -> bool:
    """A test command's arguments select no subset: they name a suite root, or nothing
    narrower. pytest runs the union of its paths, so a root named beside a file is still
    the whole root (0926-18: 1,388 tests, twice); only a selector narrows it."""
    words = [token.strip("'\"") for token in rest.split()]
    if any(word.split("=", 1)[0] in NARROWING_FLAGS for word in words):
        return False
    if any(word.replace("\\", "/") in SUITE_ROOTS for word in words):
        return True
    for word in words:
        if word.startswith("-"):
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


def damageable_heredoc(command: str) -> bool:
    """A heredoc body in `command` carries a doubled backslash: the one thing the Bash
    tool alters. It collapses each `\\\\` to `\\` and leaves a lone `\\n`, `\\t` or `\\s` as
    written, which a write through the tool on 2026-09-26 confirmed byte for byte.

    Not when the same command dumps the bytes back: that is the probe that confirmed it."""
    if BYTE_DUMP.search(command):
        return False
    return any("\\\\" in found.group("body") for found in HEREDOC_BODY.finditer(command))


def scratch_only(command: str) -> bool:
    """Every `--no-verify` git statement in `command` runs in a scratch repository: after
    a `cd` into one, or with `git -C` naming one."""
    where, verdicts = "", []
    for part in STATEMENT.split(command):
        words = [word.strip("'\"") for word in part.split()]
        if words and words[0] in ("cd", "pushd"):
            where = words[1] if len(words) > 1 else ""
        elif NO_VERIFY.search(part):
            target = GIT_C.search(part)
            verdicts.append(bool(SCRATCH_DIR.search(target[1].strip("'\"") if target else where)))
    return bool(verdicts) and all(verdicts)


def changes_the_suite(event: Event) -> bool:
    """`event` changes what every test reads: it edits a `SUITE_WIDE_FILE`, or relocks.
    `Read` names a `file_path` too, and reading the lock changes nothing."""
    edited = event.path if event.tool in EDIT_TOOLS else ""
    paths = [edited, *(found.strip() for found in PATCHED_FILE.findall(event.command))]
    return any(SUITE_WIDE_FILE.search(path) for path in paths if path) or bool(
        SUITE_WIDE_COMMAND.search(event.command)
    )


def _command_classes(command: str, suite_changed: bool = False) -> Iterator[tuple[str, str]]:
    """What `command` is friction as; a whole-suite test run is not, while `suite_changed`
    says no run has checked a suite-wide change yet. The push gate still is."""
    for cls, pattern in COMMAND_PATTERNS:
        if cls == "heredoc-write" and not damageable_heredoc(command):
            continue
        if cls == "no-verify" and scratch_only(command):
            continue
        if pattern.search(command):
            yield cls, COMMAND_DETAIL[cls]
    if suite_changed:
        return
    runs = TEST_RUN.finditer(command_position(command))
    if any(full_suite(run.group("rest") or "") for run in runs):
        yield "full-suite", COMMAND_DETAIL["full-suite"]


def _quoted(text: str, at: int) -> bool:
    """The match at `at` sits inside a string its line opened: a regex's source, a diff
    of the rule's prose, pytest echoing an assertion's operands. That quotes an error
    rather than having one -- seven groups in the first supervised rehearsal."""
    line = ESCAPED.sub("", text[text.rfind("\n", 0, at) + 1 : at])
    return any(line.count(mark) % 2 for mark in QUOTE_MARKS) or bool(ASSERTION.search(line))


def _result_class(text: str, command: str = "") -> tuple[str, str]:
    """The class and snippet a failed call's output files under; `("", "")` for none."""
    text = ANSI.sub("", text)
    probe = bool(PATH_CONVERSION_PROBE.search(command))
    for cls, pattern in RESULT_PATTERNS:
        for found in pattern.finditer(text):
            if _quoted(text, found.start()) or (probe and REWRITTEN_REV.match(found.group())):
                continue
            return cls, normalize(text[found.start() : found.start() + SNIPPET])
    return "", ""


def _complaint(event: Event, spoken_before: int) -> str:
    """What the user objected to, or "" -- never the opening message, which is the task."""
    if spoken_before == 0 or event.text.lstrip().startswith("<"):
        return ""
    return _snippet(event.text, FRUSTRATION) if FRUSTRATION.search(event.text) else ""


def git_reads_only(command: str) -> bool:
    """Every statement in `command` is git printing the repository's text, beside any that
    print nothing at all: a `cd` into the tree first is still only git talking (72e04231)."""
    statements = [part.split("|", 1)[0].split() for part in STATEMENT.split(command)]
    statements = [words for words in statements if words and words[0] not in SILENT]
    return bool(statements) and all(
        words[0] == "git"
        and next((w for w in words[1:] if not w.startswith("-")), "") in GIT_READERS
        for words in statements
    )


def environment_text(command: str, text: str) -> str:
    """The part of a call's output that can report the environment: git's diagnostics
    alone when git only printed the repository's own text."""
    return "\n".join(GIT_DIAGNOSTIC.findall(text)) if git_reads_only(command) else text


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
    # A suite-wide change no test run has checked yet (`changes_the_suite`).
    suite_changed: bool = False

    def note(self, cls: str, what: str, event: Event) -> None:
        """Keep the first event of each `(cls, what)`; an empty `what` is no finding."""
        if cls and what:
            self.found.setdefault((cls, what), event)
            self.at.setdefault(event.line, set()).add(cls)

    def retract(self, cls: str, what: str, call_id: str) -> None:
        """Drop `(cls, what)` when the call `call_id` is the event it was noted for."""
        noted = self.found.get((cls, what))
        if noted is not None and noted.call_id == call_id:
            del self.found[(cls, what)]
            self.at.get(noted.line, set()).discard(cls)

    def user(self, event: Event) -> None:
        if self.spoken == 0:
            self.dispatched = DISPATCHED in event.text
        self.note("user-frustration", _complaint(event, self.spoken), event)
        self.spoken += 1

    def call(self, event: Event) -> None:
        self.calls[event.call_id] = event.command
        if event.tool != "AskUserQuestion":
            self.last_said = None  # it went on working: that text was not how it ended
        self.suite_changed = self.suite_changed or changes_the_suite(event)
        for cls, what in _command_classes(event.command, self.suite_changed):
            # A `Monitor` until-loop is the wait Claude Code's `SLEEP_GUARD` prescribes.
            if not (cls == "poll" and event.tool in WAIT_TOOLS):
                self.note(cls, what, event)
        if runs_tests(event.command):
            self.suite_changed = False  # checked: the next whole run is the habit again
        if event.tool == "AskUserQuestion" and self.dispatched:
            self.note(
                "asked-user", "a dispatched session asked a question nobody would answer", event
            )
        if event.tool in EDIT_TOOLS or ENVIRONMENT_CHANGE.search(event.command):
            self.unchanged.clear()
        elif runs_tests(event.command):
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
        runs = SEES_ENVIRONMENT.search(command_position(command))
        if not runs or READS_HARNESS_TEXT.search(command):
            return
        cls, what = _result_class(environment_text(command, event.text), command)
        if cls == "environment":
            self.note(cls, what, replace(event, command=command))

    def failed(self, event: Event) -> None:
        """A failed call only: a file that merely quotes an error is not one."""
        command = self.calls.get(event.call_id, "")
        event = replace(event, command=command)
        if SLEEP_GUARD.search(event.text):
            self.retract("poll", COMMAND_DETAIL["poll"], event.call_id)
            return
        if not READS_HARNESS_TEXT.search(command):
            cls, what = _result_class(event.text, command)
            if cls == "environment":
                cls, what = _result_class(environment_text(command, event.text), command)
            self.note(cls, what, event)
        # A test run failing again is the work of fixing it, not a wasted retry.
        if command and not runs_tests(command):
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


# The classes the corrected session's own shipped branch settles (`Finding.settles_with`).
SETTLED_BY_THE_SESSION = frozenset({"user-frustration"})
DEFAULT_BRANCHES = frozenset({"main", "master"})
# The vendored rule that takes the suite off a session. A project without it -- one held
# back from adoption (workspace.jsonc `devkit.onHold`), or never adopted -- asked for no
# targeted run, so a whole one there is no friction: 0a4b17f3 was a data-lake session
# whose CLAUDE.md names `run-tests.py` as "the suite", filed as if it had been told not to.
SCOPE_RULE = ".claude/rules/session-scope.md"


def asks_for_targeted_runs(cwd: str, workspace_root: Path, project: str) -> bool:
    """The session's tree carries `SCOPE_RULE`; its project's checkout decides once the
    tree is gone. Neither there to read: it may well have, so the finding stands."""
    for root in (Path(cwd), workspace_root / project):
        if root.is_dir():
            return (root / SCOPE_RULE).is_file()
    return True


def task_branch(cwd: str, runner=sweep.run_windowless) -> str:
    """The branch the session's tree is on, when it is a task branch; "" otherwise.

    A default branch, a detached head, a tree already gone: nothing that a merge could
    settle, so the finding is filed open as before.
    """
    try:
        done = runner(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    branch = (done.stdout or "").strip() if done.returncode == 0 else ""
    return "" if branch in DEFAULT_BRANCHES else branch


def session_findings(
    path: Path, chunk: st.Chunk, cwd: str, workspace_root: Path, branch_of=task_branch
) -> list[fix_findings.Finding]:
    """What one transcript's new rows show, as findings; none for a session outside the workspace."""
    if not _under(cwd, workspace_root):
        return []
    agent = "codex" if st.is_codex(path) else "claude"
    project = harness_events.project_name(Path(cwd))
    found = detect(st.events(path, chunk.rows))
    if not asks_for_targeted_runs(cwd, workspace_root, project):
        found = [row for row in found if row[0] != "full-suite"]
    branch = branch_of(cwd) if any(cls in SETTLED_BY_THE_SESSION for cls, _, _ in found) else ""
    return [
        fix_findings.Finding(
            cls,
            project,
            what,
            evidence=f"{path}#L{event.line}",
            command=_said(cls, event, cwd),
            event=fix_findings.FRICTION,
            agent=agent,
            settles_with=branch if cls in SETTLED_BY_THE_SESSION else "",
        )
        for cls, what, event in found
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
