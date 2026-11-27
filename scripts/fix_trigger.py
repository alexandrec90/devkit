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
| `ready:` a commit of devkit passed: a release may be owed, and this machine's copy is old | GitHub |
| `conflict:` an open PR stopped merging | GitHub |
| `pr-red:` an open PR's checks failed: the PRs that do wait for a gate | GitHub |
| `local:` a local end-to-end or medium tier run came back red | `logs/e2e`, `logs/medium-tier` |
| `tree:` a session reported a blocker or friction, or its ship was refused | each worktree's `logs/` |
| `event:` a scheduled job or collector failed, or an agent reported a defect | the events ledger |
| `revert-due:` a direct-flow red reached its revert grace or ceiling (`fix_revert.py`) | `logs/red-seen` |

Rows the pass writes itself (`fix-pass-finding`, `session-friction`) are not signals, or
each pass would start the next.

**It also moves what waits on a passing gate**, on every PC, whatever `devkit.fixPass` says.
`green` is what the harness, a deploy and every queued task follow, and the workflow's own
merge raises no event that would move it, so it waited for an hourly job GitHub may drop
(on 2026-11-25 it sat two commits behind for over an hour). `advance_green` fast-forwards
it, and `FOLLOWERS` -- the checkouts' sync, then the collectors' redeploy -- are started
once it has moved, one a fire, since each reads what the one before it did.

**A signal is spent only by a pass that starts after it.** While one is running
(`FIX_LOCK`) the new ones are left unspent, and the next fire starts a pass for them.

Starts no pass unless `devkit.fixPass` is `dispatch`. Exits 0 on an
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
import red_seen
import sweep
import worktree_tiers

REPO_ROOT = Path(__file__).resolve().parents[1]

# `install-fix-pass-task.TASK_NAME`, and `fix-pass.RUN_LOCK_NAME` / `RUN_LOCK_STALE`; a
# test holds each to its owner, since importing the pass would load all of it every fire.
# Not spelled `PASS_*`: ruff's S105 reads a string under that name as a password.
FIX_TASK = "devkit-fix-pass"
FIX_LOCK = "fix-pass.lock"
FIX_LOCK_STALE = 26 * 60.0

STATE_NAME = "fix-trigger.json"
# How long a spent signal is remembered. A key carries its commit, so it comes back only
# when the very same thing is still true; past this the half-hourly pass has long had it.
REMEMBER = _dt.timedelta(days=7)

GATE = "PR Gate"
COMMITS = 20
PRS = 40
GRAPHQL_SECONDS = 60
REF_SECONDS = 20
START_SECONDS = 30

# What a ledger row must be to start a pass: what something outside the pass writes.
EVENTS = frozenset({"scheduled-job-failed", "workspace-failure", "agent-report"})
# How much of a shard's end is read: far more than two minutes of rows.
LEDGER_TAIL_BYTES = 64 * 1024

# What a session leaves in its worktree's `logs/` for the pass to read.
REPORTS = ("fix-blocked.md", "friction.md")
SHIP_STATE = "ship-state.json"
REFUSED = "refused"  # ship_intent.REFUSED

GREEN = "green"  # green-ref.GREEN
DEVKIT = "devkit"
# What runs the code `green` just moved to, in the order each needs the one before it:
# `install-reconcile-task.TASK_NAME` syncs the checkouts, `install-collectors.TASK_NAME`
# redeploys a collector whose checkout moved.
FOLLOWERS = ("devkit-worktree-reconcile", "devkit-collectors")

Run = Callable[..., object]

