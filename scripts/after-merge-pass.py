#!/usr/bin/env python3
"""Run what every project's `.devkit-queue/` says is due: `devkit-after-merge`, on every PC.

    after-merge-pass.py run [PROJECT ...]     # what the task runs every 5 minutes
    after-merge-pass.py status [PROJECT ...]  # what this PC would do now, doing nothing
    after-merge-pass.py tidy [PROJECT ...]    # the clean-up PR now, whatever the day

`after_merge.py` decides and `after_merge_board.py` reads; this acts.

**A task is open, handed, done or failed, and no outcome here is a log line.**

- `run` executes in the static checkout, fast-forwarded the way `e2e-pass.py` does it, or
  in a disposable tree cut for the purpose when that checkout is a person's to keep. Its
  output is a parseable file under `logs/after-merge/`. Passing, the task is done. A
  failure is handed to an agent on this machine, the command and output in its prompt.
- `agent` is a background session in a fresh tree of that project, which finishes with
  `/ship`. Its tree is marked as the pass's own, so its PR merges itself.
- `elevated` writes a request into the spool for the elevated worker, whose file name is
  the whole request. With no spool folder the worker is not installed, which is a
  person's step until it is, and past `UNCLAIMED_AFTER_DAYS` an agent is sent too.
- A launch that is refused writes nothing, and the next cycle tries again.

**A handed task is read back here, on the machine that holds it**, every cycle, whatever
`devkit.fixPass` says on that PC (`_read_back`): an intent in the tree or a merged PR is
done, the fix pass's own `fix_reports.session_state` says whether a session is alive,
and one that died offers the task again, up to `MAX_ATTEMPTS`, after which it is failed
and filed. The worker's taking a request is done too. A PC gone quiet for days loses its
handed task to another.

**A claim says who, a done ref says it is over.** `job_claims` decides which PC takes a
task, and a network failure there is "try next cycle". Having won, a machine asks the
server whether the task was finished meanwhile, and asks a fresh fetch whether its file
is still that task, since a pass is minutes long and a revert may have landed. The
outcome is kept locally before the done ref is pushed, so a push that failed is retried
without running the job twice, and the claim stands until the server holds that ref. A
run interrupted by a power cut has neither; another PC breaks its claim at the TTL, so
a job can run twice and is never silently lost.

**Every state has a clock.** A due task unstarted after `UNCLAIMED_AFTER_DAYS`, for
whatever reason, is filed once (`_clock`), and so is a project this PC could not read
for that long.

Nothing here is vendored: a task file is data, and the runner lives in devkit alone.

Exits 0 whenever the pass reached the end, whatever the tasks did; a task's failure has
its own road, and `log-wrap.py` is for the pass itself breaking.

Tested in `tests/test_after_merge_pass.py` and `tests/test_after_merge_lifecycle.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import after_merge
import after_merge_board
import after_merge_tidy
import agent_models
import agent_worktrees as aw
import devkit_project
import fix_findings
import fix_prompts
import fix_reports
import fix_send
import fix_trees
import git_trust
import host_memory
import job_claims
import ship_intent
import sweep
import task_branch as tb
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]
e2e_pass = load_by_path("e2e_pass", REPO_ROOT / "scripts" / "e2e-pass.py")

Task = after_merge.Task
Board = after_merge_board.Board
Finding = fix_findings.Finding
triage = fix_findings.triage

OUTPUT_DIR = Path("logs") / "after-merge"
STATE_FILE = "state.json"
# Set in the environment of every queued command, as `DEVKIT_E2E` is for an e2e run.
ENV_FLAG = "DEVKIT_AFTER_MERGE"
# The checkout whose repository holds every machine's heartbeat.
DEVKIT = "devkit"

# One command's ceiling, inside `job_claims.TTL`'s claim and the task's hour-long limit
# with `BUDGET_SECONDS` of the pass before it.
RUN_TIMEOUT = 30 * 60
BUDGET_SECONDS = 25 * 60
DOCKER_SECONDS = 20
# How much of a failed command's output rides in the prompt; the rest is in its file.
PROMPT_OUTPUT = 4000
# How much of it the file keeps, from each end: enough to read, and bounded per task.
KEPT_OUTPUT = 60_000

# Where a task stands on this machine, in its result file. The first three are final: only
# the done ref is owed. `HANDED` and `REQUESTED` are out of this pass's hands, `FAILED` is a
# command whose session has yet to start, and `OFFERED` a task open to be taken again.
FINAL = tuple(sorted(after_merge.SUCCESSES))
HANDED = "handed-to-agent"
REQUESTED = "requested"
OUT = (HANDED, REQUESTED)
FAILED = "failed"
OFFERED = "offered"

SESSION = agent_models.Launch("claude-bg")


def docker_works(runner=sweep.run_bounded) -> bool:
    """A daemon answers here, which is what `on = "docker"` asks of a machine."""
    argv = ["docker", "version", "--format", "{{.Server.Version}}"]
    try:
        return runner(argv, DOCKER_SECONDS).returncode == 0
    except OSError:
        return False


@dataclass(frozen=True)
class Session:
    """What a launch came to: a code, its words, and where the session is."""

    code: int
    said: str = ""
    tree: str = ""
    branch: str = ""


def _open_session(task: Task, board: Board, prompt: str) -> tuple[int, str, str]:
    name = tb.branch_name(tb.slugify(f"after merge {task.name}"), set())
    tree, branch = fix_trees.cut_fresh_tree(board.path, name, board.base)
    if tree is None:
        print(f"could not cut {branch} off origin/{board.base}", file=sys.stderr)
        return fix_send.EXIT_FAILED, "", ""
    for note in fix_trees.provision_tree(tree):
        print(f"  {note}")
    key = f"{after_merge.SESSION_KEY}{after_merge.seen_key(task)}"
    fix_reports.stamp(tree, key, f"after-merge task {task.path}", problem=key, agent=SESSION.agent)
    (tree / fix_reports.ORIGIN_FILE).write_text("fix-pass\n", encoding="utf-8")
    body = f"{fix_prompts.ROLE} {prompt} {fix_prompts.FINISH} {fix_prompts.STOP}"
    title = f"{task.project} after-merge {task.name}"
    code = fix_send.fix_prs.open_session(
        SESSION, tree, branch, fix_send.fix_prs.tab_safe(body), title
    )
    return code, str(tree), branch


def launch_session(task: Task, board: Board, prompt: str) -> tuple[int, str, str, str]:
    """A background session at `prompt` in a fresh tree of `board`:
    `(exit code, its words, the tree, its branch)`, the fields of a `Session`."""
    (code, tree, branch), said = fix_send.saying(_open_session, task, board, prompt)
    return code, said, tree, branch


def session_state(tree: Path, now: float) -> str:
    """What the fix pass's own read-back says of the session in `tree`. A tree it cannot
    judge, with no stamp or a dead one, has no session left to wait for."""
    moment = _dt.datetime.fromtimestamp(now, _dt.UTC)
    state, _ = fix_reports.session_state(tree, moment, booted=fix_reports.booted_at(moment))
    return state or fix_reports.NO_OUTCOME


@dataclass(frozen=True)
class Seams:
    """Everything the pass reaches outside itself, so a test replaces it in one argument."""

    root: Path = REPO_ROOT
    host: str = field(default_factory=fix_findings.harness_events.host_name)
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    git_for: Callable[[Path], sweep.Git] = sweep.git_for
    gh_for: Callable[[Path], sweep.Git] = sweep.gh_for
    # On the process rather than its pipes: a command may leave a child running.
    runner: Callable[..., object] = sweep.run_to_files
    free_mb: Callable[[], int | None] = host_memory.available_mb
    docker: Callable[[], bool] = docker_works
    launch: Callable[[Task, Board, str], tuple] = launch_session
    session_of: Callable[[Path, float], str] = session_state
    provision: Callable[[Path], list[str]] = fix_trees.provision_tree
    spool: Path = Path(after_merge.ELEVATED_SPOOL)


# --- what is kept on this machine ---------------------------------------------------------------


def _output_dir(seams: Seams) -> Path:
    return seams.root / OUTPUT_DIR


def _result_path(task: Task, seams: Seams) -> Path:
    return _output_dir(seams) / f"{task.project}-{tb.slugify(task.name)}-{task.id}.json"


def _write_result(task: Task, seams: Seams, **fields) -> Path:
    """The parseable account of `task` on this machine, overwritten per attempt. `fields`
    says what happened; anything unsaid keeps what the last write said."""
    path = _result_path(task, seams)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**_read_result(task, seams), **asdict(task), **fields}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _read_result(task: Task, seams: Seams) -> dict:
    try:
        loaded = json.loads(_result_path(task, seams).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def read_state(seams: Seams) -> dict:
    """What this machine keeps between cycles: `seen`, when it first saw each open task;
    `due`, when each due task first failed to start; `unread`, since when each project's
    fetch has failed; and `tidied`, the day each project last had its tidy."""
    try:
        loaded = json.loads((_output_dir(seams) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_state(state: dict, seams: Seams) -> None:
    path = _output_dir(seams) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def clipped(text: str, limit: int) -> str:
    """`text`, or its head and tail when longer: a command's error is usually at the end."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... {len(text) - limit} characters cut ...\n{text[-half:]}"


