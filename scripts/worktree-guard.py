#!/usr/bin/env python3
"""PreToolUse hook: give a cross-checkout edit its own box instead of refusing it.

An agent edit must never land on a checkout's **home branch**, because that is the one
act that makes a checkout outlive its task — and every state `sweep.py` hunts for
(`needs-branch`, `spent-branch`, the anchor marker, `home_ref`) follows from it. The
agent would be manufacturing the exact backlog the sweep exists to clear.

`branch-on-write.py` used to prevent that by cutting a task branch *in place*, which
solved the branch and kept the problem: the checkout still outlived the task. It is
retired, and this hook covers both session shapes in its place.

Refusing the edit would fix that and cost the turn. So this hook does the other
thing: it **spawns the box the edit should have been made in** and hands the path
back. One box per (session, project), so a session that touches three repos gets
three boxes and a session that makes forty edits in one repo gets one.

**The edit is re-aimed, not refused.** Claude Code honours `updatedInput` from a
PreToolUse hook, so the guard rewrites the tool's path argument to the same file
inside the box and lets the call through; the route-out text arrives as
`additionalContext` rather than as an error. That closes the cost the old design could
not — every guarded session paid one failed tool call, and a blocked `Write` had its
entire payload re-sent at the returned path. The rewrite is honoured **only when the
same object sets no `permissionDecision`** (the runner reads `updatedInput &&
permissionBehavior === undefined`), so this hook sets none and the call goes on to be
permission-checked at its new path like any other.

It still blocks wherever a rewrite cannot honestly express the outcome, and
`redirect_blocker` is the single predicate: a spawn that failed **with no box left
behind by anyone else**, so there is nothing to aim at; a tool whose arguments it does
not know how to rewrite; an `old_string` the
box's copy does not contain, which would turn a clear block into an opaque "string not
found"; and any session reached through Codex's hook adapter, which has no
`updatedInput` contract and would read the allow as permission to write the *original*
path. The block message is the same one as before, with the reason added as a note.

**Two copies of this hook run on every tool call**, because it is registered in the
user's `settings.json` and in the project's, and Claude Code fires both. They race to
cut the same box, and the loser's `git worktree add` dies on the branch the winner has
just created. Both responses then reach the agent as one object: an error saying the
spawn failed and nothing was written, beside an `additionalContext` saying the edit was
applied in the box. Whichever half it believes, the other is a lie — and believing the
context is the expensive one, because the agent goes on to build on a change that was
never made. So the spawn is held under `worktree.spawn_lock`, and a spawn that fails
anyway looks for the box the winner registered before it blocks; `after_failed_spawn`
is that recovery.

Either way, by the time the agent reads the message the worktree exists, is on a fresh
task branch off `origin/<default>`, and has its own `COMPOSE_PROJECT_NAME` and port
lease. It does *not* have a toolchain — installing one is minutes and a hook may not
take minutes — so the message carries the provision command along with the rest of the
route out.

**A shell command is judged too**, on the paths its own command line names as writes —
see the shell tier below `old_strings`. Editor calls were the whole scope until Claude
Code's bypass-permissions mode began telling sessions, in text indistinguishable from
their operator's, to prefer `sed`/heredocs over Edit and Write; that made the blind side
a route. A shell command is never re-aimed, because the rewrite replaces a path argument
and a command line has none, so this tier always blocks toward the box.

**A `git checkout`/`git switch` onto a task branch is blocked outright** — see the branch
tier below the shell one. It is the only judgement here that ends in neither a rewrite nor
a box, because the call was never going to write anything: parking a static checkout on
somebody else's `agent/...` branch freezes its home branch *and* turns this hook off for
that checkout, since an edit onto a task branch carrying commits is the one case
`needs_box` declines. That is how carameli spent two days on another session's branch with
every tier here working exactly as designed.

**Silent on everything else**, which is most calls:

  - an edit inside a checkout on a managed task branch **that carries commits of its
    own**: something deliberately put it there, and the commonest reason is "fix PR
    #42", where a fresh box would put the fix somewhere the PR never sees. A task
    branch with nothing on it is *not* one of these (see `needs_box`);
  - an edit already inside a box;
  - an edit to a **git-ignored** path (`.env`, `.local/`, `logs/`): it cannot land on
    any branch, so there is no branch for a box to protect;
  - any path that is not under a registered checkout;
  - any machine with no multi-root workspace file, which is every CI runner and every
    fresh clone.

Wired in devkit's own `.claude/settings.json` as well as the workspace root's. In a
devkit session it is one `Path.resolve()` and out — but it fires for real the moment a
devkit session edits a sibling checkout, which is the same class of mistake and the
reason devkit runs the hooks it ships.

Pure helpers (`edited_path`, `owning_project`, `redirect_decision`, `deny_message`)
are unit-tested in `tests/test_worktree_guard.py`; `main` is the thin shell that
spawns and reports.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # For annotations only. At runtime the module is bound by `load_by_path` below;
    # mypy cannot see through that, but it can resolve `scripts/worktree.py` by name.
    from worktree import Box

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
# Resolved by the sys.path insert above; `scripts/precommit/` is not a package. The
# shared loader is used because `worktree.py` is importable by name but this file is
# not — see that module's docstring for why the registration order matters.
from _loader import load_by_path

import devkit_project

# Also resolved by the sys.path insert above. Read for the slug the UserPromptSubmit
# hook recorded for this session — see `session_slug`.
import task_slug

worktree = load_by_path("worktree", Path(__file__).resolve().parent / "worktree.py")

# The harness-events ledger, so a block leaves a record outside the blocked session's
# chat. Guarded, and `_record_event` checks for None: any unhandled exception in a
# PreToolUse hook exits non-2, which Claude Code reports as a non-blocking error and
# lets the edit PROCEED — an ImportError in the diagnostics would turn the guard off.
try:
    harness_events = load_by_path(
        "harness_events", Path(__file__).resolve().parent / "hooks" / "harness_events.py"
    )
except Exception:  # pragma: no cover - a checkout missing the ledger module
    harness_events = None  # type: ignore[assignment]  # Module-or-None, checked at each use

# Where the ledger lives: this checkout, which is the workspace's static devkit — the
# workspace settings wire this hook by that path, so `__file__` resolves there even
# when the edit under judgment is aimed at a box.
LEDGER_ROOT = Path(__file__).resolve().parents[1]

# Claude Code hook contract, matching `enforce-capped-bash.py`: 0 allows the call, 2
# blocks it and feeds stderr back to the model. A blocking hook MUST write its reason
# to stderr — stdout is not surfaced *as an error*. On the allow path stdout is read as
# the structured hook response, which is what carries the rewrite (see `emit_redirect`).
EXIT_ALLOW = 0
EXIT_BLOCK = 2

# Set by `scripts/hooks/codex-hook-adapter.py` around every hook it runs. Codex's
# PreToolUse response has no `updatedInput` member, and the adapter passes a rc-0 hook's
# stdout through verbatim — so an unrecognised rewrite there is read as a plain allow and
# the edit lands on the home branch, silently, which is the one outcome this hook exists
# to prevent. Absence of the marker is the only thing that licenses the rewrite; a
# session whose harness is unknown gets the block, which is correct under every contract.
ADAPTER_ENV = "DEVKIT_HOOK_ADAPTER"

# Tools whose path argument the guard knows how to rewrite. Deliberately narrower than
# `MUTATING_TOOLS`: `apply_patch` and `create_file` are Codex's, and Codex is on the
# blocked side of `ADAPTER_ENV` anyway, so there is no shape here worth guessing at.
REWRITABLE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Where the tools keep the path, most specific first. `path` is last because it is the
# generic one; a payload carrying both wants the named key.
PATH_KEYS = ("file_path", "filePath", "notebook_path", "notebookPath", "path")


def _record_event(event: str, fields: tuple[tuple[str, object], ...]) -> None:
    """Best-effort ledger append; never raises, never changes the decision."""
    if harness_events is None:
        return
    harness_events.record(event, fields, root=LEDGER_ROOT)


# Tools that hand a shell a command line rather than a path. They write files too — see
# the shell tier below — but nothing in their arguments says which, so every helper that
# reads a path out of a payload has to ask whether it is looking at one of these.
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})

# Tools that write a file — the question this hook exists to answer is "is the agent
# about to change a file, and may it land where it is pointing?".
MUTATING_TOOLS = (
    frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "create_file"})
    | SHELL_TOOLS
)

# Per-git-step ceiling while spawning. Lower than `worktree.apply_new`'s default
# because an agent's tool call is blocked for the duration; a `git fetch` that has not
# answered in 30s is a network problem, and the box is better cut from a stale local
# `origin/<default>` than not cut at all.
SPAWN_TIMEOUT = 30.0

# How long to wait for the other registration of this hook to finish cutting the same
# box before giving up on the lock and trying anyway. `worktree.SPAWN_LOCK_WAIT` owns
# the number and the reasoning behind it; it is named here so a test can drive the wait
# without pausing for it, and so the ceiling is read from one place if the harness's
# hook timeout ever moves.
SPAWN_LOCK_WAIT = worktree.SPAWN_LOCK_WAIT


def parse_hook_input(raw: str) -> dict | None:
    """Parse raw stdin into a dict, or None when absent/malformed."""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _tool_name(payload: dict) -> str:
    return str(payload.get("tool_name") or payload.get("toolName") or "")


def tool_input(payload: dict) -> dict:
    """The tool's arguments, tolerating snake_case and camelCase as the other hooks do."""
    raw = payload.get("tool_input") or payload.get("toolInput") or {}
    return raw if isinstance(raw, dict) else {}


