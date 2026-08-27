#!/usr/bin/env python3
"""Reopen the most recently active Claude and/or Codex sessions, one tab each.

Backs the workspace-level VS Code task "Agents: Resume Recent Sessions".

**Scope-blind on purpose.** Every other agent task in this workspace starts from a
checkout: it asks which project, and the answer bounds what it can touch. Picking up
yesterday's work is the opposite shape — what you want back is the last few *sessions*,
wherever they happened to run, and a session's directory is a fact about it rather than
a question to answer. Three of the last few may be in one repo and another in an
ephemeral box that no picker lists (boxes are absent from the workspace file by design,
so `${input:project}` could not offer one). So this reads the selected agents' transcript
stores, which know every session's directory, and takes no project argument at all. When
both agents are selected, they are merged before the recency limit is applied: "last 10"
means ten sessions total, regardless of which agent owns them.

Recency is the transcript's **mtime**, not a timestamp parsed out of it: the store
appends to a session's file for as long as the session lives, so the file's mtime is its
last activity, and reading it costs one syscall instead of a megabyte of JSON. Only the
head of each transcript is parsed, for the two things the filename does not carry — the
working directory and the opening prompt, which becomes the tab title.

The tabs are laid out **oldest first**, so reading left to right walks forward through
the day.

Two kinds of transcript are deliberately skipped, and both would otherwise displace a
real session out of the requested set:

- **Claude sidechains.** A subagent's transcript is a session file like any other and is
  written more recently than the parent that spawned it. `--resume` on one reopens a
  subagent's context, which is never what "resume my last session" means.
- **A directory that is gone.** A reaped box takes its checkout with it; `--resume`
  keyed to that directory has nothing to reopen. These are reported by name rather
  than passed over silently — a session you remember working in, missing from the
  list, should say why.

**The CLIs are updated in the gap before the tabs open**, which is the one moment on
this machine when that is possible at all: an update replaces the binary a running agent
is executing, so the nightly pass in `global-tools.py` steps over any agent that is up,
and on a desk where a window is nearly always open it steps over them most nights. Here
the answer is different by construction — you are about to open the sessions, so they are
not open yet. Only the agents being resumed are touched (`--agent codex` never moves
`claude`), any that is somehow already running is still skipped, and `--no-update` opts
out. `agent_clis.py` owns the pass and the reasoning; this module owns only the moment.

It runs before `wt.exe` rather than after, and blocks: launching first would hand the new
tabs the old binary and then rewrite it underneath them. The cost is a few seconds of
"updating…" before the window appears, and it is the price of the tabs being current.
`--list` and `--dry-run` stay read-only, so neither updates anything.

Pure helpers are unit-tested in `tests/test_resume_sessions.py`; `main` is the thin
subprocess shell around them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_clis
import task_input

# The update stage, taken as an argument so a test of the launch path cannot spawn a real
# updater by forgetting to stub one. `agent_clis.run_pass` is its only production value.
AgentPass = Callable[..., agent_clis.Report]

DEFAULT_COUNT = 4
SUPPORTED_AGENTS = ("claude", "codex")

# How much of a transcript to parse. The working directory and the first prompt are both
# in its opening records; everything after that is turns, and a long session's file runs
# to megabytes. Two limits rather than one because either can be hit first: a transcript
# whose opening records are enormous pasted attachments, and one that opens with hundreds
# of tiny hook lines.
HEAD_BYTES = 262_144
HEAD_LINES = 400

TITLE_MAX = 44

# The tab title reaches wt.exe, which re-parses its own command line and treats `;` as
# the tab separator and `"` as quoting. A prompt containing either would silently
# rearrange the window, so the title is reduced to a safe character class rather than
# escaped. Control characters go for the same reason.
_UNSAFE_TITLE_RE = re.compile(r'[;"\x00-\x1f]+')

# Injected wrappers that are user-role records but not something anyone typed:
# `<command-name>` for a slash command's expansion, `<local-command-stdout>` for its
# output, `<system-reminder>` for harness context. A session whose first *typed* prompt
# is preceded by any of these must still be titled by that prompt.
_WRAPPED_RE = re.compile(r"<(command-[a-z-]+|local-command-[a-z]+|system-reminder)>", re.I)

# Codex persists these as user-role input blocks before the first thing the human
# typed. They are context, not a useful tab title.
_CODEX_CONTEXT_PREFIXES = ("# AGENTS.md instructions", "<environment_context>")


@dataclass(frozen=True)
class Session:
    """One resumable agent session."""

    agent: str  # which CLI owns the transcript and must resume it
    session_id: str  # what the chosen agent's resume command takes
    cwd: Path  # where it ran — resume semantics are directory-sensitive
    prompt: str  # its opening prompt, for the tab title
    mtime: float  # last activity, from the transcript file


# --- reading the transcript store -------------------------------------------


def sessions_root(agent: str, config_dir: str | None = None) -> Path:
    """Where the chosen agent keeps its session transcripts.

    Both CLIs honour a config-home environment variable. A machine that moved either
    store would otherwise get "no sessions found" while the sessions exist elsewhere.
    """
    if agent == "claude":
        base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
        return (Path(base) if base else Path.home() / ".claude") / "projects"
    base = config_dir or os.environ.get("CODEX_HOME", "")
    return (Path(base) if base else Path.home() / ".codex") / "sessions"


def head_records(
    path: Path, max_bytes: int = HEAD_BYTES, max_lines: int = HEAD_LINES
) -> list[dict]:
    """The opening JSON records of a transcript, as far as the read limits allow.

    Unparseable lines are skipped rather than fatal: the store is append-only and the
    last line of a *live* session's file can be half-written at the moment it is read.
    """
    records: list[dict] = []
    read = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for count, line in enumerate(handle):
                read += len(line)
                if count >= max_lines or read > max_bytes:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def is_sidechain(records: list[dict]) -> bool:
    """Whether these records open a subagent's transcript.

    Read from the FIRST record carrying the flag, not from any of them: a parent session
    that spawned a subagent has sidechain records of its own further in, and `any()` over
    the head would throw the parent away along with it.
    """
    for record in records:
        if "isSidechain" in record:
            return bool(record["isSidechain"])
    return False


def _block_text(message: object) -> str:
    """The typed text of a user message; "" for tool results and other non-prose."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def claude_first_prompt(records: list[dict]) -> str:
    """The first thing a human typed in this session, or "" if there is none.

    "" is what marks a session as **not worth resuming**: a transcript with no typed
    prompt is one that was opened and abandoned, and it is written recently enough to
    take one of the four slots from a session that has something in it.
    """
    for record in records:
        if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
            continue
        for line in _block_text(record.get("message")).splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("<") and not _WRAPPED_RE.search(stripped):
                return stripped
    return ""


