#!/usr/bin/env python3
"""The read half of the harness-events ledger: what is still open, and what was fixed.

`harness_events.py` gave the harness a place to record what it did to an agent, and
`report-harness-defect.py` gave an agent a place to record its judgment of it. Neither
gave anyone a way to say **"that one is fixed"** -- the ledger is append-only by design,
so the only state an event ever had was its own existence.

That is not a cosmetic gap. `workspace-status.events_line` reported the triage backlog
by counting the last seven days, which means an event left the session-start line by
**ageing out**, never by being dealt with: a defect fixed the same hour kept being
counted for a week, and one nobody looked at vanished silently on day eight. The two
failure directions are the ones a debt list must not have, and it had both.

So a resolution is itself an event -- `triage-resolved`, carrying `ref=<item id>` -- and
"open" means an event with no resolution naming it. Three properties follow, and a
change here has to keep all three:

- **The ledger stays append-only, and carries no state file.** A separate file recording
  what is resolved was the alternative, and it is the one `events_line`'s own docstring
  already rejected: a second file that can go stale, for a list this short. (It is no
  longer a *single* file -- there is one shard per machine, unioned on read -- but that
  is a partition of the same append-only log, not state kept beside it.)
- **An id is content-addressed** (`item_id`), not a line number, so appending never
  renumbers anything and a resolution recorded months ago still names its event.
- **Nothing here can mark an item resolved on its own.** Ageing out was the silent
  laundering that made the count meaningless; a resolution needs a `--note` saying what
  actually fixed it, exactly as `.devkit-untested.txt` refuses to be seeded over.

Tested in `tests/test_harness_triage.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import harness_events  # noqa: E402 - sibling hooks dir, put on the path just above

# The event names that mean a human or an agent should look. Kept here rather than in
# `workspace-status.py` so the session-start line and this tool cannot disagree about
# what the backlog is; `workspace-status` imports it.
TRIAGE_EVENTS = ("agent-report", "guard-spawn-failed", "codex-translation-gap")
RESOLVED_EVENT = "triage-resolved"

# The field each event name carries its human-readable substance in, in preference
# order. A signature groups by it, so `--resolve-like` can retire one recurrence of a
# defect and every other recurrence of the same one with it.
DETAIL_KEYS = ("message", "detail", "command", "files")
SIGNATURE_WIDTH = 80

ARTIFACT = Path("logs") / "harness-triage.log"


@dataclass(frozen=True)
class Item:
    """One ledger line, parsed. `raw` is kept so the id can be recomputed from it."""

    stamp: str
    event: str
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""

    @property
    def id(self) -> str:
        return item_id(self.raw)

    @property
    def project(self) -> str:
        """The repo, normalised on read as well as on write.

        The writers recorded a box directory name for months (`harness_events.
        project_name` is the fix), and the ledger is append-only, so those rows keep
        saying `devkit--guard-quoted-redirect-0823` forever. Normalising here is what
        makes a signature group across them instead of splitting one recurring defect
        into one pseudo-project per box.
        """
        return harness_events.project_name(Path(self.fields.get("project", "-")))

    @property
    def agent(self) -> str:
        """Which runtime's hook wrote this row -- `claude`, `codex`, or `unknown`.

        Unknown is not a degenerate case to paper over: every row written before
        `harness_events.agent_name` existed has no `agent=` field and never will, the
        ledger being append-only, so the honest answer for those is that nobody
        recorded it.
        """
        return self.fields.get("agent", "").strip(" -") or harness_events.UNKNOWN_AGENT

    @property
    def host(self) -> str:
        """Which machine's harness wrote this row.

        Deliberately absent from `signature`: see `harness_events.HOST_ENV`. One defect
        hit on two machines is one defect, and a single `--resolve-like` has to retire
        both copies. Which machines saw it is evidence -- "only reproduces on the
        laptop" is a real diagnosis -- and `render` shows it for that reason.
        """
        return self.fields.get("host", "").strip(" -") or harness_events.UNKNOWN_HOST

    @property
    def detail(self) -> str:
        for key in DETAIL_KEYS:
            if self.fields.get(key, "").strip(" -"):
                return self.fields[key]
        return "-"

    @property
    def signature(self) -> tuple[str, str, str, str]:
        """What `--resolve-like` treats as the same defect recurring.

        `agent` is in here because the same hook does not behave the same under both
        runtimes, so identical prose from Codex and from Claude is **not** one defect
        recurring -- the capped-Bash gate is unported to Codex, and a PreToolUse
        response that re-aims a call under Claude is dropped under Codex. Grouping the
        two together let one fix retire the other's evidence, which is the failure the
        user of this ledger described: a hook reporting an error for Codex says nothing
        about whether Claude has it too.
        """
        return (self.event, self.agent, self.project, self.detail[:SIGNATURE_WIDTH])


def item_id(raw: str) -> str:
    """A stable handle for one ledger line.

    Content-addressed on purpose: the ledger is append-only, so a positional id would
    be correct only until the next event, and a resolution recorded against one would
    silently come to name a different line.
    """
    return hashlib.blake2s(raw.strip().encode("utf-8", "replace"), digest_size=4).hexdigest()


def parse_line(raw: str) -> Item | None:
    """One ledger line as an `Item`; None for a blank or malformed one.

    Malformed is not an error worth raising: the ledger is written by best-effort
    appenders from several processes, and one torn line must not stop the read side.
    """
    text = raw.strip()
    if not text:
        return None
    stamp, _, rest = text.partition("\t")
    if not rest.startswith("event="):
        return None
    pairs = rest.split("\t")
    fields: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if sep:
            fields[key] = value
    event = fields.pop("event", "")
    if not event:
        return None
    return Item(stamp=stamp, event=event, fields=fields, raw=text)


def read_items(text: str) -> list[Item]:
    """Every parseable event in ledger `text`, in file order."""
    return [item for item in (parse_line(line) for line in text.splitlines()) if item]


def resolved_refs(items: list[Item]) -> set[str]:
    """Every item id some `triage-resolved` event names."""
    return {
        item.fields["ref"]
        for item in items
        if item.event == RESOLVED_EVENT and item.fields.get("ref")
    }


def open_items(items: list[Item], events: tuple[str, ...] = TRIAGE_EVENTS) -> list[Item]:
    """The triage events with no resolution naming them, newest first."""
    done = resolved_refs(items)
    found = [i for i in items if i.event in events and i.id not in done]
    return list(reversed(found))


def for_agent(items: list[Item], agent: str) -> list[Item]:
    """Only the rows some runtime's hooks wrote. `""` keeps everything.

    Triage is per-agent work far more often than not: a Codex-only block is evidence
    about the translation layer, and reading it while fixing Claude's side wastes the
    pass. `unknown` selects the rows that predate the field.
    """
    wanted = agent.strip().lower()
    return [i for i in items if not wanted or i.agent == wanted]


def groups(items: list[Item]) -> list[tuple[tuple[str, str, str, str], list[Item]]]:
    """Open items collected by signature, most recurrences first.

    24 of the first 39 items in this machine's backlog were one spawn race recorded 24
    times. Listing them flat reads as 24 problems, which is how a backlog stops being
    read at all.
    """
    buckets: dict[tuple[str, str, str, str], list[Item]] = {}
    for item in items:
        buckets.setdefault(item.signature, []).append(item)
    return sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def render(items: list[Item]) -> str:
    """The open backlog as text -- the artifact, and what the CLI prints."""
    if not items:
        return "harness-triage: nothing open\n"
    lines = ["# source: scripts/harness_triage.py", f"# open: {len(items)}", ""]
    for (event, agent, project, _), bucket in groups(items):
        head = bucket[0]
        seen = f" (x{len(bucket)}, since {bucket[-1].stamp})" if len(bucket) > 1 else ""
        lines.append(f"=== {event}  [{agent}]  {project}  [{head.id}]{seen} ===")
        lines.append(f"  when   {head.stamp}")
        seen_on = sorted({i.host for i in bucket} - {harness_events.UNKNOWN_HOST})
        if seen_on:
            lines.append(f"  host   {' '.join(seen_on)}")
        lines.append(f"  detail {head.detail}")
        if head.fields.get("version", "").strip(" -"):
            lines.append(f"  version {head.fields['version']}")
        if len(bucket) > 1:
            lines.append(f"  ids    {' '.join(i.id for i in bucket)}")
        lines.append("")
    lines.append(
        "resolve: python scripts/harness_triage.py --resolve-like <id> --note '<what fixed it>'"
    )
    return "\n".join(lines) + "\n"


def ledger_file(root: Path | None = None) -> Path:
    """The shard this tool *writes* to -- this machine's own.

    `$DEVKIT_DIR` first, for the reason `workspace-status.events_line` documents: every
    writer resolves the ledger to the permanent checkout, so from a box the local
    `logs/` is a directory nothing has ever written to.

    A resolution is an event like any other, so it lands here rather than in the shard
    holding the row it retires. That is what keeps the pooled directory conflict-free:
    no machine ever writes to another's file. `expand_like` is what makes it correct --
    see `load`.
    """
    return harness_events.ledger_path(root) or (REPO_ROOT / harness_events.LEDGER)


def ledger_files(root: Path | None = None) -> list[Path]:
    """Every shard this tool *reads*: this machine's, and any other machine's beside it."""
    return harness_events.ledger_paths(root) or [REPO_ROOT / harness_events.LEDGER]


def load(root: Path | None = None) -> list[Item]:
    """Every event on every shard in reach, oldest first; empty when there is none.

    Reading the union is what makes a pooled ledger work as one backlog rather than as
    two that happen to share a directory. Two consequences are load-bearing:

    - **A group spans machines.** `signature` excludes the host, so one defect hit on
      both is one group, and the `--resolve-like` that retires it expands over *both*
      machines' open rows -- ids are content-addressed per line, so the two copies have
      different ids and nothing else would have connected them.
    - **Order is by stamp, not by file.** Concatenating shards gives file order, which is
      chronological within a shard and meaningless across them; `open_items` reverses
      this for "newest first" and would otherwise interleave two machines' history
      arbitrarily. Stamps are ISO-8601 in UTC, so lexicographic *is* chronological.

    A shard that cannot be read is skipped rather than fatal: a half-synced file is the
    ordinary state of a directory two machines write into, and one unreadable shard must
    not take the whole backlog down with it.
    """
    items: list[Item] = []
    for path in ledger_files(root):
        try:
            items.extend(read_items(path.read_text(encoding="utf-8")))
        except OSError:
            continue
    items.sort(key=lambda item: item.stamp)
    return items


def resolve(ids: list[str], note: str, pr: str = "", root: Path | None = None) -> list[str]:
    """Record one `triage-resolved` per id. Returns the ids written."""
    if not note.strip():
        raise ValueError("a resolution needs a --note saying what fixed it")
    ledger = ledger_file(root)
    written = []
    for one in ids:
        harness_events.record(
            RESOLVED_EVENT,
            (("ref", one), ("pr", pr or "-"), ("note", note)),
            root=ledger.parent.parent,
        )
        written.append(one)
    return written


def expand_like(ids: list[str], items: list[Item]) -> list[str]:
    """Every open id sharing a signature with one of `ids`."""
    opened = open_items(items)
    wanted = {i.signature for i in opened if i.id in set(ids)}
    return [i.id for i in opened if i.signature in wanted]


def write_artifact(text: str, root: Path | None = None) -> Path:
    """Persist the backlog, per the failure-artifact rule: fix from a file.

    `root` resolves inside rather than as `root: Path = REPO_ROOT` in the signature: a
    default is bound once at import, so the parameter would keep pointing at the real
    checkout however `REPO_ROOT` is repointed afterwards -- and every test of `main()`
    wrote its fixture backlog over this repo's own `logs/harness-triage.log`, silently,
    while appearing to be pinned to a `tmp_path`.
    """
    path = (root or REPO_ROOT) / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read and retire the harness-events backlog.")
    parser.add_argument("--all", action="store_true", help="list resolved items too")
    parser.add_argument("--resolve", nargs="+", metavar="ID", help="mark these ids resolved")
    parser.add_argument(
        "--resolve-like",
        nargs="+",
        metavar="ID",
        help="mark these ids and every open item with the same signature resolved",
    )
    parser.add_argument(
        "--agent",
        default="",
        help="only rows this runtime's hooks wrote: claude, codex, or unknown",
    )
    parser.add_argument("--note", default="", help="what fixed it -- required to resolve")
    parser.add_argument("--pr", default="", help="the PR that fixed it, when there is one")
    args = parser.parse_args(argv)

    items = load()
    if args.resolve or args.resolve_like:
        ids = list(args.resolve or [])
        if args.resolve_like:
            ids += expand_like(list(args.resolve_like), items)
        try:
            written = resolve(sorted(set(ids)), args.note, args.pr)
        except ValueError as exc:
            print(f"harness-triage: {exc}")
            return 2
        print(f"harness-triage: resolved {len(written)} item(s): {' '.join(written)}")
        items = load()

    shown = for_agent(items if args.all else open_items(items), args.agent)
    text = render(shown)
    # The artifact is always the *whole* backlog, whatever the terminal was filtered to:
    # `logs/harness-triage.log` is what the next session reads, and one written under a
    # `--agent` filter would read as "this is everything" while hiding the other runtime.
    path = write_artifact(render(open_items(items)))
    print(text, end="")
    print(f"harness-triage: {len(open_items(items))} open -- {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
