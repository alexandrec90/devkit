#!/usr/bin/env python3
"""Measure what the instruction tier costs a session, and name what can move.

The premise this exists to correct: an instruction file feels free because nothing
bills it at the point of writing. It is billed on **every API call of every session**,
because each call re-sends the whole conversation â€” so a paragraph added once is paid
for thousands of times, and a paragraph nobody reads costs exactly as much as one that
saves the session an hour.

devkit already retired one attempt at this (`.claude/skills/audit-claude-md/SKILL.md`,
in `sync-devkit.py`'s `_RETIRED_CLAUDE_PATHS`). It failed because it was a checklist
with no measurement: it asked whether a file was "under 200 lines", told the agent to
"highlight at least 3 lines that can be pruned", and had no idea which files a session
actually loads. A quota produces cuts whether or not any are warranted, and a line
count cannot tell the expensive tier from the free one. This module reports **tokens by
tier**, because the tier is the whole decision â€” see `TIERS`.

It reads. It never edits: the judgment about which paragraph is inert lives in the
`prune-instructions` skill, and the one about which file may be edited at all lives in
`sync-devkit.py`'s `MANIFEST` (a vendored file edited in a consumer is drift, not a
cleanup). `vendored` on each `Doc` carries that answer to the caller.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The instructions switch's record, so a stood-down tier is reported at its real weight
# rather than at zero. This tool exists to answer "what is a session paying for these
# files"; with the files moved aside the honest answer is what they *would* cost, and a
# report saying nothing is loaded would read as the pruning having already been done.
import harness_state

REPO_ROOT = Path(__file__).resolve().parents[1]

# What loading a file actually costs the session that loads it.
#
#   hot       â€” read in full at session start, on every session, forever.
#   lazy      â€” read only when a file it is scoped to is touched (a subtree CLAUDE.md,
#               a rule with `paths:`). Costs nothing in a session that never goes there.
#   on-demand â€” read only when something asks for it by name (a skill body, a sibling
#               reference file linked from an instruction file).
#
# Moving a paragraph down this list is the cheapest edit in the repo: it drops the
# recurring cost to near zero and loses no information, which is why it is the default
# remedy and deletion is the exception.
TIERS = ("hot", "lazy", "on-demand")

# Chars per token. A rough divisor, deliberately: the point is comparing sections
# against each other and tracking a total over time, and both survive the estimate
# being off by a few percent. Prose in this repo measures close to this.
CHARS_PER_TOKEN = 3.7

# `.claude/rules/authoring.md` owns this number in prose ("under **500 lines**").
# It is duplicated here because prose cannot be parsed reliably, and
# `test_the_line_cap_matches_authoring_md` checks the duplicate against the source, so
# the two cannot drift apart silently.
LINE_CAP = 500

# A rule with no `paths:` loads in every session, so it is held to a budget a scoped
# rule is not. Nothing magic about the figure â€” it is roughly the point at which a
# rule is carrying reference material rather than policy, and `windowless-jobs.md` is
# the precedent for what to do about it.
UNSCOPED_RULE_TOKENS = 1200

# A memory file nobody has touched in this long is reported for review. Memories go
# stale silently â€” nothing re-checks one when the code it describes changes â€” and the
# harness recalls them as authority regardless.
STALE_MEMORY_DAYS = 45

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H2 = re.compile(r"^##\s+(.+?)\s*$")
_PATHS_KEY = re.compile(r"^paths:\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Doc:
    """One instruction file, with what it costs and whether this repo may edit it."""

    path: Path
    tier: str
    tokens: int
    lines: int
    vendored: bool
    note: str = ""

    @property
    def rel(self) -> str:
        try:
            return self.path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return self.path.as_posix()


@dataclass(frozen=True)
class Finding:
    """Something worth a human's attention, with the remedy named rather than implied."""

    kind: str
    subject: str
    detail: str
    remedy: str


def tokens_from_chars(chars: int) -> int:
    """The estimate, from a character count.

    Split out from `estimate_tokens` because `sections` accumulates lengths as it
    walks a file and has no string left to measure by the time it reports. Calling
    the string form with that integer raised `TypeError: object of type 'int' has no
    len()` — only on a file with at least one H2, which is every hot file and no
    fixture short enough to notice.
    """
    return round(chars / CHARS_PER_TOKEN)


def estimate_tokens(text: str) -> int:
    return tokens_from_chars(len(text))