def parse_claude_session(path: Path) -> Session | None:
    """One Claude transcript as a `Session`, or None when it is not resumable.

    None covers every reason at once — unreadable, a sidechain, no working directory
    recorded, never prompted — because each means the same thing to the caller and none
    is worth a message of its own. A *missing directory* is the one exception, handled a
    level up where it can be reported by name.
    """
    records = head_records(path)
    if not records or is_sidechain(records):
        return None
    cwd = next((str(r.get("cwd") or "") for r in records if r.get("cwd")), "")
    prompt = claude_first_prompt(records)
    if not cwd or not prompt:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    # The filename, not the records' `sessionId`: the filename is the key `--resume`
    # looks up, and a transcript copied or renamed by hand would disagree.
    return Session(agent="claude", session_id=path.stem, cwd=Path(cwd), prompt=prompt, mtime=mtime)


def codex_first_prompt(records: list[dict]) -> str:
    """The first human input in a Codex rollout, excluding injected workspace context."""
    for record in records:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        if payload.get("role") != "user" or not isinstance(payload.get("content"), list):
            continue
        for block in payload["content"]:
            if not isinstance(block, dict) or block.get("type") != "input_text":
                continue
            text = str(block.get("text") or "").strip()
            if not text or text.startswith(_CODEX_CONTEXT_PREFIXES):
                continue
            return next((line.strip() for line in text.splitlines() if line.strip()), "")
    return ""


def parse_codex_session(path: Path) -> Session | None:
    """One Codex rollout as a `Session`, or None when it is not resumable."""
    records = head_records(path)
    meta = next((r.get("payload") for r in records if r.get("type") == "session_meta"), None)
    if not isinstance(meta, dict):
        return None
    session_id = str(meta.get("id") or meta.get("session_id") or "")
    cwd = str(meta.get("cwd") or "")
    prompt = codex_first_prompt(records)
    if not session_id or not cwd or not prompt:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return Session(agent="codex", session_id=session_id, cwd=Path(cwd), prompt=prompt, mtime=mtime)


