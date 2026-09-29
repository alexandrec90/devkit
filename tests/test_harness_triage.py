"""The read half of the harness-events ledger: open vs resolved, and the grouping.

Every test here writes its own ledger under `tmp_path`. The live one is append-only and
machine-wide, so a test that touched it would both depend on and pollute a file the rest
of the workspace is writing to concurrently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from support import load_script

triage = load_script("scripts/harness_triage.py")
harness_events = triage.harness_events

STAMP = "2026-08-24T12:00:00+00:00"
# Two stamps for the tests that need one defect recorded *twice*: an id is content-
# addressed, so two byte-identical lines are one record and would not exercise grouping.
_STAMPS = ("2026-08-24T12:00:01+00:00", "2026-08-24T12:00:02+00:00")


def _line(event: str, project: str = "carameli", stamp: str = STAMP, **fields: str) -> str:
    """One ledger line. `stamp` varies where a test needs two *distinct* records of the
    same defect -- byte-identical lines are one record by construction, since the id is
    content-addressed."""
    pairs = "".join(f"\t{k}={v}" for k, v in fields.items())
    return f"{stamp}\tevent={event}\tproject={project}{pairs}"


def _ledger(root, *lines: str):
    """The legacy, unsharded ledger -- every row written before a machine had a name."""
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "harness-events.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _shard(root, host: str, *lines: str):
    """One machine's shard. Two of these beside each other is a pooled `logs/`."""
    (root / "logs").mkdir(parents=True, exist_ok=True)
    path = root / "logs" / f"harness-events-{host}.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


# --- parsing ------------------------------------------------------------------


def test_a_line_parses_into_its_fields():
    item = triage.parse_line(_line("agent-report", message="the guard blocked a grep"))
    assert item is not None
    assert item.event == "agent-report"
    assert item.stamp == STAMP
    assert item.detail == "the guard blocked a grep"


def test_a_blank_or_shapeless_line_is_none_rather_than_an_error():
    """The ledger is written by best-effort appenders in several processes, so a torn
    line is expected. One must not stop the read side."""
    assert triage.parse_line("") is None
    assert triage.parse_line("   \n") is None
    assert triage.parse_line("no tabs at all") is None
    assert triage.parse_line(f"{STAMP}\tproject=x") is None  # no event=
    assert triage.parse_line(f"{STAMP}\tevent=") is None


def test_detail_falls_through_to_the_first_field_that_has_substance():
    """`clean` writes "-" for an empty value, so a present-but-empty field is not a
    detail. Reading it as one put "-" in the group heading and hid the real text."""
    item = triage.parse_line(_line("guard-spawn-failed", message="-", detail="port slots"))
    assert item is not None
    assert item.detail == "port slots"


def test_an_event_with_no_detail_field_at_all_still_renders():
    item = triage.parse_line(_line("agent-report"))
    assert item is not None
    assert item.detail == "-"


# --- ids ----------------------------------------------------------------------


def test_the_id_is_content_addressed_not_positional():
    """The reversion check for using a line number.

    The ledger only ever grows, so a positional id is correct until the next append and
    a resolution recorded against one silently comes to name a different event.
    """
    first = _line("agent-report", message="a")
    second = _line("agent-report", message="b")
    assert triage.item_id(first) != triage.item_id(second)
    assert triage.item_id(first) == triage.item_id(first)
    # Same line, different position in the file: same id.
    early = triage.open_items(triage.read_items("\n".join([first, second])))
    late = triage.open_items(triage.read_items("\n".join([second, first])))
    assert {i.id for i in early} == {i.id for i in late}


def test_surrounding_whitespace_does_not_change_an_id():
    assert triage.item_id(_line("agent-report", message="a")) == triage.item_id(
        "  " + _line("agent-report", message="a") + "  \n"
    )


# --- open vs resolved ---------------------------------------------------------


def test_only_the_triage_events_are_open():
    """Routine guard redirects and capped-Bash blocks are forensics, not a backlog."""
    items = triage.read_items(
        "\n".join(
            [
                _line("guard-block", detail="x"),
                _line("capped-bash-block", command="ls"),
                _line("lint-fix-block", detail="F401"),
                _line("guard-route", detail="x"),
                _line("agent-report", message="real"),
            ]
        )
    )
    assert [i.event for i in triage.open_items(items)] == ["agent-report"]


def test_a_resolution_retires_exactly_the_item_it_names():
    report = _line("agent-report", message="one")
    other = _line("guard-spawn-failed", detail="two")
    resolved = _line("triage-resolved", ref=triage.item_id(report), note="fixed in 202")
    items = triage.read_items("\n".join([report, other, resolved]))
    assert [i.detail for i in triage.open_items(items)] == ["two"]


def test_resolved_refs_names_every_ref_and_ignores_the_refless():
    items = triage.read_items(
        "\n".join(
            [
                _line("triage-resolved", ref="aaaabbbb", note="one"),
                _line("triage-resolved", ref="ccccdddd", note="two"),
                _line("triage-resolved", note="no ref at all"),
                _line("agent-report", ref="notaresolution"),
            ]
        )
    )
    assert triage.resolved_refs(items) == {"aaaabbbb", "ccccdddd"}


def test_an_item_built_by_hand_behaves_like_a_parsed_one():
    """`Item` is the shape the rest of the tool passes around, so it has to be usable
    without a ledger line behind it -- a caller constructing one directly gets the same
    id, project and detail rules."""
    raw = _line("agent-report", "devkit--a-box-0824", message="hand built")
    made = triage.Item(
        stamp=STAMP,
        event="agent-report",
        fields={"project": "devkit--a-box-0824", "message": "hand built"},
        raw=raw,
    )
    assert made.id == triage.item_id(raw)
    assert made.project == "devkit"
    assert made.detail == "hand built"
    assert made == triage.parse_line(raw)


