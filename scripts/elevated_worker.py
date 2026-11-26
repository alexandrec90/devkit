#!/usr/bin/env python3
"""The one thing devkit runs as an administrator: two verbs, taking no input.

Merged code here is unreviewed, so a dispatcher that ran tasks as admin would hand one bad
merge the machine. This is the opposite shape. Task Scheduler runs a *copy* of this file
every five minutes (`install-elevated-worker.py`, task `devkit-elevated`); an unelevated
process may only leave an empty file in the spool, and **the file's name is the entire
message**. What the worker does for a name is `VERBS`, built from the literal argvs below.

What a change here has to keep, each held by a test in `tests/test_elevated_worker.py`:

- **Nothing unelevated reaches an elevated process.** A request's content is never opened.
  Only an exact name selects a verb, and anything else in the spool is deleted, logged and
  ignored. `main` takes no arguments, and this file reads neither `sys.argv` nor the
  environment, because the task runs in the logged-on user's session and so with their
  environment, which any unelevated process can set in `HKCU\\Environment`. That is why
  every program below is a literal path rather than `%SystemRoot%`, and every child gets
  `CHILD_ENV` instead of inheriting.
- **The spool is hostile.** The user can write in it, so nothing in it is followed.
  `cycle` refuses outright if the spool is a reparse point, because deleting "anything
  else" through a junction would be an administrator deleting whatever it points at.
  Nothing is deleted recursively, for the same reason. `SENTINEL` is why the junction
  cannot appear in the first place: Windows only lets an empty directory become a mount
  point, and the user may not delete that file.
- **Everything this file writes is under `ROOT`**, with one exception, appended to and
  never rewritten: sshd's `administrators_authorized_keys` (`fleet-authorize`). No tree of
  the user's is touched, which is what keeps this out of `elevated_writes.py`'s findings.
  `wsl --update` installs WSL itself, as `docker-maint.py recover-wsl` does, with `TEMP`
  inside `ROOT` so the installer it downloads is never in a folder the user can write.
- **A healthy service is left alone.** Ending `WSLService` ends every distro and Docker's
  VM, so a request is not an instruction: `unhealthy` has to find the service stuck, with
  queries that need no elevation and a bound on each.
- **A loop cannot restart it twice.** `RESTART_EVERY` is kept in the admin-only folder,
  where an unelevated bug cannot reset it. The window is written *before* anything runs,
  and a verb whose window cannot be written does not run.
- **The pass hears about it.** `RESULTS` is what `fix_elevated.py` reads: a run that
  failed or was refused goes on the ledger, and `worker.log` is only the detail. This job
  cannot write the ledger, and its exit code alone is overwritten by the next fire.

No import from the rest of devkit: this runs from a copy, under an interpreter whose
`sys.path` holds the standard library and nothing else. So the recovery below is a copy of
`docker-maint.wsl_stuck` and `elevated_recover`, and a test holds its constants to theirs.

## The verb, and why it is here

Measured 2026-11-06 on a Windows 10 workstation with `sc qc` and `sc sdshow`: `WSLService`
(`wslservice.exe`, LocalSystem, auto start) grants interactive users `CCLCSWLOCRRC`: query,
not `RP` (start) or `WP` (stop). `wsl --shutdown` needs no elevation and is not this: it
cannot end a service stuck in STOP_PENDING, the case that needed a person
(`collector_tasks.WSL_STUCK`). `sc stop` is refused while a stop is pending too, so this
ends the process, as `docker-maint.py recover-wsl` does.

Bringing Docker Desktop back afterwards is deliberately not here: from here it would run
the backend elevated, the failure `docker-maint.desktop_launch` exists to avoid. That is
`collectors.py`'s, unelevated.

## The second verb, `fleet-authorize`, and what it trusts

Windows OpenSSH reads an administrator's keys from
`C:\\ProgramData\\ssh\\administrators_authorized_keys`, which only an administrator may
write, and a PC whose only SSH login is an administrator (`AllowUsers administrator`) is
unreachable to the fleet until each peer's key is there. `fleet_ssh.py join` asks for
this verb by name when one is missing; the request still carries nothing.

What it adds is decided here, from sources no unelevated process on this PC controls:

- **The records come from devkit's origin, not the checkout.** `refs/devkit/fleet/*` is
  fetched from `FLEET_ORIGIN`, a literal URL, into a bare repository under `HOME`, by the
  literal `GIT` with `GIT_ENV`: no user config, no prompt, no code of the checkout's.
- **A peer is who Tailscale says it is.** A record counts only for a host that is a peer
  in `tailscale status --json` (literal `TAILSCALE`) with the very tailnet address the
  record claims.
- **A key is one bare ed25519 key.** `ssh-ed25519 <base64> [comment]`, no options prefix,
  one line, its blob decoding to an ed25519 public key; the line written carries this
  file's own comment, never the record's.
- **Keys are only ever added**, and a new file gets the ACL sshd insists on (`ICACLS`).
  What the file then holds is written to `FLEET_SEEN` under `HOME`, readable by the user,
  since an unelevated `join` cannot read the keys file to see that its request was met.

**The trade-off, which this cannot close: whoever can push refs to devkit's origin AND is
a peer on this tailnet can add an SSH key that logs in to this PC as an administrator.**
Either alone cannot: a ref from someone off the tailnet names a host Tailscale does not
list or an address it does not give that host, and a tailnet peer without push rights has
no record to publish. Both are the operator's own accounts today; a collaborator with push
access, or a shared tailnet, makes this the line to revisit.

Windows-only by nature; on any other platform `main` says so and exits 0.

Tested in `tests/test_elevated_worker.py`.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import re
import stat
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

ROOT = r"C:\ProgramData\devkit"
SPOOL = ROOT + r"\requests"
HOME = ROOT + r"\elevated"
# Where `wsl --update` downloads to, under the admin-only folder. The installer makes it.
SCRATCH = HOME + r"\tmp"
# Installed in the spool, undeletable by the user, so the spool is never empty and so can
# never be turned into a mount point (see the module docstring).
SENTINEL = "spool.keep"
REQUEST_SUFFIX = ".request"
LIMITS_NAME = "rate-limit.json"
LOG_NAME = "worker.log"
# What the fix pass reads: each subject's latest result. `elevated_request.results`.
RESULTS_NAME = "results.json"
# A bound, not a retention policy: the spool is the user's, so what it can make this file
# say must not be able to fill the disk.
LOG_BYTES = 256 * 1024

# The result vocabulary, spelled again in `elevated_request`; a test holds them equal.
HEALTHY = "healthy"
RECOVERED = "recovered"
REFUSED = "refused"
FAILED = "failed"
# The subject of a result about the worker itself rather than a verb.
WORKER = "worker"

SYSTEM32 = r"C:\Windows\System32"
SC = SYSTEM32 + r"\sc.exe"
TASKKILL = SYSTEM32 + r"\taskkill.exe"
WSL = SYSTEM32 + r"\wsl.exe"
# What every child runs with. Not an inherited environment, for the module docstring's
# reason. Machine constants, plus a `TEMP` an unelevated process cannot write in.
CHILD_ENV = {
    "SystemRoot": r"C:\Windows",
    "windir": r"C:\Windows",
    "SystemDrive": "C:",
    "ProgramData": r"C:\ProgramData",
    "ProgramFiles": r"C:\Program Files",
    "TEMP": SCRATCH,
    "TMP": SCRATCH,
}

# Copied from `docker-maint.py` and `collector_tasks.py`, which this cannot import; a test
# holds these to theirs.
WSL_SERVICE = "WSLService"
WSL_SETTLED = ("RUNNING", "STOPPED")
WSL_SETTLE = 30
WSL_SETTLE_WAIT = 60
WSL_POLL = 2
WSL_ANSWER_TIMEOUT = 60
WSL_UPDATE_TIMEOUT = 600
TIMED_OUT = 124
SERVICE_STATE = re.compile(r"^\s*STATE\s*:\s*\d+\s+(?P<state>\w+)", re.M)

WSL_QUERY = (SC, "query", WSL_SERVICE)
WSL_KILL = (TASKKILL, "/F", "/FI", f"SERVICES eq {WSL_SERVICE}")
WSL_LIST = (WSL, "--list", "--quiet")
WSL_UPDATE = (WSL, "--update")

# How often the verb may run. Ending the service ends every distro and Docker's VM, and a
# service this restart did not cure wants a person rather than another restart.
RESTART_EVERY = 6 * 60 * 60

QUERY_TIMEOUT = 10
KILL_TIMEOUT = 30
# Not `wsl.exe`'s to outlast: `run_step` captures nothing, so a child's child holding a
# pipe open cannot hold a timed-out wait open behind it.
SPAWN_FAILED = -1

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FILE_ATTRIBUTE_NORMAL = 0x80

# `fleet-authorize` (see the module docstring). Programs by literal path, as above.
PROGRAM_DATA = r"C:\ProgramData"
PROGRAM_FILES = r"C:\Program Files"
GIT = PROGRAM_FILES + r"\Git\cmd\git.exe"
TAILSCALE = PROGRAM_FILES + r"\Tailscale\tailscale.exe"
ICACLS = SYSTEM32 + r"\icacls.exe"
SSH_DIR = PROGRAM_DATA + r"\ssh"
ADMIN_KEYS_NAME = "administrators_authorized_keys"
# `sync-devkit.DEVKIT_REPO`, which this cannot import; a test holds them equal. Public, so
# the fetch needs no credential, and none is offered.
FLEET_ORIGIN = "https://github.com/alexandrec90/devkit.git"
FLEET_REFS = "refs/devkit/fleet/"
FLEET_REPO_NAME = "fleet.git"
# What the keys file held after the last look, for the unelevated `join`:
# `elevated_request.FLEET_SEEN`.
FLEET_SEEN_NAME = "fleet-authorized.json"
FLEET_EVERY = 15 * 60
FETCH_TIMEOUT = 120
ADMINS_SID = "S-1-5-32-544"
SYSTEM_SID = "S-1-5-18"
# git's environment: `CHILD_ENV`, a `HOME` in the admin-only folder so no user config is
# read, and no prompt for a credential nobody may give.
GIT_ENV = {
    **CHILD_ENV,
    "HOME": SCRATCH,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}
TAILSCALE_STATUS = (TAILSCALE, "status", "--json")
# The blob of an ed25519 public key: its type, length-prefixed, then 32 bytes.
ED25519_BLOB = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
ED25519_BLOB_BYTES = len(ED25519_BLOB) + 32
HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
KEY_COMMENT = re.compile(r"^[\x21-\x7e]{1,128}$")


@dataclass(frozen=True)
class Layout:
    """Where the worker reads and writes. `main` builds the only real one, from constants."""

    root: str = ROOT
    spool: str = SPOOL
    home: str = HOME
    ssh: str = SSH_DIR

    @property
    def admin_keys(self) -> str:
        return os.path.join(self.ssh, ADMIN_KEYS_NAME)

    @property
    def fleet_repo(self) -> str:
        return os.path.join(self.home, FLEET_REPO_NAME)

    @property
    def fleet_seen(self) -> str:
        return os.path.join(self.home, FLEET_SEEN_NAME)

    @property
    def limits(self) -> str:
        return os.path.join(self.home, LIMITS_NAME)

    @property
    def log(self) -> str:
        return os.path.join(self.home, LOG_NAME)

    @property
    def results(self) -> str:
        return os.path.join(self.home, RESULTS_NAME)


@dataclass(frozen=True)
class Machine:
    """What `cycle` does to the machine, passed in so a test can do none of it."""

    run: Callable[[Sequence[str], int], int]
    query: Callable[[Sequence[str], int], str]
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    # git under `GIT_ENV`: its stdout, None when it failed. None here is no git at all,
    # which fails `fleet-authorize` closed.
    git: Callable[[Sequence[str], int], str | None] | None = None


@dataclass(frozen=True)
class Entry:
    """A spool entry as `lstat` saw it, with nothing followed."""

    name: str
    path: str
    regular: bool
    directory: bool


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failed: bool = False
    results: dict[str, tuple[str, str]] = field(default_factory=dict)

    def say(self, line: str, failed: bool = False) -> None:
        self.lines.append(line)
        self.failed = self.failed or failed

    def result(self, subject: str, result: str, detail: str) -> None:
        self.results[subject] = (result, detail)

    def absorb(self, other: Report) -> None:
        self.lines += other.lines
        self.failed = self.failed or other.failed
        self.results.update(other.results)


# --- the verb ---------------------------------------------------------------------


def service_state(listing: str) -> str:
    found = SERVICE_STATE.search(listing)
    return found["state"] if found else ""


def wsl_state(machine: Machine) -> str:
    return service_state(machine.query(WSL_QUERY, QUERY_TIMEOUT))


def wsl_unhealthy(machine: Machine) -> str:
    """Why WSL needs recovering, or "" when it does not: `docker-maint.wsl_stuck`. A
    service still `*_PENDING` `WSL_SETTLE` seconds later (a start in progress is not
    stuck), or a `wsl.exe` that does not answer. Neither query needs elevation, each has a
    timeout, and only silence counts: an exit code is WSL answering."""
    state = wsl_state(machine)
    if state.endswith("_PENDING"):
        machine.sleep(WSL_SETTLE)
        state = wsl_state(machine)
        if state.endswith("_PENDING"):
            return f"{WSL_SERVICE} is stuck in {state}"
    if machine.run(WSL_LIST, WSL_ANSWER_TIMEOUT) == TIMED_OUT:
        return f"{WSL_SERVICE} is {state or 'in an unknown state'} and wsl --list does not answer"
    return ""


def wsl_recover(machine: Machine) -> tuple[bool, list[str]]:
    """`(WSL answers afterwards, the lines to log)`: `docker-maint.elevated_recover`. End
    the service, wait for it to settle, then finish the WSL update while it is fresh, so
    Windows Update has no install left to retry and hang on. The update's own exit code
    does not decide it: offline, that fails, and WSL is still recovered."""
    lines = [f"taskkill exited {machine.run(WSL_KILL, KILL_TIMEOUT)}"]
    state = ""
    for _ in range(WSL_SETTLE_WAIT // WSL_POLL):
        state = wsl_state(machine)
        if state in WSL_SETTLED:
            break
        machine.sleep(WSL_POLL)
    lines.append(f"{WSL_SERVICE} after the kill: {state or 'unknown'}")
    lines.append(f"wsl --update exited {machine.run(WSL_UPDATE, WSL_UPDATE_TIMEOUT)}")
    answered = machine.run(WSL_LIST, WSL_ANSWER_TIMEOUT)
    lines.append(f"wsl --list exited {answered}")
    return answered not in (TIMED_OUT, SPAWN_FAILED), lines


# --- the second verb: peers' SSH keys ---------------------------------------------


def ed25519_key(text: object) -> str:
    """`ssh-ed25519 <base64>` of `text` when it is exactly one bare ed25519 public key --
    one line, no options prefix, at most a comment after it, the blob decoding to that
    key type -- and "" for anything else."""
    if not isinstance(text, str) or any(c in text for c in "\r\n\t\0"):
        return ""
    parts = text.split(" ")
    if len(parts) not in (2, 3) or parts[0] != "ssh-ed25519":
        return ""
    if len(parts) == 3 and not KEY_COMMENT.match(parts[2]):
        return ""
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return ""
    if len(blob) != ED25519_BLOB_BYTES or not blob.startswith(ED25519_BLOB):
        return ""
    return f"ssh-ed25519 {parts[1]}"


def fleet_records(listing: str) -> dict[str, tuple[str, str]]:
    """`{host: (key, address)}` from `for-each-ref` output of `fleet_list`: each ref's
    first JSON line, kept only when it names its own ref's host. The key and address are
    still claims here; `admitted` decides."""
    parts = listing.split("\0")
    found: dict[str, tuple[str, str]] = {}
    for at in range(0, len(parts) - 1, 2):
        name, body = parts[at].strip(), parts[at + 1]
        host = name.removeprefix(FLEET_REFS)
        if host == name or not HOST_NAME.match(host):
            continue
        line = next((ln for ln in body.splitlines() if ln.strip().startswith("{")), "")
        try:
            raw = json.loads(line) if line else None
        except ValueError:
            continue
        if isinstance(raw, dict) and raw.get("host") == host:
            key, address = raw.get("key"), raw.get("address")
            if isinstance(key, str) and isinstance(address, str):
                found[host] = (key, address)
    return found


def tailnet_peers(status: str) -> dict[str, frozenset[str]]:
    """`{host: its tailnet addresses}` for each peer in `tailscale status --json`, this
    machine (`Self`) left out, the host spelled as `fleet_ssh` publishes it."""
    try:
        loaded = json.loads(status)
    except ValueError:
        return {}
    peers = loaded.get("Peer") if isinstance(loaded, dict) else None
    found: dict[str, frozenset[str]] = {}
    for peer in peers.values() if isinstance(peers, dict) else ():
        if not isinstance(peer, dict) or not isinstance(peer.get("HostName"), str):
            continue
        host = peer["HostName"].strip().lower().split(".")[0]
        addresses = peer.get("TailscaleIPs")
        if HOST_NAME.match(host) and isinstance(addresses, list):
            found[host] = frozenset(a for a in addresses if isinstance(a, str))
    return found


def admitted(
    records: dict[str, tuple[str, str]], peers: dict[str, frozenset[str]]
) -> tuple[dict[str, str], dict[str, str]]:
    """`({host: key}, {host: why refused})`: a record counts only for a current tailnet
    peer at the address it claims, with a bare ed25519 key."""
    keys: dict[str, str] = {}
    refused: dict[str, str] = {}
    for host, (claimed, address) in sorted(records.items()):
        if host not in peers:
            refused[host] = "not a peer on this tailnet"
        elif not address or address not in peers[host]:
            refused[host] = "claims an address Tailscale does not give it"
        elif not (key := ed25519_key(claimed)):
            refused[host] = "its key is not one bare ssh-ed25519 key"
        else:
            keys[host] = key
    return keys, refused


def held_keys(text: str) -> frozenset[str]:
    """`type base64` of every key line in an authorized_keys file, options or not."""
    found = set()
    for line in text.splitlines():
        tokens = line.split()
        for at, token in enumerate(tokens[:-1]):
            if token.startswith(("ssh-", "ecdsa-", "sk-")):
                found.add(f"{token} {tokens[at + 1]}")
                break
    return frozenset(found)


def read_text(path: str) -> str:
    """The file's text, "" when it does not exist; any other failure raises."""
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