def frontmatter(text: str) -> str:
    match = _FRONTMATTER.match(text)
    return match.group(1) if match else ""


def rule_is_scoped(text: str) -> bool:
    """True when a rule declares `paths:`, which is what makes it load lazily.

    Read off the frontmatter block only. A `paths:` line in the body is prose about
    frontmatter â€” this file's own rule sections contain several â€” and counting one
    would report an unscoped rule as scoped, which is the direction that hides cost.
    """
    return bool(_PATHS_KEY.search(frontmatter(text)))


def sections(text: str) -> list[tuple[str, int]]:
    """H2 sections with their token cost, in document order.

    The unit a re-tiering edit actually moves. A file's total says it is expensive;
    only the section breakdown says which paragraph to move and where.
    """
    current = "(preamble)"
    sizes: dict[str, int] = {current: 0}
    order = [current]
    for line in text.splitlines(keepends=True):
        heading = _H2.match(line.rstrip("\n"))
        if heading:
            current = heading.group(1)
            if current not in sizes:
                sizes[current] = 0
                order.append(current)
        sizes[current] += len(line)
    return [(name, tokens_from_chars(sizes[name])) for name in order]


def manifest_paths(devkit_root: Path) -> frozenset[str]:
    """The vendored set, read from `sync-devkit.py` rather than restated.

    A vendored file is byte-compared against upstream, so editing one in a consuming
    project is reported as drift by that project's PR gate â€” the cleanup would land as
    a red gate in someone else's repo. Returns empty when the file cannot be read,
    which fails toward reporting nothing as vendored; the caller decides what that is
    worth, and `--check` treats an empty set as a reason to say so.
    """
    source = devkit_root / "scripts" / "sync-devkit.py"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(re.findall(r'^\s*"([^"]+\.(?:md|py|json|yaml|yml))",\s*$', text, re.MULTILINE))


def slug_for(cwd: Path) -> str:
    """The `~/.claude/projects/` directory name for a working directory.

    Every non-alphanumeric run in the absolute path becomes a dash, so
    `C:\\Users\\a\\vs_code\\devkit` becomes `C--Users-a-vs-code-devkit`. Reproduced
    here because a mistyped cwd produces a *different* slug and therefore a second,
    permanently orphaned memory directory that nothing will ever recall from â€”
    `find_orphan_memory` exists to name those.
    """
    return re.sub(r"[^A-Za-z0-9]+", lambda m: "-" * len(m.group()), str(cwd))


def memory_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude" / "projects"


def _memory_files(root: Path) -> list[Path]:
    """Every `CLAUDE.md` under `root` that a session could load, by its LIVE path.

    Live files and ones the instructions switch has moved aside, unioned: a report saying
    nothing is loaded while that group is off would read as the pruning having already
    been done, and `_read` finds each body wherever it currently is.

    Deliberately skips `.worktrees/` and dependency trees: an ephemeral box holds a copy
    of the same files, and counting those reports a workspace as carrying ten times the
    instruction load it does.
    """
    skip = {".worktrees", "node_modules", ".venv", ".git", "__pycache__", "templates"}
    found = set(root.rglob("CLAUDE.md")) | _switched_off(root)
    return sorted(
        path
        for path in found
        if path.name == "CLAUDE.md" and not skip & set(path.relative_to(root).parts)
    )


def _rule_files(root: Path) -> list[Path]:
    """`.claude/rules/*.md`, live or switched off. See `_memory_files`."""
    rules = root / ".claude" / "rules"
    live = set(rules.glob("*.md")) if rules.is_dir() else set()
    return sorted(live | {path for path in _switched_off(root) if path.parent == rules})


def _switched_off(root: Path) -> set[Path]:
    """The instruction files the switch is holding, by the path they came from."""
    return {root / relpath for relpath, _held in harness_state.instruction_sources(root)}


