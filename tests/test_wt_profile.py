"""Tests for the Windows Terminal profile devkit's agent tabs are launched under.

Two properties carry the whole feature, and both are ways it could fail silently on a
desk rather than loudly here:

- **The GUID is derived, not chosen.** A random one regenerated on any edit would make
  every `--yes` append a second `Agent` profile instead of repairing the first, and the
  operator's terminal would fill with duplicates that all look right.
- **Reading is total.** `launch_name` runs on the path that opens a paid agent session.
  A settings file that is missing, half-written by Windows Terminal at that instant, or
  full of comments must degrade to "no profile" -- never to a traceback in place of the
  session.
"""

from __future__ import annotations

import json
import uuid

from support import wt_profile as wt


def settings(*profiles: dict) -> dict:
    """A settings.json in the shape Windows Terminal writes it."""
    return {
        "$schema": "https://aka.ms/terminal-profiles-schema",
        "defaultProfile": "{574e775e-4f2a-5b96-ac1e-a2962a402336}",
        "profiles": {"defaults": {}, "list": list(profiles)},
    }


def other(name: str = "PowerShell") -> dict:
    return {"guid": "{574e775e-4f2a-5b96-ac1e-a2962a402336}", "name": name}


# --- the definition ---------------------------------------------------------------


def test_the_guid_is_derived_so_a_reinstall_repairs_rather_than_duplicates():
    """The comment in `wt_profile.py` claims this derivation; if the two ever disagree,
    an existing profile stops being found and `--yes` appends a second one."""
    derived = uuid.uuid5(uuid.NAMESPACE_URL, "https://devkit.invalid/windows-terminal/agent")
    assert wt.PROFILE_GUID == "{" + str(derived) + "}"


def test_the_profile_runs_a_shell_and_never_an_agent():
    """Duplicate Tab replays the profile's command line. If that were `claude`, the
    gesture this whole change exists to fix would spend a session every time."""
    assert "pwsh" in wt.PROFILE["commandline"]
    for agent in ("claude", "codex"):
        assert agent not in wt.PROFILE["commandline"]


def test_the_profile_is_visibly_not_an_ordinary_shell():
    """The other half of the ask: a paid session should not be drawn like a shell."""
    assert wt.PROFILE["name"] == wt.PROFILE_NAME
    assert wt.PROFILE["icon"] and wt.PROFILE["tabColor"] and wt.PROFILE["colorScheme"]


def test_the_profile_does_not_elevate():
    """The workstation's default profile carries `elevate: true`, and Windows Terminal
    cannot host an elevated session as a tab in an unelevated window -- which is why
    agent tabs that inherited it broke out into a separate window with a UAC prompt
    instead of landing in the one `-w 0` names. A profile that reintroduced that would
    reintroduce the whole reported defect."""
    assert "elevate" not in wt.PROFILE


# --- finding the file -------------------------------------------------------------


def test_settings_candidates_covers_every_install_windows_terminal_has(tmp_path):
    """Store, Preview and unpackaged. `settings_path` picks among these, so a delivery
    missing here is a machine where the profile silently never installs."""
    candidates = wt.settings_candidates(tmp_path)
    assert [path.relative_to(tmp_path) for path in candidates] == list(wt.SETTINGS_RELATIVE)
    assert all(path.name == "settings.json" for path in candidates)


def test_the_store_install_is_preferred_over_an_unpackaged_one(tmp_path):
    for relative in wt.SETTINGS_RELATIVE:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    assert wt.settings_path(tmp_path) == tmp_path / wt.SETTINGS_RELATIVE[0]


