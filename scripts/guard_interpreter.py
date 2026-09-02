#!/usr/bin/env python3
"""The interpreter tier of `worktree-guard.py`: what a script hands an interpreter writes.

Split out of the guard rather than added to it, on the structural gate's own advice. The
guard is a 2300-line module already over every limit it has a baseline for, and this tier
is a genuinely separate question: the rest of that file asks *how a shell splits a command
line*, and everything here asks *what a program does with a path*. Nothing below imports
the guard, and the guard imports only `write_targets`.

**Why the tier exists at all.** The shell tier covered `sed -i`, `>` and `tee` but not a
heredoc'd `Path(...).write_text(...)` — and Claude Code's bypass-permissions mode tells
sessions, in text indistinguishable from their operator's, to "make file changes with sed,
heredocs, or short scripts". So the uncovered spelling is the one that instruction
produces most often, and a session following its operator wrote to a home branch every
time with the guard wired, running and silent. The suite had the gap pinned as
`test_an_interpreter_script_is_the_documented_gap`, asking whoever closed it to leave the
limit where closing it would show up as a failure.

**Gated on a write sink, never on a path.** `open('x')` and `open('x','w')` name the same
literal and only one of them writes; reading is most of what a guarded session does
through an interpreter, so a tier keyed on the path would be a false positive on nearly
every call.

Stdlib only, and no side effects at import: `worktree-guard.py` imports it on every tool
call.

Tested in `tests/test_guard_interpreter.py`.
"""

from __future__ import annotations

import re

# The heredoc marker, matching `worktree-guard.strip_heredocs`. Duplicated deliberately
# and narrowly: importing a hyphen-named script from here would mean loading the guard to
# read one regex, and the guard imports this module.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# An interpreter, its inline-code flag, and the program after it — matched as one thing.
#
# Anchoring on the verb is what makes the flag safe to read: `-e` belongs to `grep` and
# `sed` far more often than to an interpreter, and only the word in front of it says
# which. Matching the whole shape also removes any need to split the command line into
# statements first, which is the part that went wrong when it did: splitting on `;` cut
# `python -c 'import pathlib; Path(...).write_text(...)'` in half, and the half that kept
# the flag no longer had its closing quote.
INLINE_CALL = re.compile(
    r"""(?:^|[\s;&|])                       # a statement boundary, loosely
        (?:\S*[/\\])?                       # an optional path in front of the verb
        (?:python[0-9.]*|pythonw|py|node|nodejs)(?:\.exe)?
        \s+(?:-\S+\s+)*?                    # any flags before the code one
        (?:-c|-e|--command|--eval)\s+
        (?:'([^']*)'|"([^"]*)"|(\S+))       # the program, quoted or bare
    """,
    re.VERBOSE,
)

# Calls that write. Matching the CALL rather than the literal is the whole design.
#
# Two deliberate omissions, each costing coverage to buy precision:
#   - `.replace(` — `str.replace` is everywhere and `Path.replace` is rare;
#   - a bare `.write(` — `sys.stdout.write(open('x').read())` writes nothing to disk and
#     names a real path, so including it would block reads.
# `open(..., "w")` already covers the file-handle case, which is how those paths are named.
WRITE_SINK = re.compile(
    r"""
      \.write_text\s*\(
    | \.write_bytes\s*\(
    | \.writelines\s*\(
    | \.touch\s*\(
    | \.unlink\s*\(
    | \.rename\s*\(
    | \bopen\s*\([^)]*?,\s*['"][^'"]*[wax][^'"]*['"]
    | \bos\.(?:remove|unlink|rename|replace|makedirs|mkdir)\s*\(
    | \bshutil\.(?:copy2?|copyfile|copytree|move|rmtree)\s*\(
    | \bjson\.dump\s*\(
    | \bfs\.(?:write|append|rm|unlink|copy|rename)[A-Za-z]*\s*\(
    """,
    re.VERBOSE,
)

# Every quoted literal in a snippet. Deliberately naive: the tier is sink-gated, and the
# caller drops anything that does not resolve into a registered checkout.
CODE_STRING = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")

# A literal worth treating as a path: it carries a separator, or ends in an extension
# whose first character is a LETTER. That last clause is what keeps `3.14` out — a version
# number otherwise reads as `<name>.<ext>` and routes a write nobody asked for.
PATHLIKE_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,5}$")


def heredoc_bodies(command: str) -> list[str]:
    """The heredoc bodies in `command` — the program text, without its markers.

    The exact complement of the guard's `strip_heredocs`, which drops these so the command
    line can be tokenised without reading `>=` as a redirection. One tier needs the line
    with the scripts removed; this one needs the scripts.
    """
    lines = (command or "").split("\n")
    bodies: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        for tag in (match.group(2) for match in HEREDOC.finditer(line)):
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != tag:
                body.append(lines[index])
                index += 1
            index += 1  # the terminator line itself
            bodies.append("\n".join(body))
    return bodies


def inline_snippets(command: str) -> list[str]:
    """Every `-c`/`-e` program handed to an interpreter on this command line.

    One regex rather than the guard's tokeniser, and the reason is the one that put this
    tier in its own module: `shell_tokens` answers "how does a shell split this line", and
    this needs only "is the word in front of `-c` an interpreter". Taking the guard's
    answer would mean importing the guard, which imports this.

    The verb is part of the match because the flag is not distinctive: `grep -e pattern`
    and `sed -e s/a/b/` both take one and neither is a program.
    """
    return [
        match.group(1) or match.group(2) or match.group(3)
        for match in INLINE_CALL.finditer(command or "")
    ]


def _pathlike(value: str) -> bool:
    """Whether a string literal from a snippet is worth treating as a path."""
    if not value or len(value) > 400 or value.startswith("-"):
        return False
    if "/" in value or "\\" in value:
        return True
    return bool(PATHLIKE_EXTENSION.search(value))


def code_write_targets(code: str) -> list[str]:
    """Paths a snippet names, when it also contains a call that writes. [] otherwise.

    No attempt is made to associate a literal with the sink it belongs to. A snippet that
    writes is judged on every path it names, which over-reaches on a script that reads one
    file and writes another; the caller then drops anything outside a registered checkout,
    git-ignored, or already dirty, and what survives is a real write to a home branch often
    enough to be worth the occasional extra one. Parsing properly is the alternative, and
    an `ast.parse` of a snippet that may not be Python at all is a hook that raises on a
    PreToolUse call.
    """
    if not code or not WRITE_SINK.search(code):
        return []
    found: list[str] = []
    for match in CODE_STRING.finditer(code):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        if _pathlike(value) and value not in found:
            found.append(value)
    return found


def write_targets(command: str) -> list[str]:
    """Every path the programs embedded in `command` are about to write.

    The guard's single entry point into this tier. Heredoc bodies are read whenever one is
    present, without checking the verb: a heredoc is only program text because something
    reads it as such, and the readers vary (`python -`, `uv run -`, a wrapper). Requiring a
    recognised verb would reintroduce the gap for every spelling not on the list, and
    reading a body that turns out to be data costs nothing — nothing here fires without a
    sink.

    **A leading `cd` is not followed**, unlike the command-line tier's operands. The paths
    are handed back relative, and the guard resolves them against the tool call's own cwd
    — which is exactly what that tier already does for any `cd` form it cannot follow, and
    for the reason it gives there: a base that cannot be followed means a relative name,
    and a relative name still resolves into the checkout. The conservative direction.
    """
    found: list[str] = []
    for snippet in (*heredoc_bodies(command), *inline_snippets(command)):
        for target in code_write_targets(snippet):
            if target not in found:
                found.append(target)
    return found