def discover(root: Path, vendored: frozenset[str]) -> list[Doc]:
    """Every instruction file under `root`, classified by tier.

    Deliberately skips `.worktrees/` and dependency trees: an ephemeral box holds a
    copy of the same files, and counting those reports a workspace as carrying ten
    times the instruction load it does.
    """
    docs: list[Doc] = []

    for path in _memory_files(root):
        text = _read(path)
        at_root = path.parent == root
        docs.append(
            Doc(
                path=path,
                tier="hot" if at_root else "lazy",
                tokens=estimate_tokens(text),
                lines=len(text.splitlines()),
                vendored=_is_vendored(path, root, vendored),
                note="root â€” every session"
                if at_root
                else f"loaded on touch of {path.parent.name}/",
            )
        )

    for path in _rule_files(root):
        text = _read(path)
        scoped = rule_is_scoped(text)
        docs.append(
            Doc(
                path=path,
                tier="lazy" if scoped else "hot",
                tokens=estimate_tokens(text),
                lines=len(text.splitlines()),
                vendored=_is_vendored(path, root, vendored),
                note="scoped by paths:" if scoped else "UNSCOPED â€” every session",
            )
        )

    skills = root / ".claude" / "skills"
    for path in sorted(skills.rglob("*.md")) if skills.is_dir() else []:
        text = _read(path)
        docs.append(
            Doc(
                path=path,
                tier="on-demand",
                tokens=estimate_tokens(text),
                lines=len(text.splitlines()),
                vendored=_is_vendored(path, root, vendored),
                note="body loads only when invoked",
            )
        )

    return docs


def _read(path: Path) -> str:
    try:
        return harness_state.instruction_text(path)
    except OSError:
        return ""


def _is_vendored(path: Path, root: Path, vendored: frozenset[str]) -> bool:
    try:
        return path.relative_to(root).as_posix() in vendored
    except ValueError:
        return False


def hot_total(docs: list[Doc]) -> int:
    """What a session pays before its first tool call."""
    return sum(doc.tokens for doc in docs if doc.tier == "hot")


