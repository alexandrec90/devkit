"""`scripts/project_registry.py`: the registry and settings, with no live-file fallback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_switch
import machine_role
import project_registry as pr
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """`(root, devkit)`: a projects root holding a devkit checkout, and nothing else."""
    root = tmp_path / "projects"
    devkit = root / "devkit"
    devkit.mkdir(parents=True)
    return root, devkit


def _live(root: Path, folders: list[str], settings: dict | None = None) -> Path:
    payload: dict = {"folders": [{"path": name} for name in folders]}
    if settings is not None:
        payload["settings"] = settings
    path = root / sweep.WORKSPACE_FILE_NAME
    # A comment, as the real file carries: the fallback must read it through JSONC.
    path.write_text("// live\n" + json.dumps(payload), encoding="utf-8")
    return path


def _registry(devkit: Path, text: str) -> None:
    (devkit / pr.REGISTRY_NAME).write_text(text, encoding="utf-8")


# --- where the files are ---------------------------------------------------------------


def test_the_root_of_a_static_checkout_is_its_parent(tmp_path):
    root, devkit = _tree(tmp_path)
    assert pr.projects_root(devkit) == root


def test_the_root_needs_no_workspace_file(tmp_path):
    root, devkit = _tree(tmp_path)
    assert not (root / sweep.WORKSPACE_FILE_NAME).exists()
    assert pr.projects_root(devkit) == root


def test_a_box_resolves_to_the_root_above_its_tier(tmp_path):
    root, _ = _tree(tmp_path)
    box = root / sweep.BOXES_DIR_NAME / "devkit--topic-0810"
    assert pr.projects_root(box) == root
    assert pr.machine_path(box) == root / pr.MACHINE_FILE_NAME


def test_an_agent_worktree_resolves_to_its_checkouts_root(tmp_path):
    root, devkit = _tree(tmp_path)
    tree = devkit / ".claude" / "worktrees" / "some-task"
    assert pr.projects_root(tree) == root
    assert pr.live_workspace(tree) == root / sweep.WORKSPACE_FILE_NAME


def test_the_paths_reuse_the_sweeps_resolution(tmp_path):
    root, devkit = _tree(tmp_path)
    assert pr.live_workspace(devkit) == sweep.default_workspace(devkit)
    assert pr.registry_path(devkit) == devkit / "projects.toml"
    assert pr.machine_path(devkit) == root / ".devkit-machine.json"


# --- parsing ---------------------------------------------------------------------------


def test_the_registry_keeps_file_order_and_drops_repeats():
    text = 'projects = ["b", "a", "b"]\n'
    assert pr.parse_registry(text) == ["b", "a"]


def test_a_registry_without_projects_is_empty():
    assert pr.parse_registry("[settings]\n") == []


@pytest.mark.parametrize(
    "text",
    ["projects = [\n", 'projects = "devkit"\n', "projects = [1]\n", 'projects = [""]\n'],
)
def test_a_malformed_registry_raises(text):
    with pytest.raises(pr.RegistryError):
        pr.parse_registry(text)


def test_shared_settings_are_keyed_by_full_name():
    text = '[settings."devkit.localE2e".hosts]\n"*" = "pc-a"\n'
    assert pr.parse_shared_settings(text) == {"devkit.localE2e": {"hosts": {"*": "pc-a"}}}


@pytest.mark.parametrize("text", ["settings = 3\n", "[settings\n"])
def test_malformed_shared_settings_raise(text):
    with pytest.raises(pr.RegistryError):
        pr.parse_shared_settings(text)


def test_the_machine_file_is_one_object():
    assert pr.parse_machine('{"devkit.fixPass": "plan"}') == {"devkit.fixPass": "plan"}


@pytest.mark.parametrize("text", ["[]", "{", ""])
def test_a_malformed_machine_file_raises(text):
    with pytest.raises(pr.RegistryError):
        pr.parse_machine(text)


@pytest.mark.parametrize("text", ["not json", "[]", '{"settings": []}', "{}"])
def test_workspace_settings_are_empty_for_anything_else(text):
    assert pr.workspace_settings(text) == {}


def test_workspace_settings_read_through_comments():
    text = '{\n  // a reason\n  "settings": {"devkit.onHold": ["x"]}\n}'
    assert pr.workspace_settings(text) == {"devkit.onHold": ["x"]}


# --- the registry ----------------------------------------------------------------------


def test_the_registry_file_is_the_registry_and_the_live_file_is_never_read(tmp_path):
    root, devkit = _tree(tmp_path)
    _registry(devkit, 'projects = ["devkit", "radio"]\n')
    _live(root, ["devkit", "other"])
    assert pr.all_projects(devkit) == ["devkit", "radio"]


def test_the_registry_excludes_nothing(tmp_path, monkeypatch):
    _, devkit = _tree(tmp_path)
    monkeypatch.setattr(sweep, "DEFAULT_EXCLUDE", frozenset({"radio"}))
    _registry(devkit, 'projects = ["devkit", "radio"]\n')
    assert pr.all_projects(devkit) == ["devkit", "radio"]


def test_a_box_reads_its_own_copy_of_the_registry(tmp_path):
    root, _ = _tree(tmp_path)
    box = root / sweep.BOXES_DIR_NAME / "devkit--topic"
    box.mkdir(parents=True)
    _registry(box, 'projects = ["devkit", "radio"]\n')
    assert pr.all_projects(box) == ["devkit", "radio"]


def test_a_missing_registry_file_is_an_error_naming_it_even_with_a_live_file(tmp_path):
    """No silent empty registry, and no fallback to the retired live file: a checkout
    without `projects.toml` is broken, and "no projects" would read as "nothing to do"."""
    root, devkit = _tree(tmp_path)
    _live(root, ["devkit", "radio"])
    with pytest.raises(pr.RegistryError, match=r"no projects\.toml at .*projects\.toml"):
        pr.all_projects(devkit)
    with pytest.raises(pr.RegistryError, match=r"no projects\.toml"):
        pr.projects(repo_root=devkit)


def test_a_malformed_registry_file_raises(tmp_path):
    root, devkit = _tree(tmp_path)
    _registry(devkit, "projects = [\n")
    _live(root, ["devkit"])
    with pytest.raises(pr.RegistryError):
        pr.all_projects(devkit)


def test_projects_are_the_registered_ones_checked_out_here(tmp_path):
    root, devkit = _tree(tmp_path)
    (root / "radio").mkdir()
    (root / "stray").mkdir()
    (root / "notadir").write_text("", encoding="utf-8")
    _registry(devkit, 'projects = ["radio", "absent", "devkit", "notadir"]\n')
    assert pr.projects(repo_root=devkit) == ["radio", "devkit"]


def test_projects_can_be_asked_of_another_root(tmp_path):
    _, devkit = _tree(tmp_path)
    other = tmp_path / "other"
    (other / "absent").mkdir(parents=True)
    _registry(devkit, 'projects = ["devkit", "absent"]\n')
    assert pr.projects(other, repo_root=devkit) == ["absent"]


def test_a_pc_holding_none_of_them_has_none(tmp_path):
    _, devkit = _tree(tmp_path)
    _registry(devkit, 'projects = ["radio"]\n')
    assert pr.projects(repo_root=devkit) == []


# --- settings --------------------------------------------------------------------------


def _machine(root: Path, values: dict) -> None:
    (root / pr.MACHINE_FILE_NAME).write_text(json.dumps(values), encoding="utf-8")


def test_a_machine_key_is_read_from_the_machine_file(tmp_path):
    root, devkit = _tree(tmp_path)
    _machine(root, {"devkit.fixPass": "dispatch"})
    _live(root, ["devkit"], {"devkit.fixPass": "off"})
    assert pr.setting("devkit.fixPass", repo_root=devkit) == "dispatch"


def test_a_machine_key_the_file_lacks_is_the_default_whatever_the_live_file_says(tmp_path):
    root, devkit = _tree(tmp_path)
    _machine(root, {"devkit.fixPass": "plan"})
    _live(root, ["devkit"], {"devkit.onHold": ["radio"]})
    assert pr.setting("devkit.onHold", [], repo_root=devkit) == []


def test_without_a_machine_file_a_machine_key_is_the_default(tmp_path):
    """A PC that never set anything; the live file is `migrate_machine_settings`'s alone."""
    root, devkit = _tree(tmp_path)
    _live(root, ["devkit"], {"devkit.machineRole": "worker"})
    assert pr.setting("devkit.machineRole", repo_root=devkit) is None