@dataclass(frozen=True)
class FleetPlan:
    """What `fleet-authorize` would add, what it refused, and what stopped it looking."""

    missing: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    held: frozenset[str] = frozenset()
    error: str = ""
    records: dict[str, tuple[str, str]] = field(default_factory=dict)


def fleet_plan(machine: Machine, layout: Layout) -> FleetPlan:
    """Fetch the records, ask Tailscale, read the keys file: `FleetPlan`. Writes nothing
    but the bare repository under `HOME` the fetch fills."""
    git = machine.git
    if git is None:
        return FleetPlan(error="no git to fetch the fleet's records with")
    repo = layout.fleet_repo
    if not os.path.isdir(repo) and git((GIT, "init", "--quiet", "--bare", repo), QUERY_TIMEOUT) is None:
        return FleetPlan(error="its record store could not be made")
    spec = f"+{FLEET_REFS}*:{FLEET_REFS}*"
    fetch = (GIT, "--git-dir", repo, "fetch", "--quiet", "--prune", "--no-tags", FLEET_ORIGIN, spec)
    if git(fetch, FETCH_TIMEOUT) is None:
        return FleetPlan(error="devkit's origin could not be fetched")
    form = "--format=%(refname)%00%(contents:body)%00"
    listing = git((GIT, "--git-dir", repo, "for-each-ref", form, FLEET_REFS), QUERY_TIMEOUT)
    if listing is None:
        return FleetPlan(error="the fetched records could not be listed")
    peers = tailnet_peers(machine.query(TAILSCALE_STATUS, QUERY_TIMEOUT))
    if not peers:
        return FleetPlan(error="Tailscale names no peer")
    records = fleet_records(listing)
    keys, refused = admitted(records, peers)
    try:
        held = held_keys(read_text(layout.admin_keys))
    except OSError:
        return FleetPlan(error=f"{ADMIN_KEYS_NAME} could not be read")
    missing = {host: key for host, key in keys.items() if key not in held}
    return FleetPlan(missing, refused, held, records=records)


