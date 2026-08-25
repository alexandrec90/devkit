"""The read half of the harness-events ledger: open vs resolved, and the grouping.

Every test here writes its own ledger under `tmp_path`. The live one is append-only and
machine-wide, so a test that touched it would both depend on and pollute a file the rest
of the workspace is writing to concurrently.
"""

from __future__ import annotations

from support import load_script

triage = load_script("scripts/harness_triage.py")

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
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "harness-events.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
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
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    assert triage.ledger_file() == tmp_path / "logs" / "harness-events.log"
    monkeypatch.delenv("DEVKIT_DIR", raising=False)
    assert triage.ledger_file().name == "harness-events.log"


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


def test_the_ledger_is_only_ever_appended_to(tmp_path):
    report = _line("agent-report", message="one")
    _ledger(tmp_path, report)
    triage.resolve([triage.item_id(report)], "done", root=tmp_path)
    text = (tmp_path / "logs" / "harness-events.log").read_text(encoding="utf-8")
    assert text.startswith(report)


# --- rendering and the artifact -----------------------------------------------


def test_an_empty_backlog_renders_as_nothing_open():
    assert "nothing open" in triage.render([])


def test_the_rendering_names_an_id_and_the_command_that_retires_it():
    items = triage.open_items(triage.read_items(_line("agent-report", message="the detail")))
    text = triage.render(items)
    assert items[0].id in text
    assert "the detail" in text
    assert "--resolve-like" in text


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
