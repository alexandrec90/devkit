#!/usr/bin/env python3
"""The channels back from a session: the stamp a dispatch leaves, and what came of it.

The fix pass learned a session's outcome only from the gate. A fixer that stopped with
"cannot be done", or a background session that died on a permission prompt, left a
ledger entry and nothing else, and the record read "already dispatched" for as long as
the entry stood. Files under the worktree's ignored `logs/` close that:

- `logs/fix-dispatch.json`, written by the pass when it opens a session in a worktree:
  the ledger key and problem the dispatch was recorded under, so anything from that
  tree can be matched back to the decision it answers, whatever branch the tree is on.
- `logs/fix-blocked.md`, written by the fixer instead of an intent when it cannot
  finish. The pass marks the ledger entry blocked and escalates it as a finding
  (`fix_budget.budget`), then files the report away so it is read once.
- `logs/friction.md`, which any session may write: one line per thing the harness cost
  it turns. The pass files each line on the harness-defect ledger.
- No outcome at all: a stamped tree with no intent, no report and a transcript gone
  quiet (`session_state`) is a session that died, which frees its dispatch at once.

`agent_trees` is the walk both this and `ship_intent.find_intents` make: every
worktree of every registered checkout, through `git worktree list`, so a box, a
`--worktree` checkout and the static checkout on a task branch are all found the same
way. Stdlib plus this repo's modules. Tested in `tests/test_fix_reports.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_worktrees as aw
import sweep

STAMP_FILE = Path("logs") / "fix-dispatch.json"
# What a background launch was given and answered (`record_launch`): the evidence of a
# session that never started, which otherwise lived only in the pass's own output.
LAUNCH_FILE = Path("logs") / "fix-launch.json"
BLOCKED_FILE = Path("logs") / "fix-blocked.md"
FRICTION_FILE = Path("logs") / "friction.md"
# The mark of a tree the fix pass cut for a fixer, which makes its PR merge itself once
# green (`ship_intent.labels_for`). Only what the pass authored carries it.
ORIGIN_FILE = Path("logs") / "fix-origin"
CHECKOUT = Path(__file__).resolve().parents[1]
# The message a refused intent was being shipped with, set aside by the pass at
# dispatch (`ship_intent.set_aside`) where the fixer it sent can reuse it.
REFUSED_FILE = Path("logs") / "ship-intent.refused.md"
# What a session leaves when it is done one way or another. The intent names are
# `ship_intent`'s; spelled here because that module imports this one.
OUTCOME_FILES = (
    Path("logs") / "ship-intent.md",
    Path("logs") / "ship-intent.shipped.md",
    Path("logs") / "ship-state.json",
    BLOCKED_FILE,
    Path("logs") / "fix-blocked.filed.md",  # the report once the pass has read it
)

# How much of a report reaches the record: one line's worth, not the essay.
REASON_LIMIT = 400
# A launch's diagnosis is a status page, not a line: `claude daemon status` is ~800 chars.
DIAGNOSIS_LIMIT = 2000

# The refusals of Claude Code's `claude --worktree` isolation guard, in its own words.
CLAUDE_CODE_GUARD = re.compile(
    r"too complex to verify|stays? inside the worktree|cannot be shown not to be git|"
    r"isolated in the worktree|worktree isolation guard",
    re.I,
)

# A dispatched session with no transcript this long after its stamp never started; one
# whose transcript has been quiet this long, with nothing left behind, has ended. Both
# well past a permission prompt a person might still answer in a tab.
START_GRACE = _dt.timedelta(minutes=30)
QUIET_AFTER = _dt.timedelta(minutes=90)
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

GitFor = Callable[[Path], Callable[..., object]]


def stamp(
    tree: Path,
    key: str,
    what: str,
    now: _dt.datetime | None = None,
    problem: str = "",
    agent: str = "claude",
) -> Path:
    """Mark the tree with the key this dispatch is recorded under.

    A report left by an earlier session in the same tree is cleared with it: read
    against the new key, it would mark this dispatch blocked before its session had
    started.
    """
    when = (now or _dt.datetime.now(_dt.UTC)).isoformat(timespec="seconds")
    path = tree / STAMP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    (tree / BLOCKED_FILE).unlink(missing_ok=True)
    (tree / LAUNCH_FILE).unlink(missing_ok=True)
    payload = {"key": key, "what": what, "when": when, "problem": problem, "agent": agent}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def note_on_stamp(tree: Path, field: str, value: str) -> None:
    """Add `field` to the tree's stamp, so a verdict about this dispatch is reached once."""
    payload = read_stamp(tree)
    if payload:
        payload[field] = value
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        (tree / STAMP_FILE).write_text(text, encoding="utf-8")


