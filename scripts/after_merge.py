#!/usr/bin/env python3
"""The after-merge queue's decisions: what a task file says, and whether it is due here.

Nothing may depend on the operator remembering. A session that needs something to happen
later -- a rebuild, a command, a step on the other PC -- commits `.devkit-queue/<name>.toml`
in the PR that needs it, and `after-merge-pass.py` does the rest on whichever PC gets
there. It is a queue, not a change to merging, so it works for a gated project and a
direct one alike. The file is the task:

    when = "green"          # or merged, released, pr:<repo>#<n>, task:<name>, task:<repo>/<name>
    on = "any"              # or every-machine, docker, or a hostname
    run = "<command>"       # or agent = "<prompt>", or elevated = "<verb>": exactly one

**A task is its path and the commit that added it.** That hash is its id (`task_id`), so
the same bytes queued again later are a new task, two files alike are two tasks, and a
fixer rewriting a file's whitespace is no task at all. An edit changes what an unfinished
task does and never runs a finished one again: to run something again, add a new file. A
revert removes the file and the task with it. A PR that never merged has no file on the
default branch, so its task never existed.

**A malformed file becomes an agent task saying so.** The operator will never look, so
parsing cannot end in a dropped file: `parse` is total, and anything it cannot read,
anything else sitting in the directory included, is due at once on any machine, with a
prompt quoting the file and what is wrong with it.

**A task is open, handed, done or failed, and only done means finished.** Those states are
refs on GitHub, so every PC reads the same one (`Marks`). A command that passed is done.
Handed to a session or the elevated worker, a task is handed, not done: done comes when
the session has shipped or the worker has taken the request. A session that died offers
the task again, up to `MAX_ATTEMPTS`, and then it is failed, which is filed and stays
visible and in the repo. A dependency needs a success (`is_success`), and the tidy counts
done alone (`removable`).

**Nothing waits forever.** A task nobody took for `UNCLAIMED_AFTER_DAYS` because its host
never came or no PC has docker becomes an agent task on whichever PC is awake (`judge`,
`rerouted`), told why. A condition that has not come true by then sends an agent too, at
most that often, but uses nothing up: the task still runs when its condition does
(`noted`). A handed task whose PC has gone quiet that long is offered to another.

**`green` is the `green` branch, wherever it exists.** The post-merge job keeps one in
every project, gated or direct, and a commit is green once that branch has reached it.
A project may not have the branch yet. There a gated project reads `green` as `merged`,
since every commit of its default branch passed the gate before it merged and nothing
should stall before adoption, and a direct project waits, since merging there consulted
no gate. The runner hands `judge` that answer through `Facts.green`.

Everything here is pure: git, `gh` and the clock arrive as arguments. Tested in
`tests/test_after_merge.py` and `tests/test_after_merge_lifecycle.py`.
"""

from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import elevated_request
import harness_events

QUEUE_DIR = ".devkit-queue"
# Hex digits of the hash that names a task.
ID_LENGTH = 12

GREEN = "green"
MERGED = "merged"
RELEASED = "released"
ANY = "any"
EVERY_MACHINE = "every-machine"
DOCKER = "docker"

RUN = "run"
AGENT = "agent"
ELEVATED = "elevated"
KINDS = (RUN, AGENT, ELEVATED)
FIELDS = frozenset({"when", "on", *KINDS})

# The verbs the elevated worker takes, and the folder it reads requests from, as
# `elevated_request.py` spells them. One verb on purpose: nothing else justifies running
# as admin, so any other asked for is a malformed task, and an agent handles it some other
# way.
ELEVATED_VERBS = elevated_request.VERBS
ELEVATED_SPOOL = elevated_request.SPOOL
REQUEST_SUFFIX = elevated_request.REQUEST_SUFFIX
# `-I -S` before anything loads: the installer's own re-exec cannot undo a first `site`.
ELEVATED_INSTALL = "python -I -S scripts/install-elevated-worker.py --yes"

# The project's delivery flow, off its `.devkit.toml`, and the label a PR of its needs.
DIRECT = "direct"
GATED = "gated"
LABELS = {DIRECT: "merge-now", GATED: "automerge"}

