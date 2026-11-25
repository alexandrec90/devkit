#!/usr/bin/env python3
"""Start the fix pass the moment something new is red, rather than at its next half hour.

The pass (`fix-pass.py`) is the expensive, careful half: it ships, reads every gate, plans
and sends. Asking only *whether anything changed* takes one GraphQL call and a few file
reads, so `devkit-fix-trigger` asks that every two minutes and starts the pass's own
scheduled task when the answer is yes. The half-hourly fire stays as the backstop for
whatever raises no signal: a hold that expires, a session that died.

**A signal is a key that names one thing at one commit** (`red:<repo>@<sha>`), and each
starts one pass. Nothing here decides what a red is worth: the pass's ledger already
refuses to send twice at one problem, so a signal too many costs a pass that sends no one.

| Signal | Read from |
| --- | --- |
| `red:` the newest judged commit of a default branch failed its gate | GitHub |
| `green:` it passed, where the one before failed: held fixers may go | GitHub |
| `conflict:` an open PR stopped merging | GitHub |
| `pr-red:` an open PR's checks failed: the PRs that do wait for a gate | GitHub |
| `local:` a local end-to-end or medium tier run came back red | `logs/e2e`, `logs/medium-tier` |
| `tree:` a session reported a blocker or friction, or its ship was refused | each worktree's `logs/` |
| `event:` a scheduled job or collector failed, or an agent reported a defect | the events ledger |

Rows the pass writes itself (`fix-pass-finding`, `session-friction`) are not signals, or
each pass would start the next.

**A signal is spent only by a pass that starts after it.** While one is running
(`PASS_LOCK`) the new ones are left unspent, and the next fire starts a pass for them.

Sends nothing and starts nothing unless `devkit.fixPass` is `dispatch`. Exits 0 on an
unreadable GitHub: the next fire is two minutes away, and the outage is not a defect.

Tested in `tests/test_fix_trigger.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import devkit_project
import e2e_results
import fix_switch
import harness_events
import sweep
import worktree_tiers

REPO_ROOT = Path(__file__).resolve().parents[1]

# `install-fix-pass-task.TASK_NAME`, and `fix-pass.RUN_LOCK_NAME` / `RUN_LOCK_STALE`; a
# test holds each to its owner, since importing the pass would load all of it every fire.
PASS_TASK = "devkit-fix-pass"
PASS_LOCK = "fix-pass.lock"
PASS_LOCK_STALE = 26 * 60.0

STATE_NAME = "fix-trigger.json"
# How long a spent signal is remembered. A key carries its commit, so it comes back only
# when the very same thing is still true; past this the half-hourly pass has long had it.
REMEMBER = _dt.timedelta(days=7)

GATE = "PR Gate"
COMMITS = 10
PRS = 40
GRAPHQL_SECONDS = 60
START_SECONDS = 30

# What a ledger row must be to start a pass: what something outside the pass writes.
EVENTS = frozenset({"scheduled-job-failed", "workspace-failure", "agent-report"})
# How much of a shard's end is read: far more than two minutes of rows.
LEDGER_TAIL_BYTES = 64 * 1024

# What a session leaves in its worktree's `logs/` for the pass to read.
REPORTS = ("fix-blocked.md", "friction.md")
SHIP_STATE = "ship-state.json"
REFUSED = "refused"  # ship_intent.REFUSED

Run = Callable[..., object]

REPOSITORY = f"""
fragment R on Repository {{
  defaultBranchRef {{ target {{ ... on Commit {{ history(first: {COMMITS}) {{ nodes {{
    oid checkSuites(first: 20) {{ nodes {{ conclusion workflowRun {{ workflow {{ name }} }} }} }}
  }} }} }} }} }}
  pullRequests(states: OPEN, first: {PRS}, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
    nodes {{
      number isDraft mergeable headRefOid
      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}
    }}
  }}
}}
"""


# --- GitHub --------------------------------------------------------------------------------


def slug_of(checkout: Path) -> str:
    """`owner/name` off the checkout's own config, or "" when it has no GitHub remote."""
    try:
        text = (checkout / ".git" / "config").read_text(encoding="utf-8")
    except OSError:
        return ""
    found = re.search(r"url\s*=\s*\S*github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?\s*$", text, re.M)
    return found.group(1) if found else ""


def query_for(slugs: Iterable[str]) -> str:
    """One query for every repository, so a fire is one call however many there are."""
    fields = "\n".join(
        f"  r{index}: repository(owner: {json.dumps(slug.split('/')[0])}, "
        f"name: {json.dumps(slug.split('/')[1])}) {{ ...R }}"
        for index, slug in enumerate(slugs)
    )
    return f"query {{\n{fields}\n}}\n{REPOSITORY}"