def edited_path_key(payload: dict) -> str:
    """Which key of the tool's input holds the path, or "".

    Split out from `edited_path` because a rewrite has to put the box path back under
    the **same** key it was read from. `updatedInput` replaces the input object whole
    and the runner re-validates it against the tool's own schema, so adding a `path`
    beside the `file_path` an Edit still carries fails that validation — at which point
    Claude Code logs `permission_updated_input_invalid` and calls the tool with the
    ORIGINAL input. That failure mode is the reason to read the key rather than assume
    it: it lands the edit on the home branch with nothing red anywhere.
    """
    arguments = tool_input(payload)
    for key in PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return key
    return ""


def edited_path(payload: dict) -> str:
    """The path a mutating tool is about to write, or "".

    Reads the several spellings the tools use for the same argument (`file_path` for
    Edit and Write, `path` for apply_patch/create_file, `notebook_path` for
    NotebookEdit).
    """
    key = edited_path_key(payload)
    return str(tool_input(payload)[key]) if key else ""


def old_strings(payload: dict) -> list[str]:
    """Every literal this call expects to already be present in the file it targets.

    The precondition an Edit carries and a Write does not, and the whole reason the
    rewrite is not unconditional: the box holds `origin/<default>`'s copy of the file
    while the agent read the checkout's, and a checkout that has drifted turns a
    re-aimed Edit into `String to replace not found` — an error about the agent's own
    argument, at a path it never named, which is strictly worse than the block it
    replaced. `MultiEdit` keeps its literals in `edits`; a missing or non-string
    `old_string` yields nothing, which reads as "no precondition" and is right for
    `Write` and for an Edit creating a new file.
    """
    arguments = tool_input(payload)
    edits = arguments.get("edits")
    if isinstance(edits, list):
        return [
            edit["old_string"]
            for edit in edits
            if isinstance(edit, dict) and isinstance(edit.get("old_string"), str)
        ]
    value = arguments.get("old_string")
    return [value] if isinstance(value, str) else []


# --- The shell tier ------------------------------------------------------------------
#
# Everything above reads the path out of a tool argument, which is why for a year `Bash`
# was the guard's blind side: `sed -i`, `cat > file <<EOF` or a `cp` writes a checkout's
# tracked file with nothing between it and the home branch, and the hook never saw the
# call. A hole, but a quiet one, while the agent's habit was to reach for `Edit`.
#
# Claude Code's own bypass-permissions guidance is what turned it into a route. In that
# mode the session is told, in text it cannot tell apart from its operator's: *"make file
# changes with sed, heredocs, or short scripts, rather than using the dedicated Read,
# Edit, or Write tools."* So the one tier the workspace is built to protect was a single
# nudge away from being bypassed by an agent doing exactly as it was told — and the agent
# that reported this had to reason its way to disobeying, on every session, from first
# principles. A guarantee that depends on that is not one.
#
# This tier recognises spellings; it does **not** model the shell. That is
# `enforce-capped-bash.py`'s lesson taken as read: the gate that made every command prove
# itself had to model the shell, and 46% of every block it ever issued was its own false
# positive. So the verb list is closed and short, an unrecognised command is allowed, and
# the paths it does extract go through the same `redirect_decision` an `Edit` would — so a
# git-ignored path, a path in a box, a checkout on a task branch holding work, and
# anything outside a registered checkout are all still allowed, silently.
#
# The gap it cannot close is an interpreter: `python -c`, or a `python - <<'EOF'` script,
# computes its target at runtime and names it nowhere in the argv. Written down rather
# than left implicit, because a silent gap in a guard reads as coverage.

# Verbs whose every non-option operand is a file they write.
SHELL_WRITE_ALL = frozenset(
    {
        "tee",
        "truncate",
        "touch",
        "rm",
        "unlink",
        "shred",
        # PowerShell, which is this workspace's other shell and has its own tool.
        "set-content",
        "add-content",
        "out-file",
        "new-item",
        "remove-item",
        "clear-content",
    }
)

# Copy-shaped verbs, where every operand but the last is a source being *read*. Taking
# them all would block `cp devkit/x.py /tmp/`, which writes nothing a branch can carry.
SHELL_WRITE_LAST = frozenset(
    {"cp", "mv", "install", "rsync", "ln", "copy-item", "move-item", "rename-item"}
)

# Wrappers that stand in front of the real verb rather than being one.
SHELL_PREFIXES = frozenset({"sudo", "env", "command", "exec", "time", "nohup", "nice"})

# A leading `FOO=bar` assignment, same case as the wrappers.
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# `cmd >file`, `cmd >> file`, `cmd 2> file` — the heredoc route's other half, since
# `cat > x <<EOF` is a redirect like any other. `>&1` duplicates a descriptor and names
# no file, hence the exclusion; a `>` inside a quoted string can only invent a candidate,
# and an invented candidate resolves to no checkout.
REDIRECT = re.compile(r"\d?>>?\s*(?![&>])([^\s;|&<>]+)")

# Statement separators. Splitting inside a quoted string can only lose a detection or
# invent an unrecognised verb, and both of those allow — the safe direction for a
# splitter this crude.
STATEMENTS = re.compile(r"(?:&&|\|\||[;\n|]|&(?!\d))")