# --- one cycle --------------------------------------------------------------------------------


@dataclass
class Cycle:
    """What one pass carries from task to task."""

    seams: Seams
    journal: fix_findings.Journal
    free_mb: int | None = None
    docker: bool = False
    deadline: float = float("inf")
    waited: bool = False
    lines: list[str] = field(default_factory=list)
    due: dict[str, float] = field(default_factory=dict)
    hosts: frozenset[str] = frozenset()

    def say(self, line: str) -> None:
        self.lines.append(line)
        print(line, flush=True)

    def turn(self) -> None:
        """This machine's wait before its first claim of the pass (`job_claims.wait_turn`)."""
        if not self.waited:
            self.waited = True
            job_claims.wait_turn(self.free_mb, self.seams.sleep)

    def machine(self, project: str) -> job_claims.Machine:
        return job_claims.Machine(self.seams.host, self.free_mb, self.docker, frozenset({project}))

    def spent_session(self) -> None:
        if self.free_mb is not None:
            self.free_mb -= host_memory.SESSION_MB

    def git(self, board: Board) -> sweep.Git:
        return self.seams.git_for(board.path)


def _key(task: Task, cycle: Cycle) -> str:
    return after_merge.done_key(task, cycle.seams.host)


def _release(task: Task, board: Board, cycle: Cycle) -> None:
    if task.on != after_merge.EVERY_MACHINE:
        job_claims.release(cycle.git(board), task.id, cycle.seams.host)