def test_a_machine_key_is_never_read_from_the_shared_file(tmp_path):
    _, devkit = _tree(tmp_path)
    _registry(devkit, '[settings]\n"devkit.fixPass" = "dispatch"\n')
    assert pr.setting("devkit.fixPass", "off", repo_root=devkit) == "off"


def test_a_shared_key_is_read_from_the_registry_file(tmp_path):
    root, devkit = _tree(tmp_path)
    _registry(devkit, '[settings]\n"devkit.installers" = {"x.py" = ["--merge"]}\n')
    _live(root, ["devkit"], {"devkit.installers": {"x.py": []}})
    assert pr.setting("devkit.installers", repo_root=devkit) == {"x.py": ["--merge"]}


def test_a_shared_key_is_never_read_from_the_machine_file(tmp_path):
    root, devkit = _tree(tmp_path)
    _registry(devkit, 'projects = ["devkit"]\n')
    _machine(root, {"devkit.collectors": {"x": {}}})
    assert pr.setting("devkit.collectors", "unset", repo_root=devkit) == "unset"


def test_a_shared_key_the_registry_lacks_is_the_default_whatever_the_live_file_says(tmp_path):
    root, devkit = _tree(tmp_path)
    _registry(devkit, 'projects = ["devkit"]\n[settings]\n')
    _live(root, ["devkit"], {"devkit.remoteControl": {"projects": "all"}})
    assert pr.setting("devkit.remoteControl", repo_root=devkit) is None