def _unquote(token: str) -> str:
    """Strip one layer of matching quotes; `shlex(posix=False)` leaves them attached."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _verb(token: str) -> str:
    """The command word, normalised: basename, no `.exe`, case-folded.

    Case-folded because PowerShell's cmdlets are conventionally `Set-Content` and its
    parser does not care, so a matcher that did would be one capital away from useless.
    """
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def shell_tokens(statement: str) -> list[str]:
    """Split one statement into tokens, keeping Windows backslashes intact.

    `posix=True` eats them — `C:\\Users\\x` arrives as `C:Usersx` — and every path this
    hook judges on this machine is a Windows path.
    """
    try:
        tokens = shlex.split(statement, posix=False)
    except ValueError:  # an unbalanced quote; whitespace is a good enough fallback
        tokens = statement.split()
    return [_unquote(token) for token in tokens if token]


def shell_write_targets(command: str) -> list[str]:
    """Every path this command line names as something it is about to write.

    Order-preserving and deduplicated, so a block names the first target the command
    would have hit rather than an arbitrary one. Operands that are not paths at all — a
    `sed` script, a `-Value` string — are left in: they resolve to no checkout, and
    picking the "real" one out of a verb's argv is more shell modelling than this tier
    is willing to do.
    """
    found: list[str] = []

    def add(value: str) -> None:
        value = _unquote(value.strip())
        if value and value not in found:
            found.append(value)

    for statement in STATEMENTS.split(command or ""):
        for match in REDIRECT.finditer(statement):
            add(match.group(1))
        tokens = shell_tokens(statement)
        while tokens and (_verb(tokens[0]) in SHELL_PREFIXES or ENV_ASSIGN.match(tokens[0])):
            tokens = tokens[1:]
        if not tokens:
            continue
        verb = _verb(tokens[0])
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        if verb == "sed":
            # Only `-i`/`--in-place` writes. Every other `sed` reads, and `sed -n '1,5p'`
            # over a checkout's file is how half the reading in a guarded session is done.
            if any(t.startswith(("-i", "--in-place")) for t in tokens[1:]):
                for operand in operands:
                    add(operand)
        elif verb == "dd":
            for token in tokens[1:]:
                if token.startswith("of="):
                    add(token[3:])
        elif verb in SHELL_WRITE_ALL:
            for operand in operands:
                add(operand)
        elif verb in SHELL_WRITE_LAST and operands:
            add(operands[-1])
    return found


def guarded_targets(payload: dict) -> list[str]:
    """Every path this call is about to write, whichever kind of tool it is.

    One list for both tiers, because `main` asks the same questions of each element and
    a shell command can name more than one.
    """
    if _tool_name(payload) in SHELL_TOOLS:
        return shell_write_targets(str(tool_input(payload).get("command") or ""))
    path = edited_path(payload)
    return [path] if path else []


def shell_note(target: str) -> str:
    """The extra line a routed shell command gets, which a routed `Edit` does not need.

    An `Edit` names its path, so the block message's path is self-evidently the thing to
    change. A command does not: the agent has to be told *which* of its words was read as
    a write, or it re-issues the same line with the box path bolted somewhere else.
    """
    return (
        f"This was a shell command, not an editor call. It was read as writing "
        f"`{target}`; re-issue it with that word replaced by the path above. A shell "
        f"write is not re-aimed automatically because the guard rewrites path arguments, "
        f"and a command line has none."
    )


# --- The branch tier -------------------------------------------------------------------
#
# `git checkout` writes no file, so neither tier above can see it -- and it is exactly the
# act that parked carameli's static checkout on another session's `agent/...` branch for
# two days in August 2026. Nothing had edited that checkout. The boxing tier had done its
# job: that session's edits went to a box, and their PR was open. Something simply ran
# `git checkout agent/seems-only-1-preview-time-0821` in the static copy, and it stayed
# there, because a clean fully-pushed task branch is a state `sweep.py` *reports* and
# could not clear (`sweep.PARKED`, added in the same change, is that half).
#
# A parked checkout is worse than untidy, and the second-order effect is the one worth
# writing down: the boxing tier goes **quiet** there. An edit onto a task branch that
# carries commits of its own is the "fix PR #42" case `needs_box` deliberately declines,
# so the moment a checkout is parked on somebody else's live branch it becomes unguarded
# space, and every later edit lands on that branch with no block and no box. Meanwhile its
# home branch stops advancing and every session start reports stranded work.
#
# So this tier judges the move rather than the write, and it is the one tier that neither
# re-aims nor spawns: nothing was going to be written, and the answer to "I want to be on
# that branch" is one of `worktree.py`'s verbs, not a worktree cut behind the agent's back.
#
# Scoped to task branches, both ways round. Moving a checkout *home* is the repair, never
# the problem, so it is allowed; and a box moved onto a branch its lease does not name is
# blocked too, because `reconcile` looks a box's PR up by the branch the registry records
# and a box that has wandered off it can be reaped as never-used with the work still in it.

# Git's own options, before the subcommand. Only `-C` changes which checkout the command
# lands in; the rest are here because they consume the token after them and would
# otherwise be misread as the subcommand.
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

# The two subcommands that move HEAD onto another ref.
SWITCH_VERBS = frozenset({"checkout", "switch"})

# Flags that name a branch being *created*, so the ref is the token after them rather than
# the first bare operand: `-b`/`-B` for checkout, `-c`/`-C` for switch. Creating a task
# branch in a static checkout is the same park as moving onto one -- it is what
# `branch-on-write.py` used to do, and retiring that hook is what the box tier replaced.
SWITCH_CREATE_FLAGS = frozenset({"-b", "-B", "-c", "-C", "--orphan"})

# `git checkout -- file`, `git checkout -p`: these restore file content and leave HEAD
# exactly where it was. Ordinary, frequent, and not a park -- so their presence anywhere in
# the operands abandons the statement rather than trying to tell the two shapes apart.
SWITCH_RESTORES = frozenset({"--", "-p", "--patch", "--pathspec-from-file"})


def _switch_ref(operands: list[str]) -> str:
    """The ref a `checkout`/`switch` moves onto: a created name, else the first operand."""
    for index, token in enumerate(operands):
        if token in SWITCH_CREATE_FLAGS:
            return operands[index + 1] if index + 1 < len(operands) else ""
        if not token.startswith("-"):
            return token
    return ""


def switch_targets(command: str) -> list[tuple[str, str]]:
    """`(directory, ref)` for every `git checkout`/`git switch` on this command line.

    `directory` is git's `-C` when the call gave one and `""` otherwise, meaning the tool
    call's own cwd. Same crude statement splitter and the same closed-list discipline as
    the shell tier above: a spelling this does not recognise yields nothing and is
    allowed, because the cost of the two mistakes is not symmetric here either.
    """
    found: list[tuple[str, str]] = []
    for statement in STATEMENTS.split(command or ""):
        tokens = shell_tokens(statement)
        while tokens and (_verb(tokens[0]) in SHELL_PREFIXES or ENV_ASSIGN.match(tokens[0])):
            tokens = tokens[1:]
        if not tokens or _verb(tokens[0]) != "git":
            continue
        rest = tokens[1:]
        directory = ""
        while rest and rest[0].startswith("-"):
            if rest[0] in GIT_VALUE_OPTIONS and len(rest) > 1:
                if rest[0] == "-C":
                    directory = rest[1]
                rest = rest[2:]
            else:
                rest = rest[1:]
        if not rest or rest[0].lower() not in SWITCH_VERBS:
            continue
        operands = rest[1:]
        if any(token in SWITCH_RESTORES for token in operands):
            continue
        ref = _switch_ref(operands)
        if ref:
            found.append((directory, ref))
    return found


def switch_branch(ref: str) -> str:
    """The task branch `ref` names, or "" when it names anything else.

    `refs/heads/agent/x` and `origin/agent/x` are the same park as `agent/x`. The second
    detaches HEAD rather than moving onto the branch, which is if anything worse: a
    detached checkout is a state `sweep.py` can only report, and `needs_box` declines a
    branch git will not name, so the box tier is quiet there too.

    Exactly one remote segment is stripped, and only when what remains is a managed
    prefix, so no home branch can become a task branch by being spelled with a slash.
    """
    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
    if worktree.sweep.is_task_branch(ref):
        return ref
    head, _, tail = ref.partition("/")
    if head and tail and worktree.sweep.is_task_branch(tail):
        return tail
    return ""


def switch_decision(
    directory: str,
    branch: str,
    cwd: str,
    root: Path,
    projects: list[str],
    boxes: Mapping[str, Box],
) -> tuple[str, str, str] | None:
    """`(kind, name, registered)` when this move is a park; None when it is ordinary.

    `kind` is `"checkout"` for a registered static checkout and `"box"` for an ephemeral
    one whose lease records a different branch; `registered` carries that lease's branch
    and is `""` for a checkout, which has none to compare against.

    Three cases allow, and each is somebody else's decision already made:

    - a directory under no registered checkout at all -- a scratch clone, `VanillaLand`,
      anything outside the workspace. Same silence every other tier keeps there;
    - a box already standing on this branch, which is what `worktree.py resume` leaves
      behind and what re-attaching after a detached HEAD does;
    - a path under `.worktrees/` that is in no live box -- a husk, a stray directory.
      There is no lease to desync, so there is nothing to protect.
    """
    here = resolve_target(directory or ".", cwd)
    if here is None:
        return None
    try:
        root = root.resolve()
        boxes_root = worktree.boxes_root(root).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if _within(here, boxes_root):
        for box in boxes.values():
            if not _within(here, worktree.box_path(root, box.name).resolve()):
                continue
            return None if box.branch == branch else ("box", box.name, box.branch)
        return None
    project = owning_project(here, root, projects)
    return ("checkout", project, "") if project else None


def switch_message(kind: str, name: str, branch: str, registered: str = "") -> str:
    """What the agent reads when a branch move is refused. No box, and nothing to re-issue.

    Every other block in this file ends with a path to write to instead. This one cannot:
    the call was not going to write anything, so the useful content is the three things
    the move is usually a proxy for, each spelled as the command that does it properly.
    """
    worktree_py = Path(__file__).parent / "worktree.py"
    if kind == "box":
        return "\n".join(
            [
                f"Blocked: the box {name} is registered on '{registered}', and this would "
                f"move it onto '{branch}'.",
                "",
                "worktree.py records a box's branch in its lease registry, and `reconcile` "
                "looks that box's PR up by that name. A box standing on a branch its lease "
                "does not name is invisible to the reaper as shipped work and reapable as "
                "work that never happened - which destroys the worktree with the commits "
                "still in it.",
                "",
                "To continue that branch's work, resume its own box instead:",
                f"    python {worktree_py} resume <box> --yes",
            ]
        )
    return "\n".join(
        [
            f"Blocked: this would park the static checkout {name} on '{branch}', a task branch.",
            "",
            "A parked checkout is not merely untidy. Its home branch stops advancing, every "
            "session start reports it as stranded work, and - the part that is easy to miss "
            "- this hook goes QUIET there: an edit onto a task branch carrying commits is "
            "deliberately left alone, so the checkout becomes unguarded space and every "
            "later edit lands on that branch with no box and no block.",
            "",
            "One of these is almost certainly what was wanted:",
            "",
            "  - to EDIT that work: just edit it. This hook cuts a box on its own branch, "
            "or resumes the one this session already had:",
            f"        python {worktree_py} resume <box> --yes",
            "  - to RUN it in a browser: a preview is a copy, and never moves the checkout:",
            f"        python {worktree_py} preview {name} --branch {branch} --yes",
            f"  - to READ it: `git -C {name} log/show/diff {branch}` needs no checkout.",
        ]
    )


def _within(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or sits underneath it."""
    try:
        return child == parent or parent in child.parents
    except (OSError, ValueError):
        return False


