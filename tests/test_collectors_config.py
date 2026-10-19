"""`collectors_config.py`: which collectors exist, and what this machine was told.

The property worth pinning is the default. A machine with no assignment is **hands off**,
and so is one whose assignment file is missing, corrupt or misspelt -- the direction that
neither starts a second writer on a laptop nor stops the only one on the desktop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import REPO_ROOT, devkit_jsonc, load_script

config = load_script("scripts/collectors_config.py")


def workspace(settings: dict) -> str:
    return "// a comment, as the real file has\n" + json.dumps(
        {"folders": [], "settings": settings}
    )


# --- the workspace setting -------------------------------------------------------


def test_a_declared_collector_is_read_with_its_health_command():
    text = workspace(
        {config.SETTING: {"ibkr_trader": {"service": "app", "health": ["ibkr-trader", "health"]}}}
    )
    found, notes = config.parse_setting(text)
    assert found == [config.Collector("ibkr_trader", "app", ("ibkr-trader", "health"))]
    assert notes == []


def test_health_is_optional():
    found, _ = config.parse_setting(workspace({config.SETTING: {"p": {"service": "s"}}}))
    assert found == [config.Collector("p", "s", ())]
    assert found[0].settle == config.DEFAULT_SETTLE


@pytest.mark.parametrize("settle", [0, 180])
def test_a_container_collector_may_set_its_settle(settle):
    text = workspace({config.SETTING: {"p": {"service": "s", "settle": settle}}})
    found, notes = config.parse_setting(text)
    assert found == [config.Collector("p", "s", (), settle=settle)] and notes == []


def test_a_container_collector_may_name_the_checkouts_its_image_copies():
    text = workspace({config.SETTING: {"p": {"service": "s", "buildsFrom": ["data-lake"]}}})
    found, notes = config.parse_setting(text)
    assert found == [config.Collector("p", "s", (), builds_from=("data-lake",))] and notes == []


def test_the_workspace_declares_what_ibkr_trader_s_image_copies():
    """Its Dockerfile `COPY`s the sibling data-lake checkout (110defb0)."""
    found, _notes = config.parse_setting(
        (REPO_ROOT / "workspace.jsonc").read_text(encoding="utf-8")
    )
    [ibkr] = [c for c in found if c.project == "ibkr_trader"]
    assert ibkr.builds_from == ("data-lake",)


def test_a_container_collector_may_declare_its_handoff():
    handoff = ["uv", "run", "python", "scripts/handoff.py"]
    text = workspace({config.SETTING: {"p": {"service": "s", "handoff": handoff}}})
    found, notes = config.parse_setting(text)
    assert found == [config.Collector("p", "s", (), handoff=tuple(handoff))] and notes == []


def test_no_handoff_is_a_collector_moved_by_hand():
    found, _ = config.parse_setting(workspace({config.SETTING: {"p": {"service": "s"}}}))
    assert found[0].handoff == ()
    found, _ = config.parse_setting(
        workspace({config.SETTING: {"p": {"command": ["x"], "minutes": 5}}})
    )
    assert found[0].handoff == ()


def test_a_scheduled_collector_may_declare_its_handoff():
    """Every collector must be movable between the two PCs, social-scraper included: its
    task is removed for the export rather than a service stopped (`collectors.stop_task`)."""
    handoff = ["uv", "run", "python", "scripts/handoff.py"]
    entry = {"command": ["uv", "run", "s"], "minutes": 30, "needs": ["db"], "handoff": handoff}
    found, notes = config.parse_setting(workspace({config.SETTING: {"s": entry}}))
    assert notes == []
    assert found == [
        config.Collector(
            "s", command=("uv", "run", "s"), minutes=30, needs=("db",), handoff=tuple(handoff)
        )
    ]


def test_the_workspace_declares_a_handoff_for_every_collector():
    """The user may keep neither PC, so each collector has to be movable, data and all."""
    found, _notes = config.parse_setting(
        (REPO_ROOT / "workspace.jsonc").read_text(encoding="utf-8")
    )
    by_name = {c.project: c.handoff for c in found}
    plain = ("uv", "run", "python", "scripts/handoff.py")
    assert by_name["social-scraper"] == plain, "boto3 is a core dependency there"
    assert all(by_name.values()), [name for name, argv in by_name.items() if not argv]


def test_the_workspace_s_sports_betting_handoff_brings_the_sibling_data_lake():
    """7fb217a7: its pyproject declares data-lake in no form, so a hand install into the
    venv was the only thing holding the store's import up -- missing on the second PC,
    and removed by any `uv sync`, so `pending` exited 2 there on every pass."""
    found, _notes = config.parse_setting(
        (REPO_ROOT / "workspace.jsonc").read_text(encoding="utf-8")
    )
    [sports] = [c for c in found if c.project == "sports_betting"]
    assert sports.handoff == (
        "uv",
        "run",
        "--with-editable",
        "../data-lake[archive]",
        "python",
        "scripts/handoff.py",
    )


def test_the_workspace_declares_ibkr_trader_s_handoff_with_the_extra_boto3_needs():
    found, _notes = config.parse_setting(
        (REPO_ROOT / "workspace.jsonc").read_text(encoding="utf-8")
    )
    [ibkr] = [c for c in found if c.project == "ibkr_trader"]
    assert ibkr.handoff == ("uv", "run", "--extra", "archive", "python", "scripts/handoff.py")


def test_no_setting_declares_nothing_and_says_nothing():
    assert config.parse_setting(workspace({})) == ([], [])


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("app", "expected an object"),
        ({}, "no `service`"),
        ({"service": ""}, "no `service`"),
        ({"service": "app", "health": "ibkr-trader health"}, "list of strings"),
        ({"service": "app", "health": ["ok", 3]}, "list of strings"),
        ({"service": "app", "settle": -1}, "0 or more"),
        ({"service": "app", "settle": "60"}, "0 or more"),
        ({"service": "app", "settle": True}, "0 or more"),
        ({"service": "app", "buildsFrom": "data-lake"}, "sibling checkout names"),
        ({"service": "app", "buildsFrom": ["../data-lake"]}, "sibling checkout names"),
        ({"command": [], "minutes": 30}, "non-empty list"),
        ({"command": "uv run x", "minutes": 30}, "non-empty list"),
        ({"command": ["uv"]}, "positive whole number"),
        ({"command": ["uv"], "minutes": 0}, "positive whole number"),
        ({"command": ["uv"], "minutes": "30"}, "positive whole number"),
        ({"command": ["uv"], "minutes": True}, "positive whole number"),
        ({"command": ["uv"], "minutes": 30, "needs": "db"}, "compose service names"),
        ({"command": ["uv"], "minutes": 30, "service": "app"}, "one kind"),
        ({"service": "app", "handoff": "uv run python scripts/handoff.py"}, "list of strings"),
        ({"service": "app", "handoff": ["uv", ""]}, "list of strings"),
        ({"service": "app", "handoff": ["uv", 3]}, "list of strings"),
        ({"command": ["uv"], "minutes": 30, "handoff": "uv run x"}, "list of strings"),
        ({"command": ["uv"], "minutes": 30, "handoff": ["uv", ""]}, "list of strings"),
    ],
)
def test_a_malformed_entry_is_a_note_not_silence(entry, reason):
    """Hand-edited: an entry that silently declared nothing would read, on the machine
    meant to run it, exactly like a collector nobody asked for."""
    found, notes = config.parse_setting(
        workspace({config.SETTING: {"bad": entry, "good": {"service": "s"}}})
    )
    assert [c.project for c in found] == ["good"]
    assert len(notes) == 1 and notes[0].startswith("bad:") and reason in notes[0]


def test_a_scheduled_collector_is_read_with_its_cadence_and_needs():
    text = workspace(
        {
            config.SETTING: {
                "social-scraper": {
                    "command": ["uv", "run", "social-scraper", "scrape"],
                    "minutes": 30,
                    "needs": ["db"],
                }
            }
        }
    )
    (found,), notes = config.parse_setting(text)
    assert notes == []
    assert found == config.Collector(
        "social-scraper",
        command=("uv", "run", "social-scraper", "scrape"),
        minutes=30,
        needs=("db",),
    )
    assert found.scheduled and not config.Collector("p", "s").scheduled


def test_a_scheduled_collector_may_not_take_devkits_task_namespace():
    """Its task is named after it, and a `devkit-` task is read as one of devkit's own
    jobs -- by the fix pass too, which would file the project's failures as devkit's."""
    found, notes = config.parse_setting(
        workspace({config.SETTING: {"devkit-scraper": {"command": ["x"], "minutes": 5}}})
    )
    assert found == [] and "may not start with `devkit-`" in notes[0]


def test_a_setting_of_the_wrong_shape_is_reported():
    found, notes = config.parse_setting(workspace({config.SETTING: ["ibkr_trader"]}))
    assert found == [] and "keyed by project" in notes[0]


def test_an_unparseable_workspace_file_is_reported_rather_than_raised():
    assert config.parse_setting("{ not json") == ([], ["the workspace file does not parse"])


def test_a_missing_workspace_file_is_reported(tmp_path):
    found, notes = config.declared(tmp_path / "devkit")
    assert found == [] and "no workspace file" in notes[0]


def test_the_canonical_workspace_declares_the_collectors():
    """The declarations, read the way the job reads them."""
    text = (REPO_ROOT / "workspace.jsonc").read_text(encoding="utf-8")
    found, notes = config.parse_setting(text)
    assert notes == []
    containers = [c for c in found if not c.scheduled]
    assert {c.project: c.service for c in containers} == {
        "ibkr_trader": "app",
        "sports_betting": "collector",
    }
    assert all(c.health for c in containers)
    scheduled = {c.project: c for c in found if c.scheduled}
    assert set(scheduled) == {"social-scraper"}
    assert scheduled["social-scraper"].needs == ("db",)
    devkit_jsonc.loads(text)  # and the file as a whole still parses


# --- this machine's assignment ---------------------------------------------------


def test_a_machine_never_assigned_is_hands_off(tmp_path):
    assert config.load_assignment(tmp_path / "nothing.json") == {}


@pytest.mark.parametrize("body", ["{ torn", "[]", '"run"', '{"p": "Run"}', '{"p": true}'])
def test_an_unreadable_or_misspelt_assignment_is_hands_off(tmp_path, body):
    path = tmp_path / "a.json"
    path.write_text(body, encoding="utf-8")
    assert config.load_assignment(path) == {}


def test_an_assignment_round_trips(tmp_path):
    path = tmp_path / "logs" / "a.json"
    config.save_assignment(path, {"b": config.STOP, "a": config.RUN})
    assert config.load_assignment(path) == {"a": config.RUN, "b": config.STOP}


def test_assign_sets_and_releases():
    start = {"a": config.RUN}
    assert config.assign(start, ["b"], config.STOP) == {"a": config.RUN, "b": config.STOP}
    assert config.assign(start, ["a"], None) == {}
    assert start == {"a": config.RUN}, "assign must not mutate its input"


def test_assign_refuses_an_unknown_mode():
    with pytest.raises(ValueError, match="unknown mode"):
        config.assign({}, ["a"], "sometimes")


def test_pick_defaults_to_every_declared_collector_and_names_typos():
    declared = [config.Collector("a", "s"), config.Collector("b", "s")]
    assert config.pick(declared, []) == (declared, [])
    chosen, unknown = config.pick(declared, ["b", "typo"])
    assert [c.project for c in chosen] == ["b"] and unknown == ["typo"]


# --- where the answer lives ------------------------------------------------------


def test_home_prefers_devkit_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    assert config.home(Path("/elsewhere")) == tmp_path


def test_home_maps_a_box_to_its_static_checkout(tmp_path, monkeypatch):
    """`run-here` typed in a box must land where the scheduled job reads, not in a tree
    deleted when the box's PR merges."""
    monkeypatch.delenv("DEVKIT_DIR", raising=False)
    box = tmp_path / ".worktrees" / "devkit--topic-0929"
    assert config.home(box) == tmp_path / "devkit"


def test_the_assignment_lives_under_ignored_logs():
    """Machine-local is the whole point; a tracked path would follow the repo to every PC."""
    assert config.ASSIGNMENT.parts[0] == "logs"
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(line.strip().rstrip("/") in {"logs", "/logs"} for line in ignored)