def collect(root: Path, agent: str) -> list[Session]:
    """Every resumable session for the chosen agent, in no particular order."""
    if not root.is_dir():
        return []
    if agent == "claude":
        found = (parse_claude_session(path) for path in root.glob("*/*.jsonl"))
    else:
        found = (parse_codex_session(path) for path in root.rglob("rollout-*.jsonl"))
    return [session for session in found if session is not None]


# --- choosing which to reopen ------------------------------------------------


def partition(sessions: list[Session]) -> tuple[list[Session], list[Session]]:
    """Split into (live, orphaned) by whether each session's directory still exists."""
    live = [s for s in sessions if s.cwd.is_dir()]
    orphaned = [s for s in sessions if not s.cwd.is_dir()]
    return live, orphaned


def select(sessions: list[Session], count: int = DEFAULT_COUNT) -> list[Session]:
    """The `count` most recent sessions, returned **oldest first**.

    Two sorts rather than one: recency decides *which*, chronology decides the order
    they are laid out in. A single pass would give the newest session the leftmost tab.
    """
    newest = sorted(sessions, key=lambda s: s.mtime, reverse=True)[: max(count, 0)]
    return sorted(newest, key=lambda s: s.mtime)


def tab_title(session: Session) -> str:
    """`<directory> - <opening prompt>`, trimmed to fit a tab and safe for wt.exe."""
    prompt = _UNSAFE_TITLE_RE.sub(" ", session.prompt).strip()
    if len(prompt) > TITLE_MAX:
        prompt = prompt[: TITLE_MAX - 1].rstrip() + "…"
    name = _UNSAFE_TITLE_RE.sub(" ", session.cwd.name).strip() or "session"
    return f"{name} - {prompt}" if prompt else name


def describe(session: Session) -> str:
    """One report line: when it was last active, where it ran, and what it was about."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(session.mtime))
    return f"{when}  {tab_title(session)}  [{session.agent}:{session.session_id[:8]}]"


# --- launching ---------------------------------------------------------------


def resume_args(agent: str, session_id: str) -> list[str]:
    """The agent-specific CLI syntax for resuming one interactive session."""
    if agent == "claude":
        return [agent, "--resume", session_id]
    return [agent, "resume", session_id]


def wt_args(sessions: list[Session], agent: str | None = None) -> list[str]:
    """The wt.exe argument list: one tab per session, each resuming it in its own cwd.

    `-w -1` forces a new window rather than tabs bolted onto whichever one has focus.
    The `;` separators are their own tokens because wt parses its command line itself:
    joined into one string they are swallowed by the outer shell, and every tab after
    the first is lost.
    """
    args = ["-w", "-1"]
    for index, session in enumerate(sessions):
        if index:
            args.append(";")
        args += [
            "new-tab",
            "--title",
            tab_title(session),
            "-d",
            str(session.cwd),
            # -NoExit keeps the tab alive after the agent exits, so a session that dies
            # immediately still leaves its error on screen. Everything after -Command is
            # concatenated into the one command line the tab runs.
            "pwsh.exe",
            "-NoLogo",
            "-NoExit",
            "-Command",
            *resume_args(agent or session.agent, session.session_id),
        ]
    args += [";", "focus-tab", "-t", "0"]
    return args


def shell_lines(sessions: list[Session], agent: str | None = None) -> list[str]:
    """The same work as one command per session, for a machine with no Windows Terminal."""
    return [
        f'cd "{session.cwd}" && {" ".join(resume_args(agent or session.agent, session.session_id))}'
        for session in sessions
    ]


def find_terminal() -> str:
    """Path to wt.exe, or "" when Windows Terminal is not installed."""
    return shutil.which("wt.exe") or shutil.which("wt") or ""


# --- entrypoint -------------------------------------------------------------


def _parse_agents(value: str) -> tuple[str, ...]:
    """A CLI agent name or the checkbox picker's comma-separated selection."""
    selected = SUPPORTED_AGENTS if value == "any" else tuple(value.split(","))
    if not selected or any(agent not in SUPPORTED_AGENTS for agent in selected):
        choices = ", ".join((*SUPPORTED_AGENTS, "any"))
        raise argparse.ArgumentTypeError(f"choose one of {choices}, or select both agents")
    # The checkbox extension can return either click order. Canonical order makes the
    # report stable; the global mtime sort, not this order, decides which sessions win.
    return tuple(agent for agent in SUPPORTED_AGENTS if agent in selected)