def owning_project(target: Path, root: Path, projects: list[str]) -> str:
    """Which registered checkout contains `target`; "" when none does.

    The longest match wins, so `apt-finder-b/x.py` is attributed to `apt-finder-b`
    rather than to `apt-finder` — the two are separate checkouts of one repo and
    routing an edit to the wrong one would be worse than not routing it at all.
    """
    best = ""
    for name in projects:
        if _within(target, root / name) and len(name) > len(best):
            best = name
    return best


def needs_box(branch: str, protects_open_work: bool = True) -> bool:
    """True when an edit landing on `branch` would land somewhere nothing owns.

    The rule that replaces `branch-on-write.py`. That hook answered the same question
    by cutting a branch in place; this one answers it by routing the edit to a box,
    which is strictly better on the axis that matters — a box is disposable, so the
    checkout never outlives the task and never reaches any of the states `sweep.py`
    exists to find.

    Two cases decline, and both are cases where someone has already made the decision:

    - **on a managed task branch that carries commits of its own**
      (`protects_open_work`). Something deliberately put the checkout there, and the
      commonest reason is the one `branch-on-write.py` was rewritten for: "fix PR #42,
      it has conflicts" means checking that PR's branch out and editing it. Routing to
      a fresh box would put the fix somewhere the PR never sees.
    - **a branch git would not name.** Detached HEAD, or a git call that failed. The
      two are indistinguishable from here, and guessing would block edits on a machine
      where git is simply unavailable — so this declines and `sweep.py`, which is still
      running, is what catches a detached HEAD.

    **A task branch with no commits of its own is not one of them**, and used to be.
    Being on a managed task branch was the whole test, so the *first* session to leave one
    checked out turned this hook off for every session afterwards — the checkout became
    shared, unguarded space until someone parked it back on a home branch. Two sessions
    landed in one checkout that way, and neither could see it: one inherited a branch
    whose PR had already merged (`sweep.py` calls that state `spent-branch`), which
    protects no PR because there is nothing left on it to protect.

    So the question is not "is this a task branch" but "is there work here a box would
    strand". `protects_open_work` answers it, and the caller resolves it lazily —
    `branch_has_own_commits` costs a `git rev-list` and only a task branch can reach it.
    """
    if not branch:
        return False
    if not worktree.sweep.is_task_branch(branch):
        return True
    return not protects_open_work


def _git(checkout: Path, *args: str):
    """Run git in `checkout`, capturing stdout. Never raises; never opens a window."""
    return worktree.subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=worktree.sweep.NO_WINDOW,
    )


def path_is_ignored(checkout: Path, target: Path) -> bool:
    """True when `target` is git-ignored inside `checkout`.

    The premise of every block this hook issues is that the edit "would land on the home
    branch" — and for an ignored path that premise is simply false. `.env`, `.local/`,
    `logs/` and the rest of a checkout's untracked local state cannot land on any branch,
    so routing them to a box does not protect a branch from anything. It costs the turn
    and delivers nothing: the box gets its *own* seeded `.env`, so re-issuing the edit
    there writes the value into a worktree that is destroyed without ever shipping it,
    and the file the agent was actually asked to configure stays unchanged.

    `git check-ignore` is the right oracle rather than a hard-coded list of names,
    because what is ignored is per-project and already written down in `.gitignore`.
    It consults the index by default (no `--no-index`), so a *tracked* file that also
    matches an ignore rule reports as not-ignored and still gets a box — tracked is
    tracked, and that is the case the hook exists for.

    **Fails closed**: any error means "not ignored", i.e. route it. A hook that cannot
    read the repo must not start letting edits through on the strength of a failed
    subprocess.
    """
    try:
        result = _git(checkout, "check-ignore", "--quiet", "--", str(target))
    except (OSError, worktree.subprocess.SubprocessError):
        return False
    return result.returncode == 0