def write_seen(layout: Layout, plan: FleetPlan, held: frozenset[str]) -> None:
    """What the keys file holds and which records were refused, the record as claimed, so
    the unelevated `join` asks again only once a refused record has changed."""
    refused = {
        host: {
            "key": plan.records.get(host, ("", ""))[0][:200],
            "address": plan.records.get(host, ("", ""))[1][:64],
            "why": why,
        }
        for host, why in plan.refused.items()
    }
    write_json(layout.fleet_seen, {"keys": sorted(held), "refused": refused})


def fleet_unhealthy(machine: Machine, layout: Layout) -> str:
    """Why keys are owed, or "" when every admitted peer's key is there. Stable words: the
    pass files a failure by them. A refused record alone owes nothing: it is logged when a
    key is added, and `admitted` will not change its mind for another request."""
    plan = fleet_plan(machine, layout)
    if plan.error:
        return f"the fleet's keys could not be checked: {plan.error}"
    if plan.missing:
        return f"a fleet peer's SSH key is missing from {ADMIN_KEYS_NAME}"
    write_seen(layout, plan, plan.held)
    return ""


def fleet_recover(machine: Machine, layout: Layout) -> tuple[bool, list[str]]:
    """Append each admitted peer's missing key, never removing one; `(all added, lines)`.
    A file this makes gets sshd's ACL, Administrators and SYSTEM alone."""
    plan = fleet_plan(machine, layout)
    lines = [f"refused {host}: {why}" for host, why in plan.refused.items()]
    if plan.error:
        return False, [*lines, plan.error]
    if not plan.missing:
        write_seen(layout, plan, plan.held)
        return True, [*lines, "nothing to add"]
    path = layout.admin_keys
    existed = os.path.isfile(path)
    try:
        text = read_text(path)
        head = "" if not text or text.endswith("\n") else "\r\n"
        added = "".join(f"{key} devkit-fleet:{host}\r\n" for host, key in plan.missing.items())
        with open(path, "a", encoding="utf-8", newline="") as handle:
            handle.write(head + added)
    except OSError as exc:
        return False, [*lines, f"{ADMIN_KEYS_NAME} could not be appended to: {exc}"]
    lines.append(f"added {', '.join(plan.missing)}")
    secured = True
    if not existed:
        acl = (ICACLS, path, "/inheritance:r", "/grant", f"*{ADMINS_SID}:F", "/grant", f"*{SYSTEM_SID}:F")
        code = machine.run(acl, QUERY_TIMEOUT)
        secured = code == 0
        lines.append(f"icacls exited {code}")
    write_seen(layout, plan, plan.held | frozenset(plan.missing.values()))
    return secured, lines