def _trust_refusal(board: Board, cycle: Cycle) -> str:
    """Why `board`'s checkout may not run a queued command, or "".

    `git_trust.adopt` makes every read here work on any checkout, so that is not the
    question. A command is code run in that directory, so what decides is git with the
    pass's own trust taken off: whether this user's configuration accepts the checkout.
    """
    bare = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    done = cycle.git(board)("rev-parse", "--is-inside-work-tree", env=bare)
    if done.returncode == 0:
        return ""
    if any(mark in (done.stderr or "") for mark in fix_findings.OWNERSHIP_MARKS):
        return "git does not trust this checkout (detected dubious ownership)"
    return f"git cannot read this checkout: {after_merge_board.said_by(done)}"


def _no_worker(task: Task, cycle: Cycle) -> str:
    """File the person's step a missing worker is; why this task waits."""
    step = (
        f"{triage.PERSON_STEP} run `{after_merge.ELEVATED_INSTALL}` from an administrator "
        f"shell on {cycle.seams.host}"
    )
    detail = (
        f"{task.project}: task {task.name} needs {task.elevated}, and that machine has no "
        f"elevated worker -- {step}"
    )
    cycle.journal.add(Finding("after-merge-no-worker", task.project, detail))
    return f"no elevated worker here ({task.elevated})"


def _held_here(task: Task, board: Board, cycle: Cycle) -> str:
    """Why this machine does not claim `task`, or ""."""
    needs = job_claims.Needs(
        docker=task.on == after_merge.DOCKER,
        checkout=board.project,
        memory=task.kind != after_merge.ELEVATED,
    )
    if why := job_claims.refusal(needs, cycle.machine(board.project)):
        return why
    if task.kind == after_merge.ELEVATED and not cycle.seams.spool.is_dir():
        return _no_worker(task, cycle)
    if task.kind == after_merge.RUN and (why := _trust_refusal(board, cycle)):
        detail = f"{task.project}: task {task.name} refused: {why}"
        cycle.journal.add(Finding("after-merge-untrusted", task.project, detail))
        return why
    return ""


def _hand_over(task: Task, board: Board, prompt: str, cycle: Cycle) -> tuple[str, str]:
    """A session at `prompt`: `(HANDED, "")`, or `("", why not)` with nothing written, so the
    next cycle tries again."""
    session = Session(*cycle.seams.launch(task, board, prompt))
    if session.code != 0:
        words = session.said.strip().splitlines()
        return "", f"the launch was refused: {words[-1] if words else session.code}"
    cycle.spent_session()
    _write_result(task, cycle.seams, status=HANDED, tree=session.tree, branch=session.branch)
    return HANDED, ""


def _disposable(board: Board, task: Task, cycle: Cycle) -> Path | None:
    """A throwaway tree at `origin/<base>`, provisioned; None when none could be cut."""
    git = cycle.git(board)
    tree = aw.default_root(board.path) / f"after-merge-{task.id}"
    git("worktree", "remove", "--force", str(tree))
    tree.parent.mkdir(parents=True, exist_ok=True)
    added = git("worktree", "add", "--detach", str(tree), f"origin/{board.base}")
    if added.returncode != 0:
        return None
    cycle.seams.provision(tree)
    return tree