def current_branch(checkout: Path) -> str:
    """The branch `checkout` has checked out; "" when git will not say.

    Spawned per edit that targets a static checkout, which sounds expensive and is not:
    once the first such edit is blocked, every subsequent edit of the session goes to
    the box path and returns at the `.worktrees/` test above without reaching this.
    """
    try:
        result = _git(checkout, "branch", "--show-current")
    except (OSError, worktree.subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def branch_has_own_commits(checkout: Path) -> bool:
    """True when `checkout`'s HEAD carries commits `origin/<default>` does not.

    The signal behind `needs_box`'s `protects_open_work`: a task branch with commits of
    its own has somewhere for an edit to belong — an open PR, or one about to exist. A
    task branch with none is either freshly cut or already merged, and in both cases a
    box strands nothing.

    Local only, deliberately. The honest question is "has this branch got an open PR",
    and asking GitHub would answer it exactly — but this runs in a PreToolUse hook on
    every edit that reaches a static checkout, where a network round trip is latency the
    agent experiences as a hang. `git rev-list` against the *already fetched*
    `origin/<default>` costs milliseconds and agrees with the PR in every case that
    matters; the disagreement is a branch pushed and merged since the last fetch, which
    the next fetch resolves.

    **Fails closed** — every error returns True, meaning "decline, leave the edit
    alone". A hook that cannot read the repo must not start diverting edits into boxes
    on the strength of a failed subprocess, and declining is what this hook did for
    every task branch before the distinction existed.
    """
    try:
        default = worktree.sweep.tb.detect_default_branch(
            lambda *args: _git(checkout, *args), fallback="main"
        )
        probe = _git(checkout, "rev-list", "--count", f"origin/{default}..HEAD")
    except (OSError, worktree.subprocess.SubprocessError, ValueError):
        return True
    if probe.returncode != 0:
        return True
    try:
        return int(probe.stdout.strip()) > 0
    except ValueError:
        return True


def redirect_decision(
    target: str,
    cwd: str,
    root: Path,
    projects: list[str],
    branch_of: Callable[[Path], str] | None = None,
    commits_of_own: Callable[[Path], bool] | None = None,
    ignored: Callable[[Path, Path], bool] | None = None,
) -> tuple[str, str] | None:
    """`(project, path relative to that checkout)` when this edit needs its own box.

    None — allow, silently — for every case someone else already owns:

    - a path under `.worktrees/`: the edit is already in a box, which is the whole
      point of having sent it there. Whether it is in the *right* box is not this
      function's question — `main` asks `foreign_box` before calling here, so by the
      time this allow fires the ownership check has already passed;
    - a checkout on a managed task branch **that carries commits of its own** —
      see `needs_box`. **Whether or not the session is inside it**: the reason to
      decline is that something deliberately put that branch there and a box would
      bypass it, and where the editor happens to sit says nothing about that. A task
      branch with no commits of its own is not covered: it is either freshly cut or
      already merged, so there is no PR for a box to bypass;
    - anything outside a registered checkout, including the workspace file itself and
      any scratch directory beside the projects;
    - a **git-ignored** path inside a checkout — see `path_is_ignored`. It cannot land
      on the home branch, so there is no branch for a box to protect, and the box would
      swallow the edit whole.

    A session inside a checkout parked on a **home** branch is no longer among them.
    That was the case `branch-on-write.py` owned, and with that hook retired an edit
    there would land on the home branch with nothing underneath it — so it is routed
    like any other. `branch_of` is injected so the whole decision stays unit-testable
    without a repo on disk.
    """
    if not target:
        return None
    try:
        base = Path(cwd) if cwd else Path.cwd()
        resolved = (
            (base / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        )
        root = root.resolve()
        here = base.resolve()
    except (OSError, ValueError, RuntimeError):
        return None

    if _within(resolved, worktree.boxes_root(root)):
        return None
    project = owning_project(resolved, root, projects)
    if not project:
        return None
    checkout = root / project
    # Asked before the branch is read, and it is the cheaper order: an ignored path is
    # allowed on one subprocess and never consults the branch, while for everything else
    # this is one `check-ignore` added to a path that was already going to spawn a box.
    if (ignored or path_is_ignored)(checkout, resolved):
        return None
    lookup = branch_of or current_branch
    has_commits = commits_of_own or branch_has_own_commits
    branch = lookup(checkout)
    # Resolved lazily and at most once: only a task branch can be protected, and the
    # probe is a subprocess in a hook that runs on every edit.
    protects = worktree.sweep.is_task_branch(branch) and has_commits(checkout)
    if _within(here, checkout):
        if not needs_box(branch, protects):
            return None
    elif protects:
        # The "fix PR #42" case, reached from *outside* the checkout -- which is how a
        # workspace-level session reaches it, and how `upgrade-project.py` leaves one:
        # parked on `claude/devkit-upgrade-<mmdd>` holding the adoption its commit gate
        # rejected. A box cut from `origin/<default>` puts the fix somewhere that
        # branch and its PR never see, which is the one outcome the decline exists to
        # prevent -- and the session being elsewhere does not change that.
        #
        # Asymmetric with the branch above on purpose. Inside, a branch git will not
        # name declines (git may simply be unavailable, and `sweep.py` still catches a
        # detached HEAD). From outside, silence is not consent: only a name git
        # positively reports as a task branch is allowed through.
        return None
    try:
        relative = resolved.relative_to(root / project)
    except ValueError:
        return None
    return project, str(relative)


def session_slug(session: str, recorded: str = "") -> str:
    """The branch topic for a box the guard cut.

    `recorded` is what `task-slug.py` wrote for this session on the UserPromptSubmit
    that preceded this edit, and it is the whole reason that hook exists: a PreToolUse
    hook sees a tool call, never the prompt, so without it every guard-cut box was
    named `ws-<8 hex of session id>` and every resulting PR title said nothing about
    what the PR did.

    The fallback stays for the cases where there is genuinely nothing to read — a
    session whose slug file was pruned, a tool call with no session id, a workspace
    that has not wired the slug hook — and says what it honestly is.
    """
    return recorded or (f"ws-{session[:8]}" if session else "ws")


def block_reason(
    project: str, relative: str, branch: str, inside: bool, prefix: str = "Blocked"
) -> str:
    """The opening line: why this edit is being routed, naming the branch it was judged on.

    `prefix` is the one word that differs between the two outcomes. The *judgement* does
    not: an edit that is re-aimed into a box was judged on exactly the state that used to
    refuse it, so a second wording would be a second thing to keep true, and this
    docstring's own lesson is what that costs.

    Three different facts arrive here and they used to share one sentence. A session
    sitting in `carameli` on a freshly cut `claude/...` branch was therefore told the
    checkout was "parked on a home branch" — false on both counts — and did the
    reasonable thing with a message that contradicts what it can see: it read the hook
    as broken, spent its turn reporting the bug, and never re-issued the edit in the box
    that was waiting for it. A block message that misdescribes the state it blocked on
    costs more than no message, because the agent has no way to tell a wrong reason from
    a wrong decision.

    So each case says what it actually saw, and every one of them names the branch. The
    task-branch case additionally has to say *why* a task branch was not enough, since
    that is the half nothing else in the harness explains: `needs_box` asks whether there
    is work here a box would strand, not whether the name looks managed.
    """
    lands = f"an edit to {relative} would land on it with no task branch under it."
    if worktree.sweep.is_task_branch(branch):
        return (
            f"{prefix}: {project} is on '{branch}', which is a task branch but carries no "
            f"commits of its own - so it is either freshly cut or already merged, there is "
            f"no open work on it for this edit to belong to, and a box strands nothing. (A "
            f"task branch WITH commits is left alone; this one has none.)"
        )
    if not branch:
        return (
            f"{prefix}: git would not name a branch for {project} (detached HEAD, or git did "
            f"not answer) and this session is not inside that checkout, so {lands}"
        )
    if inside:
        return f"{prefix}: {project} is parked on '{branch}', a home branch, so {lands}"
    return (
        f"{prefix}: this session is not inside {project}, which is on '{branch}' "
        f"(a home branch), so {lands}"
    )


def deny_message(
    project: str,
    relative: str,
    box_path: str,
    box: str,
    notes: list[str],
    spawned: bool = True,
    inside: bool = False,
    branch: str = "",
    reason: str = "",
) -> str:
    """What the agent reads. The path first, because that is the actionable part.

    `spawned` distinguishes the first edit into a project from the fortieth. Both are
    blocked and both name the same box, but "a box has been spawned" is simply untrue
    on the reuse path, and a message that misdescribes what just happened is how an
    agent concludes it is in a loop.

    `inside` and `branch` decide the opening sentence, which is `block_reason`'s whole
    job: the reasons an edit reaches here are different states and a message that names
    the wrong one reads as a bug in the hook and invites working around it. Telling a
    session sitting in `carameli` that it "is not inside carameli" was the first version
    of that mistake; telling one on a freshly cut task branch that it is "parked on a
    home branch" was the second.

    Every remaining step is spelled out as a command, including the two that are not
    obvious from inside a session that is somewhere else:

    - **the box has no toolchain.** A worktree checks out tracked files, so there is no
      `.venv`; the guard does not wait for an install (see `worktree.apply_new`). An
      agent that goes straight to `/ship` hits the changed-scope lint gate with no ruff.
    - **`/ship` is read from the repo it runs in.** Its first step is a relative
      `python scripts/ship.py`, so run from here it would preflight *this* checkout and
      report that the box's branch is not the current one. "ship from inside the box"
      was accurate and, from a session that cannot cd, not actionable.

    `reason`, when non-empty, replaces the `block_reason` opening entirely: the
    foreign-box case is not a branch judgement, so every sentence `block_reason` can
    produce would misdescribe it, which is this docstring's own first lesson.
    """
    lines = [
        reason or block_reason(project, relative, branch, inside),
        "",
        (
            "A box has been spawned for it. Re-issue the edit against:"
            if spawned
            else "This session already has a box for this project. Re-issue the edit against:"
        ),
        f"    {Path(box_path) / relative}",
        "",
        *route_out_lines(project, box, box_path),
    ]
    if notes:
        lines += ["", *[f"note: {note}" for note in notes]]
    return "\n".join(lines)


def route_out_lines(project: str, box: str, box_path: str) -> list[str]:
    """The steps that hold whether the edit was re-aimed into the box or refused.

    Shared rather than duplicated because the two messages are read in the same
    situation and differ only in what already happened. The install and the `cd` are the
    two an agent cannot infer from anywhere else — see `deny_message` for why each is
    spelled as a command — and the reap paragraph has to survive both, since an agent
    that has just been *allowed* is the more likely of the two to go straight to /ship.
    """
    devkit_worktree = Path(__file__).parent / "worktree.py"
    return [
        f"The box is on a fresh agent/... branch cut from origin/<default>, with its own "
        f"COMPOSE_PROJECT_NAME ({box}) and port lease, so its stack cannot collide with "
        f"{project}'s.",
        "",
        # ASCII only, like every other hook's runtime output: this text is read back
        # through a pipe whose encoding is the console's, and an em dash arrives as a
        # replacement character on a cp1252 Windows terminal.
        "It has no .venv or node_modules yet - install them before running its tests or "
        "shipping it:",
        f"    python {devkit_worktree} provision {box} --yes",
        "",
        f"Then run every /ship step with the BOX as the working directory, because each "
        f"one reads the repo it is run in (`cd {box_path}`).",
        "",
        "Do NOT reap the box afterwards. `worktree.py reconcile` runs on a schedule and "
        "destroys it once its PR has actually MERGED; reaping at /ship time would do it "
        "on the strength of the push instead, which is the one moment the work exists "
        "only here if the PR was never created.",
        "",
        f"For a task worth naming, `worktree.py new {project} --slug <topic> --yes` cuts "
        f"a better-named one.",
    ]


def redirect_blocker(payload: dict, destination: Path, env: Mapping[str, str] | None = None) -> str:
    """ "" when this call can be re-aimed at `destination`, else why it must be blocked.

    Fails closed in every branch, because the two outcomes are not symmetric: a needless
    block costs a turn and says exactly what to do about it, while a rewrite that the
    harness does not honour puts the edit on the home branch and reports success.
    """
    if (env if env is not None else os.environ).get(ADAPTER_ENV):
        return (
            f"the session is running under a hook adapter ({ADAPTER_ENV} is set), whose "
            f"PreToolUse response has no updatedInput member"
        )
    if _tool_name(payload) not in REWRITABLE_TOOLS:
        return f"{_tool_name(payload) or 'this tool'}'s arguments are not rewritable by this hook"
    if not edited_path_key(payload):
        return "the call names no path to rewrite"
    literals = [literal for literal in old_strings(payload) if literal]
    if not literals:
        return ""
    try:
        content = destination.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"the box's copy of the file could not be read ({type(exc).__name__})"
    if any(literal not in content for literal in literals):
        return "the box's copy of the file does not contain the text this edit replaces"
    return ""


def redirect_message(
    project: str,
    relative: str,
    box_path: str,
    box: str,
    notes: list[str],
    spawned: bool = True,
    inside: bool = False,
    branch: str = "",
    reason: str = "",
) -> str:
    """What the agent reads when the edit went through at the box path instead.

    Leads with the fact that the write already happened, because the failure this text
    exists to prevent is the agent trusting its own request over the hook: it asked to
    write `<project>/<relative>`, got no error, and will read that path back unchanged
    unless something says otherwise. Naming the stale-read trap explicitly is the half
    the block message never needed — a refused edit leaves nothing to be wrong about.

    `spawned` carries the same distinction it carries in `deny_message`, for the same
    reason: "a box has been spawned" on the fortieth edit is untrue, and a message that
    misdescribes what just happened is how an agent concludes it is in a loop.
    """
    lines = [
        reason or block_reason(project, relative, branch, inside, prefix="Re-aimed"),
        "",
        (
            "A box has been spawned for it, and the edit was applied there instead, at:"
            if spawned
            else "This session already has a box for this project, and the edit was "
            "applied there instead, at:"
        ),
        f"    {Path(box_path) / relative}",
        "",
        f"Nothing was written to {project}. Read, edit, test and ship against the box from "
        f"here on: reading {relative} under {project} returns the file WITHOUT this change "
        f"and will contradict what you just wrote.",
        "",
        *route_out_lines(project, box, box_path),
    ]
    if notes:
        lines += ["", *[f"note: {note}" for note in notes]]
    return "\n".join(lines)


def emit_redirect(payload: dict, destination: Path, message: str) -> None:
    """Write the structured PreToolUse response that re-aims this call.

    No `permissionDecision`: the runner honours `updatedInput` only when the same object
    leaves the permission behaviour undefined, so setting even `"allow"` here would send
    the rewrite down the permission path and, worse, waive a decision that is not this
    hook's to make. The input is echoed whole with one key changed, because it replaces
    the original rather than merging into it.
    """
    arguments = dict(tool_input(payload))
    arguments[edited_path_key(payload)] = str(destination)
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": arguments,
                "additionalContext": message,
            }
        },
        sys.stdout,
    )