# How long a task may sit with nobody taking it before an agent is sent at it instead, a
# due task unstarted before it is filed, and a handed task on a silent PC before another
# may take it.
UNCLAIMED_AFTER_DAYS = 3
DAY_SECONDS = 86_400.0
UNCLAIMED_SECONDS = UNCLAIMED_AFTER_DAYS * DAY_SECONDS
# How many sessions a task gets before it is failed: the first, and two more for a session
# that died of something other than the task.
MAX_ATTEMPTS = 3

# How a finished task's done ref says it finished: its command passed, its session
# shipped, or the worker took its request. Nothing else is a success, so a dependency
# reads the word rather than the ref's existence.
PASSED = "passed"
SHIPPED = "shipped"
CONSUMED = "consumed"
SUCCESSES = frozenset({PASSED, SHIPPED, CONSUMED})

# What the stamp in a tree a queued session runs in starts with: `fix_loop` leaves those
# trees to the queue, whose attempts are not the dispatch ledger's.
SESSION_KEY = "after-merge:"

PR_WHEN = re.compile(r"pr:(?P<repo>[\w.-]+(?:/[\w.-]+)?)#(?P<number>[1-9]\d*)$")
TASK_WHEN = re.compile(r"task:(?:(?P<repo>[\w.-]+)/)?(?P<name>[\w.-]+)$")
HOSTNAME = re.compile(r"[A-Za-z0-9][\w.-]*$")
TASK_NAME = re.compile(r"[A-Za-z0-9][\w.-]*$")
HANDED_SUBJECT = re.compile(r"(?P<word>handed|offered) \S+ by (?P<host>\S+): attempt (?P<n>\d+)$")

# What `judge` can say of a task on this machine.
DONE = "done"
FAILED = "failed"
HANDED = "handed"  # with a session or worker of this machine's, so this machine reads it back
TAKE = "take"
REROUTE = "reroute"  # past its time on a machine that will not come: an agent takes it
NOTE = "note"  # its condition is late: an agent looks, and the task stays as it is
WAIT = "wait"


def task_id(path: str, added: str) -> str:
    """The id of the task `path` holds since commit `added` added it."""
    return hashlib.sha256(f"{path}\0{added}".encode()).hexdigest()[:ID_LENGTH]


@dataclass(frozen=True)
class Task:
    """One file of the queue. `id` is `task_id`; `commit` and `added` are the commit of the
    default branch that added the file, and when."""

    project: str
    path: str
    id: str
    when: str = GREEN
    on: str = ANY
    run: str = ""
    agent: str = ""
    elevated: str = ""
    malformed: str = ""  # what is wrong with the file; "" for one that parsed
    commit: str = ""
    added: float = 0.0

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).stem

    @property
    def kind(self) -> str:
        return RUN if self.run else ELEVATED if self.elevated else AGENT


@dataclass(frozen=True)
class Handed:
    """A task with a session or the worker. `open` is one offered again: `attempts` spent,
    nobody on it. `at` is when that was last said."""

    host: str
    attempts: int
    at: float
    open: bool = False


@dataclass(frozen=True)
class Marks:
    """What a project's `refs/devkit/*` say of its tasks, keyed as `done_key` keys them;
    `rerouted` by id, with when an agent was last sent at a late condition."""

    done: frozenset[str] = frozenset()
    handed: Mapping[str, Handed] = field(default_factory=dict)
    failed: frozenset[str] = frozenset()
    rerouted: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Facts:
    """What a condition is judged against, injected. `done` answers for a task of any
    project, by `(project, name)`; each answer may be slow, so nothing asks it twice for
    a task it can already rule on."""

    host: str
    docker: bool = False
    green: Callable[[str], bool] = lambda commit: False
    released: Callable[[str], bool] = lambda commit: False
    merged: Callable[[str, int], bool] = lambda repo, number: False
    done: Callable[[str, str], bool] = lambda project, name: False


@dataclass(frozen=True)
class Verdict:
    action: str
    why: str = ""


class _Malformed(ValueError):
    """Why a file is not a task, in words its fixer can act on."""


# --- the file ------------------------------------------------------------------------------