def test_an_unpackaged_install_is_found_when_it_is_the_only_one(tmp_path):
    path = tmp_path / wt.SETTINGS_RELATIVE[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    assert wt.settings_path(tmp_path) == path


def test_no_windows_terminal_is_none_rather_than_an_error(tmp_path):
    """Every CI runner and every non-Windows checkout takes this branch."""
    assert wt.settings_path(tmp_path) is None


# --- reading it -------------------------------------------------------------------


def test_a_settings_file_with_comments_still_parses(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{\n  // mine\n  "profiles": {"list": []}\n}', encoding="utf-8")
    assert wt.read_settings(path) == {"profiles": {"list": []}}


def test_every_way_reading_can_fail_answers_empty(tmp_path):
    """`launch_name` is called while opening a paid session; none of these may raise."""
    missing = tmp_path / "nope.json"
    half_written = tmp_path / "half.json"
    half_written.write_text('{"profiles": {"li', encoding="utf-8")
    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2]", encoding="utf-8")
    assert wt.read_settings(None) == {}
    assert wt.read_settings(missing) == {}
    assert wt.read_settings(half_written) == {}
    assert wt.read_settings(not_an_object) == {}


def test_both_documented_shapes_of_the_profiles_key_are_read():
    assert wt.profiles_list(settings(other())) == [other()]
    assert wt.profiles_list({"profiles": [other()]}) == [other()]
    assert wt.profiles_list({}) == []
    assert wt.profiles_list({"profiles": {"list": "nonsense"}}) == []
    assert wt.profiles_list({"profiles": {"list": [other(), "nonsense"]}}) == [other()]


# --- recognising devkit's own -----------------------------------------------------


def test_the_profile_is_found_by_guid_not_by_name():
    renamed = {**wt.PROFILE, "name": "Something else"}
    assert wt.installed(settings(other(), renamed)) == renamed
    assert wt.installed(settings(other())) is None


def test_current_means_every_field_devkit_owns_is_the_value_it_owns():
    assert wt.is_current(settings(other(), dict(wt.PROFILE)))
    assert not wt.is_current(settings(other()))
    assert not wt.is_current(settings({**wt.PROFILE, "tabColor": "#000000"}))


def test_an_operators_own_additions_are_not_drift():
    """`--check` must not report 1 forever because somebody set a font on the profile."""
    assert wt.is_current(settings({**wt.PROFILE, "opacity": 80, "font": {"size": 12}}))


# --- installing it ----------------------------------------------------------------


def test_a_missing_profile_is_appended_beside_the_others():
    before = settings(other())
    after = wt.merged(before)
    assert wt.profiles_list(after) == [other(), dict(wt.PROFILE)]
    assert before == settings(other()), "the input must not be mutated"


def test_a_drifted_profile_is_repaired_in_place_keeping_what_devkit_does_not_own():
    drifted = {**wt.PROFILE, "tabColor": "#000000", "opacity": 80}
    after = wt.merged(settings(other(), drifted))
    repaired = wt.installed(after)
    assert wt.profiles_list(after) == [other(), repaired], "repaired, not appended"
    assert repaired["tabColor"] == wt.PROFILE["tabColor"]
    assert repaired["opacity"] == 80


def test_everything_else_in_the_file_survives_the_merge():
    """It is the operator's settings file, not devkit's."""
    before = {**settings(other()), "copyOnSelect": True, "keybindings": [{"id": "x"}]}
    after = wt.merged(before)
    assert after["copyOnSelect"] is True
    assert after["keybindings"] == [{"id": "x"}]
    assert after["profiles"]["defaults"] == {}
    assert after["defaultProfile"] == before["defaultProfile"]


def test_a_bare_list_or_an_empty_file_is_written_back_in_the_documented_shape():
    assert wt.merged({"profiles": [other()]})["profiles"] == {"list": [other(), dict(wt.PROFILE)]}
    assert wt.merged({})["profiles"] == {"list": [dict(wt.PROFILE)]}


def test_merging_twice_installs_one_profile():
    assert wt.profiles_list(wt.merged(wt.merged(settings()))) == [dict(wt.PROFILE)]


# --- what the launchers ask --------------------------------------------------------


def test_the_launchers_get_a_name_only_when_one_is_registered():
    assert wt.profile_name(settings(other(), dict(wt.PROFILE))) == wt.PROFILE_NAME
    assert wt.profile_name(settings(other())) == ""
    assert wt.profile_name({}) == ""


def test_a_renamed_profile_turns_the_flag_off_rather_than_aiming_at_a_stale_name():
    """`-p` resolves by name. An operator who renamed it gets the pre-profile tab back,
    which is a working tab, rather than a `-p` naming something that is not there."""
    assert wt.profile_name(settings({**wt.PROFILE, "name": "Mine"})) == ""


def test_launch_name_reads_this_machines_file(tmp_path):
    path = tmp_path / wt.SETTINGS_RELATIVE[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings(other(), dict(wt.PROFILE))), encoding="utf-8")
    assert wt.launch_name(tmp_path) == wt.PROFILE_NAME


def test_launch_name_on_a_machine_with_no_windows_terminal_is_empty(tmp_path):
    assert wt.launch_name(tmp_path) == ""