def _run_command(task: Task, where: Path, cycle: Cycle) -> object:
    env = {**os.environ, ENV_FLAG: "1", "PYTHONUTF8": "1"}
    return cycle.seams.runner(task.run, RUN_TIMEOUT, cwd=str(where), env=env, shell=True)  # noqa: S604 - the command is committed on the project's default branch, by the project


def _failed_prompt(task: Task, said: str, artifact: Path) -> str:
    return after_merge.failure_prompt(task, said[-PROMPT_OUTPUT:], str(artifact))


def _perform_run(task: Task, board: Board, cycle: Cycle) -> tuple[str, str]:
    """The command, wherever it can run: `(what it came to, why not)`. A failed command whose
    launch was refused is not run again: it has said what it had to say."""
    kept = _read_result(task, cycle.seams)
    if kept.get("status") == FAILED:
        path = _result_path(task, cycle.seams)
        prompt = _failed_prompt(task, str(kept.get("output", "")), path)
        return _hand_over(task, board, prompt, cycle)
    git = cycle.git(board)
    _, why = e2e_pass.prepare(git, board.base)
    tree = _disposable(board, task, cycle) if why else None
    if why and tree is None:
        return "", f"no tree to run in ({why})"
    try:
        done = _run_command(task, tree or board.path, cycle)
    finally:
        if tree is not None:
            git("worktree", "remove", "--force", str(tree))
    code = int(getattr(done, "returncode", 1))
    said = f"{getattr(done, 'stdout', '') or ''}\n{getattr(done, 'stderr', '') or ''}".strip()
    artifact = _write_result(
        task,
        cycle.seams,
        command=task.run,
        where=str(tree or board.path),
        exit_code=code,
        output=clipped(said, KEPT_OUTPUT),
        status=after_merge.PASSED if code == 0 else FAILED,
    )
    if code == 0:
        return after_merge.PASSED, ""
    return _hand_over(task, board, _failed_prompt(task, said or f"exit {code}", artifact), cycle)


def _perform_elevated(task: Task, cycle: Cycle) -> tuple[str, str]:
    """`<verb>.request` in the spool: `(REQUESTED, "")`, or `("", why not)`.

    The file's name is the whole request: the worker never reads its content, so what is
    written is only a record for a person looking in the folder, and two tasks wanting
    one verb are rightly one request."""
    path = cycle.seams.spool / f"{task.elevated}{after_merge.REQUEST_SUFFIX}"
    body = {"project": task.project, "task": task.name, "id": task.id, "host": cycle.seams.host}
    try:
        path.write_text(json.dumps({**body, "at": cycle.seams.now()}) + "\n", encoding="utf-8")
    except OSError as why:
        return "", f"could not write {path.name}: {why}"
    _write_result(task, cycle.seams, status=REQUESTED, request=str(path))
    return REQUESTED, ""


def _perform(task: Task, board: Board, cycle: Cycle) -> tuple[str, str]:
    if task.kind == after_merge.RUN:
        return _perform_run(task, board, cycle)
    if task.kind == after_merge.ELEVATED:
        return _perform_elevated(task, cycle)
    return _hand_over(task, board, after_merge.task_prompt(task), cycle)


# --- what a claim becomes ---------------------------------------------------------------------


def _finish(task: Task, board: Board, status_word: str, cycle: Cycle) -> bool:
    """Record `task` over, on GitHub, so no PC takes it again. The claim goes only once the
    server holds that; whether it does."""
    host = cycle.seams.host
    every = task.on == after_merge.EVERY_MACHINE
    ref = job_claims.done_ref(task.id, host if every else "")
    subject = after_merge.done_subject(task, host, status_word)
    if not job_claims.mark_done(cycle.git(board), ref, subject, cycle.seams.now()):
        cycle.say(f"{task.project} {task.name}: {status_word}; done ref retried next cycle")
        return False
    _release(task, board, cycle)
    cycle.say(f"{task.project} {task.name}: {status_word}")
    return True


def _ref_sha(board: Board, ref: str) -> str:
    return next((found.sha for found in board.refs if found.name == ref), "")


def _record_handed(task: Task, board: Board, attempt: int, cycle: Cycle) -> None:
    """Say on GitHub that `task` is out, on its `attempt`. A push that fails is retried from
    the local result, so a session is not started twice for it."""
    ref = job_claims.handed_ref(_key(task, cycle))
    subject = after_merge.handed_subject(task, cycle.seams.host, attempt)
    moved = job_claims.stamp(
        cycle.git(board), ref, subject, _ref_sha(board, ref), cycle.seams.now()
    )
    _write_result(task, cycle.seams, attempt=attempt, recorded=moved)
    cycle.say(f"{task.project} {task.name}: attempt {attempt} out" + ("" if moved else "; unsaid"))


