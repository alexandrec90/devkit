"""Tests for the three-group harness switch.

The file-moving half lives in `harness_state.py` and is covered by
`test_harness_state.py`; what is asserted here is the *decisions* — which groups exist,
what each one writes, and what the CLI does when nobody names a verb.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from support import harness_state, load_script

switch = load_script("scripts/harness-switch.py")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """`apply` writes the ledger, so the state module's constants have to move even in a
    test that only cares about the hooks group."""
    monkeypatch.setattr(harness_state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(harness_state, "LEDGER", tmp_path / "state" / "ledger.json")
    monkeypatch.setattr(harness_state, "STASH", tmp_path / "state" / "files")


def _never(*_args, **_kwargs):
    raise AssertionError("nothing should have run here")


# --- the hooks group -----------------------------------------------------------------


def test_setting_the_switch_leaves_every_other_env_key_alone():
    """This machine's user settings already carry five unrelated variables. A group that
    rewrote the block would take an operator's `MAX_THINKING_TOKENS` with it."""
    payload = {"theme": "dark", "env": {"MAX_THINKING_TOKENS": "63999"}}
    updated = switch.settings_with_env(payload, switch.HOOKS_OFF_ENV, "1")
    assert updated["env"] == {"MAX_THINKING_TOKENS": "63999", switch.HOOKS_OFF_ENV: "1"}
    assert updated["theme"] == "dark"


def test_clearing_the_switch_removes_only_that_key():
    payload = {"env": {"DEVKIT_HOOKS_OFF": "1", "OTHER": "x"}}
    assert switch.settings_with_env(payload, switch.HOOKS_OFF_ENV, None)["env"] == {"OTHER": "x"}


def test_clearing_the_last_key_removes_the_env_block_rather_than_leaving_it_empty():
    payload = {"env": {"DEVKIT_HOOKS_OFF": "1"}}
    assert "env" not in switch.settings_with_env(payload, switch.HOOKS_OFF_ENV, None)


def test_a_settings_file_with_no_env_block_gains_one():
    assert switch.settings_with_env({}, switch.HOOKS_OFF_ENV, "1") == {
        "env": {switch.HOOKS_OFF_ENV: "1"}
    }


