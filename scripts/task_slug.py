#!/usr/bin/env python3
"""UserPromptSubmit hook: remember what this session's task is called.

The surviving half of `branch-per-task.py`. That hook wrote the prompt's slug to a
per-*worktree* marker so `branch-on-write.py` could name the branch it cut in that
checkout. Both are gone: agent work now happens in an ephemeral box, and the thing
that names a box is `worktree-guard.py`, which fires on a PreToolUse event and so has
never seen the prompt.

The consequence was visible in every PR title. With nothing to read, the guard fell
back to `session_slug()` — `agent/ws-3f2a1b8c-0807`, a branch named after a session
id — so a list of open PRs said nothing about what any of them did.

So the slug is keyed by **session**, not by worktree. That is the only key both ends
share: the prompt arrives in a session, the box is cut for a session, and the two
events happen in different processes with different working directories (a
workspace-root session's cwd is not any checkout's).

Written under the boxes directory rather than a repo's git dir for the same reason.
It is the one location both a workspace-root session and a session inside any
checkout can compute, and it is already where the tier keeps its state.

Best-effort and **always exits 0**: this runs before every prompt, and a failure to
record a name must never cost the turn. A missing slug degrades to the old
`ws-<session>` name, which is ugly and works.

Pure helpers are unit-tested in `tests/test_task_slug.py`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_branch as tb
import worktree

SLUGS_DIR_NAME = "slugs"

# Terms that must never reach a branch name, one per line, `#` for a comment.
#
# The slug is cut from the *prompt*, and the prompt states the task — so a task whose
# whole point is a word turns that word into a branch name and then pushes it. That has
# happened: "make sure references to <licensed product> are removed" from a **public**
# repo got `agent/make-sure-references-<brandname>-0824`, which is the guard about to
# push the exact token it was asked to delete. Nothing downstream can catch it; by the
# time the branch has a name it is already what `git push` will publish.
#
# Kept beside the leases rather than in any project, and that placement is the point: a
# denylist committed to the repo it protects publishes the same word it is hiding. This
# file is machine-local state under the boxes directory, which no repo tracks.
DENY_FILE_NAME = "slug-deny.txt"

# The same list for a session that would rather not write a file: comma-separated.
DENY_ENV = "DEVKIT_SLUG_DENY"

# Session ids are UUIDs, but this value reaches the filesystem as a name, so it is
# constrained here rather than trusted. Anything outside the class is dropped, and an
# empty result is simply not recorded — never a traversal, never a write outside the
# slugs directory.
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9_-]+")
_INTERNAL_PROMPT_RE = re.compile(
    r"^\s*<(?:task-notification|system-reminder|command-[a-z-]+|local-command-[a-z]+)(?:\s|>)",
    re.I,
)


def safe_session(session: str, max_len: int = 64) -> str:
    """`session` reduced to a filename, or "" when nothing usable survives.

    Length-capped because the id is attacker-irrelevant but filesystem-relevant: a
    pathological value would otherwise produce a name no filesystem accepts, and the
    write would fail on the one path that is supposed to be free.
    """
    return _SAFE_SESSION_RE.sub("", session or "")[:max_len]


def slugs_dir(workspace_root: Path) -> Path:
    """Where per-session slugs live: beside the leases, under the boxes directory.

    Deliberately a sibling of `leases.json` rather than a directory of its own at the
    workspace root. Nothing enumerates the boxes directory by listing it — `live_boxes`
    reads the lease file and checks each recorded name — so an extra directory here is
    invisible to every existing caller.
    """
    return worktree.boxes_root(workspace_root) / SLUGS_DIR_NAME


def deny_terms(workspace_root: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """The lowercased terms `redact` must strip, from the file and the environment.

    Both sources, unioned, because they answer different questions: the file is the
    machine's standing list and survives a reboot, the variable is one session saying
    "not this word, not today" without editing anything. Neither is required, and an
    unreadable file is an empty list — this is called on the path that names a box, and
    a missing denylist must degrade to today's behaviour rather than cost the turn.
    """
    terms: list[str] = []
    source = os.environ if env is None else env
    with contextlib.suppress(OSError):
        raw = (worktree.boxes_root(workspace_root) / DENY_FILE_NAME).read_text(encoding="utf-8")
        terms += raw.splitlines()
    terms += (source.get(DENY_ENV) or "").split(",")
    seen = {term.strip().lower() for term in terms if term.strip() and not term.startswith("#")}
    return sorted(seen)


def redact(slug: str, terms: list[str]) -> str:
    """`slug` with every hyphen-separated word containing a denied term dropped.

    Substring rather than whole-word: slugification glues punctuation away, so a brand
    reaches the slug as `cloudlis` or `cloudli2` as readily as on its own, and a match
    that only fires on the bare token publishes the near-miss.

    Returns "" when nothing survives, which `record` treats as "do not name this
    session" — the guard then falls back to `ws-<session>`, ugly and safe.
    """
    if not terms:
        return slug
    kept = [word for word in slug.split("-") if word and not any(term in word for term in terms)]
    return "-".join(kept)


def record(workspace_root: Path, session: str, slug: str) -> Path | None:
    """Write `slug` as this session's task name. Returns the path, or None if not written.

    Overwrites, so the name always reflects the most recent statement of intent. That
    matters less than it did for the branch hook — the box is cut once and keeps the
    name it was cut with — but a session whose first prompt is "hi" and whose second is
    the real task should still get the real task's name if the box has not been cut yet.

    Redaction happens **here**, not in the caller, so that every writer of a session
    slug gets it and none of them has to remember to — see `DENY_FILE_NAME`. A slug
    redacted down to nothing is not recorded at all, which is the same "" the fallback
    already handles.
    """
    key = safe_session(session)
    slug = redact(slug, deny_terms(workspace_root))
    if not key or not slug:
        return None
    path = slugs_dir(workspace_root) / key
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(slug + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def read(workspace_root: Path, session: str) -> str:
    """This session's recorded slug, or "" when there is none.

    The guard's side of the contract. Every failure is "", which lands the caller on
    its own fallback name rather than on an exception inside a PreToolUse hook.
    """
    key = safe_session(session)
    if not key:
        return ""
    try:
        return (slugs_dir(workspace_root) / key).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def prune(workspace_root: Path, keep: int = 200) -> int:
    """Drop all but the `keep` most recent slug files. Returns how many went.

    A slug is written per prompt per session and read at most once per (session,
    project), so nothing ever deletes one on the normal path. They are tiny, but this
    directory would otherwise grow for the life of the workstation — and this tier
    exists on a machine that is short of disk.
    """
    try:
        files = sorted(
            (p for p in slugs_dir(workspace_root).iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0
    dropped = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            dropped += 1
        except OSError:
            pass
    return dropped


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    workspace = Path(args[args.index("--workspace") + 1]) if "--workspace" in args else None
    workspace = (workspace or worktree.DEFAULT_WORKSPACE).resolve()
    # No multi-root registry means no box tier to name anything for: a CI runner, a
    # fresh clone, anyone else's machine. Silence is correct, not an error.
    if not workspace.is_file():
        return 0

    # UTF-8, never the platform codec: the payload this reads is a *prompt*, which is
    # where non-ASCII is a certainty rather than a possibility, and the slug it derives
    # becomes a branch name. `worktree-guard.read_stdin` owns the reasoning.
    try:
        buffer = getattr(sys.stdin, "buffer", None)
        raw = (
            buffer.read().decode("utf-8", errors="replace")
            if buffer is not None
            else sys.stdin.read()
        )
    except (OSError, ValueError):
        return 0

    try:
        payload = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session = str(payload.get("session_id") or payload.get("sessionId") or "")
    prompt = tb.parse_prompt(raw)
    if _INTERNAL_PROMPT_RE.match(prompt):
        return 0
    slug = tb.slug_from_prompt(prompt)
    if record(workspace.parent, session, slug):
        prune(workspace.parent)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A UserPromptSubmit hook that raises prints a traceback into the session and
        # buys nothing: the worst outcome of failing here is an uglier branch name.
        sys.exit(0)
