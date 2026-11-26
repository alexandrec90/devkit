#!/usr/bin/env python3
"""Who each PC is for SSH, published where every PC reads it, and the setup that follows.

The PCs share no login: one logs in as `alexa`, another only as `administrator`
(`AllowUsers`). A bare `ssh <host>` offers this PC's own user name, so `fleet.py` read one
of them as `Permission denied` for weeks, and nothing said why. So each PC says who it
is, and each PC configures itself from what the others said:

- **The record.** `refs/devkit/fleet/<host>` in devkit's repository, one commit of the
  empty tree whose message carries a JSON line (`Record`): the login name, its ed25519
  public key, its tailnet address and where its devkit checkout sits under its home. A
  ref only, as `job_claims.py` keeps its claims, so no branch moves and every PC reads it
  with the fetch it already makes. Nothing secret goes there: a public key and a host
  name are what any peer is shown on connecting anyway. It is pushed only when it
  changes, and `job_claims._push` refuses anything outside `refs/devkit/`.
- **The config.** A block in `~/.ssh/config` between `BEGIN` and `END`, `Host` /
  `HostName` / `User` per peer, rewritten whole; nothing outside the markers is touched.
  A hand-written entry above it wins in ssh's first-match order, so one naming a
  different `User` is reported rather than overridden.
- **The keys.** Each peer's key in the file this PC's sshd reads for its login:
  `administrators_authorized_keys` under `ProgramData\\ssh` for an administrator (Windows
  OpenSSH's default `Match Group administrators`), else `~/.ssh/authorized_keys`. Keys are
  added, never removed. The administrators file is writable only elevated, so for it
  `join` asks the elevated worker for `fleet-authorize` by name (`elevated_request`) and
  writes nothing itself: the worker fetches the records from devkit's origin, admits a key
  only for a host Tailscale lists at the address its record claims, appends, and leaves
  `elevated_request.FLEET_SEEN` saying what the file holds, since an unelevated run
  cannot read that file to see its request was met. `elevated_worker.py` states what that
  trusts. A PC with no worker gets the one person's step there is, to install it
  (`fix_elevated.py`), and nothing else is ever a person's.

`join` does all three; `devkit-after-merge` runs it every cycle on every PC
(`scheduled`). A PC with no key gets one (`ssh-keygen -t ed25519`).

`diagnose` turns ssh's refusal into the reason in words: no published record (wrong
user), a record but no config entry here, a key the peer has not authorized, sshd not
listening, no answer at all.

Tested in `tests/test_fleet_ssh.py`.
"""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collector_handoff as handoff
import elevated_request
import job_claims

Runner = Callable[[Sequence[str], int], tuple[int, str]]
Git = job_claims.Git

