"""`rc_config.py`: the opt-in, and the predicate that decides when the daily cycle runs.

Nothing here touches a process or the disk. The properties worth stating are that the
default is **off** -- a machine that upgrades into this job must not acquire servers it
never asked for -- and that malformed input serves nothing rather than raising, because
a scheduled task's traceback goes nowhere at all.
"""

from __future__ import annotations

import datetime as _dt
import json

import project_registry
import pytest
from support import REPO_ROOT, TEMPLATES, load_script

rc_config = load_script("scripts/rc_config.py")

# The project registry `ALL_PROJECTS` expands to, as the last `configured` set it.
REGISTERED: list[str] = []


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    REGISTERED.clear()
    monkeypatch.setattr(project_registry, "all_projects", lambda *_a, **_k: list(REGISTERED))


def configured(setting: object = None, folders: tuple[str, ...] = ("devkit", "carameli")):
    """`RC_SETTING` read as stored, with `folders` the registered projects."""
    REGISTERED[:] = folders
    return rc_config.config_of(setting)


# --- config_of ----------------------------------------------------------------


def test_a_bare_list_is_the_shorthand_for_a_config_of_defaults():
    config = configured(["devkit"])
    assert config.projects == ("devkit",)
    assert config.spawn == rc_config.DEFAULT_SPAWN
    assert config.permission_mode == rc_config.DEFAULT_PERMISSION_MODE
    assert config.idle_minutes == rc_config.DEFAULT_IDLE_MINUTES


def test_the_object_form_carries_the_knobs():
    config = configured(
        {
            "projects": ["devkit"],
            "spawn": "worktree",
            "permissionMode": "acceptEdits",
            "updateAt": "05:30",
            "idleMinutes": 45,
        }
    )
    assert config == rc_config.Config(("devkit",), "worktree", "acceptEdits", "05:30", 45)


def test_no_setting_serves_nothing():
    """The default has to be off. A machine that upgraded into this job and got servers
    it never asked for would be paying 300-400 MB per project to find out."""
    assert configured().projects == ()


@pytest.mark.parametrize(
    "value",
    [None, "", 3, "devkit", {"spawn": "worktree"}],
    ids=["unset", "empty", "scalar", "a-name", "no-projects"],
)
def test_a_setting_that_is_neither_list_nor_object_serves_nothing(value):
    """A scheduled task runs windowless: its traceback goes nowhere. Serving nothing is
    visible in the artifact, which a crash is not."""
    assert configured(value).projects == ()


@pytest.mark.parametrize(
    "setting",
    [rc_config.ALL_PROJECTS, {"projects": rc_config.ALL_PROJECTS, "spawn": "worktree"}],
    ids=["bare", "object"],
)
def test_all_serves_every_registered_project(setting):
    """A list is a second copy of the registry, and a project plugged in after it was
    written got no server. `all` reads the registry on every fire instead."""
    REGISTERED[:] = ["devkit", "carameli", "freshly-plugged"]
    assert rc_config.config_of(setting).projects == ("devkit", "carameli", "freshly-plugged")


def test_all_serves_nothing_off_a_registry_that_cannot_be_read(monkeypatch):
    monkeypatch.setattr(
        project_registry,
        "all_projects",
        lambda *_a, **_k: project_registry.parse_registry("projects = ["),
    )
    assert rc_config.config_of(rc_config.ALL_PROJECTS).projects == ()


def test_all_keeps_the_object_form_s_knobs():
    assert configured({"projects": rc_config.ALL_PROJECTS, "spawn": "worktree"}).spawn == "worktree"


def test_all_still_serves_a_project_on_hold():
    """The phone matches the registry, always. Subtracting `devkit.onHold` here once left
    three of seven checkouts unreachable from the phone with nothing in the artifact
    saying why -- a hold pauses adoption and box reporting, not this."""
    config = configured(rc_config.ALL_PROJECTS, ("devkit", "carameli"))
    assert config.projects == ("devkit", "carameli")
    assert rc_config.selected(config, ["devkit", "carameli"]) == (["devkit", "carameli"], [])


def test_all_with_nothing_registered_serves_nothing():
    assert configured(rc_config.ALL_PROJECTS, ()).projects == ()


def test_an_explicit_list_serves_a_held_project_it_names():
    config = configured(["devkit", "carameli"], ("devkit", "carameli"))
    assert rc_config.selected(config, ["devkit", "carameli"]) == (["devkit", "carameli"], [])