def failure_message(
    project: str,
    relative: str,
    error: str,
    inside: bool = False,
    branch: str = "",
    reason: str = "",
) -> str:
    """When spawning failed. Still a block, because allowing the edit is the bad outcome.

    Naming the manual command matters more than usual here: the whole promise of this
    hook is that being blocked is never a dead end, and a spawn that failed is the one
    case where the agent has to finish the job itself.

    Opens through `block_reason` for the same reason `deny_message` does: this line used
    to assert the session was outside the checkout and the branch was a home branch,
    and it is read in exactly the situation where an agent is already deciding whether
    to trust the hook.
    """
    return "\n".join(
        [
            reason or block_reason(project, relative, branch, inside),
            "",
            f"Spawning a box for it failed: {error}",
            "",
            "Cut one by hand and re-issue the edit there:",
            f"    python {Path(__file__).parent / 'worktree.py'} new {project} --slug <topic> --yes",
        ]
    )


def resolve_target(target: str, cwd: str) -> Path | None:
    """The edit's absolute target, or None when it cannot be resolved.

    Same resolution `redirect_decision` performs; split out so `main` can ask "is this
    under the box tier" before that function's checkout logic, which returns None for
    every box path and so cannot carry the ownership question.
    """
    if not target:
        return None
    try:
        base = Path(cwd) if cwd else Path.cwd()
        return (
            (base / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        )
    except (OSError, ValueError, RuntimeError):
        return None


def foreign_box(
    resolved: Path, root: Path, boxes: Mapping[str, Box], session: str
) -> tuple[Box, str] | None:
    """`(box, path relative to it)` when this edit targets a box leased to someone else.

    None — allow — for everything under `.worktrees/` that is not that:

    - the session's own box, including one whose lease records a hand-abbreviated
      session id (`sessions_match`);
    - an **unowned** box (empty lease session): an adopted orphan's lease cannot name
      an owner, so there is nobody to defend and blocking would dead-end every box
      that survived a lost lease file;
    - a payload with no session id: with no name to compare, a block would lock every
      box against a harness that simply did not send one;
    - a path in no live box at all — `leases.json`, the `slugs/` directory, a stray
      folder.

    This check exists because the allow at the top of `redirect_decision` used to be
    unconditional, and a second session that found a live box through `worktree.py
    list` could adopt it wholesale: two sessions' edits interleaved in one worktree
    until one of them watched files change under it mid-turn.
    """
    if not session:
        return None
    for box in boxes.values():
        home = worktree.box_path(root, box.name).resolve()
        if not _within(resolved, home):
            continue
        if not box.session or worktree.sessions_match(box.session, session):
            return None
        try:
            relative = str(resolved.relative_to(home))
        except ValueError:
            relative = resolved.name
        return box, relative
    return None


def foreign_box_reason(box: Box, relative: str) -> str:
    """The opening line for a foreign-box block: an ownership fact, not a branch one."""
    return (
        f"Blocked: {box.name} is a box leased to a different session, so an edit to "
        f"{relative} there would interleave two sessions' work in one worktree - the "
        f"collision the box tier exists to prevent."
    )


def claim_hint(box: Box, session: str) -> str:
    """The sanctioned takeover, spelled as the command that performs it."""
    return (
        f"if the user really has handed that box's work to this session, take its lease "
        f"over instead: python {Path(__file__).parent / 'worktree.py'} claim {box.name} "
        f"--session {session} --yes"
    )


def deliver(
    payload: dict,
    project: str,
    relative: str,
    box_path: str,
    box: str,
    notes: list[str],
    spawned: bool = True,
    inside: bool = False,
    branch: str = "",
    reason: str = "",
) -> tuple[int, str]:
    """Re-aim the call into the box, or block toward it. Returns (exit code, outcome).

    One function decides, because the two callers below differ only in whether the box
    was just cut — and a second copy of this choice is a second place for the adapter
    check in `redirect_blocker` to be forgotten, which fails by allowing the write.
    """
    destination = Path(box_path) / relative
    blocker = redirect_blocker(payload, destination)
    if blocker:
        print(
            deny_message(
                project,
                relative,
                box_path,
                box,
                [*notes, f"not re-aimed automatically: {blocker}"],
                spawned=spawned,
                inside=inside,
                branch=branch,
                reason=reason,
            ),
            file=sys.stderr,
        )
        return EXIT_BLOCK, "block"
    emit_redirect(
        payload,
        destination,
        redirect_message(
            project,
            relative,
            box_path,
            box,
            notes,
            spawned=spawned,
            inside=inside,
            branch=branch,
            reason=reason,
        ),
    )
    return EXIT_ALLOW, "redirect"


def current_boxes(root: Path, fallback: Mapping[str, Box]) -> Mapping[str, Box]:
    """The lease file as it stands *now*, or the caller's snapshot if it cannot be read.

    `main` reads the leases before this process waits on the spawn lock, and the whole
    point of the wait is that a box appears during it — so the mapping it passed is
    exactly the one that cannot answer "does this session have a box yet?".
    """
    try:
        return worktree.live_boxes(root)
    except Exception:
        return fallback


def after_failed_spawn(
    payload: dict,
    project: str,
    relative: str,
    root: Path,
    session: str,
    error: str,
    locked: bool = True,
    kind: str = "checkout",
    inside: bool = False,
    branch: str = "",
    reason: str = "",
    extra_notes: tuple[str, ...] = (),
) -> int:
    """Block toward a box — unless another run of this hook has just cut one.

    The recovery for the double registration described at the top of this module. Both
    copies handle the *same* tool call, so a failure here is very often not a failure at
    all: the winner's box is cut, registered, and about to be re-aimed into by the other
    process. Blocking on that reported the exact opposite of what the agent was told in
    the same object — "spawning a box for it failed" beside "the edit was applied there
    instead" — and nothing had been written either way.

    Blind to *why* the spawn failed, deliberately. Matching on `a branch named ... already
    exists` would model git's message; asking whether the box exists asks the question the
    decision actually turns on, and answers it correctly for any other way of losing a
    race. When there is genuinely no box, this is the block it always was.
    """
    raced = worktree.find_session_box(current_boxes(root, {}), project, session)
    if raced is None:
        _record_event(
            "guard-spawn-failed",
            (
                ("project", project),
                ("session", session),
                ("kind", kind),
                ("target", relative),
                ("locked", locked),
                ("detail", error),
            ),
        )
        print(
            failure_message(project, relative, error, inside=inside, branch=branch, reason=reason),
            file=sys.stderr,
        )
        return EXIT_BLOCK

    code, routed = deliver(
        payload,
        project,
        relative,
        str(worktree.box_path(root, raced.name)),
        raced.name,
        [
            *extra_notes,
            f"this hook's own spawn failed ({error}), but a concurrent run of it had "
            f"already cut this session's box - the edit was routed there rather than "
            f"refused",
        ],
        spawned=False,
        inside=inside,
        branch=branch,
        reason=reason,
    )
    _record_event(
        "guard-spawn-raced",
        (
            ("project", project),
            ("session", session),
            ("kind", kind),
            ("outcome", "raced" if routed == "redirect" else "raced-block"),
            ("box", raced.name),
            ("branch", raced.branch),
            ("target", relative),
            ("locked", locked),
            ("detail", error),
        ),
    )
    return code


def route_to_own_box(
    project: str,
    relative: str,
    workspace: Path,
    root: Path,
    session: str,
    boxes: Mapping[str, Box],
    payload: dict,
    inside: bool = False,
    branch: str = "",
    reason: str = "",
    kind: str = "checkout",
    extra_notes: tuple[str, ...] = (),
) -> int:
    """Route toward the session's box for `project`, reusing or spawning it.

    The one flow both entry points share: an edit that would land on a checkout's home
    branch, and an edit aimed into another session's box. The box is the outcome either
    way; `deliver` decides whether the edit reaches it by rewrite or by re-issue.

    A failed spawn is the one case that can be neither, and it blocks — unless the
    failure was this hook's other registration winning the race, which `after_failed_spawn`
    checks for. Allowing the edit where no box exists is the outcome this hook exists to
    prevent; the whole sequence runs under `worktree.spawn_lock` so that race is normally
    settled by waiting rather than by recovering from it.
    """
    with worktree.spawn_lock(
        root, f"{project}--{session or 'anon'}", wait=SPAWN_LOCK_WAIT
    ) as locked:
        # Read the leases again here rather than trusting `main`'s snapshot: the box this
        # session needs may have been cut by the other registration of this hook while
        # this process waited on the lock, and seeing it is what turns a lost race into
        # an ordinary reuse.
        existing = worktree.find_session_box(current_boxes(root, boxes), project, session)
        if existing is not None:
            code, routed = deliver(
                payload,
                project,
                relative,
                str(worktree.box_path(root, existing.name)),
                existing.name,
                list(extra_notes),
                spawned=False,
                inside=inside,
                branch=branch,
                reason=reason,
            )
            _record_event(
                "guard-route" if routed == "redirect" else "guard-block",
                (
                    ("project", project),
                    ("session", session),
                    ("kind", kind),
                    ("outcome", "reused"),
                    ("box", existing.name),
                    ("branch", existing.branch),
                    ("target", relative),
                ),
            )
            return code

        return spawn_and_route(
            project,
            relative,
            workspace,
            root,
            session,
            payload,
            locked=locked,
            inside=inside,
            branch=branch,
            reason=reason,
            kind=kind,
            extra_notes=extra_notes,
        )


def spawn_and_route(
    project: str,
    relative: str,
    workspace: Path,
    root: Path,
    session: str,
    payload: dict,
    locked: bool = True,
    inside: bool = False,
    branch: str = "",
    reason: str = "",
    kind: str = "checkout",
    extra_notes: tuple[str, ...] = (),
) -> int:
    """Cut this session's box for `project` and route the edit into it.

    Split from `route_to_own_box` so the spawn is one flat function under the lock its
    caller holds, rather than a second level of indentation inside the reuse decision.
    """
    try:
        slug = session_slug(session, task_slug.read(root, session))
        # quiet=True: this process's stderr is the block message, and a plan-time
        # provisioning warning would land as its first line, ahead of the reason.
        # `plan_respawn`, not `plan_new`: when this session already had a box in this
        # project and `reconcile` reaped it while its PR was still open, the branch is
        # the one thing worth keeping and cutting a new one would split the task across
        # two PRs. See `worktree.plan_respawn`.
        plan = worktree.plan_respawn(
            project, workspace, slug=slug, session=session, fetch=True, quiet=True
        )
        ok, notes = worktree.apply_new(plan, workspace, timeout=SPAWN_TIMEOUT, provision=False)
    except Exception as exc:
        return after_failed_spawn(
            payload,
            project,
            relative,
            root,
            session,
            f"{type(exc).__name__}: {exc}",
            locked=locked,
            kind=kind,
            inside=inside,
            branch=branch,
            reason=reason,
            extra_notes=extra_notes,
        )

    if not ok:
        return after_failed_spawn(
            payload,
            project,
            relative,
            root,
            session,
            "; ".join(notes) or "no detail",
            locked=locked,
            kind=kind,
            inside=inside,
            branch=branch,
            reason=reason,
            extra_notes=extra_notes,
        )

    # Said out loud because the alternative is an agent silently assuming an empty box.
    # A resumed one already carries this session's commits and an open PR, so `/ship`
    # updates that PR rather than opening one, and a `git log` here is not a mystery.
    resumed = (
        [
            f"resumed {plan.box.branch} -- this session's earlier box for {project} was "
            f"reaped while the work was still open, and its commits are on this branch."
        ]
        if plan.resumed
        else []
    )
    code, routed = deliver(
        payload,
        project,
        relative,
        plan.path,
        plan.box.name,
        [*resumed, *notes, *extra_notes],
        inside=inside,
        branch=branch,
        reason=reason,
    )
    _record_event(
        "guard-route" if routed == "redirect" else "guard-block",
        (
            ("project", project),
            ("session", session),
            ("kind", kind),
            ("outcome", "resumed" if plan.resumed else "spawned"),
            ("box", plan.box.name),
            ("branch", plan.box.branch),
            ("target", relative),
        ),
    )
    return code


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    workspace = Path(args[args.index("--workspace") + 1]) if "--workspace" in args else None
    # Resolved for the same reason `worktree.main` resolves it: the box path in the block
    # message is built from `workspace.parent`, and the agent has to be able to use it
    # from wherever its next tool call runs.
    workspace = (workspace or worktree.DEFAULT_WORKSPACE).resolve()
    # No multi-root registry means no cross-checkout edit is possible: a CI runner, a
    # fresh clone, anyone else's machine. Silence is the correct answer, not an error.
    if not workspace.is_file():
        return EXIT_ALLOW

    payload = parse_hook_input(sys.stdin.read())
    if payload is None or _tool_name(payload) not in MUTATING_TOOLS:
        return EXIT_ALLOW

    shell = _tool_name(payload) in SHELL_TOOLS

    # Before the registry read, because widening the matcher to the shell tools put this
    # hook on *every* Bash call rather than on the handful that edit files. A command that
    # writes nothing and moves no branch has to cost a payload parse and nothing else --
    # both of these are string work over the argv, with no subprocess and no file read.
    targets = guarded_targets(payload)
    switches = switch_targets(str(tool_input(payload).get("command") or "")) if shell else []
    if not targets and not switches:
        return EXIT_ALLOW

    try:
        projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    except OSError:
        return EXIT_ALLOW

    cwd = str(payload.get("cwd") or "")
    root = workspace.parent
    session = str(payload.get("session_id") or payload.get("sessionId") or "")

    # The branch tier, ahead of the write tiers: a command doing both is pathological, and
    # of the two outcomes the park is the one that would silence the write tier afterwards.
    # `switch_branch` is checked first so the lease registry is read only for a git call
    # that actually names a task branch -- which is the case being blocked anyway.
    for directory, ref in switches:
        moving_to = switch_branch(ref)
        if not moving_to:
            continue
        parked = switch_decision(
            directory, moving_to, cwd, root, projects, worktree.live_boxes(root)
        )
        if parked is None:
            continue
        kind, name, registered = parked
        _record_event(
            "guard-branch-block",
            (
                ("project", name),
                ("session", session),
                ("kind", kind),
                ("branch", moving_to),
                ("registered", registered),
            ),
        )
        print(switch_message(kind, name, moving_to, registered), file=sys.stderr)
        return EXIT_BLOCK

    # The branch is what the decision turns on, so it is also what the block message has
    # to name -- and reading it a second time would be a second subprocess per blocked
    # edit and, worse, could report a different branch than the one that was judged.
    # `redirect_decision` calls its lookup at most once, so recording it is enough.
    # Defined outside the loop so it closes over nothing that changes under it.
    observed: list[str] = []

    def observe(checkout: Path) -> str:
        observed.append(current_branch(checkout))
        return observed[-1]

    # An editor call names one path; a shell command can name several, and the first one
    # that needs a box decides the whole call — there is no partial outcome for a command
    # line. Everything below is what used to run once, run per candidate.
    for candidate in targets:
        # The box tier first: `redirect_decision` allows everything under `.worktrees/`,
        # so the ownership question has to be asked before it swallows the path. The
        # lease read happens only for edits actually aimed at the box tier — the common
        # checkout edit never pays it here.
        target = resolve_target(candidate, cwd)
        if target is not None and _within(target, worktree.boxes_root(root).resolve()):
            boxes = worktree.live_boxes(root)
            conflict = foreign_box(target, root, boxes, session)
            if conflict is None:
                continue
            box, relative = conflict
            return route_to_own_box(
                box.project,
                relative,
                workspace,
                root,
                session,
                boxes,
                payload,
                reason=foreign_box_reason(box, relative),
                kind="foreign-box",
                extra_notes=(
                    claim_hint(box, session),
                    *((shell_note(candidate),) if shell else ()),
                ),
            )

        observed.clear()
        decision = redirect_decision(candidate, cwd, root, projects, branch_of=observe)
        if decision is None:
            continue
        project, relative = decision
        branch = observed[-1] if observed else ""
        inside = _within(Path(cwd or "."), (root / project).resolve())

        return route_to_own_box(
            project,
            relative,
            workspace,
            root,
            session,
            worktree.live_boxes(root),
            payload,
            inside=inside,
            branch=branch,
            kind="shell" if shell else "checkout",
            extra_notes=(shell_note(candidate),) if shell else (),
        )

    return EXIT_ALLOW


if __name__ == "__main__":
    sys.exit(main())