def inherit_origin(tree: Path, home: Path | None = None) -> bool:
    """Copy `home`'s `ORIGIN_FILE` -- by default this script's own checkout, a fixer's
    tree when a fix-pass session runs its copy -- into `tree`; whether it had one.

    A fix-pass session that cuts a sibling tree -- a devkit sweep fixing a carameli file
    -- is doing fixer work there too, so that PR should merge itself once green. The
    sweep's prompt said to copy the mark and one did not, so carameli #395 waited on a
    person. `agent-worktree.py new` calls this, so nobody has to remember it.
    """
    mark = (home or CHECKOUT) / ORIGIN_FILE
    if not mark.is_file():
        return False
    target = tree / ORIGIN_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(mark.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def filed(relative: Path) -> Path:
    """Where `file_away` keeps `relative`: what a finding's evidence names."""
    return relative.with_name(f"{relative.stem}.filed{relative.suffix}")


def file_away(tree: Path, relative: Path) -> None:
    """`logs/x.md` onto the end of `logs/x.filed.md`: read once, kept for whoever looks.

    Appended, not replaced: every finding filed from an earlier copy names this file as
    its evidence, and a replace would take those lines away from under it.
    """
    source = tree / relative
    if not source.is_file():
        return
    with (tree / filed(relative)).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(source.read_text(encoding="utf-8").rstrip("\n") + "\n")
    source.unlink()


def record_launch(tree: Path, argv: list[str], done: object, diagnosis: str = "") -> None:
    """Keep what a background launch was given and what it answered, for `launch_line`.

    A session that never started was filed with its tree alone, and a sweep grepped
    transcripts for nine calls to learn the launcher had swallowed its prompt (3728bf21).
    The prompt, the last argument, is kept as its length: its words are the stamp's.
    `diagnosis` -- what the launcher's service said of itself -- is evidence only, kept
    out of `launch_line` so the finding's detail stays the same across recurrences.
    """
    shown = [*argv[:-1], f"<prompt: {len(argv[-1])} chars>"] if argv else []
    tail = {
        name: str(getattr(done, name, "") or "")[-REASON_LIMIT:] for name in ("stdout", "stderr")
    }
    payload = {"argv": shown, "returncode": getattr(done, "returncode", None), **tail}
    if diagnosis:
        payload["diagnosis"] = diagnosis[-DIAGNOSIS_LIMIT:]
    path = tree / LAUNCH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def launch_refused(tree: Path) -> bool:
    """This dispatch's launcher exited non-zero; `stamp` clears an older record."""
    try:
        record = json.loads((tree / LAUNCH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("returncode") not in (0, None)


def launch_line(tree: Path) -> str:
    """The launch record as one line of a finding's detail; "" when there is none."""
    try:
        record = json.loads((tree / LAUNCH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(record, dict):
        return ""
    said = " ".join(str(record.get("stderr") or record.get("stdout") or "").split())
    return f"launcher exited {record.get('returncode')}" + (f": {said[-160:]}" if said else "")


def read_stamp(tree: Path) -> dict:
    try:
        loaded = json.loads((tree / STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def blocked_reason(tree: Path) -> str:
    """The report's text as one line, cut to `REASON_LIMIT`; "" when there is none."""
    try:
        text = (tree / BLOCKED_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    words = " ".join(line.strip().lstrip("#").strip() for line in text.splitlines()).split()
    return " ".join(words)[:REASON_LIMIT]


def agent_trees(
    root: Path, projects: list[str], git_for: GitFor = sweep.git_for
) -> Iterator[tuple[str, Path, str]]:
    """`(project, tree, branch)` for every worktree of every checkout on disk.

    A detached tree yields an empty branch. A checkout git cannot list is passed over:
    a missing or broken one is somebody else's report.
    """
    for project in projects:
        project_dir = root / project
        if not project_dir.is_dir():
            continue
        listed = git_for(project_dir)("worktree", "list", "--porcelain")
        if getattr(listed, "returncode", 1) != 0:
            continue
        for path, branch in aw.parse_worktree_list(str(getattr(listed, "stdout", "") or "")):
            yield project, Path(path), branch


# --- what the trees say beyond a report ---------------------------------------------------


@dataclass(frozen=True)
class Tree:
    """One agent worktree and what it carries for the pass."""

    project: str
    path: Path
    branch: str
    stamp: dict
    friction: tuple[str, ...]


def _entries(path: Path) -> tuple[str, ...]:
    """A friction file's entries, one per line: headings and blanks dropped, list
    markers stripped; empty when there is no file.

    Whole, never cut to `REASON_LIMIT`: a line ends with what fixed it and the
    `fixed on this branch` that settles it (`fixed_here`), so a cut dropped exactly that
    half -- 2d0bc76f was filed open against a fix already merging on #435. The ledger's
    own `harness_events.FIELD_LIMITS` is the ceiling on what reaches the file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    kept = (line.strip() for line in text.splitlines())
    lines = (line.lstrip("-*0123456789. ").strip() for line in kept if not line.startswith("#"))
    return tuple(line for line in lines if line)


def friction_lines(tree: Path) -> tuple[str, ...]:
    """The tree's `logs/friction.md` entries this tree has not already had filed, less
    any about Claude Code's own worktree guard.

    A session writes the file whole, and once the pass has filed it away the next write
    carries the old lines again: a174d816's line was filed twice from one tree, and the
    second filing reopened a group already retired with its fix as a recurrence.

    The guard is not devkit's (`.claude/rules/engineering.md` says so, and carries the
    spellings that pass it), which is why `session_friction` never files its refusals
    from a transcript. Written into a friction file, one still became a group a sweep
    could retire only with that same note (ced1c085).
    """
    seen = set(_entries(tree / filed(FRICTION_FILE)))
    return tuple(
        line
        for line in _entries(tree / FRICTION_FILE)
        if line not in seen and not CLAUDE_CODE_GUARD.search(line)
    )


# How a session says it fixed what a friction line reports, in the tree that wrote it:
# the spelling `fix_prompts.FINISH` and the ship skill ask for, and its near variants.
FIXED_HERE = re.compile(r"\bfixed (?:it )?(?:on|in) this (?:branch|tree)\b", re.I)


def fixed_here(line: str) -> bool:
    """Whether a friction line says its own session fixed it on the tree's branch.

    Such a line is filed settled by that branch (`Finding.settles_with`) rather than open:
    three of 0927-3's lines said so, were filed open, and sent a second fixer at fixes
    already in review on #429. `fix_verify` reopens the row if the branch never merges.
    """
    return bool(FIXED_HERE.search(line))


def read_trees(root: Path, projects: list[str], git_for: GitFor = sweep.git_for) -> list[Tree]:
    """Every agent worktree with its stamp and friction, walked once for the whole pass."""
    return [
        Tree(project, tree, branch, read_stamp(tree), friction_lines(tree))
        for project, tree, branch in agent_trees(root, projects, git_for)
    ]


def transcript_dir(tree: Path, projects_root: Path | None = None) -> Path:
    """Where Claude Code keeps the transcripts of sessions started in `tree`."""
    return (projects_root or CLAUDE_PROJECTS) / re.sub(r"[^A-Za-z0-9]", "-", str(tree))


def _mtime(path: Path) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.UTC)
    except OSError:
        return None


def last_spoke(path: Path) -> _dt.datetime | None:
    """When a transcript's session last did anything: its last record with a `timestamp`.

    Not the file's mtime -- `claude stop` appends `last-prompt` and `cost-state` rows,
    which carry none, to a session that has already ended, and the mtime then held its
    tree as busy for `QUIET_AFTER`. Only the file's tail is read; a tail holding no dated
    record (one enormous last line) falls back to the mtime, which errs towards busy.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
            when = _dt.datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        if when.tzinfo:
            return when
    return _mtime(path)


TAIL_BYTES = 256 * 1024


def newest_transcript(tree: Path, projects_root: Path | None = None) -> Path | None:
    dated = [
        (when, path)
        for path in transcript_dir(tree, projects_root).glob("*.jsonl")
        if (when := last_spoke(path))
    ]
    return max(dated)[1] if dated else None


def started_at(path: Path) -> _dt.datetime | None:
    """When a transcript's session began: its first record's own `timestamp`.

    Not the file's mtime -- every session in a tree appends to its own file, so the
    newest file is whoever spoke last, which in the first supervised run was the
    interactive session sharing the tree rather than the fixer the pass had stamped.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(50), handle, strict=False):
                stamp = _TIMESTAMP.search(line)
                if stamp:
                    return _dt.datetime.fromisoformat(stamp.group(1).replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None
    return None


_TIMESTAMP = re.compile(r'^\{.*?"timestamp":\s*"([0-9T:.+\-Z]+)"')
# A session starts a beat after the pass stamps its tree; a clock this far off still counts.
STAMP_SKEW = _dt.timedelta(seconds=5)


def session_transcript(
    tree: Path, since: _dt.datetime, projects_root: Path | None = None
) -> Path | None:
    """The first transcript started in `tree` at or after `since`: the dispatched session's."""
    started = [
        (when, path)
        for path in transcript_dir(tree, projects_root).glob("*.jsonl")
        if (when := started_at(path)) and when >= since - STAMP_SKEW
    ]
    return min(started)[1] if started else None


def active_transcript(
    tree: Path, now: _dt.datetime, projects_root: Path | None = None
) -> Path | None:
    """A transcript in `tree` written within `QUIET_AFTER`: someone is working there now."""
    newest = newest_transcript(tree, projects_root)
    touched = last_spoke(newest) if newest else None
    return newest if touched and now - touched <= QUIET_AFTER else None


WORKING = "working"
DONE = "done"
NEVER_STARTED = "never started"
NO_OUTCOME = "ended without an outcome"
DEAD = (NEVER_STARTED, NO_OUTCOME)


def session_state(
    tree: Path, now: _dt.datetime, projects_root: Path | None = None
) -> tuple[str, str]:
    """`(state, transcript)` of the session the tree's stamp sent.

    `DONE` once it left an intent or a report, `WORKING` while its transcript moves (or
    it is still inside `START_GRACE`), one of `DEAD` otherwise. `""` for a tree with no
    stamp, one already judged dead, or a session this cannot see -- only Claude Code
    keeps a transcript here, and a Codex tab is watched by the person who opened it.
    """
    payload = read_stamp(tree)
    if (
        not payload
        or payload.get("dead")
        or not str(payload.get("agent", "claude")).startswith("claude")
    ):
        return "", ""
    try:
        sent = _dt.datetime.fromisoformat(str(payload.get("when", "")))
    except ValueError:
        return "", ""
    transcript = session_transcript(tree, sent, projects_root)
    if any((_mtime(tree / name) or sent) > sent for name in OUTCOME_FILES):
        return DONE, str(transcript or "")
    touched = last_spoke(transcript) if transcript else None
    if touched is None or touched < sent:
        # The grace is for a session starting, not one its launcher refused: 0927-2's
        # held every ledger send for two iterations while the backlog grew.
        refused = launch_refused(tree)
        return (NEVER_STARTED, "") if refused or now - sent > START_GRACE else (WORKING, "")
    if now - touched > QUIET_AFTER:
        return NO_OUTCOME, str(transcript)
    return WORKING, str(transcript)