FLEET = job_claims.FLEET
BEGIN = "# >>> devkit fleet: written by scripts/fleet.py join; edits inside are replaced"
END = "# <<< devkit fleet"
KEY_TYPE = "ssh-ed25519"
KEY_NAME = "id_ed25519"
# The well-known SID of BUILTIN\Administrators, listed by `whoami /groups` for a member
# even in an unelevated token, where it is "used for deny only".
ADMINS_SID = "S-1-5-32-544"
ADMIN_KEYS = "administrators_authorized_keys"
WINDOWS = sys.platform == "win32"
TIMEOUT = 30
# `devkit-after-merge` runs `join` under pythonw.exe: see `scripts/windowless-jobs.md`.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# What a record may carry. Each lands in `~/.ssh/config` or an authorized_keys file, so a
# value that could end a line or start a directive is refused, not escaped.
_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY = re.compile(rf"^{KEY_TYPE} [A-Za-z0-9+/]+={{0,3}}$")
_PATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def spawn(argv: Sequence[str], timeout: int = TIMEOUT) -> tuple[int, str]:
    """Exit code and stdout, or stderr when it failed: that is the reason."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout if done.returncode == 0 else done.stderr or done.stdout


# --- the record ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """One PC as it publishes itself for SSH."""

    host: str
    user: str
    key: str
    address: str = ""
    devkit: str = ""  # its devkit checkout, relative to its home, POSIX-spelled

    def payload(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def valid(record: Record) -> str:
    """Why `record` cannot be used, or ""."""
    if not _HOST.match(record.host):
        return f"host {record.host!r} is not a host name"
    if not _USER.match(record.user):
        return f"login {record.user!r} is not one ssh_config can carry"
    if not _KEY.match(record.key):
        return f"key is not a bare {KEY_TYPE} public key"
    if record.address:
        try:
            ipaddress.ip_address(record.address)
        except ValueError:
            return f"address {record.address!r} is not an IP address"
    if record.devkit and (not _PATH.match(record.devkit) or ".." in record.devkit.split("/")):
        return f"devkit path {record.devkit!r} is not a plain relative path"
    return ""


def parse_record(body: str) -> Record | None:
    """The record in a commit body: its first line that is a JSON object, if it is one."""
    for line in body.splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            return None
        if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
            return None
        try:
            record = Record(
                host=raw["host"],
                user=raw["user"],
                key=raw["key"],
                address=raw.get("address", ""),
                devkit=raw.get("devkit", ""),
            )
        except KeyError:
            return None
        return None if valid(record) else record
    return None


LISTING_FORMAT = "%(refname)%00%(objectname)%00%(contents:body)%00"


@dataclass(frozen=True)
class Published:
    """One `refs/devkit/fleet/*` ref as the last fetch left it."""

    host: str
    sha: str
    record: Record | None


def parse_listing(text: str) -> dict[str, Published]:
    """`for-each-ref` output in `LISTING_FORMAT`, by host. A record naming another host
    than its ref is read as no record: each PC writes only its own."""
    parts = text.split("\0")
    found: dict[str, Published] = {}
    for at in range(0, len(parts) - 2, 3):
        name, sha, body = parts[at].strip(), parts[at + 1].strip(), parts[at + 2]
        if not name.startswith(FLEET):
            continue
        host = name.removeprefix(FLEET)
        record = parse_record(body)
        found[host] = Published(host, sha, record if record and record.host == host else None)
    return found


def read_published(git: Git, fetch: bool = True) -> tuple[dict[str, Published], str]:
    """Every PC's ref, and why origin could not be asked ("" when it could). A failed
    fetch still reads what the last one left."""
    why = ""
    if fetch:
        fetched = git("fetch", "--quiet", "origin", f"+{FLEET}*:{FLEET}*")
        if fetched.returncode != 0:
            why = " ".join(f"{fetched.stderr or ''} {fetched.stdout or ''}".split())[:160]
            why = why or f"git fetch exited {fetched.returncode}"
    listed = git("for-each-ref", f"--format={LISTING_FORMAT}", FLEET)
    return (parse_listing(listed.stdout or "") if listed.returncode == 0 else {}), why


def records_of(published: Mapping[str, Published]) -> dict[str, Record]:
    return {host: p.record for host, p in published.items() if p.record is not None}


def publish(git: Git, mine: Record, published: Mapping[str, Published], now=None) -> str:
    """Push `mine` when the ref does not already say it; what happened, "" for nothing."""
    held = published.get(mine.host)
    if held is not None and held.record == mine:
        return ""
    moved = job_claims.stamp(
        git,
        job_claims.fleet_ref(mine.host),
        f"fleet {mine.host}",
        held.sha if held else "",
        now,
        detail=mine.payload(),
    )
    return f"published {mine.host} as {mine.user}" if moved else "could not publish this PC"


# --- this PC ---------------------------------------------------------------------------------


def ensure_key(ssh_dir: Path, comment: str, run: Runner = spawn) -> tuple[str, str]:
    """`(this PC's ed25519 public key, what was done)`, making the key when there is none.
    The key is "" when it could not be had, and the note says why."""
    private, public = ssh_dir / KEY_NAME, ssh_dir / f"{KEY_NAME}.pub"
    note = ""
    if not public.is_file():
        if private.is_file():
            code, out = run(["ssh-keygen", "-y", "-f", str(private)], TIMEOUT)
            if code != 0:
                return "", f"could not read the public half of {private}: {out.strip()[:120]}"
            public.write_text(out.strip() + "\n", encoding="utf-8")
            note = f"wrote {public} from its private key"
        else:
            ssh_dir.mkdir(parents=True, exist_ok=True)
            argv = ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment]
            code, out = run([*argv, "-f", str(private)], TIMEOUT)
            if code != 0 or not public.is_file():
                return "", f"ssh-keygen could not make a key: {out.strip()[:120]}"
            note = f"generated {private}"
    try:
        fields = public.read_text(encoding="utf-8").split()
    except OSError as exc:
        return "", f"could not read {public}: {exc}"
    if len(fields) < 2 or fields[0] != KEY_TYPE:
        return "", f"{public} is not an {KEY_TYPE} key"
    return f"{fields[0]} {fields[1]}", note


def tailnet_address(run: Runner = spawn) -> str:
    """This PC's tailnet IPv4, or "" without Tailscale."""
    code, out = run(["tailscale", "ip", "-4"], TIMEOUT)
    for line in out.splitlines() if code == 0 else []:
        try:
            return str(ipaddress.ip_address(line.strip()))
        except ValueError:
            continue
    return ""