def test_a_settings_file_that_is_not_an_object_is_replaced_rather_than_indexed():
    assert switch.settings_with_env(["nonsense"], switch.HOOKS_OFF_ENV, "1") == {
        "env": {switch.HOOKS_OFF_ENV: "1"}
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("all", True),
        ("stop,lint-fix", True),
        ("0", False),
        ("", False),
        ("off", False),
    ],
)
def test_hooks_are_off_reads_the_same_asymmetry_the_hooks_do(tmp_path, value, expected):
    """`agent-box.py` exports the variable into the terminal it opens on the strength of
    this, so it has to agree with `harness_config.hooks_off` about what "off" means --
    `DEVKIT_HOOKS_OFF=0` is somebody turning the harness back ON."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {switch.HOOKS_OFF_ENV: value}}), encoding="utf-8")
    assert switch.hooks_are_off(path) is expected


def test_hooks_are_off_is_false_when_there_is_no_settings_file(tmp_path):
    assert switch.hooks_are_off(tmp_path / "absent.json") is False


@pytest.mark.parametrize("text", ["", "{", "not json at all"])
def test_a_settings_file_that_cannot_be_parsed_reads_as_empty(tmp_path, text):
    """A hand-edited settings file with a trailing comma must not stop the switch from
    writing the one key it owns -- the operator is mid-repair, and refusing here would
    make the repair need the switch it is trying to reach."""
    path = tmp_path / "settings.json"
    path.write_text(text, encoding="utf-8")
    assert switch.read_settings(path) == {}


# --- which roots the instruction group reaches ----------------------------------------


def test_the_roots_are_the_user_tier_the_workspace_and_every_checkout(tmp_path, monkeypatch):
    """The first two come first because they are the ones that reach a session running in
    an ephemeral box, where the checkout tier is the box's own copy."""
    workspace = tmp_path / "ws" / "w.code-workspace"
    (tmp_path / "ws" / "proj").mkdir(parents=True)
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    workspace.write_text('{"folders": []}', encoding="utf-8")
    monkeypatch.setattr(switch.Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.setattr(switch.devkit_project, "known_projects", lambda _text: ["proj"])

    assert switch.default_roots(workspace) == [
        tmp_path / "home" / ".claude",
        tmp_path / "ws",
        tmp_path / "ws" / "proj",
    ]


def test_a_registered_checkout_that_is_not_on_disk_is_skipped(tmp_path, monkeypatch):
    """`workspace.jsonc` outlives an unplugged clone, and a root that does not exist would
    make the whole group fail rather than switch the ones that do."""
    workspace = tmp_path / "ws" / "w.code-workspace"
    (tmp_path / "ws").mkdir(parents=True)
    workspace.write_text('{"folders": []}', encoding="utf-8")
    monkeypatch.setattr(switch.Path, "home", classmethod(lambda _cls: tmp_path / "absent"))
    monkeypatch.setattr(switch.devkit_project, "known_projects", lambda _text: ["nope"])
    assert switch.default_roots(workspace) == [tmp_path / "ws"]


def test_switching_hooks_writes_the_file_and_the_user_environment(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(switch, "WINDOWS", True)
    path = tmp_path / "settings.json"

    switch.switch_hooks(True, path, runner=lambda argv, **_: calls.append(argv))
    assert switch.hooks_are_off(path) is True
    assert calls == [["setx", switch.HOOKS_OFF_ENV, switch.HOOKS_OFF_VALUE]]

    calls.clear()
    switch.switch_hooks(False, path, runner=lambda argv, **_: calls.append(argv))
    assert switch.hooks_are_off(path) is False
    assert calls[0][:2] == ["reg", "delete"]


def test_off_windows_the_hooks_group_says_what_it_cannot_do(tmp_path, monkeypatch):
    monkeypatch.setattr(switch, "WINDOWS", False)
    lines = switch.switch_hooks(True, tmp_path / "s.json", runner=_never)
    assert any("shell profile" in line for line in lines)


# --- the jobs group ------------------------------------------------------------------


def test_jobs_are_disabled_not_deleted():
    """The registration carries the trigger, the windowless interpreter and the time limit
    its installer chose; re-creating one from memory is how those are lost."""
    assert switch.job_change_argv("devkit-release", False) == [
        "schtasks",
        "/Change",
        "/TN",
        "devkit-release",
        "/DISABLE",
    ]
    assert switch.job_change_argv("devkit-release", True)[-1] == "/ENABLE"


def test_the_jobs_group_names_only_the_branch_delivery_jobs():
    """Docker prune, the tray and the global-tools pass are machine maintenance that has
    nothing to do with whether an agent is running."""
    assert set(switch.BRANCH_DELIVERY_JOBS) == {
        "devkit-worktree-reconcile",
        "devkit-upgrade-projects",
        "devkit-release",
    }


def test_a_job_that_is_not_registered_is_reported_not_recorded(monkeypatch):
    monkeypatch.setattr(switch, "WINDOWS", True)

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "ERROR: cannot find the file")

    lines, changed = switch.switch_jobs(True, ("devkit-release",), runner=runner)
    assert changed == []
    assert any("not registered" in line for line in lines)


def test_off_windows_the_jobs_group_changes_nothing(monkeypatch):
    monkeypatch.setattr(switch, "WINDOWS", False)
    lines, changed = switch.switch_jobs(True, runner=_never)
    assert changed == []
    assert lines


# --- the groups together ---------------------------------------------------------------


def test_the_group_list_is_the_order_off_applies_them():
    """Cheapest and most reversible first, so a run interrupted halfway has stood down a
    prefix of the list rather than an arbitrary subset."""
    assert switch.GROUPS == ("hooks", "instructions", "jobs")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("all", ["hooks", "instructions", "jobs"]),
        ("", ["hooks", "instructions", "jobs"]),
        ("hooks", ["hooks"]),
        (" jobs , hooks ", ["jobs", "hooks"]),
        ("hookz", None),
        ("hooks,nope", None),
    ],
)
def test_the_group_argument_is_parsed_or_refused(raw, expected):
    assert switch.selected_groups(raw) == expected