def test_a_resolution_naming_nothing_retires_nothing():
    report = _line("agent-report", message="one")
    items = triage.read_items("\n".join([report, _line("triage-resolved", note="oops")]))
    assert len(triage.open_items(items)) == 1


def test_open_items_are_newest_first():
    items = triage.read_items(
        "\n".join([_line("agent-report", message="old"), _line("agent-report", message="new")])
    )
    assert [i.detail for i in triage.open_items(items)] == ["new", "old"]


# --- the project field --------------------------------------------------------


def test_a_box_directory_reads_as_the_project_it_was_cut_from():
    """The ledger is append-only, so months of rows keep naming a box. Normalising on
    read is what lets one recurring defect group instead of splitting per box."""
    item = triage.parse_line(_line("agent-report", "devkit--guard-quoted-redirect-0823", m="x"))
    assert item is not None
    assert item.project == "devkit"


def test_a_plain_project_name_is_left_alone():
    item = triage.parse_line(_line("agent-report", "carameli", message="x"))
    assert item is not None
    assert item.project == "carameli"


# --- grouping -----------------------------------------------------------------


def test_one_defect_recorded_many_times_is_one_group():
    """24 of this machine's first 39 open items were a single spawn race. Listing them
    flat reads as 24 problems, which is how a backlog stops being read."""
    detail = "RegistryError: all 16 port slots are in use (4 pinned checkouts, 12 live boxes)"
    same = [_line("guard-spawn-failed", stamp=s, detail=detail) for s in _STAMPS]
    items = triage.read_items("\n".join(same))
    grouped = triage.groups(triage.open_items(items))
    assert len(grouped) == 1
    assert len(grouped[0][1]) == 2


def test_groups_split_on_project_and_event():
    items = triage.read_items(
        "\n".join(
            [
                _line("agent-report", "carameli", message="same text"),
                _line("agent-report", "devkit", message="same text"),
                _line("guard-spawn-failed", "devkit", detail="same text"),
            ]
        )
    )
    assert len(triage.groups(triage.open_items(items))) == 3


def test_the_signature_ignores_the_tail_of_a_long_detail():
    """Two recurrences of one defect differ in a path or a timestamp near the end. The
    ledger truncates at 300 characters; a signature that used all of it grouped nothing."""
    head = "the guard blocked a grep whose quoted pattern held a redirect operator, and "
    a = _line("agent-report", message=head + "box A")
    b = _line("agent-report", message=head + "box B")
    assert len(triage.groups(triage.open_items(triage.read_items(a + "\n" + b)))) == 1


def test_expand_like_reaches_every_recurrence_and_nothing_else():
    a = _line("agent-report", stamp=_STAMPS[0], message="port slots exhausted")
    b = _line("agent-report", stamp=_STAMPS[1], message="port slots exhausted")
    c = _line("agent-report", message="a different problem entirely")
    items = triage.read_items("\n".join([a, b, c]))
    reached = triage.expand_like([triage.item_id(a)], items)
    assert set(reached) == {triage.item_id(a), triage.item_id(b)}


# --- resolving ----------------------------------------------------------------


def test_resolving_without_a_note_is_refused():
    """The property that makes this debt rather than configuration.

    Ageing out was the silent laundering the window had; a resolution that needs no
    reason would be the same hole with a command in front of it.
    """
    for empty in ("", "   ", "\n"):
        try:
            triage.resolve(["abcd1234"], empty)
        except ValueError:
            continue
        raise AssertionError(f"a note of {empty!r} was accepted")


def test_the_ledger_is_the_machine_wide_one_not_the_cwd(tmp_path, monkeypatch):
    """`ledger_file` resolves `$DEVKIT_DIR` first, for the reason every writer does: run
    from a box, the local `logs/` is a directory nothing has ever appended to, so a tool
    reading it would report an empty backlog rather than the machine's."""
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    assert triage.ledger_file() == tmp_path / "logs" / "harness-events-laptop.log"
    monkeypatch.delenv("DEVKIT_DIR", raising=False)
    assert triage.ledger_file().name == "harness-events-laptop.log"


def test_an_absent_ledger_reads_as_empty_rather_than_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    assert triage.load() == []


def test_resolving_appends_an_event_the_next_read_honours(tmp_path):
    report = _line("agent-report", message="one")
    _ledger(tmp_path, report)
    triage.resolve([triage.item_id(report)], "fixed in PR 202", pr="202", root=tmp_path)
    items = triage.load(tmp_path)
    assert triage.open_items(items) == []
    written = [i for i in items if i.event == triage.RESOLVED_EVENT]
    assert written[0].fields["note"] == "fixed in PR 202"
    assert written[0].fields["pr"] == "202"