def devkit_under_home(devkit_dir: Path, home: Path) -> str:
    try:
        return devkit_dir.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        return ""


def local_record(
    devkit_dir: Path, run: Runner = spawn, home: Path | None = None, host: str = ""
) -> tuple[Record | None, list[str]]:
    """This PC's record, and what was done or why there is none."""
    home = Path.home() if home is None else home
    host = host or handoff.this_host()
    user = getpass.getuser()
    key, note = ensure_key(home / ".ssh", f"{user}@{host}", run)
    notes = [note] if note else []
    if not key:
        return None, notes
    record = Record(host, user, key, tailnet_address(run), devkit_under_home(devkit_dir, home))
    if why := valid(record):
        return None, [*notes, f"not published: {why}"]
    return record, notes


def is_admin(run: Runner = spawn) -> bool:
    """Whether this login is in Administrators, elevated or not. Off Windows, never."""
    if not WINDOWS:
        return False
    code, out = run(["whoami", "/groups"], TIMEOUT)
    return code == 0 and ADMINS_SID in out


def program_data() -> Path:
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData")


def authorized_keys_path(
    admin: bool, sshd_config: str | None, home: Path, programdata: Path
) -> Path:
    """The file this PC's sshd reads a login's keys from. For an administrator, the shared
    file whenever sshd_config sends administrators there, or is missing or unreadable (None)
    so cannot say -- Windows OpenSSH's shipped config does."""
    if admin:
        active = not sshd_config or any(
            ADMIN_KEYS in line and not line.strip().startswith("#")
            for line in sshd_config.splitlines()
        )
        if active:
            return programdata / "ssh" / ADMIN_KEYS
    return home / ".ssh" / "authorized_keys"


# --- ~/.ssh/config -----------------------------------------------------------------------------


def render_block(records: Iterable[Record], here: str) -> list[str]:
    """The managed block's lines, markers included: every peer but `here`, by name."""
    lines = [BEGIN]
    for record in sorted(records, key=lambda r: r.host):
        if record.host == here:
            continue
        lines.append(f"Host {record.host}")
        if record.address:
            lines.append(f"    HostName {record.address}")
        lines.append(f"    User {record.user}")
        # The tailnet authenticates the peer, so a first sight of its host key is taken;
        # a changed one still fails, which `diagnose` names.
        lines.append("    StrictHostKeyChecking accept-new")
    lines.append(END)
    return lines