def _give_up(task: Task, board: Board, why: str, cycle: Cycle) -> None:
    """Out of attempts: failed on GitHub, so every PC stops offering it, and filed."""
    subject = f"failed {task.project}/{task.name} by {cycle.seams.host}: {why[:120]}"
    ref = job_claims.failed_ref(_key(task, cycle))
    if not job_claims.mark_done(cycle.git(board), ref, subject, cycle.seams.now()):
        cycle.say(f"{task.project} {task.name}: failed; its ref retried next cycle")
        return
    _release(task, board, cycle)
    text = f"{why}\n\n{after_merge.what_it_asks(task)}"
    where = fix_findings.evidence_file(text, cycle.journal.devkit_dir, f"after-merge-{task.id}")
    detail = (
        f"{task.project}: after-merge task {task.name} ({task.id}) failed after "
        f"{after_merge.MAX_ATTEMPTS} attempts"
    )
    cycle.journal.add(Finding("after-merge-failed", task.project, detail, evidence=where))
    cycle.say(f"{task.project} {task.name}: failed after {after_merge.MAX_ATTEMPTS} attempts")


def _spent_attempt(
    task: Task, held: after_merge.Handed, board: Board, why: str, cycle: Cycle
) -> None:
    """A session that left no outcome: the task is offered again, or it is out of attempts."""
    if held.attempts >= after_merge.MAX_ATTEMPTS:
        _give_up(task, board, why, cycle)
        return
    ref = job_claims.handed_ref(_key(task, cycle))
    subject = after_merge.handed_subject(task, cycle.seams.host, held.attempts, offered=True)
    if job_claims.stamp(cycle.git(board), ref, subject, _ref_sha(board, ref), cycle.seams.now()):
        _write_result(task, cycle.seams, status=OFFERED, offered_because=why)
        _release(task, board, cycle)
        cycle.say(f"{task.project} {task.name}: offered again: {why}")


def _merged(branch: str, board: Board, cycle: Cycle) -> str:
    """`SHIPPED` when a PR from `branch` merged, "" when `gh` cannot say, else why not."""
    gh = cycle.seams.gh_for(board.path)
    listed = gh("pr", "list", "--state", "merged", "--head", branch, "--json", "number")
    if listed.returncode != 0:
        return ""
    return (
        after_merge.SHIPPED if json.loads(listed.stdout or "[]") else "its tree is gone, unmerged"
    )


def _outcome(result: dict, held: after_merge.Handed, board: Board, cycle: Cycle) -> str:
    """A success word, "" while it is still out, or why it failed."""
    seams = cycle.seams
    if request := result.get("request"):
        if not Path(str(request)).exists():
            return after_merge.CONSUMED
        late = seams.now() - held.at >= after_merge.UNCLAIMED_SECONDS
        return "the elevated worker never took its request" if late else ""
    if not result.get("tree"):
        return "this machine has no record of its session"
    tree = Path(str(result["tree"]))
    if not tree.is_dir():
        return _merged(str(result.get("branch", "")), board, cycle)
    if any((tree / name).is_file() for name in (ship_intent.INTENT_FILE, ship_intent.SHIPPED_FILE)):
        return after_merge.SHIPPED
    state = seams.session_of(tree, seams.now())
    return "" if state == fix_reports.WORKING else f"its session {state}"


def _read_back(task: Task, board: Board, cycle: Cycle) -> None:
    """A task handed by this machine: finished, still out, or to be offered again."""
    held = board.marks.handed[_key(task, cycle)]
    result = _read_result(task, cycle.seams)
    status = str(result.get("status", ""))
    outcome = status if status in FINAL else _outcome(result, held, board, cycle)
    if after_merge.is_success(outcome):
        _write_result(task, cycle.seams, status=outcome)
        _finish(task, board, outcome, cycle)
    elif outcome:
        _spent_attempt(task, held, board, outcome, cycle)


def _claimed(task: Task, board: Board, cycle: Cycle) -> str:
    """Why `task` is not this machine's to act on now, or "" with the claim held. A job done
    once per machine needs no claim. Having won one, the server is asked whether the task
    finished in the time this machine spent waiting its turn."""
    if task.on == after_merge.EVERY_MACHINE:
        return ""
    if holder := _held_elsewhere(task, board, cycle):
        return f"{holder} has it"
    cycle.turn()
    seams, git = cycle.seams, cycle.git(board)
    attempt = job_claims.claim(git, task.id, task.kind, seams.host, seams.now())
    if not attempt.won:
        return attempt.holder or attempt.said or attempt.outcome
    finished = job_claims.on_server(git, job_claims.done_ref(task.id))
    if finished is None:
        job_claims.release(git, task.id, seams.host)
        return "the server could not say whether it was finished"
    if finished:
        job_claims.release(git, task.id, seams.host)
        return "another PC finished it first"
    return ""