def parse(project: str, path: str, identity: str, text: str) -> Task:
    """The task `path` holds, as `identity` (`task_id`). Total: a file that is not one comes
    back as the agent task that says so."""
    try:
        return _strict(project, path, identity, text)
    except _Malformed as why:
        return malformed(project, path, identity, str(why), text)


def malformed(project: str, path: str, identity: str, why: str, text: str) -> Task:
    """The agent task standing in for a file that could not be read, due now, anywhere."""
    prompt = (
        f"{path} in {project} is not a valid after-merge task, so nothing ran: {why}. "
        f"Its contents were: {text[:1500]} -- "
        "Work out what it was meant to do from the file and the commit that added it, then "
        "do that. If it has to run later instead, add a new corrected file under another "
        "name and remove this one: an edit to this file is not a new task."
    )
    return Task(project, path, identity, when=MERGED, agent=prompt, malformed=why)


def _strict(project: str, path: str, identity: str, text: str) -> Task:
    where = PurePosixPath(path)
    if str(where.parent) != QUEUE_DIR or where.suffix != ".toml":
        raise _Malformed(f"only {QUEUE_DIR}/<name>.toml is read, and this is {path}")
    if not TASK_NAME.match(where.stem):
        raise _Malformed(f"{where.stem!r} is not a task name")
    table = _table(text)
    when, on = table.get("when", GREEN), table.get("on", ANY)
    _check_when(when, where.stem)
    if on not in (ANY, EVERY_MACHINE, DOCKER) and not HOSTNAME.match(on):
        raise _Malformed(f"on = {on!r} is neither a known word nor a hostname")
    if table.get(ELEVATED, ELEVATED_VERBS[0]) not in ELEVATED_VERBS:
        raise _Malformed(
            f"elevated = {table[ELEVATED]!r} did not happen: the only elevated step there is "
            f"is {', '.join(ELEVATED_VERBS)}, so this has to be done another way"
        )
    return Task(
        project,
        path,
        identity,
        when=when,
        on=on,
        run=table.get(RUN, ""),
        agent=table.get(AGENT, ""),
        elevated=table.get(ELEVATED, ""),
    )


def _table(text: str) -> dict[str, str]:
    """`text` as a task's keys: all known, all non-empty strings, exactly one action."""
    try:
        table = tomllib.loads(text)
    except tomllib.TOMLDecodeError as why:
        raise _Malformed(f"it is not TOML: {why}") from why
    unknown = sorted(set(table) - FIELDS)
    if unknown:
        raise _Malformed(f"unknown keys: {', '.join(unknown)}")
    wrong = sorted(key for key, value in table.items() if not isinstance(value, str) or not value)
    if wrong:
        raise _Malformed(f"these must be non-empty strings: {', '.join(wrong)}")
    actions = [key for key in KINDS if key in table]
    if len(actions) != 1:
        raise _Malformed(f"it needs exactly one of run, agent, elevated, and has {len(actions)}")
    return table


def _check_when(when: str, name: str) -> None:
    named = TASK_WHEN.match(when)
    if when not in (GREEN, MERGED, RELEASED) and not PR_WHEN.match(when) and not named:
        raise _Malformed(f"when = {when!r} is not a condition this queue knows")
    if named and not named["repo"] and named["name"] == name:
        raise _Malformed("it waits on itself")


def flow_of(devkit_toml: str) -> str:
    """The `[delivery] flow` a `.devkit.toml` names; anything else is today's gated flow."""
    try:
        delivery = tomllib.loads(devkit_toml).get("delivery")
    except tomllib.TOMLDecodeError:
        return GATED
    flow = delivery.get("flow") if isinstance(delivery, dict) else None
    return DIRECT if flow == DIRECT else GATED


# --- what the refs say ----------------------------------------------------------------------


def done_subject(task: Task, host: str, status: str) -> str:
    return f"done {task.project}/{task.name} by {host}: {status}"


def handed_subject(task: Task, host: str, attempt: int, offered: bool = False) -> str:
    word = "offered" if offered else "handed"
    return f"{word} {task.project}/{task.name} by {host}: attempt {attempt}"


def parse_handed(subject: str, at: float) -> Handed | None:
    """What a handed ref's subject says; None for one of any other shape."""
    found = HANDED_SUBJECT.match(subject)
    if found is None:
        return None
    return Handed(found["host"], int(found["n"]), at, found["word"] == "offered")


