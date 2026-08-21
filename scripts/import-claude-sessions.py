#!/usr/bin/env python3
"""Native-import usage-limited Claude sessions and resume them in Codex.

Backs the workspace task "Agents: Import Limited Claude Sessions". Claude and Codex
session IDs are unrelated, so this script uses Codex's own external-agent importer—the
same migration path as the interactive ``/import`` command—then opens each resulting
Codex thread with ``codex resume``.

Only top-level Claude transcripts whose final parent assistant record is an account,
session, weekly, spend, or usage-limit HTTP 429 are selected. The native importer owns
deduplication and records the Claude-source-to-Codex-thread mapping; no transcript copy,
synthetic prompt, or private Codex rollout is written by this script.

The import RPC is currently an experimental Codex CLI surface. The script opts into it
explicitly and fails loudly if the installed CLI no longer provides it.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_COUNT = 4
RPC_TIMEOUT_SECONDS = 120.0
TITLE_MAX = 44

_LIMIT_TEXT_RE = re.compile(
    r"(?:usage limit reached|you(?:'|\u2019)?ve hit your "
    r"(?:session|weekly|individual spend|spend|usage) limit)",
    re.IGNORECASE,
)
_UNSAFE_TITLE_RE = re.compile(r'[;"\x00-\x1f]+')
_WRAPPED_RE = re.compile(r"<(command-[a-z-]+|local-command-[a-z]+|system-reminder)>", re.I)


@dataclass(frozen=True)
class Session:
    """One Claude transcript eligible for native import."""

    session_id: str
    source: Path
    cwd: Path
    prompt: str
    limit_message: str
    mtime: float


@dataclass(frozen=True)
class ImportedSession:
    """A Claude session and the native Codex thread created for it."""

    session: Session
    codex_thread_id: str


class AppServerError(RuntimeError):
    """A failed or unavailable Codex app-server migration operation."""


class AppServerClient:
    """Small newline-JSON client for the local Codex app server."""

    def __init__(self, codex: str, timeout: float = RPC_TIMEOUT_SECONDS):
        self.timeout = timeout
        self._next_id = 1
        self._messages: queue.Queue[object] = queue.Queue()
        self._process = subprocess.Popen(
            [
                codex,
                "app-server",
                "--enable",
                "external_agent_memory_import",
                "--listen",
                "stdio://",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._messages.put(None)
            return
        for line in stdout:
            try:
                self._messages.put(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
        self._messages.put(None)

    def _send(self, payload: dict) -> None:
        if self._process.poll() is not None:
            raise AppServerError(self._exit_message())
        stdin = self._process.stdin
        if stdin is None:
            raise AppServerError("Codex app server has no standard input")
        try:
            stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            stdin.flush()
        except OSError as exc:
            raise AppServerError(f"could not write to Codex app server: {exc}") from exc

    def _receive(self, predicate) -> dict:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("timed out waiting for Codex app server")
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise AppServerError("timed out waiting for Codex app server") from exc
            if message is None:
                raise AppServerError(self._exit_message())
            if isinstance(message, dict) and predicate(message):
                return message

    def _exit_message(self) -> str:
        detail = ""
        if self._process.stderr is not None:
            try:
                detail = self._process.stderr.read().strip()
            except OSError:
                pass
        suffix = f": {detail}" if detail else ""
        return f"Codex app server exited unexpectedly{suffix}"

    def request(self, method: str, params: object) -> object:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        message = self._receive(lambda item: item.get("id") == request_id)
        if "error" in message:
            raise AppServerError(f"{method} failed: {message['error']}")
        return message.get("result")

    def notification(self, method: str, import_id: str) -> dict:
        message = self._receive(
            lambda item: (
                item.get("method") == method
                and isinstance(item.get("params"), dict)
                and item["params"].get("importId") == import_id
            )
        )
        return message["params"]

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

    def __enter__(self) -> AppServerClient:
        result = self.request(
            "initialize",
            {
                "clientInfo": {"name": "devkit-claude-session-import", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        if not isinstance(result, dict):
            raise AppServerError("Codex app server returned an invalid initialize response")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def claude_sessions_root(config_dir: str | None = None) -> Path:
    """Claude's transcript root, respecting its configurable home."""
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _first_prompt(record: dict) -> str:
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return ""
    for line in _message_text(record.get("message")).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("<") and not _WRAPPED_RE.search(stripped):
            return stripped
    return ""