def ask(slugs: list[str], run: Run = sweep.run_bounded) -> dict[str, dict]:
    """Slug to repository, for each one GitHub answered for.

    Read off stdout whatever the exit code: `gh` exits non-zero when one repository in the
    query errors, with every other one's answer still printed.
    """
    if not slugs:
        return {}
    done = run(["gh", "api", "graphql", "-f", f"query={query_for(slugs)}"], GRAPHQL_SECONDS)
    try:
        data = json.loads(str(getattr(done, "stdout", "") or "")).get("data") or {}
    except (ValueError, AttributeError):
        return {}
    found = {slug: data.get(f"r{index}") for index, slug in enumerate(slugs)}
    return {slug: repo for slug, repo in found.items() if isinstance(repo, dict)}


def _nodes(value: object, *path: str) -> list[dict]:
    """The `nodes` list at `path`, its dicts only; [] for any part GitHub left null."""
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    nodes = value.get("nodes") if isinstance(value, dict) else None
    return [node for node in nodes or [] if isinstance(node, dict)]


def verdict(commit: dict) -> bool | None:
    """What the gate said of one commit; None while it says nothing. A pass wins over a
    failure of the same commit: that is a re-run that cleared it."""
    said = set()
    for suite in _nodes(commit, "checkSuites"):
        workflow = (suite.get("workflowRun") or {}).get("workflow") or {}
        if workflow.get("name") == GATE:
            said.add(suite.get("conclusion"))
    if "SUCCESS" in said:
        return True
    return False if said & {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"} else None


def branch_signals(slug: str, repo: dict) -> set[str]:
    """The default branch's: judged commits only, newest first, so a gate still running on
    the tip never hides what is already known of the commit under it."""
    commits = _nodes(repo, "defaultBranchRef", "target", "history")
    judged = [(str(c.get("oid", "")), said) for c in commits if (said := verdict(c)) is not None]
    if not judged:
        return set()
    oid, passed = judged[0]
    if not passed:
        return {f"red:{slug}@{oid[:12]}"}
    recovered = len(judged) > 1 and not judged[1][1]
    return {f"green:{slug}@{oid[:12]}"} if recovered else set()


def pr_signals(slug: str, repo: dict) -> set[str]:
    """The open PRs'. `UNKNOWN` is GitHub not having decided yet, and says nothing."""
    found = set()
    for pr in _nodes(repo, "pullRequests"):
        if pr.get("isDraft"):
            continue
        at = f"{slug}#{pr.get('number')}@{str(pr.get('headRefOid', ''))[:12]}"
        if pr.get("mergeable") == "CONFLICTING":
            found.add(f"conflict:{at}")
        for node in _nodes(pr, "commits"):
            rollup = (node.get("commit") or {}).get("statusCheckRollup") or {}
            if rollup.get("state") in ("FAILURE", "ERROR"):
                found.add(f"pr-red:{at}")
    return found


def github_signals(root: Path, projects: Iterable[str], run: Run = sweep.run_bounded) -> set[str]:
    slugs = sorted({slug for name in projects if (slug := slug_of(root / name))})
    found: set[str] = set()
    for slug, repo in ask(slugs, run).items():
        found |= branch_signals(slug, repo) | pr_signals(slug, repo)
    return found


# --- this machine --------------------------------------------------------------------------


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def local_signals(devkit: Path, projects: Iterable[str]) -> set[str]:
    """Each local tier's red, at its commit and signature: the run that finished is the
    event, so the fixer goes while that commit is still the tip."""
    found = set()
    for tier in (e2e_results.E2E, e2e_results.MEDIUM):
        for project in projects:
            result = e2e_results.read(project, devkit, tier)
            if result is not None and result.status == e2e_results.FAILED:
                what = _digest("\n".join(result.signature))
                found.add(f"local:{tier.results_dir.name}/{project}@{result.sha[:12]}:{what}")
    return found


def worktrees(root: Path, projects: Iterable[str]) -> list[Path]:
    """Every directory an agent's tree can be: each checkout's own tier, and the boxes."""
    homes = [root / name / ".claude" / "worktrees" for name in projects]
    homes.append(root / worktree_tiers.BOXES_DIR_NAME)
    found: list[Path] = []
    for home in homes:
        try:
            found += [tree for tree in home.iterdir() if tree.is_dir()]
        except OSError:
            continue
    return found


def tree_signals(root: Path, projects: Iterable[str]) -> set[str]:
    """What sessions left for the pass. Keyed by content: a file rewritten with the same
    words is not news."""
    found = set()
    for tree in worktrees(root, projects):
        for name in REPORTS:
            if text := _text(tree / "logs" / name).strip():
                found.add(f"tree:{tree.name}/{name}:{_digest(text)}")
        state = _text(tree / "logs" / SHIP_STATE)
        if refused(state):
            found.add(f"tree:{tree.name}/{SHIP_STATE}:{_digest(state)}")
    return found


def refused(state: str) -> bool:
    """Whether a ship state says the commit stage refused: a failure a fixer can take."""
    try:
        loaded = json.loads(state)
    except ValueError:
        return False
    return isinstance(loaded, dict) and loaded.get("stage") == REFUSED


def event_key(row: str) -> str:
    """One ledger row as a signal, or "". The time is left out, so a job that fails every
    two minutes for the same cause is one signal rather than a pass every two minutes."""
    stamp, _, rest = row.partition("\t")
    fields = dict(pair.partition("=")[::2] for pair in rest.split("\t") if "=" in pair)
    if not stamp or fields.get("event") not in EVENTS:
        return ""
    names = ("event", "project", "command", "cause", "detail", "message")
    # A failed job's message is its exit and its time; its cause is what stays the same.
    said = [fields.get(n, "") for n in names if n != "message" or not fields.get("cause")]
    return f"event:{fields['event']}:{fields.get('project', '?')}:{_digest('|'.join(said))}"


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - LEDGER_TAIL_BYTES))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def event_signals(devkit: Path) -> set[str]:
    rows = [row for path in harness_events.ledger_paths(devkit) for row in _tail(path).splitlines()]
    return {key for row in rows if (key := event_key(row))}


