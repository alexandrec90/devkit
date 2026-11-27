"""`scripts/fleet_ssh.py`: each PC says who it is for SSH, and each configures itself.

What is worth pinning is what goes wrong quietly: a record that could inject a line into
`~/.ssh/config`, a block rewrite that touches the person's own entries, a key removed,
and an ssh refusal reported as raw stderr when the reason is knowable. The publish
drives real git against a bare repository, as `test_job_claims.py` does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import fleet_ssh as fs
import sweep

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGl0c2Fua2V5"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG90aGVya2V5"
HERE, FAR, THIRD = "desktop-here", "desktop-far", "desktop-third"
NOW = 1_800_000_000.0


def record(host: str = FAR, user: str = "administrator", **fields: str) -> fs.Record:
    fields.setdefault("key", KEY)
    fields.setdefault("address", "100.64.0.2")
    return fs.Record(host, user, **fields)


def no_spawn(argv: Sequence[str], timeout: int) -> tuple[int, str]:
    pytest.fail(f"spawned {argv}")


# --- the record --------------------------------------------------------------------------


def test_a_record_survives_its_commit_body():
    mine = record(devkit="vs-code/devkit")
    body = f"{mine.payload()}\n\nnonce abc\n"
    assert fs.parse_record(body) == mine


@pytest.mark.parametrize(
    "bad",
    [
        record(user="alexa\n    ProxyCommand evil"),
        record(user="two words"),
        record(key="ssh-rsa AAAAB3"),
        record(key=f"{KEY} comment"),
        record(address="100.64.0.2\nHost *"),
        record(host="Desktop Far"),
        record(devkit="../elsewhere"),
        record(devkit="/abs/devkit"),
    ],
)
def test_a_record_that_could_inject_a_line_is_refused(bad: fs.Record):
    assert fs.valid(bad)
    assert fs.parse_record(bad.payload()) is None


def test_a_body_with_no_record_or_a_malformed_one_reads_as_none():
    assert fs.parse_record("nonce abc") is None
    assert fs.parse_record("{not json") is None
    assert fs.parse_record(json.dumps({"host": FAR})) is None
    assert fs.parse_record(json.dumps({**json.loads(record().payload()), "user": 3})) is None


def test_the_listing_reads_each_host_and_drops_a_record_naming_another():
    far, third = record(FAR), record(THIRD)
    text = (
        f"{fs.FLEET}{FAR}\0sha1\0{far.payload()}\n\nnonce a\n\0\n"
        f"{fs.FLEET}{HERE}\0sha2\0{third.payload()}\n\0\n"
    )
    found = fs.parse_listing(text)
    assert found[FAR] == fs.Published(FAR, "sha1", far)
    assert found[HERE] == fs.Published(HERE, "sha2", None)
    assert fs.records_of(found) == {FAR: far}


# --- publishing, against real git ----------------------------------------------------------


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def clone(root: Path, name: str, remote: Path) -> sweep.Git:
    path = root / name
    path.mkdir()
    run_git(path, "init", "-q")
    run_git(path, "remote", "add", "origin", remote.as_posix())
    return sweep.git_for(path)


@pytest.fixture
def pcs(tmp_path: Path):
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "-q", "--bare", str(remote))
    return clone(tmp_path, "pc-a", remote), clone(tmp_path, "pc-b", remote), remote


def test_a_published_record_reaches_the_other_pc_and_is_pushed_only_when_it_changes(pcs):
    a, b, remote = pcs
    mine = record(FAR)
    published, why = fs.read_published(a)
    assert (published, why) == ({}, "")
    assert fs.publish(a, mine, published, NOW) == f"published {FAR} as administrator"
    first = run_git(remote, "rev-parse", f"{fs.FLEET}{FAR}").stdout.strip()

    seen, _ = fs.read_published(b)
    assert fs.records_of(seen) == {FAR: mine}

    again, _ = fs.read_published(a)
    assert fs.publish(a, mine, again, NOW) == ""
    assert run_git(remote, "rev-parse", f"{fs.FLEET}{FAR}").stdout.strip() == first

    moved = record(FAR, address="100.64.0.9")
    assert fs.publish(a, moved, again, NOW).startswith("published")
    seen, _ = fs.read_published(b)
    assert fs.records_of(seen)[FAR].address == "100.64.0.9"


def test_a_fetch_that_fails_still_reads_what_the_last_one_left(tmp_path: Path):
    git = clone(tmp_path, "pc", tmp_path / "gone.git")
    published, why = fs.read_published(git)
    assert published == {} and why


# --- this PC ---------------------------------------------------------------------------------


def test_ensure_key_reads_an_existing_key_and_drops_its_comment(tmp_path: Path):
    (tmp_path / "id_ed25519.pub").write_text(f"{KEY} me@here\n", encoding="utf-8")
    assert fs.ensure_key(tmp_path, "c", no_spawn) == (KEY, "")


def test_ensure_key_makes_a_key_when_there_is_none(tmp_path: Path):
    ssh = tmp_path / ".ssh"
    asked: list[Sequence[str]] = []

    def keygen(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        asked.append(argv)
        target = Path(argv[argv.index("-f") + 1])
        target.write_text("private", encoding="utf-8")
        Path(f"{target}.pub").write_text(f"{KEY} c\n", encoding="utf-8")
        return 0, ""

    key, note = fs.ensure_key(ssh, "alexa@here", keygen)
    assert key == KEY and "generated" in note
    assert asked[0][:7] == ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C"]


def test_ensure_key_derives_a_missing_public_half(tmp_path: Path):
    (tmp_path / "id_ed25519").write_text("private", encoding="utf-8")
    key, note = fs.ensure_key(tmp_path, "c", lambda argv, t: (0, f"{KEY}\n"))
    assert key == KEY and (tmp_path / "id_ed25519.pub").is_file() and note


def test_ensure_key_says_why_it_has_none(tmp_path: Path):
    key, note = fs.ensure_key(tmp_path, "c", lambda argv, t: (1, "no ssh-keygen"))
    assert key == "" and "no ssh-keygen" in note


def test_tailnet_address_takes_the_first_ip_or_nothing():
    assert fs.tailnet_address(lambda a, t: (0, "100.64.0.7\n")) == "100.64.0.7"
    assert fs.tailnet_address(lambda a, t: (0, "garbage\n100.64.0.8\n")) == "100.64.0.8"
    assert fs.tailnet_address(lambda a, t: (1, "not installed")) == ""


def test_is_admin_reads_the_administrators_sid_even_deny_only(monkeypatch):
    monkeypatch.setattr(fs, "WINDOWS", True)
    line = f"BUILTIN\\Administrators Alias {fs.ADMINS_SID} Group used for deny only"
    assert fs.is_admin(lambda a, t: (0, line))
    assert not fs.is_admin(lambda a, t: (0, "BUILTIN\\Users Alias S-1-5-32-545"))
    monkeypatch.setattr(fs, "WINDOWS", False)
    assert not fs.is_admin(no_spawn)


@pytest.mark.parametrize(
    ("admin", "sshd", "expected"),
    [
        (True, None, "pd/ssh/administrators_authorized_keys"),
        (
            True,
            "Match Group administrators\n  AuthorizedKeysFile __PROGRAMDATA__/ssh/"
            "administrators_authorized_keys\n",
            "pd/ssh/administrators_authorized_keys",
        ),
        (
            True,
            "#Match Group administrators\n#  AuthorizedKeysFile x/administrators_authorized_keys\n",
            "home/.ssh/authorized_keys",
        ),
        (False, None, "home/.ssh/authorized_keys"),
    ],
)
def test_authorized_keys_path_follows_what_sshd_reads(admin, sshd, expected):
    path = fs.authorized_keys_path(admin, sshd, Path("home"), Path("pd"))
    assert path.as_posix() == expected


# --- ~/.ssh/config ---------------------------------------------------------------------------


def test_the_block_names_every_peer_but_this_pc_in_order():
    block = fs.render_block([record(THIRD, "alexa", address=""), record(FAR), record(HERE)], HERE)
    assert block[0] == fs.BEGIN and block[-1] == fs.END
    assert [line for line in block if line.startswith("Host ")] == [f"Host {FAR}", f"Host {THIRD}"]
    assert "    HostName 100.64.0.2" in block and "    User administrator" in block
    third = block[block.index(f"Host {THIRD}") :]
    assert not third[1].strip().startswith("HostName")


def test_merge_appends_the_block_and_leaves_the_rest_alone():
    mine = "Host github.com\n    User git\n"
    merged = fs.merge_block(mine, fs.render_block([record()], HERE))
    assert merged is not None and merged.startswith(mine)
    assert f"\n\n{fs.BEGIN}\n" in merged and merged.endswith(f"{fs.END}\n")


def test_merge_replaces_only_between_the_markers():
    text = (
        f"Host a\n    User x\n{fs.BEGIN}\nHost old\n    User gone\n{fs.END}\nHost b\n    User y\n"
    )
    merged = fs.merge_block(text, fs.render_block([record()], HERE))
    assert merged is not None
    assert "Host old" not in merged and f"Host {FAR}" in merged
    assert merged.startswith("Host a\n    User x\n") and merged.endswith("Host b\n    User y\n")
    assert fs.merge_block(merged, fs.render_block([record()], HERE)) == merged


def test_merge_keeps_the_file_s_line_endings():
    merged = fs.merge_block("Host a\r\n    User x\r\n", fs.render_block([record()], HERE))
    assert merged is not None and "\n" not in merged.replace("\r\n", "")


def test_merge_of_an_empty_file_is_the_block():
    assert fs.merge_block("", [fs.BEGIN, fs.END]) == f"{fs.BEGIN}\n{fs.END}\n"


def test_merge_refuses_a_marker_without_its_pair():
    assert fs.merge_block(f"Host a\n{fs.BEGIN}\nHost b\n", [fs.BEGIN, fs.END]) is None
    assert fs.merge_block(f"{fs.END}\n{fs.BEGIN}\n", [fs.BEGIN, fs.END]) is None


def test_configured_hosts_are_the_block_s():
    text = "Host mine\n" + "\n".join(fs.render_block([record(), record(THIRD)], HERE)) + "\n"
    assert fs.configured_hosts(text) == {FAR, THIRD}
    assert fs.configured_hosts("Host mine\n") == frozenset()


def test_a_hand_entry_naming_another_login_is_reported():
    """ssh reads the first value, so a stale entry above the block wins over it."""
    text = (
        f"Host {FAR} far-alias\n    User alexa\n"
        f"Host {THIRD}\n    User administrator\n"
        "Host *\n    User nobody\n" + "\n".join(fs.render_block([record(FAR), record(THIRD)], HERE))
    )
    said = fs.shadowed(text, [record(FAR), record(THIRD)])
    assert len(said) == 1 and FAR in said[0] and "User alexa" in said[0]


def test_write_config_creates_the_file_and_leaves_none_with_no_peers(tmp_path: Path):
    assert fs.write_config([], HERE, tmp_path) == []
    assert not (tmp_path / ".ssh" / "config").exists()
    said = fs.write_config([record()], HERE, tmp_path)
    assert said and f"Host {FAR}" in (tmp_path / ".ssh" / "config").read_text(encoding="utf-8")
    assert fs.write_config([record()], HERE, tmp_path) == []


# --- authorized keys -------------------------------------------------------------------------


def test_key_material_drops_options_and_comment():
    assert fs.key_material(f'from="100.64.0.0/10" {KEY} me@far') == KEY
    assert fs.key_material(f"{KEY}") == KEY
    assert fs.key_material("# a comment") == ""


def test_with_keys_appends_what_is_missing_and_removes_nothing():
    text = f"ssh-rsa AAAAB3old someone\n{OTHER_KEY} third"
    merged, added = fs.with_keys(text, [record(FAR), record(THIRD, key=OTHER_KEY)])
    assert added == [FAR]
    assert merged.startswith(f"ssh-rsa AAAAB3old someone\n{OTHER_KEY} third\n")
    assert merged.endswith(f"{KEY} devkit-fleet:administrator@{FAR}\n")
    assert fs.with_keys(merged, [record(FAR)]) == (merged, [])


def test_authorize_writes_a_user_s_own_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fs, "WINDOWS", False)
    joined = fs.authorize_peers([record()], HERE, tmp_path, machine(tmp_path, ask=never_asked))
    assert joined.lines and "authorized" in joined.lines[0]
    keys = (tmp_path / ".ssh" / "authorized_keys").read_text(encoding="utf-8")
    assert KEY in keys


def never_asked(verb: str, **_: object) -> str:
    pytest.fail(f"asked the worker for {verb}")


def machine(
    tmp_path: Path, ask: fs.Ask = never_asked, seen: Callable[[], fs.Seen] = fs.Seen
) -> fs.Machine:
    """This PC with its home and ProgramData in `tmp_path` and nothing spawned."""
    return fs.Machine(no_spawn, tmp_path, tmp_path, ask=ask, seen=seen)


def test_an_empty_machine_field_is_the_real_one(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fs, "program_data", lambda: tmp_path / "pd")
    assert fs.Machine().home_dir() == Path.home()
    assert fs.Machine().programdata_dir() == tmp_path / "pd"
    assert fs.Machine(home=tmp_path).home_dir() == tmp_path
    assert fs.Machine(programdata=tmp_path).programdata_dir() == tmp_path


class Worker:
    """What `elevated_request.request` was asked, answering as an installed worker does."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, Path | None]] = []

    def __call__(self, verb: str, checkout: Path | None = None) -> str:
        self.asked.append((verb, checkout))
        return f"{verb} requested"