def test_non_string_project_names_are_dropped_not_stringified():
    assert configured({"projects": ["devkit", 7, None, ""]}).projects == (("devkit",))


def test_a_boolean_idle_window_falls_back_to_the_default():
    """`isinstance(True, int)` is true, so `"idleMinutes": true` would otherwise set a
    one-minute window -- a restart in the middle of nearly every turn."""
    config = configured({"projects": ["d"], "idleMinutes": True})
    assert config.idle_minutes == rc_config.DEFAULT_IDLE_MINUTES


@pytest.mark.parametrize("value", [0, -5, "20", 1.5], ids=["zero", "negative", "string", "float"])
def test_an_unusable_idle_window_falls_back_to_the_default(value):
    config = configured({"projects": ["d"], "idleMinutes": value})
    assert config.idle_minutes == rc_config.DEFAULT_IDLE_MINUTES


def test_the_default_spawn_mode_does_not_cut_worktrees():
    """Claude Code's worktrees land under `<repo>/.claude/worktrees/`, not the
    `<workspace>/.worktrees/` tier `worktree.py` owns. Claude Code reaps its own, so the
    gap is not lifecycle -- it is the stack half: no port lease and no
    `COMPOSE_PROJECT_NAME`, which two sessions running compose at once would collide on.
    A machine may opt in; it may not do so by accident."""
    assert rc_config.Config().spawn == "same-dir"


@pytest.mark.parametrize(
    "path",
    [REPO_ROOT / ".gitignore", TEMPLATES / "core" / "dot-gitignore.tmpl"],
    ids=["devkit", "generated"],
)
def test_every_repo_this_job_can_serve_ignores_claude_code_s_own_worktrees(path):
    """`"spawn": "worktree"` has Claude Code cut worktrees *inside* the checkout, at
    `.claude/worktrees/<name>`. Un-ignored, a nested checkout shows up untracked in every
    `git status`, `git add -A` stages the whole of it, and anything reading dirtiness
    (`sweep.classify`, the session-start banner) calls the checkout `holds_uncommitted`
    for as long as the worktree exists.

    Both copies, because `templates/` is a one-shot render: devkit's own file is what
    protects this repo, and the template is what protects the next generated one.
    """
    ignored = {line.strip().rstrip("/") for line in path.read_text(encoding="utf-8").splitlines()}
    assert ".claude/worktrees" in ignored


def test_no_permission_mode_is_granted_by_default():
    """The mode a spawned session runs in comes from the client that creates it, and the
    mobile app sends `auto`, so a mode set here does not reach a phone at all. On whatever
    it does reach it is a standing grant on an unattended machine, and opting in belongs
    to the person whose machine it is."""
    assert rc_config.Config().permission_mode == ""


def test_the_daily_update_is_timed_after_the_pass_that_owns_the_agent_clis():
    """`devkit-global-tools` runs at 04:30 and will skip while servers are up. Running
    before it would have this job racing a pass that was about to succeed on its own."""
    assert rc_config.DEFAULT_UPDATE_AT > "04:30"


# --- selected ---------------------------------------------------------------


def test_a_name_that_is_not_a_checkout_is_reported_not_skipped_silently():
    """A typo that served nothing would look exactly like a working machine with nothing
    to do, which is the one answer this job must never give wrongly."""
    serve, notes = rc_config.selected(rc_config.Config(("devkit", "typo")), ["devkit"])
    assert serve == ["devkit"]
    assert len(notes) == 1 and "typo" in notes[0] and "registry" in notes[0]


def test_everything_known_is_served_with_nothing_to_report():
    serve, notes = rc_config.selected(
        rc_config.Config(("devkit", "carameli")), ["devkit", "carameli"]
    )
    assert serve == ["devkit", "carameli"]
    assert notes == []


# --- hosts and assigned_here ------------------------------------------------


def test_without_hosts_every_machine_serves_its_own_checkouts():
    """A one-PC fleet has nothing to assign, so absent keeps the old reading."""
    config = configured(rc_config.ALL_PROJECTS)
    assert config.hosts == ()
    assert rc_config.assigned_here(config, ["devkit", "carameli"], "anyone") == (
        ["devkit", "carameli"],
        [],
    )


