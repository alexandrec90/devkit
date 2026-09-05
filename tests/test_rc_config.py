"""`rc_config.py`: the opt-in, and the predicate that decides when the daily cycle runs.

Nothing here touches a process or the disk. The properties worth stating are that the
default is **off** -- a machine that upgrades into this job must not acquire servers it
never asked for -- and that malformed input serves nothing rather than raising, because
a scheduled task's traceback goes nowhere at all.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest
from support import REPO_ROOT, TEMPLATES, load_script

rc_config = load_script("scripts/rc_config.py")


def workspace_text(
    setting: object = None, folders: tuple[str, ...] = ("devkit", "carameli")
) -> str:
    payload: dict = {"folders": [{"path": name} for name in folders], "settings": {}}
    if setting is not None:
        payload["settings"][rc_config.RC_SETTING] = setting
    return json.dumps(payload)


# --- parse_config -----------------------------------------------------------


def test_a_bare_list_is_the_shorthand_for_a_config_of_defaults():
    config = rc_config.parse_config(workspace_text(["devkit"]))
    assert config.projects == ("devkit",)
    assert config.spawn == rc_config.DEFAULT_SPAWN
    assert config.permission_mode == rc_config.DEFAULT_PERMISSION_MODE
    assert config.idle_minutes == rc_config.DEFAULT_IDLE_MINUTES


def test_the_object_form_carries_the_knobs():
    config = rc_config.parse_config(
        workspace_text(
            {
                "projects": ["devkit"],
                "spawn": "worktree",
                "permissionMode": "acceptEdits",
                "updateAt": "05:30",
                "idleMinutes": 45,
            }
        )
    )
    assert config == rc_config.Config(("devkit",), "worktree", "acceptEdits", "05:30", 45)


def test_a_workspace_with_no_setting_serves_nothing():
    """The default has to be off. A machine that upgraded into this job and got servers
    it never asked for would be paying 300-400 MB per project to find out."""
    assert rc_config.parse_config(workspace_text()).projects == ()


@pytest.mark.parametrize(
    "text",
    ["", "{", "[]", '"a string"', json.dumps({"settings": []}), json.dumps({"settings": {}})],
    ids=["empty", "truncated", "list", "scalar", "settings-not-a-dict", "no-setting"],
)
def test_malformed_input_serves_nothing_rather_than_raising(text):
    """A scheduled task runs windowless: its traceback goes nowhere. Serving nothing is
    visible in the artifact, which a crash is not."""
    assert rc_config.parse_config(text) == rc_config.Config()


def test_a_setting_that_is_neither_list_nor_object_is_ignored():
    assert rc_config.parse_config(workspace_text("devkit")) == rc_config.Config()


def test_non_string_project_names_are_dropped_not_stringified():
    assert rc_config.parse_config(
        workspace_text({"projects": ["devkit", 7, None, ""]})
    ).projects == (("devkit",))


def test_a_boolean_idle_window_falls_back_to_the_default():
    """`isinstance(True, int)` is true, so `"idleMinutes": true` would otherwise set a
    one-minute window -- a restart in the middle of nearly every turn."""
    config = rc_config.parse_config(workspace_text({"projects": ["d"], "idleMinutes": True}))
    assert config.idle_minutes == rc_config.DEFAULT_IDLE_MINUTES


@pytest.mark.parametrize("value", [0, -5, "20", 1.5], ids=["zero", "negative", "string", "float"])
def test_an_unusable_idle_window_falls_back_to_the_default(value):
    config = rc_config.parse_config(workspace_text({"projects": ["d"], "idleMinutes": value}))
    assert config.idle_minutes == rc_config.DEFAULT_IDLE_MINUTES


def test_the_default_spawn_mode_does_not_cut_worktrees():
    """Claude Code's worktrees land under `<repo>/.claude/worktrees/`, not the
    `<workspace>/.worktrees/` tier `worktree.py` owns, so one cut from the phone gets no
    provisioning, no port lease and no `reconcile` reap -- and nothing can report it
    stranded. A machine may still opt in; it may not do so by accident."""
    assert rc_config.Config().spawn == "same-dir"


@pytest.mark.parametrize(
    "path",
    [REPO_ROOT / ".gitignore", TEMPLATES / "core" / "dot-gitignore.tmpl"],
    ids=["devkit", "generated"],
)
def test_every_repo_this_job_can_serve_ignores_claude_code_s_own_worktrees(path):
    """`"spawn": "worktree"` has Claude Code cut worktrees *inside* the checkout, at
    `.claude/worktrees/<name>`. Un-ignored, the first session started from the phone
    leaves the static checkout permanently dirty -- which is `holds_uncommitted` to
    `sweep.classify`, so the nightly `reconcile` names the checkout and steps over it
    instead of bringing it home, for as long as the worktree exists.

    Both copies, because `templates/` is a one-shot render: devkit's own file is what
    protects this repo, and the template is what protects the next generated one.
    """
    ignored = {line.strip().rstrip("/") for line in path.read_text(encoding="utf-8").splitlines()}
    assert ".claude/worktrees" in ignored


def test_no_permission_mode_is_granted_by_default():
    """A phone opens spawned sessions in `auto` with no way to switch, so
    `bypassPermissions` here is the only route to one -- and a standing grant on an
    unattended machine. Opting in belongs to the person whose machine it is."""
    assert rc_config.Config().permission_mode == ""


def test_the_daily_update_is_timed_after_the_pass_that_owns_the_agent_clis():
    """`devkit-global-tools` runs at 04:30 and will skip while servers are up. Running
    before it would have this job racing a pass that was about to succeed on its own."""
    assert rc_config.DEFAULT_UPDATE_AT > "04:30"


# --- selected ---------------------------------------------------------------


def test_a_name_that_is_not_a_checkout_is_reported_not_skipped_silently():
    """A typo that served nothing would look exactly like a working machine with nothing
    to do, which is the one answer this job must never give wrongly."""
    serve, notes = rc_config.selected(rc_config.Config(("devkit", "typo")), ["devkit"], frozenset())
    assert serve == ["devkit"]
    assert len(notes) == 1 and "typo" in notes[0]


def test_a_project_on_hold_gets_no_server():
    serve, notes = rc_config.selected(
        rc_config.Config(("devkit",)), ["devkit"], frozenset({"devkit"})
    )
    assert serve == []
    assert "on hold" in notes[0]


def test_everything_known_and_running_is_served_with_nothing_to_report():
    serve, notes = rc_config.selected(
        rc_config.Config(("devkit", "carameli")), ["devkit", "carameli"], frozenset()
    )
    assert serve == ["devkit", "carameli"]
    assert notes == []


# --- valid_time and due_for_update ------------------------------------------


@pytest.mark.parametrize("at", ["04:00", "00:00", "23:59"])
def test_a_well_formed_update_time_is_accepted(at):
    assert rc_config.valid_time(at)


@pytest.mark.parametrize("at", ["4:00", "0400", "24:00", "04:60", "", "am"])
def test_a_malformed_update_time_is_rejected(at):
    assert not rc_config.valid_time(at)


def test_the_update_is_owed_once_the_hour_has_passed():
    assert rc_config.due_for_update("", _dt.datetime(2026, 9, 2, 4, 50), "04:45") is True


def test_the_update_is_not_owed_before_its_hour():
    assert rc_config.due_for_update("", _dt.datetime(2026, 9, 2, 3, 59), "04:45") is False


def test_the_update_is_not_owed_twice_in_a_day():
    assert rc_config.due_for_update("2026-09-02", _dt.datetime(2026, 9, 2, 9, 0), "04:45") is False


def test_a_day_that_was_missed_is_caught_up_on_the_next_tick():
    """The scheduler catches up a missed *fire*; every fire happens in the runner, so
    only this predicate can know a whole day went by."""
    assert rc_config.due_for_update("2026-09-01", _dt.datetime(2026, 9, 3, 11, 0), "04:45") is True


def test_a_malformed_update_time_never_fires_the_cycle():
    assert rc_config.due_for_update("", _dt.datetime(2026, 9, 2, 23, 0), "4pm") is False


# --- the memory knobs -------------------------------------------------------


def test_the_session_capacity_is_bounded_well_below_the_cli_default():
    """A server holds every session the phone spawns for as long as it lives, so
    `remote-control`'s default of 32 is an unbounded memory budget for a process meant
    to run all week."""
    assert rc_config.DEFAULT_CAPACITY < 32
    assert rc_config.Config().capacity == rc_config.DEFAULT_CAPACITY


def test_the_capacity_can_be_raised_from_the_workspace_file():
    assert (
        rc_config.parse_config(workspace_text({"projects": ["d"], "capacity": 20})).capacity == 20
    )


@pytest.mark.parametrize("value", [0, -1, "8", True], ids=["zero", "negative", "string", "bool"])
def test_an_unusable_capacity_falls_back_to_the_default(value):
    config = rc_config.parse_config(workspace_text({"projects": ["d"], "capacity": value}))
    assert config.capacity == rc_config.DEFAULT_CAPACITY


def test_power_saving_is_off_unless_it_was_asked_for():
    """It is a real trade-off, not a free saving: past four hours the sessions it stops
    are gone rather than paused."""
    assert rc_config.Config().power_saving is False
    assert rc_config.parse_config(workspace_text(["devkit"])).power_saving is False


def test_power_saving_turns_on_only_for_a_real_boolean_true():
    assert (
        rc_config.parse_config(
            workspace_text({"projects": ["d"], "powerSaving": True})
        ).power_saving
        is True
    )


@pytest.mark.parametrize(
    "value", ["true", "false", 1, "yes"], ids=["str-true", "str-false", "one", "yes"]
)
def test_a_truthy_non_boolean_never_turns_power_saving_on(value):
    """A `"false"` that read as True is how a mode nobody asked for starts destroying
    sessions."""
    config = rc_config.parse_config(workspace_text({"projects": ["d"], "powerSaving": value}))
    assert config.power_saving is False