def admin_here(monkeypatch) -> None:
    monkeypatch.setattr(fs, "is_admin", lambda run: True)


def test_the_administrators_file_is_asked_of_the_worker_and_never_written(
    tmp_path: Path, monkeypatch
):
    """No person's step: the worker adds the key, and this run writes nothing there."""
    admin_here(monkeypatch)
    worker = Worker()
    devkit = tmp_path / "devkit"
    joined = fs.authorize_peers(
        [record()], HERE, devkit, machine(tmp_path, ask=worker, seen=fs.Seen)
    )
    assert worker.asked == [(fs.elevated_request.FLEET_AUTHORIZE, devkit)]
    assert FAR in joined.lines[0] and "requested" in joined.lines[0]
    assert "person" not in joined.lines[0]
    assert not (tmp_path / "ssh" / fs.ADMIN_KEYS).exists()


def test_an_unreadable_administrators_file_is_judged_by_the_worker_s_receipt(
    tmp_path: Path, monkeypatch
):
    admin_here(monkeypatch)
    monkeypatch.setattr(fs, "_read", lambda path: None if path.name == fs.ADMIN_KEYS else "")
    joined = fs.authorize_peers(
        [record()],
        HERE,
        tmp_path,
        machine(tmp_path, ask=never_asked, seen=lambda: fs.Seen(frozenset({KEY}))),
    )
    assert joined.lines == []