def status_of(subject: str) -> str:
    """The word a done ref's subject ends in."""
    return subject.rpartition(": ")[2].strip()


def is_success(status: str) -> bool:
    return status in SUCCESSES


# --- whether it is due ---------------------------------------------------------------------


def met(task: Task, facts: Facts) -> bool:
    """`task.when` holds now, as far as `facts` can say. Unknown is false: try next cycle."""
    when = task.when
    if when == MERGED:
        return True
    if when == GREEN:
        return facts.green(task.commit)
    if when == RELEASED:
        return facts.released(task.commit)
    if found := PR_WHEN.match(when):
        return facts.merged(found["repo"], int(found["number"]))
    named = TASK_WHEN.match(when)
    if named is None:
        return False
    return facts.done(named["repo"] or task.project, named["name"])


def done_key(task: Task, host: str) -> str:
    """How refs spell this task here: `<id>`, or `<id>/<host>` for one that runs once per
    machine."""
    return f"{task.id}/{host}" if task.on == EVERY_MACHINE else task.id


def is_done(task: Task, done: frozenset[str], host: str) -> bool:
    return done_key(task, host) in done


def takes(task: Task, host: str, docker: bool) -> bool:
    """`host` is a machine `task.on` names."""
    if task.on in (ANY, EVERY_MACHINE):
        return True
    if task.on == DOCKER:
        return docker
    return harness_events.host_slug(task.on) == host


def judge(task: Task, marks: Marks, facts: Facts, since: float, now: float) -> Verdict:
    """What this machine does about `task`, first seen at `since`. The order is the point:
    only a state that is over ends the question, and a task past its time goes to an
    agent whatever was holding it."""
    key = done_key(task, facts.host)
    if key in marks.done:
        return Verdict(DONE)
    if key in marks.failed:
        return Verdict(FAILED)
    held = marks.handed.get(key)
    if held is not None and not held.open:
        if held.host == facts.host:
            return Verdict(HANDED)
        if now - held.at < UNCLAIMED_SECONDS:
            return Verdict(WAIT, f"handed to {held.host}")
    overdue = now - since >= UNCLAIMED_SECONDS
    if not met(task, facts):
        return _unmet(task, marks, overdue, now)
    if takes(task, facts.host, facts.docker):
        return Verdict(TAKE)
    if not overdue:
        return Verdict(WAIT, f"on = {task.on}")
    return Verdict(REROUTE, _unreachable(task))


def _unmet(task: Task, marks: Marks, overdue: bool, now: float) -> Verdict:
    """A condition not yet true: a wait, with an agent sent to look when it is late."""
    last = marks.rerouted.get(task.id)
    if overdue and (last is None or now - last >= UNCLAIMED_SECONDS):
        return Verdict(NOTE, f"when = {task.when} has not come true")
    return Verdict(WAIT, f"when = {task.when}")


def _unreachable(task: Task) -> str:
    if task.on == DOCKER:
        return f"no PC with a working docker took it in {UNCLAIMED_AFTER_DAYS} days"
    return (
        f"it was meant for {task.on}, which was unreachable: that machine has not taken it "
        f"in {UNCLAIMED_AFTER_DAYS} days, whether it is off or was never seen"
    )


def rerouted(task: Task, why: str) -> Task:
    """`task` as an agent task on any machine, its id kept so its claim and done still
    count. An every-machine task stays one: each machine still owes its own run."""
    prompt = (
        f"This task was not carried out as written: {why}. What it asked: {what_it_asks(task)} "
        "Do as much of it as can be done from this machine, and if it no longer applies, "
        "say so in the intent."
    )
    on = EVERY_MACHINE if task.on == EVERY_MACHINE else ANY
    return replace(task, on=on, run="", elevated="", agent=prompt)


def noted(task: Task, why: str) -> Task:
    """The agent task a late condition sends: it uses nothing up, so `task` still stands."""
    prompt = (
        f"This task is waiting and has been for {UNCLAIMED_AFTER_DAYS} days: {why}. What it "
        f"asks, once that holds: {what_it_asks(task)} Do not carry it out: it still runs by "
        "itself when its condition comes true, and nothing you do here uses it up. Find out "
        "why the condition has not come true, and if making it true is work a session can "
        "do, do that. Otherwise say in the intent what it is waiting on."
    )
    return replace(task, on=ANY, run="", elevated="", agent=prompt)