def _held_elsewhere(task: Task, board: Board, cycle: Cycle) -> str:
    """Who has a live claim on `task`, by the last fetch, or "": a push that would only be
    refused is not worth this machine's wait, nor GitHub's call."""
    ref = job_claims.claim_ref(task.id)
    held = next((found for found in board.refs if found.name == ref), None)
    if held is None or held.host == cycle.seams.host:
        return ""
    return "" if job_claims.expired(held, task.kind, cycle.seams.now()) else held.host


def _start(task: Task, board: Board, cycle: Cycle, reroute: str) -> str:
    """The claimed `task`, re-read off origin and performed; why not, or ""."""
    fresh, why = after_merge_board.current(cycle.git(board), task, board.base)
    if fresh is None:
        _release(task, board, cycle)
        if not why:
            cycle.say(f"{task.project} {task.name}: left the queue; not run")
        return why
    due = after_merge.rerouted(fresh, reroute) if reroute else fresh
    held = board.marks.handed.get(_key(task, cycle))
    attempt = (held.attempts if held else 0) + 1
    outcome, why = _perform(due, board, cycle)
    if after_merge.is_success(outcome):
        return "" if _finish(due, board, outcome, cycle) else "its done ref did not land"
    if outcome:
        _record_handed(due, board, attempt, cycle)
        return ""
    _release(task, board, cycle)
    return why


def _take(task: Task, board: Board, cycle: Cycle, reroute: str = "") -> str:
    """One due task, from claim to out of this pass's hands; why it did not start, or "".
    `reroute` says why it goes to an agent instead of being done as written."""
    seams = cycle.seams
    result = _read_result(task, seams)
    status = str(result.get("status", ""))
    if status in FINAL:  # finished earlier; only its done ref failed
        return "" if _finish(task, board, status, cycle) else "its done ref did not land"
    held = board.marks.handed.get(_key(task, cycle))
    attempts = held.attempts if held else 0
    if status in OUT and result.get("attempt") == attempts + 1 and not result.get("recorded"):
        _record_handed(task, board, attempts + 1, cycle)  # started; only its ref failed
        return ""
    if attempts >= after_merge.MAX_ATTEMPTS:
        _give_up(task, board, "its last session's machine went quiet", cycle)
        return ""
    due = after_merge.rerouted(task, reroute) if reroute else task
    if why := _held_here(due, board, cycle):
        return why
    if why := _claimed(due, board, cycle):
        return why
    return _start(task, board, cycle, reroute)


def _note(task: Task, board: Board, why: str, cycle: Cycle) -> str:
    """An agent sent at a late condition, at most once per `UNCLAIMED_AFTER_DAYS` however
    many PCs notice. The task stands: nothing here is a state of it."""
    seams, git = cycle.seams, cycle.git(board)
    due = after_merge.noted(task, why)
    if hold := job_claims.refusal(job_claims.Needs(), cycle.machine(board.project)):
        return hold
    claim_id = f"{task.id}-rerouted"
    attempt = job_claims.claim(git, claim_id, job_claims.AGENT, seams.host, seams.now())
    if not attempt.won:
        return attempt.holder or attempt.said or attempt.outcome
    session = Session(*seams.launch(due, board, after_merge.task_prompt(due)))
    if session.code != 0:
        job_claims.release(git, claim_id, seams.host)
        return "the launch was refused"
    cycle.spent_session()
    ref = job_claims.rerouted_ref(task.id)
    subject = f"rerouted {task.project}/{task.name} by {seams.host}"
    job_claims.stamp(git, ref, subject, _ref_sha(board, ref), seams.now())
    job_claims.release(git, claim_id, seams.host)
    cycle.say(f"{task.project} {task.name}: an agent was sent at its late condition")
    return ""


def _facts(board: Board, boards: dict[str, Board], docker: bool, seams: Seams) -> after_merge.Facts:
    git, gh = seams.git_for(board.path), seams.gh_for(board.path)
    return after_merge_board.facts_for(board, boards, seams.host, docker, git, gh)