REPOSITORY = f"""
fragment R on Repository {{
  defaultBranchRef {{ target {{ ... on Commit {{ history(first: {COMMITS}) {{ nodes {{
    oid parents(first: 1) {{ nodes {{ oid }} }} checkSuites(first: 20) {{ nodes {{ conclusion workflowRun {{ workflow {{ name }} }} }} }}
  }} }} }} }} }}
  ref(qualifiedName: "refs/heads/{GREEN}") {{ target {{ oid }} }}
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


def first_parents(commits: list[dict]) -> list[dict]:
    """The branch's own commits, newest first. A merge commit's history lists the merged
    branch's commits too, with that PR's gate runs, which say nothing of this branch."""
    by_oid = {commit.get("oid"): commit for commit in commits}
    chain: list[dict] = []
    commit = commits[0] if commits else None
    while commit is not None and commit not in chain:
        chain.append(commit)
        parent = next((node.get("oid") for node in _nodes(commit, "parents")), None)
        commit = by_oid.get(parent)
    return chain


def judged(repo: dict) -> list[tuple[str, bool]]:
    """`(commit, passed)` for each of the default branch's commits the gate has judged,
    newest first, so one still running never hides what is known of the one under it."""
    commits = first_parents(_nodes(repo, "defaultBranchRef", "target", "history"))
    return [(str(c.get("oid", "")), said) for c in commits if (said := verdict(c)) is not None]


def green_owed(repo: dict) -> str:
    """The commit `green` should be at and is not, or "". A `green` the branch does not
    have yet is the workflow's to create; one this query cannot see is too far behind to
    place, and the server refuses a move that is not forward."""
    at = ((repo.get("ref") or {}).get("target") or {}).get("oid")
    newest = next((oid for oid, passed in judged(repo) if passed), "")
    if not at or not newest or at == newest:
        return ""
    chain = first_parents(_nodes(repo, "defaultBranchRef", "target", "history"))
    oids = [str(commit.get("oid", "")) for commit in chain]
    return "" if at in oids[: oids.index(newest)] else newest


def advance_green(slug: str, oid: str, run: Run = sweep.run_bounded) -> bool:
    """Fast-forward `green` to `oid`. A refusal is a run that got there first, or GitHub
    being down: the next fire asks again."""
    path = f"repos/{slug}/git/refs/heads/{GREEN}"
    argv = ["gh", "api", "--method", "PATCH", path, "-f", f"sha={oid}", "-F", "force=false"]
    return getattr(run(argv, REF_SECONDS), "returncode", 1) == 0


def move_green(repos: dict[str, dict], run: Run = sweep.run_bounded) -> list[str]:
    """Each `green` that was behind and now is not, as the line the record says."""
    owed = [(slug, oid) for slug, repo in repos.items() if (oid := green_owed(repo))]
    return [f"{slug} to {oid[:9]}" for slug, oid in owed if advance_green(slug, oid, run)]


def ready_signals(slug: str, repo: dict) -> set[str]:
    """devkit's newest passing commit: what `fix_release.cut_release` waits for, and what
    the pass brings this machine's own copy, jobs and tray up to."""
    said = judged(repo)
    return {f"ready:{slug}@{said[0][0][:12]}"} if said and said[0][1] else set()


def passed_signals(slug: str, repo: dict) -> set[str]:
    """Any project's newest passing commit, which the FOLLOWERS are started for."""
    newest = next((oid for oid, passed in judged(repo) if passed), "")
    return {f"passed:{slug}@{newest[:12]}"} if newest else set()


def branch_signals(slug: str, repo: dict) -> set[str]:
    """The default branch's, off its newest judged commit."""
    said = judged(repo)
    if not said:
        return set()
    oid, passed = said[0]
    if not passed:
        return {f"red:{slug}@{oid[:12]}"}
    recovered = len(said) > 1 and not said[1][1]
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


def repositories(
    root: Path, projects: Iterable[str], run: Run = sweep.run_bounded
) -> dict[str, dict]:
    """What GitHub answered for each project that is a repository there."""
    return ask(sorted({slug for name in projects if (slug := slug_of(root / name))}), run)


def github_signals(root: Path, repos: dict[str, dict]) -> set[str]:
    found: set[str] = set()
    for slug, repo in repos.items():
        found |= branch_signals(slug, repo) | pr_signals(slug, repo)
    devkit = slug_of(root / DEVKIT)
    return found | ready_signals(devkit, repos.get(devkit, {}))


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
            # Sorted: Linux lists a directory in no order.
            found += sorted(tree for tree in home.iterdir() if tree.is_dir())
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


def revert_signals(devkit: Path, now: _dt.datetime) -> set[str]:
    """Each red whose revert clock (`red_seen`) has reached its grace, and its ceiling: a
    red nothing new lands on raises no other signal, and the half-hourly backstop would
    stretch a twenty-minute grace by up to thirty more. One signal per mark per red."""
    marks = (("grace", red_seen.REVERT_GRACE), ("ceiling", red_seen.REVERT_CEILING))
    return {
        f"revert-due:{project}@{key[:12]}:{mark}"
        for mark, after in marks
        for project, key in red_seen.lapsed(devkit, red_seen.REVERT, now, after)
    }


def signals(
    root: Path, projects: list[str], repos: dict[str, dict], now: _dt.datetime | None = None
) -> set[str]:
    devkit = root / DEVKIT
    return (
        github_signals(root, repos)
        | local_signals(devkit, projects)
        | tree_signals(root, projects)
        | event_signals(devkit)
        | (revert_signals(devkit, now) if now is not None else set())
    )


# --- what is new, and starting the pass ------------------------------------------------------


def state_path(root: Path) -> Path:
    return root / worktree_tiers.BOXES_DIR_NAME / STATE_NAME