def test_a_key_already_in_a_readable_administrators_file_asks_nothing(tmp_path: Path, monkeypatch):
    admin_here(monkeypatch)
    (tmp_path / "ssh").mkdir()
    (tmp_path / "ssh" / fs.ADMIN_KEYS).write_text(f"{KEY} x\n", encoding="utf-8")
    joined = fs.authorize_peers(
        [record()], HERE, tmp_path, machine(tmp_path, ask=never_asked, seen=fs.Seen)
    )
    assert joined.lines == []


def test_a_record_the_worker_refused_is_asked_again_only_once_it_changes():
    far = record()
    refused = fs.Seen(refused={FAR: (far.key, far.address)})
    assert fs.owed_to_worker([far], None, refused) == []
    moved = record(address="100.64.0.9")
    assert fs.owed_to_worker([moved], None, refused) == [FAR]
    assert fs.owed_to_worker([far], None, fs.Seen()) == [FAR]


def test_the_worker_s_receipt_is_read_as_it_writes_it():
    text = json.dumps(
        {
            "keys": [KEY, 3],
            "refused": {FAR: {"key": KEY, "address": "100.64.0.2", "why": "x"}, "bad": 1},
        }
    )
    seen = fs.parse_seen(text)
    assert seen.keys == {KEY}
    assert seen.refused == {FAR: (KEY, "100.64.0.2")}
    assert fs.parse_seen("not json") == fs.Seen()
    assert fs.parse_seen("[]") == fs.Seen()