def _act(task: Task, verdict: after_merge.Verdict, board: Board, cycle: Cycle) -> str | None:
    """What `verdict` does: why a due task did not start, "" when it is in hand, None when
    it was not due at all. Each of those is what `_clock` runs or stops on."""
    action = verdict.action
    if action == after_merge.HANDED:
        _read_back(task, board, cycle)
        return ""
    if action not in (after_merge.TAKE, after_merge.REROUTE, after_merge.NOTE):
        return None
    if time.monotonic() > cycle.deadline:
        cycle.say(f"{task.project} {task.name}: left for next cycle: this pass is out of time")
        return "the pass ran out of time"
    if action == after_merge.NOTE:
        return _note(task, board, verdict.why, cycle)
    return _take(task, board, cycle, verdict.why if action == after_merge.REROUTE else "")


def _clock(task: Task, why: str | None, board: Board, cycle: Cycle) -> None:
    """Whether a due task has now gone `UNCLAIMED_AFTER_DAYS` without starting, whatever kept
    it: filed once, under a detail that stays the same, so it is one open row while it is
    stuck, and a row resolved with the task still stuck is filed again."""
    key = after_merge.seen_key(task)
    if not why:
        cycle.due.pop(key, None)
        return
    cycle.say(f"{task.project} {task.name}: not started: {why}")
    now = cycle.seams.now()
    if now - cycle.due.setdefault(key, now) < after_merge.UNCLAIMED_SECONDS:
        return
    detail = (
        f"{task.project}: after-merge task {task.name} ({task.id}) has been due for "
        f"{after_merge.UNCLAIMED_AFTER_DAYS} days and has not started"
    )
    where = fix_findings.evidence_file(why, cycle.journal.devkit_dir, f"after-merge-{task.id}")
    cycle.journal.add(Finding("after-merge-stuck", task.project, detail, evidence=where))
    if task.kind == after_merge.ELEVATED and not cycle.seams.spool.is_dir():
        _take(task, board, cycle, f"no machine has run it, and {why}")


def _run_board(
    board: Board, cycle: Cycle, boards: dict[str, Board], seen: dict[str, float]
) -> None:
    """Every task of `board` this machine has something to do about, in order."""
    seams = cycle.seams
    facts = _facts(board, boards, cycle.docker, seams)
    now = seams.now()
    open_here = after_merge.first_seen(seen, board.tasks, board.done, seams.host, now)
    for task in board.tasks:
        since = open_here.get(after_merge.seen_key(task), now)
        verdict = after_merge.judge(task, board.marks, facts, since, now)
        _clock(task, _act(task, verdict, board, cycle), board, cycle)
    for key in [key for key in seen if key.startswith(f"{board.project}/")]:
        del seen[key]
    seen.update(open_here)
    known = {after_merge.seen_key(task) for task in board.tasks}
    for key in [key for key in cycle.due if key.startswith(f"{board.project}/")]:
        if key not in known:
            del cycle.due[key]


# --- the pass ---------------------------------------------------------------------------------


def here(workspace: Path, only: list[str]) -> list[str]:
    """The registered projects with a checkout on this machine, `only` if any are named.
    A project with none here is another PC's to run."""
    known = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    names = [name for name in known if not only or name in only]
    return [name for name in names if (workspace.parent / name / ".git").exists()]


def _read_boards(
    workspace: Path, names: list[str], seams: Seams, unread: dict[str, str] | None = None
) -> dict[str, Board]:
    """The boards that could be read; `unread` collects why each other one could not."""
    boards = {}
    for name in names:
        path = workspace.parent / name
        boards[name] = after_merge_board.read_board(name, path, seams.git_for(path))
        if boards[name].error:
            print(f"{name}: {boards[name].error}; next cycle", flush=True)
            if unread is not None:
                unread[name] = boards[name].error
    return {name: board for name, board in boards.items() if not board.error}


def _needs_docker(boards: dict[str, Board]) -> bool:
    """Asked first, since asking docker is a spawn, and a queue rarely wants it."""
    return any(
        task.on == after_merge.DOCKER and task.id not in board.done
        for board in boards.values()
        for task in board.tasks
    )


def _begin(boards: dict[str, Board], seams: Seams) -> Cycle:
    return Cycle(
        seams,
        fix_findings.Journal(seams.root),
        free_mb=seams.free_mb(),
        docker=_needs_docker(boards) and seams.docker(),
        deadline=time.monotonic() + BUDGET_SECONDS,
    )


def _heartbeat(boards: dict[str, Board], cycle: Cycle) -> None:
    """This machine on devkit's list of hosts, and that list for the tidy; a machine back
    from long enough away that the tidy stopped counting it is told what it missed, which
    happens once, since this refresh is what ends the absence."""
    devkit = boards.get(DEVKIT)
    if devkit is None:
        return  # no list to read: `removable` keeps what every machine owes
    seams = cycle.seams
    now, host = seams.now(), seams.host
    last_seen = job_claims.heartbeat(cycle.git(devkit), devkit.refs, host, now)
    cycle.hosts = job_claims.live_hosts(devkit.refs, now) | {host}
    unseen = job_claims.HOST_UNSEEN_AFTER.total_seconds()
    if last_seen is None or now - last_seen < unseen:
        return
    for board in boards.values():
        for name in after_merge.missed(board.done_rows, board.tasks, host, last_seen):
            detail = (
                f"{board.project}: every-machine task {name} left the queue while {host} was "
                "away, so it never ran there"
            )
            cycle.journal.add(Finding("after-merge-missed", board.project, detail))