def test_a_shared_key_with_no_registry_file_raises_naming_it(tmp_path):
    root, devkit = _tree(tmp_path)
    _live(root, ["devkit"], {"devkit.collectors": {}})
    with pytest.raises(pr.RegistryError, match=r"no projects\.toml"):
        pr.setting("devkit.collectors", repo_root=devkit)


def test_a_setting_nothing_sets_is_the_default(tmp_path):
    _, devkit = _tree(tmp_path)
    _registry(devkit, 'projects = ["devkit"]\n')
    assert pr.setting("devkit.onHold", [], repo_root=devkit) == []
    assert pr.setting("devkit.collectors", repo_root=devkit) is None
    assert pr.setting("devkit.reapStale", {"days": 3}, repo_root=devkit) == {"days": 3}


def test_a_stored_falsy_value_is_not_the_default(tmp_path):
    root, devkit = _tree(tmp_path)
    _machine(root, {"devkit.machineRole": ""})
    assert pr.setting("devkit.machineRole", "worker", repo_root=devkit) == ""


def test_a_malformed_machine_file_raises_rather_than_falls_back(tmp_path):
    root, devkit = _tree(tmp_path)
    (root / pr.MACHINE_FILE_NAME).write_text("{", encoding="utf-8")
    with pytest.raises(pr.RegistryError):
        pr.setting("devkit.fixPass", repo_root=devkit)


def test_the_machine_keys_are_spelled_as_their_owners_spell_them():
    assert pr.MACHINE_KEYS == {fix_cycle.SETTING, sweep.ON_HOLD_SETTING, machine_role.SETTING}