def _span(lines: Sequence[str]) -> tuple[int, int] | None:
    """Where the block is, `(begin, end)` inclusive; None when it is not whole."""
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
        return begins[0], ends[0]
    return None


def merge_block(text: str, block: Sequence[str]) -> str | None:
    """`text` with the managed block replaced by `block`, or added at the end; None when
    the markers are there but not as one pair, which is a hand edit not to guess about.
    Line endings are the file's own, and nothing outside the markers changes."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    has_marker = any(line.strip() in (BEGIN, END) for line in lines)
    span = _span(lines)
    if has_marker and span is None:
        return None
    if span is not None:
        merged = [*lines[: span[0]], *block, *lines[span[1] + 1 :]]
    else:
        merged = [*lines, *([""] if lines and lines[-1].strip() else []), *block]
    return eol.join(merged) + eol


def outside_block(text: str) -> list[str]:
    lines = text.splitlines()
    span = _span(lines)
    return lines if span is None else [*lines[: span[0]], *lines[span[1] + 1 :]]


def configured_hosts(text: str) -> frozenset[str]:
    """The hosts the managed block names."""
    lines = text.splitlines()
    span = _span(lines)
    if span is None:
        return frozenset()
    named = (line.split()[1:] for line in lines[span[0] : span[1]] if _directive(line) == "host")
    return frozenset(name.lower() for names in named for name in names)


def _directive(line: str) -> str:
    words = line.strip().split(None, 1)
    return words[0].lower() if words and not words[0].startswith("#") else ""


def hand_users(text: str) -> dict[str, str]:
    """`{host: User}` for each exact `Host` entry outside the block that sets one."""
    found: dict[str, str] = {}
    current: list[str] = []
    for line in outside_block(text):
        word = _directive(line)
        if word in ("host", "match"):
            current = [n.lower() for n in line.split()[1:]] if word == "host" else []
            current = [n for n in current if not any(c in n for c in "*?!")]
        elif word == "user" and len(line.split()) > 1:
            for name in current:
                found.setdefault(name, line.split()[1])
    return found


def shadowed(text: str, records: Iterable[Record]) -> list[str]:
    """A line per peer whose hand-written entry, read first by ssh, names another login."""
    hand = hand_users(text)
    return [
        f"~/.ssh/config names {r.host} above the devkit block with User {hand[r.host]}, and "
        f"ssh takes the first value it reads: {r.host} publishes {r.user}, so fix or delete "
        "that entry"
        for r in sorted(records, key=lambda r: r.host)
        if r.host in hand and hand[r.host] != r.user
    ]


# --- authorized keys ---------------------------------------------------------------------------


def key_material(line: str) -> str:
    """`type base64` of an authorized_keys or .pub line, options and comment dropped."""
    tokens = line.split()
    for at, token in enumerate(tokens[:-1]):
        if token.startswith(("ssh-", "ecdsa-", "sk-")):
            return f"{token} {tokens[at + 1]}"
    return ""


def present(text: str) -> frozenset[str]:
    return frozenset(m for line in text.splitlines() if (m := key_material(line)))


def with_keys(text: str, peers: Iterable[Record]) -> tuple[str, list[str]]:
    """`text` with each peer's key that is not in it appended, and those peers' hosts.
    Nothing already there is changed or removed."""
    have = set(present(text))
    added_lines: list[str] = []
    added: list[str] = []
    for peer in sorted(peers, key=lambda r: r.host):
        if key_material(peer.key) in have:
            continue
        have.add(key_material(peer.key))
        added_lines.append(f"{peer.key} devkit-fleet:{peer.user}@{peer.host}")
        added.append(peer.host)
    if not added_lines:
        return text, []
    eol = "\r\n" if "\r\n" in text else "\n"
    head = text if not text or text.endswith(("\n", "\r\n")) else text + eol
    return head + eol.join(added_lines) + eol, added


def _read(path: Path) -> str | None:
    """The file's text, "" when it does not exist, None when it cannot be read."""
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError:
        return None