def what_it_asks(task: Task) -> str:
    if task.run:
        return f"run this command in this project: {task.run}"
    if task.elevated:
        return (
            f"have the elevated step {task.elevated} done. An unelevated session cannot do "
            "it itself, so attempt what you can and name what is left in the blocked file."
        )
    return task.agent


def seen_key(task: Task) -> str:
    return f"{task.project}/{task.id}"


def first_seen(
    seen: Mapping[str, float], tasks: Iterable[Task], done: frozenset[str], host: str, now: float
) -> dict[str, float]:
    """When this machine first saw each of `tasks` that is still open; a closed one's entry
    goes. Tasks are one project's, so another project's entries are the caller's to merge."""
    return {
        seen_key(task): seen.get(seen_key(task), now)
        for task in tasks
        if not is_done(task, done, host)
    }


def named_done(
    name: str,
    tasks: Iterable[Task],
    successes: frozenset[str],
    names: Mapping[str, frozenset[str]],
    host: str,
) -> bool:
    """Whether the task called `name` finished well, here if it runs per machine. `successes`
    holds the keys of done refs with a success; `names` what each name finished as, which
    is all that is left once the file is tidied away."""
    current = next((task for task in tasks if task.name == name), None)
    if current is not None:
        return done_key(current, host) in successes
    keys = names.get(name, frozenset())
    if any("/" in key for key in keys):
        return any(key.endswith(f"/{host}") for key in keys)
    return bool(keys)


def removable(tasks: Iterable[Task], done: frozenset[str], hosts: Iterable[str] = ()) -> list[Task]:
    """The tasks whose file has done its work and can leave the repo: done, and nothing
    else. An every-machine task needs every one of `hosts` to have run it, and with no
    host list at all it stays, since a file kept is cheaper than a machine skipped."""
    kept = []
    for task in tasks:
        if task.on != EVERY_MACHINE:
            if task.id in done:
                kept.append(task)
            continue
        counted = set(hosts)
        if counted and all(f"{task.id}/{host}" in done for host in counted):
            kept.append(task)
    return kept


def missed(
    rows: Iterable[tuple[str, float, str]], tasks: Iterable[Task], host: str, last_seen: float
) -> list[str]:
    """The names of every-machine tasks that came and left while `host` was away. `rows` is
    `(done key, when, name)`: a task other hosts finished after `last_seen`, with no file in
    the queue now and no run here, is one this host will never see."""
    rows = list(rows)
    queued = {task.id for task in tasks}
    here = {key.split("/", 1)[0] for key, _at, _name in rows if key.endswith(f"/{host}")}
    return sorted(
        {
            name
            for key, at, name in rows
            if "/" in key and at >= last_seen and key.split("/", 1)[0] not in queued | here
        }
    )


# --- what a session is told -------------------------------------------------------------------


def task_prompt(task: Task) -> str:
    """What a session sent at `task` is told. It has no memory of the session that queued
    this, and nothing else to go on."""
    return (
        f"This is an after-merge task from {task.project}, queued in {task.path} by a session "
        "you have no memory of, for a session to carry out once its condition held. "
        f"The task: {what_it_asks(task)} "
        "When it is done, finish with /ship. If it turns out to need nothing changed, ship "
        "the same way, saying what you found: the task counts as finished only once you "
        "have. Leave the task file alone: the queue removes it once it is done."
    )


def failure_prompt(task: Task, output: str, artifact: str) -> str:
    """What a session is told when `task`'s command failed on this machine."""
    return (
        f"This is an after-merge task from {task.project}, queued in {task.path} by a session "
        f"you have no memory of. Its command failed on this machine: {task.run} -- "
        f"the last of its output, whole in {artifact}: {output} -- "
        "Find out why, make it work, run it to completion, and finish with /ship. If what "
        "was wrong was in this repository, that fix is what you ship. Leave the task file "
        "alone: the queue removes it once it is done."
    )