def is_limit_record(record: object) -> bool:
    """Whether a Claude assistant record is a subscription usage-limit ending."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return False
    if not record.get("isApiErrorMessage") or str(record.get("apiErrorStatus")) != "429":
        return False
    if str(record.get("error") or "").lower() != "rate_limit":
        return False
    return bool(_LIMIT_TEXT_RE.search(_message_text(record.get("message"))))


def parse_session(path: Path) -> Session | None:
    """Parse one transcript iff its last parent assistant turn hit a usage limit."""
    cwd = ""
    prompt = ""
    first_sidechain: bool | None = None
    last_assistant: dict | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if first_sidechain is None and "isSidechain" in record:
                    first_sidechain = bool(record["isSidechain"])
                if not cwd and record.get("cwd"):
                    cwd = str(record["cwd"])
                if not prompt:
                    prompt = _first_prompt(record)
                if record.get("type") == "assistant" and not record.get("isSidechain"):
                    last_assistant = record
    except OSError:
        return None

    if first_sidechain or not cwd or not prompt or last_assistant is None:
        return None
    if not is_limit_record(last_assistant):
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return Session(
        session_id=path.stem,
        source=path,
        cwd=Path(cwd),
        prompt=prompt,
        limit_message=_message_text(last_assistant.get("message")).strip(),
        mtime=stat.st_mtime,
    )


def collect(root: Path) -> list[Session]:
    if not root.is_dir():
        return []
    found = (parse_session(path) for path in root.glob("*/*.jsonl"))
    return [session for session in found if session is not None]


def partition(sessions: list[Session]) -> tuple[list[Session], list[Session]]:
    return (
        [session for session in sessions if session.cwd.is_dir()],
        [session for session in sessions if not session.cwd.is_dir()],
    )


def select(sessions: list[Session], count: int = DEFAULT_COUNT) -> list[Session]:
    newest = sorted(sessions, key=lambda session: session.mtime, reverse=True)[: max(count, 0)]
    return sorted(newest, key=lambda session: session.mtime)


def tab_title(session: Session) -> str:
    prompt = _UNSAFE_TITLE_RE.sub(" ", session.prompt).strip()
    if len(prompt) > TITLE_MAX:
        prompt = prompt[: TITLE_MAX - 1].rstrip() + "…"
    name = _UNSAFE_TITLE_RE.sub(" ", session.cwd.name).strip() or "session"
    return f"{name} - {prompt}"


def describe(session: Session) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(session.mtime))
    return f"{when}  {tab_title(session)}  [{session.session_id[:8]}]"


def _canonical_source(value: str | Path) -> str:
    text = str(value)
    if os.name == "nt" and text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text))


def select_detected_items(payload: object, sessions: list[Session]) -> list[dict]:
    """Keep only the selected transcripts from Codex's native detection result."""
    wanted = {_canonical_source(session.source) for session in sessions}
    selected = []
    items = payload.get("items", []) if isinstance(payload, dict) else []
    for item in items:
        if not isinstance(item, dict) or item.get("itemType") != "SESSIONS":
            continue
        details = item.get("details")
        if not isinstance(details, dict):
            continue
        matches = []
        for detected in details.get("sessions", []):
            if not isinstance(detected, dict) or not detected.get("path"):
                continue
            source = _canonical_source(str(detected["path"]))
            if source in wanted:
                matches.append(detected)
        if matches:
            filtered = dict(item)
            filtered["details"] = {**details, "sessions": matches}
            selected.append(filtered)
    return selected