@dataclass(frozen=True)
class Seen:
    """What the elevated worker last saw (`elevated_request.FLEET_SEEN`): the keys the
    administrators file holds, and each record it refused, `{host: (key, address)}`."""

    keys: frozenset[str] = frozenset()
    refused: Mapping[str, tuple[str, str]] = field(default_factory=dict)


def parse_seen(text: str) -> Seen:
    """The worker's receipt; an empty one for anything that is not its shape."""
    try:
        raw = json.loads(text)
    except ValueError:
        return Seen()
    if not isinstance(raw, dict):
        return Seen()
    keys = raw.get("keys") if isinstance(raw.get("keys"), list) else []
    refused = raw.get("refused") if isinstance(raw.get("refused"), dict) else {}
    return Seen(
        frozenset(k for k in keys if isinstance(k, str)),
        {
            host: (str(entry.get("key", "")), str(entry.get("address", "")))
            for host, entry in refused.items()
            if isinstance(entry, dict)
        },
    )


def read_seen(path: str = elevated_request.FLEET_SEEN) -> Seen:
    return parse_seen(_read(Path(path)) or "")


def owed_to_worker(peers: Iterable[Record], text: str | None, seen: Seen) -> list[str]:
    """The peers whose key the administrators file still lacks, by host. Read off the file
    when this run can read it, else off the worker's receipt. A record the worker refused
    is not asked about again until it changes: asking would only be refused again."""
    have = present(text) if text is not None else seen.keys
    owed = []
    for peer in sorted(peers, key=lambda r: r.host):
        if key_material(peer.key) in have:
            continue
        if seen.refused.get(peer.host) == (peer.key, peer.address):
            continue
        owed.append(peer.host)
    return owed


@dataclass
class Joined:
    """What a `join` did, as lines to print."""

    lines: list[str] = field(default_factory=list)


Ask = Callable[..., str]


def authorize_peers(
    peers: Sequence[Record],
    here: str,
    devkit_dir: Path,
    run: Runner = spawn,
    home: Path | None = None,
    programdata: Path | None = None,
    ask: Ask = elevated_request.request,
    seen: Callable[[], Seen] = read_seen,
) -> Joined:
    """Every peer's key into the file this PC's sshd reads: written here for the user's own
    file, asked of the elevated worker for the administrators one."""
    joined = Joined()
    if not peers:
        return joined
    home = Path.home() if home is None else home
    programdata = program_data() if programdata is None else programdata
    admin = is_admin(run)
    path = authorized_keys_path(
        admin, _read(programdata / "ssh" / "sshd_config"), home, programdata
    )
    text = _read(path)
    if path.name == ADMIN_KEYS:
        if owed := owed_to_worker(peers, text, seen()):
            said = ask(elevated_request.FLEET_AUTHORIZE, checkout=devkit_dir)
            joined.lines.append(f"{', '.join(owed)} not yet in {ADMIN_KEYS}: {said}")
        return joined
    if text is None:
        return Joined([f"could not read {path}"])
    merged, added = with_keys(text, peers)
    if not added:
        return joined
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(merged.encode("utf-8"))
    except OSError as exc:
        return Joined([f"could not write {path}: {exc}"])
    joined.lines.append(f"authorized {', '.join(added)} in {path}")
    return joined


# --- the verbs ---------------------------------------------------------------------------------


def write_config(records: Sequence[Record], here: str, home: Path) -> list[str]:
    """Rewrite the managed block of `~/.ssh/config`; what was said."""
    path = home / ".ssh" / "config"
    text = _read(path)
    if text is None:
        return [f"could not read {path}"]
    peers = [r for r in records if r.host != here]
    lines = shadowed(text, peers)
    if not peers and _span(text.splitlines()) is None:
        return lines
    merged = merge_block(text, render_block(peers, here))
    if merged is None:
        return [*lines, f"{path} has a devkit marker without its pair: fix it by hand"]
    if merged != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(merged.encode("utf-8"))
        lines.append(f"wrote {len(peers)} peer(s) to {path}")
    return lines