def test_spawn_gives_stdout_on_success_and_the_reason_on_failure():
    assert fs.spawn([sys.executable, "-c", "print('hi')"]) == (0, "hi\n")
    code, said = fs.spawn([sys.executable, "-c", "import sys; sys.exit('no')"])
    assert code == 1 and said.strip() == "no"
    code, said = fs.spawn(["devkit-no-such-program-anywhere"])
    assert code == 124 and said.startswith(("FileNotFoundError", "OSError"))


def test_devkit_under_home_is_a_relative_posix_path_or_nothing(tmp_path: Path):
    assert fs.devkit_under_home(tmp_path / "vs-code" / "devkit", tmp_path) == "vs-code/devkit"
    assert fs.devkit_under_home(tmp_path, tmp_path / "elsewhere") == ""


def test_program_data_follows_the_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    assert fs.program_data() == tmp_path
    monkeypatch.delenv("ProgramData")
    assert fs.program_data() == Path(r"C:\ProgramData")


def test_local_record_is_this_pc_or_says_why_not(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fs.getpass, "getuser", lambda: "alexa")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519.pub").write_text(f"{KEY} me\n", encoding="utf-8")

    def run(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        return 0, "100.64.0.1\n"

    mine, notes = fs.local_record(tmp_path / "devkit", run, tmp_path, HERE)
    assert mine == fs.Record(HERE, "alexa", KEY, "100.64.0.1", "devkit") and notes == []
    monkeypatch.setattr(fs.getpass, "getuser", lambda: "bad user")
    mine, notes = fs.local_record(tmp_path / "devkit", run, tmp_path, HERE)
    assert mine is None and notes[-1].startswith("not published: ")


def test_outside_block_and_hand_users_read_only_the_hand_written_entries():
    block = fs.render_block([record(FAR)], HERE)
    text = "\n".join(["Host desktop-third *.lan", "    User me", *block, "Host after"])
    assert fs.outside_block(text) == ["Host desktop-third *.lan", "    User me", "Host after"]
    assert fs.hand_users(text) == {THIRD: "me"}
    assert fs.outside_block("Host x") == ["Host x"]


def test_present_is_every_key_a_file_holds():
    assert fs.present(f"# c\n{KEY} a\nfrom=x {OTHER_KEY} b\n") == {KEY, OTHER_KEY}


def test_read_seen_is_an_empty_receipt_when_there_is_none(tmp_path: Path):
    assert fs.read_seen(str(tmp_path / "missing.json")) == fs.Seen()
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"keys": [KEY]}), encoding="utf-8")
    assert fs.read_seen(str(path)) == fs.Seen(frozenset({KEY}))