def test_a_bare_host_string_assigns_every_project_to_that_machine():
    """Spelled as `harness_events.host_name` spells it: any case, MagicDNS suffix off."""
    config = configured({"projects": "all", "hosts": "Desktop-A.tail1234.ts.net"})
    assert config.hosts == (("*", "desktop-a"),)
    assert rc_config.assigned_here(config, ["devkit", "carameli"], "desktop-a") == (
        ["devkit", "carameli"],
        [],
    )


def test_a_machine_another_is_assigned_to_serves_nothing_and_says_who_does():
    """The point of `hosts`: the setting is the same on every PC, and without it the
    phone listed each project once per machine."""
    config = configured({"projects": "all", "hosts": "desktop-a"})
    here, elsewhere = rc_config.assigned_here(config, ["devkit"], "desktop-b")
    assert here == []
    assert elsewhere == ["devkit: served by desktop-a, not here (desktop-b)"]


def test_parse_hosts_is_the_one_reading_every_hosts_knob_shares():
    """`devkit.localE2e` assigns its nightly run with this same parser, and says what
    it does with a project in its own verb."""
    assert rc_config.parse_hosts("Desktop-A") == (("*", "desktop-a"),)
    assert rc_config.parse_hosts({"carameli": "pc-b"}) == (("carameli", "pc-b"),)
    config = rc_config.Config(hosts=rc_config.parse_hosts("desktop-a"))
    _, elsewhere = rc_config.assigned_here(config, ["devkit"], "desktop-b", verb="tested")
    assert elsewhere == ["devkit: tested by desktop-a, not here (desktop-b)"]


# --- read off projects.toml, and nothing else -------------------------------------


def _checkout(tmp_path, registry: str | None, setting: object = None):
    """A devkit checkout at `tmp_path / "devkit"`, with a live file beside it that the
    readers must never consult."""
    root = tmp_path / "devkit"
    root.mkdir()
    if registry is not None:
        (root / "projects.toml").write_text(registry, encoding="utf-8")
    live = tmp_path / "alex-projects.code-workspace"
    payload = {"settings": {rc_config.RC_SETTING: setting}} if setting is not None else {}
    live.write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_read_config_reads_projects_toml_and_expands_all_from_the_registry(tmp_path):
    registry = '[settings."devkit.remoteControl"]\nprojects = "all"\nspawn = "worktree"\n'
    root = _checkout(tmp_path, registry, ["devkit"])
    REGISTERED[:] = ["devkit", "radio", "freshly-plugged"]
    config = rc_config.read_config(root)
    assert config.projects == ("devkit", "radio", "freshly-plugged")
    assert config.spawn == "worktree"


def test_read_config_never_falls_back_to_the_workspace_file(tmp_path):
    root = _checkout(tmp_path, "[settings]\n", {"projects": ["radio"], "capacity": 3})
    assert rc_config.read_config(root) == rc_config.Config()


def test_read_config_serves_nothing_for_a_malformed_or_missing_registry(tmp_path):
    root = _checkout(tmp_path, "[settings\n", ["devkit"])
    assert rc_config.read_config(root) == rc_config.Config()
    assert rc_config.read_config(tmp_path / "absent") == rc_config.Config()


def test_config_of_reads_a_stored_value_with_all_meaning_the_registry():
    REGISTERED[:] = ["a", "b"]
    assert rc_config.config_of("all").projects == ("a", "b")
    assert rc_config.config_of(["a"]).projects == ("a",)
    assert rc_config.config_of(3) == rc_config.Config()


def test_read_hosts_reads_projects_toml_and_a_malformed_one_assigns_nobody(tmp_path):
    registry = '[settings."devkit.localE2e"]\nhosts = "pc-b"\n'
    root = _checkout(tmp_path, registry)
    setting = rc_config.LOCAL_E2E_SETTING
    assert rc_config.read_hosts(setting, root).hosts == (("*", "pc-b"),)
    assert rc_config.read_hosts(rc_config.RC_SETTING, root).hosts == ()
    (root / "projects.toml").write_text("[settings\n", encoding="utf-8")
    assert rc_config.read_hosts(setting, root).hosts == ()
    assert rc_config.read_hosts(setting, tmp_path / "absent").hosts == ()


def test_hosts_of_reads_only_a_stored_values_hosts():
    assert rc_config.hosts_of({"hosts": "PC-A", "projects": "all"}).hosts == (("*", "pc-a"),)
    assert rc_config.hosts_of("pc-a") == rc_config.Config()
    assert rc_config.hosts_of(None) == rc_config.Config()