def find_stale_memory(root: Path, now: float, days: int = STALE_MEMORY_DAYS) -> list[Finding]:
    cutoff = now - days * 86400
    out: list[Finding] = []
    for path in sorted(root.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        try:
            age_days = int((now - path.stat().st_mtime) / 86400)
        except OSError:
            continue
        if path.stat().st_mtime < cutoff:
            out.append(
                Finding(
                    kind="stale-memory",
                    subject=path.name,
                    detail=f"untouched for {age_days} days",
                    remedy="verify the fact still holds; delete it if the file, flag or command it names is gone",
                )
            )
    return out


def workspace_of(root: Path) -> Path:
    """The multi-root workspace a checkout sits in.

    An ephemeral box lives at `<workspace>/.worktrees/<box>`, so its parent is the box
    store and not the workspace. Reading the parent literally makes every real checkout
    look like a slug nothing produces — including the box's own project, whose memory
    directory is the largest on the machine.
    """
    parent = root.parent
    return parent.parent if parent.name == ".worktrees" else parent


def live_slugs(root: Path) -> set[str]:
    """Every slug a directory on this machine can still produce.

    The checkout, **the workspace directory above it**, every checkout beside it, and
    every live box. The workspace is what a pruning pass found missing: a multi-root
    workspace is a working directory an agent opens sessions in — it has its own
    `CLAUDE.md` governing exactly that — so its memories are as recallable as any
    project's, and reporting them as orphaned invites deleting them.
    """
    workspace = workspace_of(root)
    candidates = [root, workspace, *_children(workspace), *_children(workspace / ".worktrees")]
    return {slug_for(path) for path in candidates if path.is_dir()}


def _children(path: Path) -> list[Path]:
    return sorted(path.iterdir()) if path.is_dir() else []


def find_orphan_memory(projects: Path, live: set[str]) -> list[Finding]:
    """Memory directories whose slug no working directory produces any more.

    A renamed, moved or mistyped project path strands its memories: nothing recalls
    them, nothing expires them, and they are invisible because the directory name is
    the only evidence of which cwd made it.

    Matched **case-insensitively**, because Windows hands back the drive letter in
    whichever case the launching shell used: `c--Users-...` and `C--Users-...` are the
    same checkout, both directories exist under `~/.claude/projects`, and a
    case-sensitive comparison reported 50 live memories across three directories as
    orphaned — a finding whose stated remedy is to delete them.
    """
    recallable = {slug.casefold() for slug in live}
    out: list[Finding] = []
    for entry in sorted(projects.iterdir()) if projects.is_dir() else []:
        memory = entry / "memory"
        if not memory.is_dir() or entry.name.casefold() in recallable:
            continue
        held = [item for item in memory.glob("*.md") if item.name != "MEMORY.md"]
        if held:
            out.append(
                Finding(
                    kind="orphan-memory",
                    subject=entry.name,
                    detail=f"{len(held)} memories under a slug no live checkout produces",
                    remedy=f"no session can recall these; delete {memory.parent}",
                )
            )
    return out


def find_oversized(docs: list[Doc]) -> list[Finding]:
    out: list[Finding] = []
    for doc in docs:
        if doc.lines > LINE_CAP:
            out.append(
                Finding(
                    kind="over-line-cap",
                    subject=doc.rel,
                    detail=f"{doc.lines} lines, cap is {LINE_CAP}",
                    remedy="split reference material into a sibling .md and link it (authoring.md, progressive disclosure)",
                )
            )
        if (
            doc.tier == "hot"
            and doc.path.parent.name == "rules"
            and doc.tokens > UNSCOPED_RULE_TOKENS
        ):
            out.append(
                Finding(
                    kind="fat-unscoped-rule",
                    subject=doc.rel,
                    detail=f"{doc.tokens} tok on every session, unscoped",
                    remedy="add paths: if it is domain-specific, or move its reference half to a sibling file",
                )
            )
    return out


def render(docs: list[Doc], findings: list[Finding], memory_note: str = "") -> str:
    lines = ["# Instruction budget", ""]
    total = hot_total(docs)
    lines.append(
        f"Always-loaded (hot) total: **{total} tok** per session, re-sent on every API call."
    )
    lines.append("")

    for tier in TIERS:
        group = sorted((d for d in docs if d.tier == tier), key=lambda d: -d.tokens)
        if not group:
            continue
        subtotal = sum(d.tokens for d in group)
        lines.append(f"## {tier} â€” {subtotal} tok across {len(group)} file(s)")
        lines.append("")
        for doc in group:
            mark = " [vendored: edit in devkit only]" if doc.vendored else ""
            lines.append(
                f"- `{doc.rel}` â€” {doc.tokens} tok, {doc.lines} lines â€” {doc.note}{mark}"
            )
        lines.append("")

    hot_docs = [d for d in docs if d.tier == "hot"]
    if hot_docs:
        lines.append("## Hot files by section")
        lines.append("")
        lines.append("The unit a re-tiering edit moves. A large section that a session needs only")
        lines.append("while inside one subtree is the candidate; one that must fire *before* the")
        lines.append("first file is touched cannot move down and is not.")
        lines.append("")
        for doc in sorted(hot_docs, key=lambda d: -d.tokens):
            lines.append(f"### `{doc.rel}`")
            for name, cost in sorted(sections(_read(doc.path)), key=lambda pair: -pair[1]):
                lines.append(f"- {cost:>5} tok â€” {name}")
            lines.append("")

    lines.append("## Findings")
    lines.append("")
    if memory_note:
        lines.append(memory_note)
        lines.append("")
    if not findings:
        lines.append("None.")
    for finding in findings:
        lines.append(f"- **{finding.kind}** `{finding.subject}` â€” {finding.detail}")
        lines.append(f"  - remedy: {finding.remedy}")
    lines.append("")
    return "\n".join(lines)


def collect(
    root: Path, devkit_root: Path, home: Path | None = None, now: float | None = None
) -> tuple[list[Doc], list[Finding], str]:
    import time

    now = now if now is not None else time.time()
    docs = discover(root, manifest_paths(devkit_root))
    findings = find_oversized(docs)

    projects = memory_root(home)
    slug = slug_for(root)
    mine = projects / slug / "memory"
    note = ""
    if mine.is_dir():
        findings.extend(find_stale_memory(mine, now))
    else:
        note = f"No memory directory for this root (`{slug}`) â€” nothing to prune."
    return docs, findings, note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root to measure")
    parser.add_argument(
        "--devkit", type=Path, default=REPO_ROOT, help="devkit checkout, for the vendored set"
    )
    parser.add_argument("--check", action="store_true", help="exit 1 when there are findings")
    parser.add_argument(
        "--orphans", action="store_true", help="also scan for orphaned memory slugs"
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    docs, findings, note = collect(root, args.devkit.resolve())

    if args.orphans:
        findings.extend(find_orphan_memory(memory_root(), live_slugs(root)))

    report = render(docs, findings, note)

    artifact = REPO_ROOT / "logs" / "instruction-budget.log"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(report, encoding="utf-8")

    print(
        f"hot: {hot_total(docs)} tok/session across {sum(1 for d in docs if d.tier == 'hot')} file(s); "
        f"{len(findings)} finding(s)"
    )
    print(f"report: {artifact}")

    return 1 if (args.check and findings) else 0


if __name__ == "__main__":
    sys.exit(main())