def test_scheduled_joins_without_fetching(tmp_path: Path, monkeypatch):
    calls: list[tuple[object, ...]] = []

    def join(*args: object, **kwargs: object) -> fs.Joined:
        calls.append((*args, kwargs))
        return fs.Joined()

    monkeypatch.setattr(fs, "join", join)
    git = object()
    fs.scheduled(tmp_path, git)  # type: ignore[arg-type]  # a stand-in git `join` never reads
    assert calls == [(tmp_path, git, {"fetch": False})]


def test_join_publishes_configures_and_authorizes(pcs, tmp_path: Path, monkeypatch):
    a, b, _ = pcs
    monkeypatch.setattr(fs, "WINDOWS", False)
    monkeypatch.setattr(fs.getpass, "getuser", lambda: "alexa")
    fs.publish(b, record(FAR, key=OTHER_KEY), {}, NOW)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519.pub").write_text(f"{KEY} me\n", encoding="utf-8")

    def run(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        return (0, "100.64.0.1\n") if argv[0] == "tailscale" else (1, "")

    joined = fs.join(tmp_path / "devkit", a, fs.Machine(run, home, tmp_path, HERE))
    assert any(line.startswith("published") for line in joined.lines)
    assert f"Host {FAR}" in (home / ".ssh" / "config").read_text(encoding="utf-8")
    assert OTHER_KEY in (home / ".ssh" / "authorized_keys").read_text(encoding="utf-8")
    seen, _ = fs.read_published(b)
    assert fs.records_of(seen)[HERE] == fs.Record(HERE, "alexa", KEY, "100.64.0.1")


# --- why a peer did not answer ---------------------------------------------------------------


DENIED = "administrator@100.64.0.2: Permission denied (publickey,keyboard-interactive)."


@pytest.mark.parametrize(
    ("said", "facts", "starts"),
    [
        (DENIED, fs.Known(), "wrong user, probably"),
        (DENIED, fs.Known({FAR: record()}), "wrong user: "),
        (DENIED, fs.Known({FAR: record()}, frozenset({FAR})), "key not authorized"),
        ("ssh: connect to host x port 22: Connection refused", fs.Known(), "sshd not listening"),
        ("ssh: connect to host x port 22: Connection timed out", fs.Known(), "no answer"),
        ("ssh: Could not resolve hostname x: unknown", fs.Known(), "name not resolved"),
        ("Host key verification failed.", fs.Known(), "host key changed"),
        ("banner\nsomething new\n", fs.Known(), "something new"),
        ("", fs.Known(), ""),
    ],
)
def test_diagnose_says_why_in_words(said: str, facts: fs.Known, starts: str):
    assert fs.diagnose(said, FAR, facts).startswith(starts)


def test_a_wrong_user_names_the_login_the_peer_published():
    assert "administrator" in fs.diagnose(DENIED, FAR, fs.Known({FAR: record()}))


def test_known_reads_the_refs_and_the_block(pcs, tmp_path: Path):
    a, b, _ = pcs
    fs.publish(b, record(FAR), {}, NOW)
    fs.read_published(a)
    (tmp_path / ".ssh").mkdir()
    block = "\n".join(fs.render_block([record(FAR)], HERE)) + "\n"
    (tmp_path / ".ssh" / "config").write_text(block, encoding="utf-8")
    facts = fs.known(a, tmp_path)
    assert facts.records == {FAR: record(FAR)} and facts.configured == {FAR}