def join(
    devkit_dir: Path,
    git: Git,
    run: Runner = spawn,
    home: Path | None = None,
    fetch: bool = True,
    host: str = "",
    programdata: Path | None = None,
    **asking: object,
) -> Joined:
    """Publish this PC, write every peer into `~/.ssh/config`, authorize their keys.
    `asking` reaches `authorize_peers` (`ask`, `seen`), for a test's worker."""
    home = Path.home() if home is None else home
    host = host or handoff.this_host()
    published, why = read_published(git, fetch)
    joined = Joined([f"could not fetch the fleet records: {why}"] if why else [])
    mine, notes = local_record(devkit_dir, run, home, host)
    joined.lines += notes
    if mine is not None and (said := publish(git, mine, published)):
        joined.lines.append(said)
    peers = [r for h, r in records_of(published).items() if h != host]
    joined.lines += write_config(peers, host, home)
    joined.lines += authorize_peers(peers, host, devkit_dir, run, home, programdata, **asking).lines
    return joined


def scheduled(devkit_dir: Path, git: Git) -> Joined:
    """`join` as `devkit-after-merge` runs it: its own fetch brought the refs already."""
    return join(devkit_dir, git, fetch=False)


# --- why a peer did not answer ---------------------------------------------------------------


@dataclass(frozen=True)
class Known:
    """What this PC knows of its peers' SSH setup, for `diagnose`."""

    records: Mapping[str, Record] = field(default_factory=dict)
    configured: frozenset[str] = frozenset()


def known(git: Git, home: Path | None = None) -> Known:
    """The records the last fetch left and the hosts the managed block names."""
    home = Path.home() if home is None else home
    published, _ = read_published(git, fetch=False)
    return Known(records_of(published), configured_hosts(_read(home / ".ssh" / "config") or ""))


def diagnose(said: str, host: str, facts: Known) -> str:
    """ssh's refusal to reach `host` as what to do about it; its last line when unknown."""
    text = said.lower()
    record = facts.records.get(host)
    join_there = f"run `python scripts/fleet.py join` on {host}"
    if "permission denied" in text:
        if record is None:
            return (
                f"wrong user, probably: {host} has published no SSH login, so ssh offered "
                f"this PC's own user name -- {join_there}"
            )
        if host not in facts.configured:
            return (
                f"wrong user: {host} logs in as {record.user}, and ~/.ssh/config here has no "
                "devkit entry for it -- run `python scripts/fleet.py join` here"
            )
        return (
            f"key not authorized: {host} refuses this PC's key for {record.user} -- its own "
            "devkit pass adds it, through its elevated worker for an administrator login; "
            f"a red `fleet-authorize` result on {host} says why it has not"
        )
    if "connection refused" in text:
        return (
            f"sshd not listening: {host} is on the tailnet but refuses port 22 -- start "
            "OpenSSH Server there (`Start-Service sshd; Set-Service sshd -StartupType "
            "Automatic`, elevated)"
        )
    if any(w in text for w in ("timed out", "no route to host", "network is unreachable")):
        return (
            f"no answer on port 22: {host} is asleep, its sshd is stopped, or its firewall "
            "blocks it"
        )
    if "could not resolve hostname" in text:
        if record is None or not record.address:
            return f"name not resolved: {host} has published no tailnet address -- {join_there}"
        return f"name not resolved: run `python scripts/fleet.py join` here to pin {host}'s address"
    if "host key verification failed" in text or "identification has changed" in text:
        return (
            f"host key changed: {host}'s key no longer matches ~/.ssh/known_hosts -- if it "
            f"was reinstalled, run `ssh-keygen -R {host}` here"
        )
    lines = [line for line in said.splitlines() if line.strip()]
    return lines[-1] if lines else ""