def test_a_group_nobody_selected_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(switch, "switch_hooks", _never)
    monkeypatch.setattr(switch, "switch_jobs", _never)
    monkeypatch.setattr(switch, "switch_instructions", _never)
    assert switch.apply([], True, tmp_path / "w.code-workspace") == ["nothing selected"]


def test_switching_off_the_jobs_group_records_only_what_actually_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(switch, "WINDOWS", True)

    def runner(argv, **_kwargs):
        code = 0 if "devkit-release" in argv else 1
        return subprocess.CompletedProcess(argv, code, "", "")

    switch.apply(["jobs"], True, tmp_path / "w.code-workspace", runner=runner)
    assert harness_state.Ledger.load().jobs == ("devkit-release",)


def test_switching_the_jobs_group_back_on_empties_the_record(tmp_path, monkeypatch):
    monkeypatch.setattr(switch, "WINDOWS", True)
    workspace = tmp_path / "w.code-workspace"
    switch.apply(["jobs"], True, workspace, runner=lambda argv, **_: _ok(argv))
    switch.apply(["jobs"], False, workspace, runner=lambda argv, **_: _ok(argv))
    assert harness_state.Ledger.load().jobs == ()


def _ok(argv):
    return subprocess.CompletedProcess(argv, 0, "", "")


def test_standing_the_instructions_group_down_warns_about_the_drift_check(tmp_path, monkeypatch):
    """`sync-devkit.py --check` is vendored and cannot import this tier, so it reports the
    held rules as drift. Said at the time rather than discovered later."""
    monkeypatch.setattr(switch, "default_roots", lambda _w: [])
    lines = switch.switch_instructions(True, harness_state.Ledger(), tmp_path)
    assert any("sync-devkit.py --check" in line for line in lines)


# --- the CLI -------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "w.code-workspace"
    path.write_text('{"folders": []}', encoding="utf-8")
    return path


def test_an_unknown_group_is_a_usage_error(workspace, capsys):
    assert switch.main(["--off", "--group", "hookz", "--workspace", str(workspace)]) == (
        switch.EXIT_USAGE
    )
    assert "unknown group" in capsys.readouterr().err


def test_a_dismissed_picker_is_a_cancel_rather_than_a_run(workspace, monkeypatch, capsys):
    """VS Code aborts a run itself only for its own input types; a dismissed `command`
    input arrives as the literal, and the receiving script is the only place that can
    treat it as a cancel."""
    monkeypatch.setattr(switch, "apply", _never)
    argv = ["--off", "--group", "${input:harnessGroups}", "--workspace", str(workspace)]
    assert switch.main(argv) == switch.EXIT_OK
    assert "nothing done" in capsys.readouterr().out


def test_no_verb_reports_rather_than_acts(workspace, monkeypatch, capsys):
    """Read-only by default, the same shape as `global-tools.py` and `upgrade-project.py`:
    the destructive spelling is the one you have to type."""
    monkeypatch.setattr(switch, "apply", _never)
    assert switch.main(["--workspace", str(workspace)]) == switch.EXIT_OK
    assert "instructions:" in capsys.readouterr().out


def test_an_explicit_status_reports_and_acts_on_nothing(workspace, monkeypatch, capsys):
    """The picker offers three literal flags, so `--status` has to mean what no verb does
    rather than being rejected as an unknown argument."""
    monkeypatch.setattr(switch, "apply", _never)
    assert switch.main(["--status", "--workspace", str(workspace)]) == switch.EXIT_OK
    assert "hooks:" in capsys.readouterr().out


def test_the_report_names_every_group_whether_or_not_it_is_off(workspace):
    lines = switch.status_lines(harness_state.Ledger(), workspace)
    assert [line.split(":")[0] for line in lines[:3]] == ["hooks", "instructions", "jobs"]