def signals(root: Path, projects: list[str], run: Run = sweep.run_bounded) -> set[str]:
    devkit = root / "devkit"
    return (
        github_signals(root, projects, run)
        | local_signals(devkit, projects)
        | tree_signals(root, projects)
        | event_signals(devkit)
    )


# --- what is new, and starting the pass ------------------------------------------------------


def state_path(root: Path) -> Path:
    return root / worktree_tiers.BOXES_DIR_NAME / STATE_NAME


def read_seen(path: Path) -> dict[str, str]:
    """Spent signal to when. Unreadable is empty: one pass too many, never one too few."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    seen = loaded.get("seen") if isinstance(loaded, dict) else None
    return {str(k): str(v) for k, v in seen.items()} if isinstance(seen, dict) else {}


def remember(seen: dict[str, str], current: Iterable[str], now: _dt.datetime) -> dict[str, str]:
    """`seen` with `current` spent as of `now`, less what nobody has seen in `REMEMBER`.

    What is still true is restamped, so a conflict left standing for a month never ages
    out and back in.
    """
    floor = (now - REMEMBER).isoformat()
    kept = {key: when for key, when in seen.items() if when >= floor}
    return kept | dict.fromkeys(current, now.isoformat())


def write_seen(path: Path, seen: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"seen": seen}, indent=1, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def pass_running(root: Path, clock: Callable[[], float] = time.time) -> bool:
    """Whether a dispatching pass holds its lock. One older than any pass may run is a
    pass that died, which the pass started here breaks."""
    try:
        made = (root / worktree_tiers.BOXES_DIR_NAME / PASS_LOCK).stat().st_mtime
    except OSError:
        return False
    return clock() - made < PASS_LOCK_STALE


def start_pass(run: Run = sweep.run_bounded) -> str:
    """Start the pass's own scheduled task; "" when the scheduler took it, else why not."""
    done = run(["schtasks", "/Run", "/TN", PASS_TASK], START_SECONDS)
    if getattr(done, "returncode", 1) == 0:
        return ""
    said = f"{getattr(done, 'stderr', '') or ''} {getattr(done, 'stdout', '') or ''}"
    return " ".join(said.split()) or "schtasks said nothing"


def run_once(
    workspace: Path,
    run: Run = sweep.run_bounded,
    now: _dt.datetime | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    now = now or _dt.datetime.now(_dt.UTC)
    mode = fix_switch.workspace_mode(workspace)
    if mode != fix_switch.DISPATCH:
        print(f"fix-trigger: devkit.fixPass is {mode} here, so nothing starts")
        return 0
    root = workspace.parent
    projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    path = state_path(root)
    seen = read_seen(path)
    current = signals(root, projects, run)
    new = sorted(current - seen.keys())
    if not new:
        write_seen(path, remember(seen, current, now))
        print(f"fix-trigger: nothing new ({len(current)} standing)")
        return 0
    listed = "\n".join(f"  {key}" for key in new)
    if pass_running(root, clock):
        print(f"fix-trigger: a pass is running; left for the next fire:\n{listed}")
        return 0
    if why := start_pass(run):
        # Unspent, so the next fire tries again; a task that never starts is the
        # scheduler's own failure, which the exit code files.
        print(f"fix-trigger: {len(new)} new signal(s):\n{listed}")
        print(f"error: could not start {PASS_TASK}: {why}", file=sys.stderr)
        return 1
    write_seen(path, remember(seen, current, now))
    print(f"fix-trigger: started {PASS_TASK} for:\n{listed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    workspace = (args.workspace or sweep.default_workspace(REPO_ROOT)).resolve()
    if not workspace.is_file():
        print(f"error: no workspace file at {workspace}", file=sys.stderr)
        return 2
    return run_once(workspace)


if __name__ == "__main__":
    sys.exit(main())