@dataclass(frozen=True)
class Verb:
    unhealthy: Callable[[Machine, Layout], str]
    recover: Callable[[Machine, Layout], tuple[bool, list[str]]]
    every: int
    # How a result says it went: `{why}, and {unfixed}` when it did not.
    fixed: str = "answers again"
    unfixed: str = "a restart did not bring it back"


VERBS: dict[str, Verb] = {
    "restart-wsl-service": Verb(
        lambda machine, _layout: wsl_unhealthy(machine),
        lambda machine, _layout: wsl_recover(machine),
        RESTART_EVERY,
    ),
    "fleet-authorize": Verb(
        fleet_unhealthy,
        fleet_recover,
        FLEET_EVERY,
        fixed="every admitted peer's key is there",
        unfixed="not every key could be added; worker.log says which and why",
    ),
}

REQUEST_NAMES: dict[str, str] = {f"{verb}{REQUEST_SUFFIX}": verb for verb in VERBS}


def verb_of(name: str) -> str | None:
    """The verb a spool entry's exact name selects, or None for every other name.

    A plain lookup and nothing cleverer: no case folding, no stripping, no parsing. A
    `..\\`, a trailing dot, a `:stream` and a homoglyph all miss it the same way.
    """
    return REQUEST_NAMES.get(name)