def _unread_clock(names: list[str], errors: dict[str, str], since: dict, cycle: Cycle) -> None:
    """A project's fetch failing is a state with a clock like any other."""
    now = cycle.seams.now()
    for name in names:
        if name not in errors:
            since.pop(name, None)
        elif now - since.setdefault(name, now) >= after_merge.UNCLAIMED_SECONDS:
            detail = f"{name}: its after-merge queue has not been readable on {cycle.seams.host}"
            cycle.journal.add(Finding("after-merge-unread", name, detail))


def _file(cycle: Cycle) -> None:
    devkit_dir = cycle.journal.devkit_dir
    written = fix_findings.record_all(cycle.journal.findings, triage.load(devkit_dir), devkit_dir)
    for finding in written:
        print(f"filed {finding.headline[:160]}", flush=True)


def run_pass(workspace: Path, only: list[str], seams: Seams) -> int:
    """One cycle: every queue here read, whatever is due taken, whatever is out read back,
    each project tidied once a day, and what went wrong filed. One project's crash is a
    finding, not the pass's end."""
    names = here(workspace, only)
    errors: dict[str, str] = {}
    boards = _read_boards(workspace, names, seams, errors)
    cycle = _begin(boards, seams)
    state = read_state(seams)
    seen = dict(state.get("seen", {}))
    tidied = dict(state.get("tidied", {}))
    cycle.due = dict(state.get("due", {}))
    unread = dict(state.get("unread", {}))
    _unread_clock(names, errors, unread, cycle)
    cycle.journal.step("heartbeat", _heartbeat, boards, cycle)
    for board in boards.values():
        cycle.journal.step("queue", _run_board, board, cycle, boards, seen)
        cycle.journal.step("tidy", after_merge_tidy.daily, board, cycle, tidied)
    _write_state({"seen": seen, "due": cycle.due, "unread": unread, "tidied": tidied}, seams)
    _file(cycle)
    print(f"after-merge: {len(boards)} queues read on {seams.host}", flush=True)
    return 0


def tidy_now(workspace: Path, only: list[str], seams: Seams) -> int:
    """The clean-up PR of each project, whatever the day, taking no task."""
    boards = _read_boards(workspace, here(workspace, only), seams)
    cycle = _begin(boards, seams)
    cycle.journal.step("heartbeat", _heartbeat, boards, cycle)
    tidied: dict[str, str] = {}
    for board in boards.values():
        cycle.journal.step("tidy", after_merge_tidy.daily, board, cycle, tidied, True)
    state = read_state(seams)
    _write_state({**state, "tidied": {**state.get("tidied", {}), **tidied}}, seams)
    _file(cycle)
    return 0


def status(workspace: Path, only: list[str], seams: Seams) -> int:
    """Each task and what this machine would do about it. Nothing is claimed or written."""
    boards = _read_boards(workspace, here(workspace, only), seams)
    seen = read_state(seams).get("seen", {})
    now = seams.now()
    docker = _needs_docker(boards) and seams.docker()
    for board in boards.values():
        facts = _facts(board, boards, docker, seams)
        for task in board.tasks:
            since = seen.get(after_merge.seen_key(task), now)
            verdict = after_merge.judge(task, board.marks, facts, since, now)
            print(f"{task.project} {task.name} [{task.id}]: {verdict.action} {verdict.why}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb, words in (
        ("run", "take whatever is due"),
        ("status", "say what is due, doing nothing"),
        ("tidy", "the clean-up PR now, whatever the day"),
    ):
        sub = verbs.add_parser(verb, help=words)
        sub.add_argument("projects", nargs="*", help="registered checkouts; none means all")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    workspace = sweep.default_workspace(REPO_ROOT)
    if not workspace.is_file():
        print(f"after-merge-pass: no workspace file at {workspace}", file=sys.stderr)
        return 2
    # Reads must work on a checkout an elevated process made; `_trust_refusal` is the
    # separate question of running a command in one.
    git_trust.adopt(workspace.parent, False)
    seams = Seams()
    verbs = {"run": run_pass, "status": status, "tidy": tidy_now}
    return verbs[args.verb](workspace, args.projects, seams)


if __name__ == "__main__":
    sys.exit(main())