def test_the_ledger_is_only_ever_appended_to(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    report = _line("agent-report", message="one")
    _shard(tmp_path, "laptop", report)
    triage.resolve([triage.item_id(report)], "done", root=tmp_path)
    text = (tmp_path / "logs" / "harness-events-laptop.log").read_text(encoding="utf-8")
    assert text.startswith(report)
    assert triage.RESOLVED_EVENT in text


# --- pooling two machines' shards ---------------------------------------------
#
# One ledger per machine was correct while there was one machine. Every test below is a
# regression test for the same defect in the two-machine case: a backlog that is only
# ever half-read, and -- because a resolution is itself an event -- a group retired on
# one machine that stays open on the other forever.


def test_ledger_files_lists_every_shard_load_will_read(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    _shard(tmp_path, "desktop", _line("agent-report", message="x"))
    assert [p.name for p in triage.ledger_files()] == [
        "harness-events-desktop.log",
        "harness-events-laptop.log",
    ]


def test_ledger_files_falls_back_to_the_repo_when_there_is_no_seam(tmp_path, monkeypatch):
    """`ledger_file`'s fallback, kept in step: a checkout with no `$DEVKIT_DIR` reads its
    own `logs/` rather than reporting that it has no backlog."""
    monkeypatch.delenv("DEVKIT_DIR", raising=False)
    monkeypatch.setattr(triage.harness_events, "REPO_ROOT", tmp_path / "someproject")
    assert triage.ledger_files() == [triage.REPO_ROOT / harness_events.LEDGER]


def test_load_unions_every_machines_shard(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    _shard(tmp_path, "laptop", _line("agent-report", message="from the laptop"))
    _shard(tmp_path, "desktop", _line("agent-report", message="from the desktop"))
    assert {i.detail for i in triage.load()} == {"from the laptop", "from the desktop"}


def test_the_legacy_unsharded_file_is_still_part_of_the_backlog(tmp_path, monkeypatch):
    """Most of the ledger's history predates the split, and it is append-only."""
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    _ledger(tmp_path, _line("agent-report", message="written before the split"))
    assert [i.detail for i in triage.load()] == ["written before the split"]


def test_items_are_ordered_by_stamp_not_by_which_shard_they_came_from(tmp_path, monkeypatch):
    """Concatenating shards gives file order, which is chronological within one and
    meaningless across two -- and `open_items` reverses it to mean 'newest first'."""
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    _shard(tmp_path, "desktop", _line("agent-report", stamp=_STAMPS[1], message="second"))
    _shard(tmp_path, "laptop", _line("agent-report", stamp=_STAMPS[0], message="first"))
    assert [i.detail for i in triage.load()] == ["first", "second"]
    assert [i.detail for i in triage.open_items(triage.load())] == ["second", "first"]


def test_one_defect_hit_on_two_machines_is_one_group(tmp_path, monkeypatch):
    """The host is deliberately not in the signature: this is one defect, not two."""
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    _shard(tmp_path, "laptop", _line("agent-report", stamp=_STAMPS[0], host="laptop", message="x"))
    _shard(
        tmp_path, "desktop", _line("agent-report", stamp=_STAMPS[1], host="desktop", message="x")
    )
    assert len(triage.groups(triage.open_items(triage.load()))) == 1


def test_resolve_like_retires_the_other_machines_copy_too(tmp_path, monkeypatch):
    """The property the whole change exists for. Ids are content-addressed per line, so
    the two machines' rows for one defect have *different* ids and nothing else would
    connect them -- a fix triaged on one machine used to leave the other's row open
    forever, and the next pass there re-verified work that had already shipped."""
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    mine = _line("agent-report", stamp=_STAMPS[0], host="laptop", message="the same block")
    theirs = _line("agent-report", stamp=_STAMPS[1], host="desktop", message="the same block")
    _shard(tmp_path, "laptop", mine)
    _shard(tmp_path, "desktop", theirs)
    assert triage.item_id(mine) != triage.item_id(theirs)

    ids = triage.expand_like([triage.item_id(mine)], triage.load())
    triage.resolve(sorted(set(ids)), "fixed in #300", pr="300", root=tmp_path)
    assert triage.open_items(triage.load()) == []


def test_a_resolution_is_written_only_to_this_machines_shard(tmp_path, monkeypatch):
    """No machine ever writes to another's file -- that is what keeps a pooled directory
    conflict-free for any transport that syncs it."""
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    theirs = _line("agent-report", host="desktop", message="x")
    _shard(tmp_path, "desktop", theirs)
    before = (tmp_path / "logs" / "harness-events-desktop.log").read_text(encoding="utf-8")
    triage.resolve([triage.item_id(theirs)], "fixed here", root=tmp_path)
    after = (tmp_path / "logs" / "harness-events-desktop.log").read_text(encoding="utf-8")
    assert after == before
    mine = (tmp_path / "logs" / "harness-events-laptop.log").read_text(encoding="utf-8")
    assert triage.RESOLVED_EVENT in mine


def test_an_unreadable_shard_does_not_take_the_backlog_down(tmp_path, monkeypatch):
    """A half-synced file is the ordinary state of a directory two machines write into."""
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    _shard(tmp_path, "laptop", _line("agent-report", message="readable"))
    (tmp_path / "logs" / "harness-events-desktop.log").mkdir()
    assert [i.detail for i in triage.load()] == ["readable"]


def test_the_rendering_names_the_machines_a_group_was_seen_on(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    _shard(tmp_path, "laptop", _line("agent-report", stamp=_STAMPS[0], host="laptop", message="x"))
    _shard(
        tmp_path, "desktop", _line("agent-report", stamp=_STAMPS[1], host="desktop", message="x")
    )
    assert "  host   desktop laptop" in triage.render(triage.open_items(triage.load()))


def test_a_pre_field_row_reports_no_host_rather_than_guessing_one(tmp_path, monkeypatch):
    """The ledger is append-only, so rows written before `host=` existed have none and
    never will. `unknown-host` is honest; naming this machine would be a guess."""
    monkeypatch.setenv("DEVKIT_HOST", "laptop")
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    _ledger(tmp_path, _line("agent-report", message="written before the split"))
    rendered = triage.render(triage.open_items(triage.load()))
    assert triage.load()[0].host == harness_events.UNKNOWN_HOST
    assert "  host " not in rendered


# --- rendering and the artifact -----------------------------------------------


def test_an_empty_backlog_renders_as_nothing_open():
    assert "nothing open" in triage.render([])


def test_the_rendering_names_an_id_and_the_command_that_retires_it():
    items = triage.open_items(triage.read_items(_line("agent-report", message="the detail")))
    text = triage.render(items)
    assert items[0].id in text
    assert "the detail" in text
    assert "--resolve-like" in text


def test_a_defect_back_after_a_resolution_is_marked_as_a_fix_that_did_not_hold():
    """A missing `.venv` kept coming back: each session provisioned its own tree by hand
    and the group was retired, so every recurrence read as new. A group whose signature
    was resolved before now says so, with what that fix was, so the next one goes to the
    cause instead of repeating the instance."""
    first = _line("session-friction", stamp=_STAMPS[0], detail="No module named pytest")
    fixed = _line(
        "triage-resolved",
        stamp="2026-08-24T13:00:00+00:00",
        ref=triage.item_id(first),
        note="ran uv sync in the tree",
    )
    again = _line(
        "session-friction", stamp="2026-08-25T09:00:00+00:00", detail="No module named pytest"
    )
    history = triage.read_items("\n".join((first, fixed, again)))
    text = triage.render(triage.open_items(history), history)
    assert "  RECURRED after 1 resolution -- last: ran uv sync in the tree" in text
    assert "fix the cause" in text
    assert "RECURRED" not in triage.render(triage.open_items(history)), "no history, no claim"
    fresh = triage.read_items(_line("session-friction", detail="something new"))
    assert "RECURRED" not in triage.render(triage.open_items(fresh), fresh)


def test_a_reopened_row_says_why_and_which_resolution_it_undid():
    """3355a63a read `RECURRED ... last: <an older, unrelated note>`, while what had
    happened was that its own resolution named `agent/fix-harness-ledger-0927`, the fix
    merged as #439 from the name it was carried to, and `fix_verify` reopened it saying
    to resolve again with the PR's number. The session learned that from the raw ledger."""
    first = _line("scheduled-job-failed", stamp=_STAMPS[0], message="job failed")
    ref = triage.item_id(first)
    held = _line(
        "triage-resolved", stamp=_STAMPS[1], ref=ref, pr="agent/x", note="pushed past the gate"
    )
    undone = _line(
        "triage-reopened", stamp="2026-08-24T12:00:03+00:00", ref=ref, note="no PR from agent/x"
    )
    history = triage.read_items("\n".join((first, held, undone)))
    assert triage.reopened(history) == {
        ref: "no PR from agent/x; the resolution it undid (pr=agent/x): pushed past the gate"
    }
    text = triage.render(triage.open_items(history), history)
    assert f"  REOPENED {ref} -- no PR from agent/x; the resolution it undid" in text
    # Resolved again after the reopening: it stands, and nothing is reopened.
    again = _line("triage-resolved", stamp="2026-08-24T12:00:04+00:00", ref=ref, note="#439")
    assert triage.reopened(triage.read_items("\n".join((first, held, undone, again)))) == {}
    # A reopening with no resolution on record still says why.
    bare = triage.read_items("\n".join((first, undone)))
    assert triage.reopened(bare) == {
        ref: "no PR from agent/x; the resolution it undid: none on record"
    }


def test_verdict_lines_prefer_a_fix_in_flight_to_a_recurrence():
    assert triage.verdict_lines(None, None) == []
    assert triage.verdict_lines("", []) == []
    assert triage.verdict_lines("410", ["old"])[0].startswith("  PENDING on 410 --")
    assert triage.verdict_lines(None, ["a", "b"]) == [
        "  RECURRED after 2 resolutions -- last: b -- that fix did not hold; "
        "fix the cause so it cannot come back"
    ]


def test_past_fixes_counts_only_resolutions_still_standing():
    """A resolution the pass reopened (its branch never merged) was no fix at all."""
    first = _line("agent-report", stamp=_STAMPS[0], message="m")
    ref = triage.item_id(first)
    held = _line("triage-resolved", stamp=_STAMPS[1], ref=ref, note="fixed the hook")
    undone = _line("triage-reopened", stamp="2026-08-24T12:00:03+00:00", ref=ref, note="no merge")
    signature = triage.read_items(first)[0].signature
    assert triage.past_fixes(triage.read_items(first + "\n" + held)) == {
        signature: ["fixed the hook"]
    }
    assert triage.past_fixes(triage.read_items("\n".join((first, held, undone)))) == {}
    assert triage.past_fixes([]) == {}


def test_a_group_whose_fix_is_still_in_flight_is_pending_not_recurred():
    """d677ea57: #410 removes the isolation-guard detector, and until it merges the
    detector keeps filing rows -- which read as "RECURRED ... that fix did not hold", so
    each sweep re-proved a group whose fix was one open PR away. A resolution naming a
    PR that has not landed (`fix_verify.in_flight`) makes the group pending instead."""
    first = _line("session-friction", stamp=_STAMPS[0], detail="isolation-guard: x")
    ref = triage.item_id(first)
    held = _line("triage-resolved", stamp=_STAMPS[1], ref=ref, pr="410", note="#410 drops it")
    again = _line(
        "session-friction", stamp="2026-08-25T09:00:00+00:00", detail="isolation-guard: x"
    )
    other = _line("agent-report", stamp=_STAMPS[1], message="unrelated")
    history = triage.read_items("\n".join((first, held, again, other)))
    signature = triage.read_items(again)[0].signature
    assert triage.pending_groups(history, {ref: "410"}) == {signature: "410"}
    assert triage.pending_groups(history, {}) == {}, "landed: a real recurrence"
    text = triage.render(triage.open_items(history), history, {signature: "410"})
    assert "PENDING on 410" in text and "RECURRED" not in text
    # A later resolution that landed outranks an older one still in flight.
    later = _line("triage-resolved", stamp="2026-08-24T12:00:05+00:00", ref=ref, note="n")
    assert triage.pending_groups(triage.read_items("\n".join((first, held, later))), {}) == {}


def _in_flight_workspace(tmp_path, *, cache: bool):
    """A devkit checkout under a workspace whose one group recurred while its fix, a
    branch with no merge on record, is still in flight."""
    now = datetime.now(UTC)

    def stamp(hours: int) -> str:
        return (now - timedelta(hours=hours)).isoformat()

    first = _line("fix-pass-finding", project="devkit", stamp=stamp(5), detail="installer-failed")
    held = _line(
        "triage-resolved", stamp=stamp(4), ref=triage.item_id(first), pr="agent/x", note="n"
    )
    again = _line("fix-pass-finding", project="devkit", stamp=stamp(1), detail="installer-failed")
    devkit = _ledger(tmp_path / "devkit", first, held, again)
    if cache:
        (tmp_path / ".worktrees").mkdir()
        (tmp_path / ".worktrees" / "triage-verified.json").write_text("[]", encoding="utf-8")
    return devkit


def test_the_cli_shows_a_group_the_pass_holds_as_pending_not_open(tmp_path, monkeypatch, capsys):
    """The pass held 06bc9ef3 for its unmerged fix and sent nobody at it, while the CLI
    -- the count the triage skill ends a sweep on -- printed it as RECURRED and open."""
    monkeypatch.setenv("DEVKIT_DIR", str(_in_flight_workspace(tmp_path, cache=True)))
    monkeypatch.setattr(triage, "REPO_ROOT", tmp_path / "devkit")
    assert triage.main([]) == 0
    out = capsys.readouterr().out
    assert "PENDING on agent/x" in out and "RECURRED" not in out
    assert "0 open, 1 pending an unmerged fix" in out
    artifact = (tmp_path / "devkit" / triage.ARTIFACT).read_text(encoding="utf-8")
    assert "PENDING on agent/x" in artifact
    assert "# open: 0 open, 1 pending an unmerged fix" in artifact


def test_the_cli_claims_nothing_pending_without_the_pass_cache(tmp_path, monkeypatch, capsys):
    """No cache means no settled refs, so a fix that merged would read as pending and
    hide a true recurrence: absent, the CLI says what it said before."""
    monkeypatch.setenv("DEVKIT_DIR", str(_in_flight_workspace(tmp_path, cache=False)))
    monkeypatch.setattr(triage, "REPO_ROOT", tmp_path / "devkit")
    assert triage.main([]) == 0
    out = capsys.readouterr().out
    assert "RECURRED" in out and "1 open --" in out


def test_the_verified_cache_sits_in_the_workspace_box_root(tmp_path):
    shard = tmp_path / "devkit" / "logs" / "harness-events-host.log"
    assert triage.verified_cache(shard) == tmp_path / ".worktrees" / "triage-verified.json"


def test_in_flight_here_claims_nothing_without_a_cache(tmp_path):
    items = triage.read_items(
        _line("triage-resolved", stamp=datetime.now(UTC).isoformat(), ref="r", pr="12", note="n")
    )
    now = datetime.now(UTC)
    assert triage.in_flight_here(items, tmp_path / "absent.json", now) == {}
    cache = tmp_path / "triage-verified.json"
    cache.write_text("[]", encoding="utf-8")
    assert triage.in_flight_here(items, cache, now) == {"r": "12"}
    cache.write_text('["r"]', encoding="utf-8")
    assert triage.in_flight_here(items, cache, now) == {}, "settled: it merged"


def test_load_settled_reads_a_list_and_nothing_else(tmp_path):
    path = tmp_path / "cache.json"
    assert triage.load_settled(path) == set()
    path.write_text('["a", "b"]', encoding="utf-8")
    assert triage.load_settled(path) == {"a", "b"}
    path.write_text('{"a": 1}', encoding="utf-8")
    assert triage.load_settled(path) == set()
    path.write_text("not json", encoding="utf-8")
    assert triage.load_settled(path) == set()


def test_within_measures_from_the_stamp_and_rejects_a_bad_one():
    now = datetime(2026, 9, 27, tzinfo=UTC)
    day = timedelta(days=1)
    assert triage.within("2026-09-26T12:00:00+00:00", now, day)
    assert not triage.within("2026-09-20T00:00:00+00:00", now, day)
    assert not triage.within("not a stamp", now, day)


def test_the_count_line_splits_pending_from_open():
    a, b = triage.read_items(
        "\n".join((_line("agent-report", message="a"), _line("agent-report", message="b")))
    )
    assert triage.count_line([a, b], {}) == "2 open"
    assert triage.count_line([a, b], {a.signature: "9"}) == "1 open, 1 pending an unmerged fix"


def test_a_group_reports_its_count_and_every_id(tmp_path):
    a = _line("agent-report", stamp=_STAMPS[0], message="same")
    b = _line("agent-report", stamp=_STAMPS[1], message="same")
    text = triage.render(triage.open_items(triage.read_items(a + "\n" + b)))
    assert "(x2" in text
    assert triage.item_id(a) in text and triage.item_id(b) in text


def test_the_backlog_is_persisted_as_an_artifact(tmp_path):
    """Per the failure-artifact rule: an agent fixes from a file, not from scrollback."""
    written = triage.write_artifact("body\n", root=tmp_path)
    assert written == tmp_path / triage.ARTIFACT
    assert written.read_text(encoding="utf-8") == "body\n"


# --- the CLI ------------------------------------------------------------------


def test_main_lists_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVKIT_DIR", str(_ledger(tmp_path, _line("agent-report", message="x"))))
    monkeypatch.setattr(triage, "REPO_ROOT", tmp_path)
    assert triage.main([]) == 0
    assert "1 open" in capsys.readouterr().out


def test_main_refuses_a_resolution_with_no_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVKIT_DIR", str(_ledger(tmp_path, _line("agent-report", message="x"))))
    monkeypatch.setattr(triage, "REPO_ROOT", tmp_path)
    assert triage.main(["--resolve", "abcd1234"]) == 2
    assert "--note" in capsys.readouterr().out


def test_main_resolves_a_whole_group(tmp_path, monkeypatch, capsys):
    a = _line("agent-report", stamp=_STAMPS[0], message="same")
    b = _line("agent-report", stamp=_STAMPS[1], message="same")
    monkeypatch.setenv("DEVKIT_DIR", str(_ledger(tmp_path, a, b)))
    monkeypatch.setattr(triage, "REPO_ROOT", tmp_path)
    assert triage.main(["--resolve-like", triage.item_id(a), "--note", "fixed"]) == 0
    assert "0 open" in capsys.readouterr().out


def test_main_with_no_ledger_anywhere_still_exits_zero(tmp_path, monkeypatch, capsys):
    """Both roots are pinned, because there are now two ways to find the live ledger.

    `harness_events.ledger_path` falls back to its own checkout when `$DEVKIT_DIR` is
    unset and that checkout is devkit -- which this one is. Pinning only `triage`'s root
    left the *other* module resolving the real machine-wide ledger, so "no ledger
    anywhere" quietly became "the backlog this workstation happens to hold", and the
    test passed or failed on how many reports were open at the time.
    """
    monkeypatch.delenv("DEVKIT_DIR", raising=False)
    monkeypatch.setattr(triage, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(triage.harness_events, "REPO_ROOT", tmp_path)
    assert triage.main([]) == 0
    assert "nothing open" in capsys.readouterr().out


def test_the_stamp_is_never_needed_to_decide_membership():
    """The reversion check for the seven-day window this replaced.

    Membership used to be "is the stamp recent", which made an unanswered report leave
    the backlog on day eight and a fixed one linger for a week. Nothing here reads the
    stamp, so an ancient event is open until someone writes down what fixed it.
    """
    ancient = "1999-01-01T00:00:00+00:00\tevent=agent-report\tproject=x\tmessage=old"
    assert len(triage.open_items(triage.read_items(ancient))) == 1


def test_an_unparseable_stamp_no_longer_drops_a_real_report():
    assert len(triage.open_items(triage.read_items("not a stamp\tevent=agent-report\tx=y"))) == 1


# --- which runtime wrote it ---------------------------------------------------
#
# The ledger records what the harness did to an agent; until `agent=` existed it did not
# record *which* agent, and the two are not interchangeable. The capped-Bash gate is
# deliberately unported to Codex; a PreToolUse response that re-aims a call under Claude
# is dropped under Codex. So a hook reporting an error for one says nothing about the
# other, and grouping them let one fix retire the other's evidence.


def test_a_row_without_the_field_reads_as_unknown_not_as_claude():
    """Every row written before the field existed is on an append-only file forever."""
    item = triage.parse_line(_line("agent-report", message="old"))
    assert item is not None
    assert item.agent == "unknown"


def test_the_recorded_runtime_is_what_is_reported():
    item = triage.parse_line(_line("agent-report", agent="codex", message="m"))
    assert item is not None and item.agent == "codex"


def test_the_same_defect_under_two_runtimes_is_two_groups():
    items = triage.read_items(
        "\n".join(
            (
                _line("agent-report", agent="codex", stamp=_STAMPS[0], message="guard blocked rg"),
                _line("agent-report", agent="claude", stamp=_STAMPS[1], message="guard blocked rg"),
            )
        )
    )
    assert len(triage.groups(triage.open_items(items))) == 2


def test_resolving_a_codex_report_does_not_retire_the_claude_one():
    """The user's case, stated exactly: one runtime's error is not the other's."""
    items = triage.read_items(
        "\n".join(
            (
                _line("agent-report", agent="codex", stamp=_STAMPS[0], message="same words"),
                _line("agent-report", agent="claude", stamp=_STAMPS[1], message="same words"),
            )
        )
    )
    opened = triage.open_items(items)
    codex_id = next(i.id for i in opened if i.agent == "codex")
    assert triage.expand_like([codex_id], items) == [codex_id]


def test_for_agent_filters_and_an_empty_filter_keeps_everything():
    items = triage.read_items(
        "\n".join(
            (
                _line("agent-report", agent="codex", stamp=_STAMPS[0], message="a"),
                _line("agent-report", agent="claude", stamp=_STAMPS[1], message="b"),
                _line("agent-report", stamp=STAMP, message="c"),
            )
        )
    )
    assert len(triage.for_agent(items, "")) == 3
    assert [i.agent for i in triage.for_agent(items, "CODEX ")] == ["codex"]
    assert [i.detail for i in triage.for_agent(items, "unknown")] == ["c"]


def test_the_rendering_names_the_runtime():
    text = triage.render(triage.open_items(triage.read_items(_line("agent-report", agent="codex"))))
    assert "[codex]" in text


def test_the_rendering_carries_the_fields_diagnosis_starts_from():
    """The 0926-8 sweep read four session-friction groups with no `evidence=` shown and
    grepped the raw ledger for the transcript line each one named."""
    rows = [
        _line("session-friction", detail="d", evidence="t.jsonl#L5312"),
        _line("agent-report", detail="e", command="git -C x status", version="abc"),
    ]
    text = triage.render(triage.open_items(triage.read_items("\n".join(rows))))
    assert "  evidence t.jsonl#L5312" in text
    assert "  command git -C x status" in text and "  version abc" in text


def test_the_rendering_names_the_tree_a_report_was_filed_from():
    """Five agent-reports cited `logs/fix-pass-supervise/...` relative to a worktree the
    group never showed, and each cited line cost the sweep a search."""
    row = _line("agent-report", message="m", cwd="C:/w/.claude/worktrees/t", command="logs/x")
    text = triage.render(triage.open_items(triage.read_items(row)))
    assert "  cwd    C:/w/.claude/worktrees/t" in text


def test_the_artifact_is_the_whole_backlog_even_under_a_filter(tmp_path, monkeypatch):
    """A filtered artifact would read as 'this is everything' while hiding a runtime."""
    _ledger(
        tmp_path,
        _line("agent-report", agent="codex", stamp=_STAMPS[0], message="codex one"),
        _line("agent-report", agent="claude", stamp=_STAMPS[1], message="claude one"),
    )
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    monkeypatch.setattr(triage, "REPO_ROOT", tmp_path)
    assert triage.main(["--agent", "codex"]) == 0
    artifact = (tmp_path / triage.ARTIFACT).read_text(encoding="utf-8")
    assert "codex one" in artifact
    assert "claude one" in artifact


def test_a_translation_gap_is_a_triage_event():
    """The adapter records one when Codex would drop a member nobody has classified."""
    assert "codex-translation-gap" in triage.TRIAGE_EVENTS
    items = triage.read_items(_line("codex-translation-gap", agent="codex", detail="novelMember"))
    assert len(triage.open_items(items)) == 1


def test_a_failed_scheduled_job_is_a_triage_event():
    """`log-wrap.py --always` records one. Nobody watches a scheduled job and its
    artifact is overwritten per run, so without this a job can fail every night and the
    only evidence is last night's -- which is how the nightly release failed three times
    before a person hit it by hand."""
    log_wrap = load_script("scripts/log-wrap.py")

    assert log_wrap.FAILED_EVENT in triage.TRIAGE_EVENTS
    items = triage.read_items(
        _line(log_wrap.FAILED_EVENT, message="unattended task 'Devkit: Cut Release' failed")
    )
    assert len(triage.open_items(items)) == 1


def test_a_session_friction_row_is_open_and_resolve_like_reaches_it(tmp_path, monkeypatch):
    """The fix pass files `session-friction` and `fix-pass-finding` rows on this machine's
    ledger. With neither event in `TRIAGE_EVENTS`, main printed "nothing open" over a
    backlog the pass had just sent a session to work, and `--resolve-like` retired
    nothing (`resolved 0 item(s)`) -- ledger groups 027445d7 and 2a88da15, filed twice."""
    first = _line("session-friction", project="devkit", detail="reported: X")
    again = _line("session-friction", project="devkit", detail="reported: X", stamp=_STAMPS[1])
    finding = _line("fix-pass-finding", project="devkit", detail="push keeps failing")
    monkeypatch.setenv("DEVKIT_DIR", str(_ledger(tmp_path, first, again, finding)))
    monkeypatch.setattr(triage, "REPO_ROOT", tmp_path)

    assert len(triage.open_items(triage.load(tmp_path))) == 3
    assert triage.main(["--resolve-like", triage.item_id(first), "--note", "fixed"]) == 0
    assert [i.event for i in triage.open_items(triage.load(tmp_path))] == ["fix-pass-finding"]


def test_the_same_job_failing_nightly_stays_one_open_defect():
    """Three bad nights are one thing to fix. The exit code and artifact path are kept
    off the signature so a job whose failure mode shifts does not fork into two items
    nobody recognises as the same job."""
    log_wrap = load_script("scripts/log-wrap.py")
    message = "unattended task 'Devkit: Cut Release' failed"
    items = triage.read_items(
        "\n".join(
            _line(
                log_wrap.FAILED_EVENT,
                stamp=stamp,
                message=message,
                exit=code,
                artifact="logs/r.log",
            )
            for stamp, code in (("2026-09-12T06:00:04Z", "2"), ("2026-09-13T06:00:03Z", "2"))
        )
    )

    assert len(items) == 2
    assert len({item.signature for item in items}) == 1


def test_a_job_failing_for_a_new_cause_is_a_new_group_not_a_recurrence():
    """950c4a96: keyed on the task alone, a job's new failure read `RECURRED` and quoted
    an unrelated earlier fix as what not to repeat, which sent the session the wrong way.
    The `cause` `log-wrap.py` records splits causes; the same cause stays one group."""
    log_wrap = load_script("scripts/log-wrap.py")
    message = "unattended task 'Scheduled: Devkit Release' failed"
    stamps = [f"2026-09-2{day}T06:00:00+00:00" for day in range(4)]
    old = _line(
        log_wrap.FAILED_EVENT, stamp=stamps[0], message=message, cause="RuntimeError: diverged"
    )
    fixed = _line(
        "triage-resolved", stamp=stamps[1], ref=triage.item_id(old), note="pull_to_fixpoint"
    )
    new = _line(log_wrap.FAILED_EVENT, stamp=stamps[2], message=message, cause="KeyError: 'tag'")
    again = _line(log_wrap.FAILED_EVENT, stamp=stamps[3], message=message, cause="KeyError: 'tag'")
    history = triage.read_items("\n".join((old, fixed, new, again)))

    open_now = triage.open_items(history)
    assert len(open_now) == 2 and len({i.signature for i in open_now}) == 1
    assert triage.item_id(old) not in {i.id for i in open_now}
    rendered = triage.render(open_now, history)
    assert "RECURRED" not in rendered
    assert "cause  KeyError: 'tag'" in rendered


# --- a resolution that did not hold -------------------------------------------------------


def test_a_later_reopening_undoes_a_resolution_and_a_later_resolution_redoes_it():
    """`fix_verify` reopens a group whose fix never merged. Stamp order decides, not
    file order: shards are unioned, and the verdicts may sit in two machines' files."""
    report = _line("agent-report", message="one")
    ref = triage.item_id(report)
    resolved = _line("triage-resolved", stamp="2026-08-25T00:00:00+00:00", ref=ref, note="fixed")
    reopened = _line(
        triage.REOPENED_EVENT, stamp="2026-08-26T00:00:00+00:00", ref=ref, note="closed unmerged"
    )
    again = _line(
        "triage-resolved", stamp="2026-08-27T00:00:00+00:00", ref=ref, note="fixed for real"
    )
    assert triage.open_items(triage.read_items("\n".join([report, resolved]))) == []
    [back] = triage.open_items(triage.read_items("\n".join([reopened, report, resolved])))
    assert back.id == ref
    assert (
        triage.open_items(triage.read_items("\n".join([report, resolved, reopened, again]))) == []
    )
    assert triage.verdicts(triage.read_items(reopened))[ref][0] == triage.REOPENED_EVENT


def test_carried_names_the_resolutions_written_on_the_retired_branch_after_the_cut():
    """A session in a tree cut on `agent/x-0928` -- a name whose PR had merged the day
    before -- resolved its groups `pr=agent/x-0928`, and its fix went out as
    `agent/x-0928-6` when the ship carried the intent. `fix_verify` rightly discounts the
    old PR, which merged before those resolutions, so they sat pending until reopened."""
    ours = _line("agent-report", message="ours")
    theirs = _line("agent-report", message="theirs")
    elsewhere = _line("agent-report", message="elsewhere")
    undone = _line("agent-report", message="undone")
    lines = [
        _line(
            "triage-resolved",
            stamp="2026-09-28T10:00:00+00:00",
            ref=triage.item_id(theirs),
            pr="agent/x-0928",
            note="the merged PR's own fix",
        ),
        _line(
            "triage-resolved",
            stamp="2026-09-29T02:00:00+00:00",
            ref=triage.item_id(ours),
            pr="agent/x-0928",
            note="ours",
        ),
        _line(
            "triage-resolved",
            stamp="2026-09-29T02:00:00+00:00",
            ref=triage.item_id(elsewhere),
            pr="agent/y-0928",
            note="another branch",
        ),
        _line(
            "triage-resolved",
            stamp="2026-09-29T02:00:00+00:00",
            ref=triage.item_id(undone),
            pr="agent/x-0928",
            note="undone",
        ),
        _line(
            triage.REOPENED_EVENT,
            stamp="2026-09-29T03:00:00+00:00",
            ref=triage.item_id(undone),
            note="closed",
        ),
    ]
    items = triage.read_items("\n".join(lines))
    # The cut: main's tip when the tree was made, after the old PR merged -- in another
    # zone, as `git log %cI` gives it.
    since = "2026-09-28T20:00:00-04:00"
    assert triage.carried(items, "agent/x-0928", since) == [
        (triage.item_id(ours), "ours", "2026-09-29T02:00:00+00:00")
    ]
    assert triage.carried(items, "agent/x-0928", "not a time") == []


def test_repoint_resolves_again_on_the_new_branch_so_its_pr_settles_it(tmp_path):
    report = _line("agent-report", message="one")
    ref = triage.item_id(report)
    resolved = _line(
        "triage-resolved", stamp="2026-09-29T02:00:00+00:00", ref=ref, pr="agent/x-0928", note="n"
    )
    _ledger(tmp_path, report, resolved)
    assert triage.repoint(
        "agent/x-0928", "agent/x-0928-6", "2026-09-29T00:00:00+00:00", root=tmp_path
    ) == [ref]
    items = triage.load(tmp_path)
    assert triage.open_items(items) == []
    now = datetime.now(UTC)
    [latest] = triage.recent(items, now, timedelta(days=3650))
    # Written again now, but still the resolution of 02:00: `fix_verify.covered` retires
    # the rows filed between it and the merge, and would otherwise start at the carry.
    assert (latest.ref, latest.pr, latest.note, latest.stamp) == (
        ref,
        "agent/x-0928-6",
        "n",
        "2026-09-29T02:00:00+00:00",
    )


def test_a_resolution_carried_twice_keeps_the_time_it_was_first_made(tmp_path):
    report = _line("agent-report", message="one")
    ref = triage.item_id(report)
    first = _line(
        "triage-resolved", stamp="2026-09-29T02:00:00+00:00", ref=ref, pr="agent/x-0928", note="n"
    )
    carried = _line(
        "triage-resolved",
        stamp="2026-09-29T05:00:00+00:00",
        ref=ref,
        pr="agent/x-0928-6",
        note="n",
        resolved="2026-09-29T02:00:00+00:00",
    )
    _ledger(tmp_path, report, first, carried)
    assert triage.repoint(
        "agent/x-0928-6", "agent/x-0928-7", "2026-09-29T00:00:00+00:00", root=tmp_path
    ) == [ref]
    [again] = triage.recent(triage.load(tmp_path), datetime.now(UTC), timedelta(days=3650))
    assert (again.pr, again.stamp) == ("agent/x-0928-7", "2026-09-29T02:00:00+00:00")


def test_reopen_appends_one_event_per_id(tmp_path):
    report = _line("agent-report", message="one")
    _ledger(tmp_path, report)
    ref = triage.item_id(report)
    triage.resolve([ref], "fixed on agent/x", root=tmp_path)
    assert triage.open_items(triage.load(tmp_path)) == []
    assert triage.reopen([ref], "no PR was ever opened from agent/x", root=tmp_path) == [ref]
    [back] = triage.open_items(triage.load(tmp_path))
    assert back.id == ref