def test_the_watchdogs_pattern_reads_the_machine_file_unchanged():
    found = fix_switch.MODE.search(json.dumps({fix_cycle.SETTING: "dispatch"}))
    assert found and found.group(1) == "dispatch"


def test_a_root_handed_in_locates_the_machine_file(tmp_path):
    """A `--root` the caller was given, not the one `repo_root` resolves to."""
    _, devkit = _tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _machine(elsewhere, {"devkit.onHold": ["b"]})
    assert pr.setting("devkit.onHold", repo_root=devkit, root=elsewhere) == ["b"]
    assert pr.setting("devkit.onHold", [], repo_root=devkit) == []


def test_a_deprecated_workspace_handed_in_names_only_a_directory(tmp_path):
    """The file a `--workspace` names is never read; only where it would be counts."""
    _, devkit = _tree(tmp_path)
    _registry(devkit, 'projects = ["devkit"]\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    handed = _live(elsewhere, [], {"devkit.onHold": ["a"], "devkit.collectors": {"x": {}}})
    assert pr.setting("devkit.onHold", repo_root=devkit, workspace=handed) is None
    assert pr.setting("devkit.collectors", repo_root=devkit, workspace=handed) is None
    _machine(elsewhere, {"devkit.onHold": ["b"]})
    assert pr.setting("devkit.onHold", repo_root=devkit, workspace=handed) == ["b"]
    absent = tmp_path / "gone" / sweep.WORKSPACE_FILE_NAME
    assert pr.setting("devkit.onHold", "none", repo_root=devkit, workspace=absent) == "none"


# --- writing the machine file -----------------------------------------------------------


def test_write_machine_replaces_the_file_and_leaves_no_temporary_behind(tmp_path):
    path = tmp_path / pr.MACHINE_FILE_NAME
    pr.write_machine(path, {"devkit.fixPass": "plan"})
    pr.write_machine(path, {"devkit.machineRole": "worker"})
    assert pr.parse_machine(path.read_text(encoding="utf-8")) == {"devkit.machineRole": "worker"}
    assert [p.name for p in tmp_path.iterdir()] == [pr.MACHINE_FILE_NAME]


def test_set_machine_setting_keeps_the_other_keys(tmp_path):
    root, devkit = _tree(tmp_path)
    _machine(root, {"devkit.fixPass": "dispatch"})
    path = pr.set_machine_setting("devkit.machineRole", "worker", repo_root=devkit)
    assert path == pr.machine_path(devkit)
    assert pr.setting("devkit.machineRole", repo_root=devkit) == "worker"
    assert pr.setting("devkit.fixPass", repo_root=devkit) == "dispatch"


def test_set_machine_setting_writes_where_root_says(tmp_path):
    _, devkit = _tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = pr.set_machine_setting("devkit.fixPass", "plan", repo_root=devkit, root=elsewhere)
    assert path == elsewhere / pr.MACHINE_FILE_NAME
    assert not pr.machine_path(devkit).exists()


def test_set_machine_setting_refuses_a_shared_key_and_a_malformed_file(tmp_path):
    root, devkit = _tree(tmp_path)
    with pytest.raises(ValueError, match="not a machine setting"):
        pr.set_machine_setting("devkit.collectors", {}, repo_root=devkit)
    (root / pr.MACHINE_FILE_NAME).write_text("{", encoding="utf-8")
    with pytest.raises(pr.RegistryError):
        pr.set_machine_setting("devkit.fixPass", "off", repo_root=devkit)
    assert (root / pr.MACHINE_FILE_NAME).read_text(encoding="utf-8") == "{"


def test_migrate_copies_only_machine_keys_the_machine_file_lacks(tmp_path):
    root, devkit = _tree(tmp_path)
    live = {
        "devkit.fixPass": "plan",
        "devkit.onHold": [],
        "devkit.machineRole": "worker",
        "devkit.collectors": {},
    }
    _live(root, ["devkit"], live)
    _machine(root, {"devkit.machineRole": "workstation"})
    assert pr.migrate_machine_settings(repo_root=devkit) == ["devkit.fixPass", "devkit.onHold"]
    assert pr.parse_machine(pr.machine_path(devkit).read_text(encoding="utf-8")) == {
        "devkit.fixPass": "plan",
        "devkit.onHold": [],
        "devkit.machineRole": "workstation",
    }
    assert pr.migrate_machine_settings(repo_root=devkit) == [], "one-shot"


def test_migrate_with_nothing_to_copy_writes_nothing(tmp_path):
    root, devkit = _tree(tmp_path)
    assert pr.migrate_machine_settings(repo_root=devkit) == []
    _live(root, ["devkit"], {"devkit.collectors": {}})
    assert pr.migrate_machine_settings(repo_root=devkit) == []
    assert not pr.machine_path(devkit).exists()


def test_migrate_reads_the_live_file_in_the_root_it_is_handed(tmp_path):
    _, devkit = _tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _live(elsewhere, [], {"devkit.fixPass": "plan"})
    assert pr.migrate_machine_settings(repo_root=devkit, root=elsewhere) == ["devkit.fixPass"]
    assert json.loads((elsewhere / pr.MACHINE_FILE_NAME).read_text(encoding="utf-8")) == {
        "devkit.fixPass": "plan"
    }


def test_migrate_leaves_a_malformed_machine_file_as_found(tmp_path):
    root, devkit = _tree(tmp_path)
    _live(root, ["devkit"], {"devkit.fixPass": "plan"})
    (root / pr.MACHINE_FILE_NAME).write_text("[]", encoding="utf-8")
    with pytest.raises(pr.RegistryError):
        pr.migrate_machine_settings(repo_root=devkit)
    assert (root / pr.MACHINE_FILE_NAME).read_text(encoding="utf-8") == "[]"


# --- the committed registry ------------------------------------------------------------


def test_the_committed_registry_agrees_with_the_canonical_workspace():
    """While both exist, a project registered in one and not the other is a drift the
    migration would ship: readers switched to `projects.toml` would lose it."""
    registry = pr.parse_registry((REPO_ROOT / pr.REGISTRY_NAME).read_text(encoding="utf-8"))
    canonical = (REPO_ROOT / "workspace.jsonc").read_text(encoding="utf-8")
    assert registry == sweep.parse_workspace(canonical, frozenset())


def test_the_committed_registry_holds_devkit_and_parses_its_settings():
    text = (REPO_ROOT / pr.REGISTRY_NAME).read_text(encoding="utf-8")
    assert "devkit" in pr.all_projects(REPO_ROOT)
    assert isinstance(pr.parse_shared_settings(text), dict)


# The settings every reader now takes from `projects.toml`. Until `workspace.jsonc` is
# deleted it is rendered into each PC's live file and still carries them, so a value
# changed in one file and not the other would read differently on a PC whose live file
# answers a key `projects.toml` lacks.
SHARED_KEYS = (
    "devkit.collectors",
    "devkit.remoteControl",
    "devkit.localE2e",
    "devkit.installers",
    "devkit.reapStale",
    "devkit.dashboards",
)


def test_the_committed_shared_settings_agree_with_the_canonical_workspace():
    shared = pr.parse_shared_settings((REPO_ROOT / pr.REGISTRY_NAME).read_text(encoding="utf-8"))
    canonical = pr.workspace_settings((REPO_ROOT / "workspace.jsonc").read_text(encoding="utf-8"))
    for key in SHARED_KEYS:
        assert shared.get(key) == canonical.get(key), key


def test_the_committed_registry_holds_no_machine_setting():
    shared = pr.parse_shared_settings((REPO_ROOT / pr.REGISTRY_NAME).read_text(encoding="utf-8"))
    assert not pr.MACHINE_KEYS & shared.keys()
    assert set(shared) <= set(SHARED_KEYS)