# --- the spool --------------------------------------------------------------------


def is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def plain_directory(path: str) -> bool:
    """Whether `path` is a real directory: no symlink, no junction, and not missing."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not is_reparse(info)


def verbatim(path: str) -> str:
    """`path` with Win32's name rewriting off, so an entry named `x.request.` is that
    entry and not the `x.request` Win32 would strip it to."""
    prefix = "\\\\?\\"
    return prefix + path if os.name == "nt" and not path.startswith(prefix) else path


def scan(spool: str) -> list[Entry]:
    """The spool's entries, each `lstat`ed, in name order. Streams are not entries. An
    entry gone between the listing and the look is not one either."""
    found = []
    with os.scandir(verbatim(spool)) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            linked = is_reparse(info)
            regular = stat.S_ISREG(info.st_mode) and not linked
            directory = stat.S_ISDIR(info.st_mode) and not linked
            found.append(Entry(entry.name, entry.path, regular, directory))
    return sorted(found, key=lambda entry: entry.name)


def clear_readonly(path: str) -> None:
    """Take the read-only attribute off, which `os.open(..., 0o444)` sets with nothing but
    the right to add a file, and which refuses a delete even to an administrator. By the
    API and not `os.chmod`, which follows a link on some of the Pythons this runs under."""
    if sys.platform != "win32":  # also what tells mypy `windll` exists below
        return
    try:
        ctypes.windll.kernel32.SetFileAttributesW(path, FILE_ATTRIBUTE_NORMAL)
    except (AttributeError, OSError):
        return


def remove(entry: Entry) -> bool:
    """Delete the entry itself and never what it names. `rmdir` is not recursive, so a
    directory with anything in it stays and is reported; a walk would be path-based, and
    the user could swap a junction in under it. The installer's ACL refuses them
    subdirectories anyway."""
    try:
        if entry.directory:
            os.rmdir(entry.path)
        else:
            if entry.regular:
                clear_readonly(entry.path)
            os.unlink(entry.path)  # a symlink or a junction goes; its target does not
    except OSError:
        return False
    return True


def shown(name: str) -> str:
    """A name the user chose, safe to put on a log line."""
    return ascii(name)[:120]


def shown_line(line: str) -> str:
    """A recovery's line, one log line whatever it carries: a host name is a record's."""
    return " ".join(line.split())[:300]


# --- what is kept across runs -----------------------------------------------------


def write_json(path: str, data: dict) -> None:
    temporary = path + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


def write_limits(path: str, limits: dict[str, float]) -> None:
    write_json(path, limits)


def window(every: int) -> str:
    """A rate limit as a result says it: `6-hour`, `15-minute`."""
    if every >= 3600 and every % 3600 == 0:
        return f"{every // 3600}-hour"
    return f"{max(every // 60, 1)}-minute"


def limited(last: float | None, every: int, now: float) -> bool:
    """Whether a verb that last ran at `last` is still inside its window. A `last` in the
    future reads as inside it: a wrong clock must not open the limit."""
    return last is not None and now - last < every


def read_limits(path: str, now: float) -> dict[str, float]:
    """The last run of each verb. A record that exists and cannot be read is every verb
    having just run, rewritten so that costs one window rather than all of them. One that
    cannot be rewritten either raises: nothing runs, and `guarded` reports it."""
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        loaded = None
    if isinstance(loaded, dict) and all(isinstance(v, int | float) for v in loaded.values()):
        return {str(verb): float(value) for verb, value in loaded.items()}
    limits = {verb: now for verb in VERBS}
    write_limits(path, limits)
    return limits


def write_results(path: str, found: dict[str, tuple[str, str]], now: float) -> None:
    """Record each subject's latest result, when it changed: a quiet machine writes nothing
    here every five minutes."""
    try:
        with open(path, encoding="utf-8") as handle:
            held = json.load(handle)
    except (OSError, ValueError):
        held = {}
    held = held if isinstance(held, dict) else {}
    changed = {
        subject: {"result": result, "detail": detail, "at": now}
        for subject, (result, detail) in found.items()
        if not isinstance(held.get(subject), dict)
        or (held[subject].get("result"), held[subject].get("detail")) != (result, detail)
    }
    if changed:
        write_json(path, {**held, **changed})


def append_log(path: str, lines: Sequence[str], when: float, keep: int = LOG_BYTES) -> None:
    """Add `lines`, then cut the file back to its newest `keep` bytes."""
    if not lines:
        return
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(f"{stamp}Z {line}\n" for line in lines)
    if os.path.getsize(path) > keep:
        with open(path, "rb") as handle:
            handle.seek(-keep, os.SEEK_END)
            tail = handle.read()
        with open(path, "wb") as handle:
            handle.write(tail[tail.find(b"\n") + 1 :])


# --- a cycle ----------------------------------------------------------------------


def serve(
    entry: Entry, verb: str, limits: dict[str, float], layout: Layout, machine: Machine
) -> Report:
    """Act on one request. Health first, so a healthy service spends no window and reports
    no refusal; then the order is the safety: the window is checked, the request is gone
    and the window is saved, all before anything runs, so a crash at any point leaves a
    verb that did not run rather than one that runs again."""
    report = Report()
    spec = VERBS[verb]
    why = spec.unhealthy(machine, layout)
    gone = remove(entry)
    if not why:
        report.say(f"{verb}: healthy; left alone", failed=not gone)
        report.result(verb, HEALTHY, "left alone")
        return report
    now = machine.now()
    if limited(limits.get(verb), spec.every, now):
        # Dropped, not held: a restart run hours late, on a stale request, ends whatever
        # is running by then. A caller that still needs it asks again.
        report.say(f"{verb}: {why}; inside its rate limit; request dropped", failed=not gone)
        report.result(verb, REFUSED, f"{why}, inside its {window(spec.every)} rate limit")
        return report
    if not gone:
        report.say(f"{verb}: the request could not be deleted; not run", failed=True)
        report.result(verb, REFUSED, "its request could not be deleted")
        return report
    limits[verb] = now
    try:
        write_limits(layout.limits, limits)
    except OSError as exc:
        report.say(f"{verb}: the rate limit could not be saved ({exc}); not run", failed=True)
        report.result(verb, REFUSED, "its rate limit could not be saved")
        return report
    recovered, lines = spec.recover(machine, layout)
    report.lines += [f"{verb}: {why}", *(f"{verb}: {shown_line(line)}" for line in lines)]
    report.failed = report.failed or not recovered
    report.result(
        verb,
        RECOVERED if recovered else FAILED,
        spec.fixed if recovered else f"{why}, and {spec.unfixed}",
    )
    return report


def cycle(layout: Layout, machine: Machine) -> Report:
    """One pass over the spool. A healthy service or a dropped request is not a failure; a
    spool that cannot be trusted, a run that failed and an entry that would not go are."""
    report = Report()
    if not all(plain_directory(path) for path in (layout.root, layout.spool, layout.home)):
        report.say(f"refused: {layout.spool} or a parent is missing or a reparse point", True)
        report.result(WORKER, REFUSED, "its spool or a parent is missing or a reparse point")
        return report
    report.result(WORKER, HEALTHY, "")
    limits = read_limits(layout.limits, machine.now())
    for entry in scan(layout.spool):
        if entry.regular and entry.name == SENTINEL:
            continue
        verb = verb_of(entry.name) if entry.regular else None
        if verb is not None:
            report.absorb(serve(entry, verb, limits, layout, machine))
        elif remove(entry):
            report.say(f"ignored {shown(entry.name)}: deleted")
        else:
            report.say(f"ignored {shown(entry.name)}: could not be deleted", failed=True)
    return report


# --- the machine ------------------------------------------------------------------


def run_step(argv: Sequence[str], timeout: int) -> int:
    """Run a fixed argv and capture nothing: no shell, a fixed cwd, a fixed environment,
    no window. `TIMED_OUT` past `timeout`, `SPAWN_FAILED` when it would not start."""
    try:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            cwd=SYSTEM32,
            env=dict(CHILD_ENV),
            creationflags=NO_WINDOW,
        ).returncode
    except subprocess.TimeoutExpired:
        return TIMED_OUT
    except (OSError, subprocess.SubprocessError):
        return SPAWN_FAILED


def query_step(argv: Sequence[str], timeout: int) -> str:
    """`run_step` for a program whose output is the answer; "" when it gives none."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            cwd=SYSTEM32,
            env=dict(CHILD_ENV),
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def git_step(argv: Sequence[str], timeout: int) -> str | None:
    """`query_step` for git, under `GIT_ENV`: its stdout, None when it failed, since an
    empty listing is an answer and a failed fetch is not."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            cwd=SYSTEM32,
            env=dict(GIT_ENV),
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def guarded(layout: Layout, machine: Machine) -> int:
    """`cycle`, its log and its results. Non-zero is what `schedule_health` reads off the
    scheduler, and the next fire overwrites it, so `RESULTS` is what lasts. A crash is
    logged, since `pythonw` has nowhere to print one, and re-raised, which is that code."""
    try:
        report = cycle(layout, machine)
    except Exception as exc:
        now = machine.now()
        append_log(layout.log, traceback.format_exc().splitlines(), now)
        write_results(layout.results, {WORKER: (FAILED, f"it crashed: {type(exc).__name__}")}, now)
        raise
    now = machine.now()
    try:
        append_log(layout.log, report.lines, now)
        write_results(layout.results, report.results, now)
    except OSError:
        return 1
    return 1 if report.failed else 0


def main() -> int:
    """No parameters and no `sys.argv`: there is nothing an unelevated caller could pass."""
    if os.name != "nt":
        print("elevated_worker: Windows-only; nothing to do here.")
        return 0
    return guarded(Layout(), Machine(run_step, query_step, git=git_step))


if __name__ == "__main__":
    sys.exit(main())