def test_the_committed_registry_serves_every_project_from_one_machine():
    """The phone needs one server per project, not one per PC."""
    REGISTERED[:] = ["devkit", "radio"]
    config = rc_config.read_config(REPO_ROOT)
    assert config.projects == ("devkit", "radio") and config.hosts


def test_a_project_named_in_hosts_overrides_the_default_machine():
    config = configured({"projects": "all", "hosts": {"*": "desktop-a", "carameli": "desktop-b"}})
    assert rc_config.assigned_here(config, ["devkit", "carameli"], "desktop-b")[0] == ["carameli"]
    assert rc_config.assigned_here(config, ["devkit", "carameli"], "desktop-a")[0] == ["devkit"]


def test_a_project_hosts_leaves_unassigned_is_served_nowhere_out_loud():
    """No `*` and no entry of its own: no machine serves it, and every machine says so
    rather than each quietly assuming another one has it."""
    config = configured({"projects": "all", "hosts": {"devkit": "a"}})
    here, elsewhere = rc_config.assigned_here(config, ["devkit", "carameli"], "b")
    assert here == []
    assert "carameli" in elsewhere[1] and "served nowhere" in elsewhere[1]


@pytest.mark.parametrize(
    "hosts",
    [{"devkit": 7}, {"devkit": ""}, {"devkit": "..."}, {"": "desktop-a"}, ["desktop-a"], 3],
    ids=["non-string-host", "empty-host", "no-name-left", "empty-project", "list", "int"],
)
def test_an_unusable_hosts_entry_is_dropped(hosts):
    config = configured({"projects": ["devkit"], "hosts": hosts})
    assert config.hosts == ()


@pytest.mark.parametrize(
    ("typed", "key"),
    [
        ("desktop-a", "desktop-a"),
        ("  Desktop-A.tail1234.ts.net ", "desktop-a"),
        ("DESKTOP-B9FC8VP", "desktop-b9fc8vp"),
        ("", ""),
        ("...", ""),
    ],
    ids=["already-a-key", "case-suffix-and-space", "upper-case", "empty", "no-name-left"],
)
def test_host_key_spells_a_typed_host_as_the_ledger_spells_the_machine(typed, key):
    assert rc_config.host_key(typed) == key


def test_host_for_prefers_a_project_s_own_entry_over_the_default():
    config = rc_config.Config(hosts=(("*", "desktop-a"), ("carameli", "desktop-b")))
    assert rc_config.host_for(config, "carameli") == "desktop-b"
    assert rc_config.host_for(config, "devkit") == "desktop-a"


def test_host_for_is_empty_for_a_project_nothing_assigns():
    config = rc_config.Config(hosts=(("devkit", "desktop-a"),))
    assert rc_config.host_for(config, "carameli") == ""
    assert rc_config.host_for(rc_config.Config(), "devkit") == ""


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


def test_the_capacity_can_be_raised_from_the_setting():
    assert configured({"projects": ["d"], "capacity": 20}).capacity == 20


@pytest.mark.parametrize("value", [0, -1, "8", True], ids=["zero", "negative", "string", "bool"])
def test_an_unusable_capacity_falls_back_to_the_default(value):
    config = configured({"projects": ["d"], "capacity": value})
    assert config.capacity == rc_config.DEFAULT_CAPACITY


def test_power_saving_is_off_unless_it_was_asked_for():
    """It is a real trade-off, not a free saving: past four hours the sessions it stops
    are gone rather than paused."""
    assert rc_config.Config().power_saving is False
    assert configured(["devkit"]).power_saving is False


def test_power_saving_turns_on_only_for_a_real_boolean_true():
    assert configured({"projects": ["d"], "powerSaving": True}).power_saving is True


@pytest.mark.parametrize(
    "value", ["true", "false", 1, "yes"], ids=["str-true", "str-false", "one", "yes"]
)
def test_a_truthy_non_boolean_never_turns_power_saving_on(value):
    """A `"false"` that read as True is how a mode nobody asked for starts destroying
    sessions."""
    config = configured({"projects": ["d"], "powerSaving": value})
    assert config.power_saving is False


def test_every_project_is_the_registry_and_nothing_when_it_cannot_be_read(monkeypatch):
    REGISTERED[:] = ["devkit", "radio"]
    assert rc_config.every_project() == ["devkit", "radio"]

    def broken(*_a, **_k):
        raise project_registry.RegistryError("projects.toml: broken")

    monkeypatch.setattr(project_registry, "all_projects", broken)
    assert rc_config.every_project() == []