def update_clis(agents: Sequence[str], agent_pass: AgentPass | None = None) -> None:
    """Run the update pass for `agents` and print its account of itself.

    Reported rather than returned, and never fatal: a failed update is a stale CLI, not a
    reason to withhold the sessions somebody asked for. The exit code belongs to `wt.exe`.
    """
    print(f"\nUpdating {'/'.join(agent.title() for agent in agents)} before opening...")
    report = (agent_pass or agent_clis.run_pass)(agent_clis.select_agents(agents), yes=True)
    for line in report.lines:
        print(line)


def main(argv: list[str] | None = None, agent_pass: AgentPass | None = None) -> int:
    # Prompts carry arrows, dashes and emoji; a Windows console is cp1252 and would
    # raise UnicodeEncodeError mid-report rather than printing the sessions it found.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # Before argparse: `--agent` has a `type=` that would reject the literal
    # `${input:resumeAgents}` a dismissed checkbox list leaves behind, turning a cancel
    # into a usage error. This task carries no wrapper, so the guard has to be here.
    dismissed = task_input.cancelled_inputs(sys.argv[1:] if argv is None else argv)
    if dismissed:
        print(task_input.cancel_report("resume-sessions", dismissed))
        return 0

    parser = argparse.ArgumentParser(
        description="Reopen the most recently active Claude and/or Codex sessions."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"how many sessions to reopen (default {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=None,
        help="transcript store to read (default: each selected agent's config home)",
    )
    parser.add_argument(
        "--agent",
        dest="agents",
        type=_parse_agents,
        default=("claude",),
        metavar="AGENT",
        help="claude, codex, any, or a comma-separated checkbox selection (default claude)",
    )
    parser.add_argument(
        "--list", action="store_true", help="print what would be reopened, launch nothing"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the wt.exe command line, launch nothing"
    )
    parser.add_argument(
        "--no-update",
        dest="update",
        action="store_false",
        help="skip the CLI update pass that otherwise runs just before the tabs open",
    )
    args = parser.parse_args(argv)

    if args.count < 1:
        print("resume-sessions: --count must be at least 1", file=sys.stderr)
        return 2

    roots = {agent: args.sessions_dir or sessions_root(agent) for agent in args.agents}
    sessions = [session for agent, root in roots.items() for session in collect(root, agent)]
    live, orphaned = partition(sessions)
    if not live:
        locations = ", ".join(str(root) for root in roots.values())
        print(f"resume-sessions: no resumable sessions found under {locations}", file=sys.stderr)
        return 1

    selected = select(live, args.count)
    owners = "/".join(agent.title() for agent in args.agents)
    print(f"Resuming {len(selected)} {owners} session(s), oldest tab first:")
    for index, session in enumerate(selected, start=1):
        print(f"  {index}. {describe(session)}")

    # Only the orphans recent enough to have been candidates. Every reaped box in the
    # store's history would otherwise be listed, and that is most of it.
    cutoff = selected[0].mtime
    stale = [s for s in orphaned if s.mtime >= cutoff]
    if stale:
        print("\nSkipped — the directory is gone (reaped box, moved checkout):")
        for session in sorted(stale, key=lambda s: s.mtime):
            print(f"  {describe(session)} in {session.cwd}")

    if args.list:
        return 0

    terminal = find_terminal()
    if not terminal:
        print("\nWindows Terminal (wt.exe) not found; run these yourself:", file=sys.stderr)
        for line in shell_lines(selected):
            print(f"  {line}", file=sys.stderr)
        return 0 if args.dry_run else 1

    command = wt_args(selected)
    if args.dry_run:
        print("\nwt.exe " + subprocess.list2cmdline(command))
        return 0
    if args.update:
        update_clis(args.agents, agent_pass)
    print(f"\nOpening {len(selected)} tab(s) in a new Windows Terminal window...")
    return subprocess.run([terminal, *command], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