def read_state(path: Path) -> tuple[dict[str, str], list[str]]:
    """`(spent signal to when, followers still to start)`. Unreadable is empty: one pass
    too many, never one too few."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, []
    loaded = loaded if isinstance(loaded, dict) else {}
    seen, follow = loaded.get("seen"), loaded.get("follow")
    kept = {str(k): str(v) for k, v in seen.items()} if isinstance(seen, dict) else {}
    return kept, [n for n in follow if n in FOLLOWERS] if isinstance(follow, list) else []


def remember(seen: dict[str, str], current: Iterable[str], now: _dt.datetime) -> dict[str, str]:
    """`seen` with `current` spent as of `now`, less what nobody has seen in `REMEMBER`.

    What is still true is restamped, so a conflict left standing for a month never ages
    out and back in.
    """
    floor = (now - REMEMBER).isoformat()
    kept = {key: when for key, when in seen.items() if when >= floor}
    return kept | dict.fromkeys(current, now.isoformat())


def write_state(path: Path, seen: dict[str, str], follow: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"seen": seen, "follow": follow}, indent=1, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def running(task: str, run: Run = sweep.run_bounded) -> bool:
    """Whether the scheduler has `task` running now. Started beside itself, a task is
    refused, and that refusal is filed as its runs overlapping."""
    done = run(["schtasks", "/Query", "/TN", task, "/FO", "CSV", "/NH"], START_SECONDS)
    return '"Running"' in str(getattr(done, "stdout", "") or "")


def start_follower(follow: list[str], run: Run = sweep.run_bounded) -> list[str]:
    """Start the first of `follow` unless it is already going; what is left for later
    fires. One that will not start -- stood down, not installed here -- is dropped: its
    own clock is the backstop, and its state is not this job's to report."""
    if not follow:
        return []
    task, *rest = follow
    if running(task, run):
        return follow
    why = start(task, run)
    print(f"fix-trigger: {'did not start' if why else 'started'} {task} for what moved")
    return rest


def pass_running(root: Path, clock: Callable[[], float] = time.time) -> bool:
    """Whether a dispatching pass holds its lock. One older than any pass may run is a
    pass that died, which the pass started here breaks."""
    try:
        made = (root / worktree_tiers.BOXES_DIR_NAME / FIX_LOCK).stat().st_mtime
    except OSError:
        return False
    return clock() - made < FIX_LOCK_STALE


def start(task: str, run: Run = sweep.run_bounded) -> str:
    """Start a scheduled task; "" when the scheduler took it, else why not."""
    done = run(["schtasks", "/Run", "/TN", task], START_SECONDS)
    if getattr(done, "returncode", 1) == 0:
        return ""
    said = f"{getattr(done, 'stderr', '') or ''} {getattr(done, 'stdout', '') or ''}"
    return " ".join(said.split()) or "schtasks said nothing"


def pass_for(
    root: Path,
    new: list[str],
    run: Run = sweep.run_bounded,
    clock: Callable[[], float] = time.time,
) -> tuple[bool, int]:
    """Start the pass for `new`: `(whether they are spent, the exit code)`."""
    if not new:
        print("fix-trigger: nothing new")
        return True, 0
    listed = "\n".join(f"  {key}" for key in new)
    if pass_running(root, clock):
        print(f"fix-trigger: a pass is running; left for the next fire:\n{listed}")
        return False, 0
    if why := start(FIX_TASK, run):
        # Unspent, so the next fire tries again; a task that never starts is the
        # scheduler's own failure, which the exit code files.
        print(f"fix-trigger: {len(new)} new signal(s):\n{listed}")
        print(f"error: could not start {FIX_TASK}: {why}", file=sys.stderr)
        return False, 1
    print(f"fix-trigger: started {FIX_TASK} for:\n{listed}")
    return True, 0


def run_once(
    workspace: Path,
    run: Run = sweep.run_bounded,
    now: _dt.datetime | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    now = now or _dt.datetime.now(_dt.UTC)
    mode = fix_switch.workspace_mode(workspace)
    root = workspace.parent
    projects = devkit_project.known_projects()  # the registry, not the workspace file
    path = state_path(root)
    seen, follow = read_state(path)
    repos = repositories(root, projects, run)
    if moved := move_green(repos, run):
        print(f"fix-trigger: moved {GREEN}: {', '.join(moved)}")
    # Keyed on the commit, not on who moved it: whichever PC or workflow did, this PC's
    # own checkouts and collectors are owed the code.
    passing = {key for slug, repo in repos.items() for key in passed_signals(slug, repo)}
    if passing - seen.keys():
        follow = list(FOLLOWERS)
    follow = start_follower(follow, run)
    spent, code = passing, 0
    if mode == fix_switch.DISPATCH:
        current = signals(root, projects, repos, now)
        done, code = pass_for(root, sorted(current - seen.keys()), run, clock)
        spent = spent | (current if done else current & seen.keys())
    else:
        print(f"fix-trigger: devkit.fixPass is {mode} here, so no pass starts")
    write_state(path, remember(seen, spent, now), follow)
    return code


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