def _successes(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    direct = payload.get("itemTypeResults")
    if isinstance(direct, list):
        return [
            success
            for result in direct
            if isinstance(result, dict) and result.get("itemType") == "SESSIONS"
            for success in result.get("successes", [])
            if isinstance(success, dict)
        ]
    histories = payload.get("data")
    if isinstance(histories, list):
        return [
            success
            for history in histories
            if isinstance(history, dict)
            for success in history.get("successes", [])
            if isinstance(success, dict) and success.get("itemType") == "SESSIONS"
        ]
    return []


def native_import(codex: str, sessions: list[Session]) -> list[ImportedSession]:
    """Run Codex's native Claude importer and resolve every resulting thread ID."""
    with AppServerClient(codex) as client:
        detected = client.request(
            "externalAgentConfig/detect",
            {
                "cwds": sorted({str(session.cwd) for session in sessions}),
                "includeHome": True,
                "maxSessionAgeDays": 30,
                "maxSessions": 1000,
                "migrationSource": "claude",
            },
        )
        migration_items = select_detected_items(detected, sessions)
        completed: dict = {}
        if migration_items:
            response = client.request(
                "externalAgentConfig/import",
                {
                    "migrationItems": migration_items,
                    "migrationSource": "claude",
                    "providerId": "devkit",
                    "source": "devkit",
                },
            )
            if not isinstance(response, dict) or not response.get("importId"):
                raise AppServerError("Codex importer returned no import ID")
            completed = client.notification(
                "externalAgentConfig/import/completed", str(response["importId"])
            )
            failures = [
                failure
                for result in completed.get("itemTypeResults", [])
                if isinstance(result, dict)
                for failure in result.get("failures", [])
                if isinstance(failure, dict)
            ]
            if failures:
                messages = "; ".join(str(item.get("message") or item) for item in failures)
                raise AppServerError(f"native Claude session import failed: {messages}")
        histories = client.request("externalAgentConfig/import/readHistories", None)

    successes = _successes(completed) + _successes(histories)
    targets: dict[str, str] = {}
    for success in successes:
        source = success.get("source")
        target = success.get("target")
        if source and target:
            targets.setdefault(_canonical_source(str(source)), str(target))

    imported = []
    missing = []
    for session in sessions:
        target = targets.get(_canonical_source(session.source))
        if not target:
            missing.append(session.session_id)
        else:
            imported.append(ImportedSession(session, target))
    if missing:
        raise AppServerError(
            "native importer returned no resumable Codex thread for Claude session(s): "
            + ", ".join(missing)
        )
    return imported


def wt_args(sessions: list[ImportedSession]) -> list[str]:
    """One Windows Terminal tab per native Codex thread, oldest first."""
    args = ["-w", "-1"]
    for index, imported in enumerate(sessions):
        if index:
            args.append(";")
        args += [
            "new-tab",
            "--title",
            tab_title(imported.session),
            "-d",
            str(imported.session.cwd),
            "pwsh.exe",
            "-NoLogo",
            "-NoExit",
            "-Command",
            "codex",
            "resume",
            imported.codex_thread_id,
        ]
    args += [";", "focus-tab", "-t", "0"]
    return args


def find_terminal() -> str:
    return shutil.which("wt.exe") or shutil.which("wt") or ""


def find_codex() -> str:
    return shutil.which("codex") or ""


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Native-import recent usage-limited Claude sessions and resume in Codex."
    )
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--sessions-dir", type=Path, default=None)
    parser.add_argument("--list", action="store_true", help="list candidates; change nothing")
    parser.add_argument(
        "--dry-run", action="store_true", help="show native import inputs; change nothing"
    )
    args = parser.parse_args(argv)

    if args.count < 1:
        print("import-claude-sessions: --count must be at least 1", file=sys.stderr)
        return 2

    source_root = args.sessions_dir or claude_sessions_root()
    live, orphaned = partition(collect(source_root))
    if not live:
        print(
            "import-claude-sessions: no live Claude sessions ending at a usage limit under "
            f"{source_root}",
            file=sys.stderr,
        )
        return 1

    selected = select(live, args.count)
    print(f"Native-importing {len(selected)} Claude session(s), oldest tab first:")
    for index, session in enumerate(selected, start=1):
        print(f"  {index}. {describe(session)}")

    cutoff = selected[0].mtime
    stale = [session for session in orphaned if session.mtime >= cutoff]
    if stale:
        print("\nSkipped — the original working directory is gone:")
        for session in sorted(stale, key=lambda item: item.mtime):
            print(f"  {describe(session)} in {session.cwd}")

    if args.list:
        return 0
    if args.dry_run:
        print("\nWould pass these transcripts to Codex's native Claude importer:")
        for session in selected:
            print(f"  {session.source}")
        return 0

    terminal = find_terminal()
    if not terminal:
        print("\nimport-claude-sessions: Windows Terminal (wt.exe) not found", file=sys.stderr)
        return 1
    codex = find_codex()
    if not codex:
        print("\nimport-claude-sessions: Codex CLI not found on PATH", file=sys.stderr)
        return 1

    try:
        imported = native_import(codex, selected)
    except (AppServerError, OSError) as exc:
        print(f"\nimport-claude-sessions: {exc}", file=sys.stderr)
        return 1

    print(f"\nOpening {len(imported)} native Codex thread(s) in a new Terminal window...")
    return subprocess.run([terminal, *wt_args(imported)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
