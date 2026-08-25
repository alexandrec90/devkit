"""Tests for `scripts/extract-codex-schema.py`.

Every test here runs against a **synthetic binary** -- a few schema objects embedded in
filler bytes -- rather than against the installed Codex. That is deliberate twice over:
CI has no Codex to read, and a test that reads the real one would pass or fail on
whichever version this workstation happens to have installed.

The committed snapshot is checked here too, because it is the artifact the adapter
actually depends on and an empty or truncated one would degrade every translation check
to "unknown-event" -- silently, which is the failure mode this whole change exists to
remove.
"""

import json

from support import REPO_ROOT, load_script

extract = load_script("scripts/extract-codex-schema.py")


def _schema(title: str, properties: dict, definitions: dict | None = None) -> bytes:
    body = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "additionalProperties": False,
        "properties": properties,
        "title": title,
        "type": "object",
    }
    if definitions:
        body["definitions"] = definitions
    return json.dumps(body).encode()


def _binary(*schemas: bytes) -> bytes:
    """Schemas separated by bytes that are not valid UTF-8, as in a real executable."""
    filler = bytes(range(200, 256))
    return filler + filler.join(schemas) + filler


# --- finding the schemas ------------------------------------------------------


def test_event_name_translates_codexs_spelling_to_the_wiring_generators():
    assert extract.event_name("pre-tool-use.command.output") == ("PreToolUse", "output")
    assert extract.event_name("session-start.command.input") == ("SessionStart", "input")


def test_a_title_that_is_not_a_hook_schema_is_not_one():
    assert extract.event_name("some.other.thing") is None
    assert extract.event_name("PreToolUse") is None


def test_schemas_are_found_between_arbitrary_binary_bytes():
    data = _binary(_schema("stop.command.output", {"decision": {"type": "string"}}))
    assert list(extract.embedded_schemas(data)) == ["stop.command.output"]


def test_a_truncated_schema_is_skipped_rather_than_raising():
    """A best-effort read of a binary must not die on a partial match."""
    good = _schema("stop.command.output", {"decision": {"type": "string"}})
    data = _binary(good[: len(good) // 2], good)
    assert list(extract.embedded_schemas(data)) == ["stop.command.output"]


def test_an_unterminated_object_walks_off_instead_of_scanning_the_whole_file():
    assert extract.schema_at(b'{"$schema": "x"', 0) is None


# --- reducing them to a member set --------------------------------------------


def test_nested_members_are_dotted_through_the_ref():
    schema = json.loads(
        _schema(
            "pre-tool-use.command.output",
            {
                "continue": {"type": "boolean"},
                "hookSpecificOutput": {"allOf": [{"$ref": "#/definitions/Wire"}]},
            },
            {"Wire": {"properties": {"hookEventName": {}, "updatedInput": {}}}},
        )
    )
    assert extract.accepted_members(schema) == [
        "continue",
        "hookSpecificOutput",
        "hookSpecificOutput.hookEventName",
        "hookSpecificOutput.updatedInput",
    ]


def test_the_contract_keeps_only_output_schemas():
    """An input schema describes what Codex sends, which is not what a hook must emit."""
    data = _binary(
        _schema("stop.command.output", {"decision": {}}),
        _schema("stop.command.input", {"cwd": {}}),
    )
    built = extract.contract(data, "codex-cli 9.9.9")
    assert list(built["events"]) == ["Stop"]
    assert built["codex_version"] == "codex-cli 9.9.9"


# --- reporting a Codex upgrade ------------------------------------------------


def test_differences_names_both_directions():
    old = {"events": {"PreToolUse": ["continue", "decision"]}}
    new = {"events": {"PreToolUse": ["continue", "reason"], "Stop": ["continue"]}}
    assert extract.differences(old, new) == [
        "+ PreToolUse.reason",
        "- PreToolUse.decision",
        "+ Stop.continue",
    ]


def test_an_identical_contract_reports_nothing():
    same = {"events": {"Stop": ["continue"]}}
    assert extract.differences(same, same) == []


def test_a_missing_snapshot_reads_as_empty_rather_than_raising(tmp_path):
    assert extract.load_snapshot(tmp_path) == {}


def test_no_codex_installed_is_not_a_finding(monkeypatch, capsys):
    """The same call `sync-devkit.py` makes for an unset `$DEVKIT_DIR`: with nothing to
    compare against there is no drift to report, and the snapshot is committed precisely
    so nothing downstream depends on this branch."""
    monkeypatch.setattr(extract.shutil, "which", lambda name: None)
    assert extract.main(["--check"]) == 0
    assert "no codex on PATH" in capsys.readouterr().out


def test_codex_binary_is_resolved_through_an_injectable_probe():
    assert extract.codex_binary(which=lambda name: None) is None
    assert extract.codex_binary(which=lambda name: "/usr/bin/codex").name.startswith("codex")


def test_check_reports_drift_and_write_removes_it(tmp_path, monkeypatch, capsys):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(_binary(_schema("stop.command.output", {"decision": {}})))
    monkeypatch.setattr(extract, "codex_binary", lambda which=None: binary)
    monkeypatch.setattr(extract, "codex_version", lambda path: "codex-cli 9.9.9")
    (tmp_path / "scripts" / "hooks").mkdir(parents=True)
    (tmp_path / extract.SNAPSHOT).write_text(
        json.dumps({"events": {"Stop": ["continue"]}}), encoding="utf-8"
    )

    assert extract.main(["--check", "--root", str(tmp_path)]) == 1
    reported = capsys.readouterr().err
    assert "+ Stop.decision" in reported and "- Stop.continue" in reported

    assert extract.main(["--write", "--root", str(tmp_path)]) == 0
    assert extract.main(["--check", "--root", str(tmp_path)]) == 0


def test_a_codex_that_embeds_no_schemas_fails_rather_than_writing_an_empty_snapshot(
    tmp_path, monkeypatch, capsys
):
    """The one outcome that must not be quiet: an empty snapshot turns every downstream
    check into `unknown-event`, which passes."""
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"no schemas in here at all")
    monkeypatch.setattr(extract, "codex_binary", lambda which=None: binary)
    monkeypatch.setattr(extract, "codex_version", lambda path: "codex-cli 9.9.9")
    assert extract.main(["--write", "--root", str(tmp_path)]) == 1
    assert "no hook schemas" in capsys.readouterr().err


def test_codex_version_survives_a_binary_that_will_not_run(tmp_path):
    assert extract.codex_version(tmp_path / "not-an-executable") == "unknown"


# --- the committed snapshot ---------------------------------------------------


def test_the_committed_snapshot_is_the_real_contract():
    """It is vendored and the adapter reads it at runtime, so an empty one is a defect
    that shows up only as translations quietly no longer being checked."""
    snapshot = json.loads((REPO_ROOT / extract.SNAPSHOT).read_text(encoding="utf-8"))
    assert snapshot["codex_version"].startswith("codex")
    events = snapshot["events"]
    # The two facts the adapter's design rests on, asserted against the artifact rather
    # than against a paragraph describing it.
    assert "hookSpecificOutput.updatedInput" in events["PreToolUse"]
    assert "hookSpecificOutput.permissionDecision" not in events["PermissionRequest"]
