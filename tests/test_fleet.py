"""`fleet.py`: every PC's memory and collectors, and a move from whichever PC you are at.

The properties worth pinning are the ones that stop a move doing harm: a rebalance never
just transfers the shortage, never picks a collector that did not opt in, and a machine
that did not answer is never guessed about.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from support import REPO_ROOT, load_script

fleet = load_script("scripts/fleet.py")
config = load_script("scripts/collectors_config.py")

HERE = "desktop-here"
FAR = "desktop-far"
THIRD = "desktop-third"


def collector(project: str = "cheap", **fields: Any) -> Any:
    fields.setdefault("service", "app")
    fields.setdefault("handoff", ("uv", "run", "handoff"))
    return fleet.config.Collector(project, **fields)


def recorder(sent: list[Sequence[str]]) -> Callable[[Sequence[str]], int]:
    """A mover that runs nothing and keeps what it was asked to run."""

    def mover(argv: Sequence[str]) -> int:
        sent.append(argv)
        return 0

    return mover


def machine(host: str, free: float, runs: Sequence[str] = (), **fields: Any) -> Any:
    fields.setdefault("ready", frozenset({"cheap", "heavy"}))
    return fleet.Machine(
        host, available_gb=free, assignment=dict.fromkeys(runs, fleet.config.RUN), **fields
    )


@pytest.fixture
def here(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """This machine is `HERE`, its devkit checkout `<home>/vs-code/devkit`."""
    base = tmp_path / "vs-code" / "devkit"
    (base / "logs").mkdir(parents=True)
    monkeypatch.setattr(fleet.handoff, "this_host", lambda: HERE)
    monkeypatch.setattr(fleet.config, "home", lambda root: base)
    monkeypatch.setattr(fleet.Path, "home", staticmethod(lambda: tmp_path))
    return base


# --- what the tailnet says ---------------------------------------------------------


def test_tailnet_hosts_keeps_workstations_and_sorts_them_by_whether_they_are_up():
    status = {
        "Self": {"HostName": "DESKTOP-Here", "OS": "windows", "Online": True},
        "Peer": {
            "a": {"HostName": "desktop-far", "OS": "windows", "Online": True},
            "b": {"HostName": "desktop-off", "OS": "linux", "Online": False},
            "c": {"HostName": "my-phone", "OS": "android", "Online": True},
        },
    }
    assert fleet.tailnet_hosts(json.dumps(status)) == (
        ["desktop-far", "desktop-here"],
        ["desktop-off"],
    )


def test_tailnet_hosts_survives_a_tailnet_of_one():
    status = {"Self": {"HostName": "desktop-here", "OS": "windows", "Online": True}}
    assert fleet.tailnet_hosts(json.dumps(status)) == (["desktop-here"], [])


def test_container_gb_adds_a_project_s_containers_up_across_units():
    stats = "ibkr_trader-app-1 1.5GiB\nibkr_trader-db-1 512MiB\nsocial-scraper-db-1 1024KiB\n\n"
    assert fleet.container_gb(stats, ["ibkr_trader", "social-scraper"]) == pytest.approx(
        {"ibkr_trader": 2.0, "social-scraper": 1 / 1024}
    )


def test_container_gb_keeps_a_hyphenated_service_with_its_project():
    """Splitting the name made `ibkr_trader-ib` a project on the live machine."""
    stats = "ibkr_trader-ib-gateway-1 1GiB\nibkr_trader-db-1 1GiB"
    assert fleet.container_gb(stats, ["ibkr_trader"]) == {"ibkr_trader": 2.0}


def test_container_gb_ignores_what_is_not_a_collector():
    assert fleet.container_gb("productivity-tracker-db-1 1GiB", ["ibkr_trader"]) == {}


# --- asking a machine --------------------------------------------------------------


def test_probe_reports_memory_assignment_and_only_checkouts_that_could_run(
    here: Path, monkeypatch: pytest.MonkeyPatch
):
    (here / fleet.config.ASSIGNMENT).write_text('{"cheap": "run"}', encoding="utf-8")
    for project, files in {"cheap": (".git", ".env"), "heavy": (".git",)}.items():
        for name in files:
            (here.parent / project / name).mkdir(parents=True)
    declared = [collector("cheap"), collector("heavy")]
    monkeypatch.setattr(fleet.config, "declared", lambda root: (declared, []))
    monkeypatch.setattr(fleet.host_memory, "available_mb", lambda: 2048)

    said = fleet.probe(here, lambda argv, timeout: (0, "cheap-app-1 1GiB"))

    assert said == {
        "host": HERE,
        "available_gb": 2.0,
        "assignment": {"cheap": "run"},
        "measured_gb": {"cheap": 1.0},
        "ready": ["cheap"],
    }
    assert fleet.machine_from(said) == fleet.Machine(
        HERE, 2.0, {"cheap": "run"}, {"cheap": 1.0}, frozenset({"cheap"})
    )


def test_probe_with_docker_silent_measures_nothing(here: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fleet.config, "declared", lambda root: ([], []))
    said = fleet.probe(here, lambda argv, timeout: (1, "docker is not running"))
    assert said["measured_gb"] == {}


def test_remote_script_uses_the_same_place_under_the_other_machine_s_home(here: Path):
    argv = fleet.remote_script(FAR, here, "fleet.py", "probe")
    assert argv[: len(fleet.SSH)] == list(fleet.SSH)
    assert "BatchMode=yes" in argv
    assert argv[len(fleet.SSH) :] == [FAR, "python", "vs-code/devkit/scripts/fleet.py", "probe"]


def test_ask_gives_the_last_line_of_a_refusal_as_the_reason(here: Path):
    answer = "banner\nalexa@far: Permission denied (publickey)\n"
    assert fleet.ask(FAR, HERE, here, lambda argv, timeout: (255, answer)) == fleet.Machine(
        FAR, error="alexa@far: Permission denied (publickey)"
    )


def test_ask_with_no_output_says_how_ssh_exited(here: Path):
    assert fleet.ask(FAR, HERE, here, lambda argv, timeout: (255, "")).error == "ssh exited 255"


def test_ask_refuses_an_answer_that_is_not_a_probe(here: Path):
    assert "answered oddly" in fleet.ask(FAR, HERE, here, lambda a, t: (0, "{}")).error
    assert "answered oddly" in fleet.ask(FAR, HERE, here, lambda a, t: (0, "not json")).error


def test_survey_asks_the_online_and_reports_the_offline_without_trying_them(
    here: Path, monkeypatch: pytest.MonkeyPatch
):
    online = {"Online": True, "OS": "windows"}
    status = {
        "Self": {"HostName": HERE, **online},
        "Peer": {
            "a": {"HostName": FAR, **online},
            "b": {"HostName": THIRD, "OS": "windows", "Online": False},
        },
    }
    payload = {
        "host": FAR,
        "available_gb": 8.0,
        "assignment": {},
        "measured_gb": {},
        "ready": [],
    }
    monkeypatch.setattr(fleet, "probe", lambda root, run: {**payload, "host": HERE})
    calls: list[Sequence[str]] = []

    def run(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        calls.append(argv)
        if argv[0] == "tailscale":
            return 0, json.dumps(status)
        return 0, json.dumps(payload)

    machines = fleet.survey(here, run)

    assert [(m.host, m.error) for m in machines] == [(FAR, ""), (HERE, ""), (THIRD, "offline")]
    assert not any(THIRD in argv for argv in calls)


def test_survey_without_tailscale_is_this_machine(here: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        fleet,
        "probe",
        lambda root, run: {
            "host": HERE,
            "available_gb": 1.0,
            "assignment": {},
            "measured_gb": {},
            "ready": [],
        },
    )
    machines = fleet.survey(here, lambda argv, timeout: (127, "no tailscale"))
    assert [m.host for m in machines] == [HERE]


# --- what is wrong with the placement ----------------------------------------------


def test_footprint_is_the_larger_of_measured_and_declared():
    declared = collector(memory_gb=2.0)
    assert fleet.footprint(declared) == 2.0
    assert fleet.footprint(declared, machine(HERE, 1, measured_gb={"cheap": 3.0})) == 3.0
    assert fleet.footprint(declared, machine(HERE, 1, measured_gb={"cheap": 0.5})) == 2.0


def test_anomalies_name_two_writers():
    machines = [machine(HERE, 8, ["cheap"]), machine(FAR, 8, ["cheap"])]
    assert fleet.anomalies(machines, [collector()]) == [
        f"cheap is assigned to {HERE}, {FAR}: two writers"
    ]


def test_anomalies_name_a_collector_nothing_runs():
    assert fleet.anomalies([machine(HERE, 8)], [collector()]) == ["cheap runs nowhere"]


def test_anomalies_do_not_guess_about_a_machine_that_did_not_answer():
    machines = [machine(HERE, 8), fleet.Machine(FAR, error="offline")]
    assert fleet.anomalies(machines, [collector()]) == []


# --- what a rebalance picks --------------------------------------------------------


def test_plan_leaves_a_fleet_with_memory_alone():
    move, notes = fleet.plan([machine(HERE, 8, ["cheap"])], [collector(rebalance=True)])
    assert move is None
    assert notes == [f"no machine is below {fleet.FLOOR_GB:g} GB"]


def test_plan_moves_off_the_short_machine_to_the_one_with_most_room():
    machines = [machine(HERE, 1, ["cheap"]), machine(FAR, 6), machine(THIRD, 10)]
    move, _ = fleet.plan(machines, [collector(rebalance=True, memory_gb=1.0)])
    assert move == fleet.Move("cheap", HERE, THIRD, 1.0)


def test_plan_starts_with_the_shortest_machine():
    machines = [
        machine(FAR, 1.5, ["heavy"]),
        machine(HERE, 0.5, ["cheap"]),
        machine(THIRD, 10),
    ]
    collectors = [collector("cheap", rebalance=True), collector("heavy", rebalance=True)]
    move, _ = fleet.plan(machines, collectors)
    assert move is not None
    assert move.source == HERE


def test_plan_takes_the_largest_collector_it_may_move():
    machines = [machine(HERE, 1, ["cheap", "heavy"]), machine(FAR, 10)]
    collectors = [
        collector("cheap", rebalance=True, memory_gb=0.5),
        collector("heavy", rebalance=True, memory_gb=2.0),
    ]
    move, _ = fleet.plan(machines, collectors)
    assert move is not None
    assert move.project == "heavy"


def test_plan_never_picks_a_collector_that_did_not_opt_in():
    machines = [machine(HERE, 1, ["cheap"]), machine(FAR, 10)]
    move, notes = fleet.plan(machines, [collector(rebalance=False)])
    assert move is None
    assert notes == [f"{HERE} is short, but runs nothing `rebalance` may move"]


def test_plan_will_not_just_transfer_the_shortage():
    machines = [machine(HERE, 1, ["cheap"]), machine(FAR, 4.5)]
    move, notes = fleet.plan(machines, [collector(rebalance=True, memory_gb=1.0)])
    assert move is None
    assert "has nowhere to go" in notes[0]


def test_plan_counts_exactly_the_margin_as_enough():
    machines = [machine(HERE, 1, ["cheap"]), machine(FAR, 5)]
    move, _ = fleet.plan(machines, [collector(rebalance=True, memory_gb=1.0)])
    assert move is not None


def test_plan_needs_the_project_s_checkout_at_the_destination():
    machines = [machine(HERE, 1, ["cheap"]), machine(FAR, 10, ready=frozenset())]
    move, _ = fleet.plan(machines, [collector(rebalance=True)])
    assert move is None


def test_plan_will_not_send_anything_to_a_machine_that_did_not_answer():
    machines = [machine(HERE, 1, ["cheap"]), fleet.Machine(FAR, 10, error="offline")]
    move, _ = fleet.plan(machines, [collector(rebalance=True)])
    assert move is None


# --- moving, from anywhere ---------------------------------------------------------


def test_owner_is_the_one_machine_running_it():
    assert fleet.owner("cheap", [machine(HERE, 8), machine(FAR, 8, ["cheap"])]) == (FAR, "")


def test_owner_refuses_two_writers():
    source, why = fleet.owner("cheap", [machine(HERE, 8, ["cheap"]), machine(FAR, 8, ["cheap"])])
    assert source == ""
    assert "settle that first" in why


def test_owner_names_who_it_did_not_hear_from():
    source, why = fleet.owner("cheap", [machine(HERE, 8), fleet.Machine(FAR, error="offline")])
    assert source == ""
    assert FAR in why


def test_owner_says_when_nothing_runs_it():
    assert fleet.owner("cheap", [machine(HERE, 8)]) == ("", "no machine runs cheap")


@pytest.mark.parametrize(
    ("target", "said"),
    [
        (machine(FAR, 8, ready=frozenset()), "no checkout of cheap"),
        (fleet.Machine(FAR, error="offline"), "cannot take it: offline"),
        (machine(THIRD, 8), "not on the tailnet"),
    ],
)
def test_refusal_stops_a_move_the_destination_could_not_pick_up(target, said):
    assert said in fleet.refusal("cheap", FAR, [target])


def test_refusal_passes_a_destination_that_can_take_it():
    assert fleet.refusal("cheap", FAR, [machine(FAR, 8)]) == ""


def test_move_argv_runs_here_when_this_machine_owns_it(here: Path):
    argv = fleet.move_argv("cheap", HERE, FAR, HERE, here)
    assert argv[0] == sys.executable
    assert argv[2:] == ["move", "cheap", "--to", FAR]


def test_move_argv_goes_over_ssh_to_the_owner(here: Path):
    argv = fleet.move_argv("cheap", FAR, HERE, HERE, here)
    assert argv[len(fleet.SSH) :] == [
        FAR,
        "python",
        "vs-code/devkit/scripts/collectors.py",
        "move",
        "cheap",
        "--to",
        HERE,
    ]


def test_move_runs_collectors_move_on_the_owner(here: Path):
    sent: list[Sequence[str]] = []
    machines = [machine(HERE, 8), machine(FAR, 8, ["cheap"])]

    assert fleet.move("cheap", HERE, machines, here, recorder(sent)) == 0

    assert sent[0][-5:] == [
        "vs-code/devkit/scripts/collectors.py",
        "move",
        "cheap",
        "--to",
        HERE,
    ]


def test_move_accepts_a_host_as_a_person_types_it(here: Path):
    sent: list[Sequence[str]] = []
    machines = [machine(HERE, 8, ["cheap"]), machine(FAR, 8)]
    typed = "DESKTOP-FAR.tail1234.ts.net"

    assert fleet.move("cheap", typed, machines, here, recorder(sent)) == 0

    assert sent[0][-1] == FAR


def test_move_refuses_a_machine_that_already_runs_it(
    here: Path, capsys: pytest.CaptureFixture[str]
):
    sent: list[Sequence[str]] = []
    machines = [machine(HERE, 8, ["cheap"])]

    assert fleet.move("cheap", HERE, machines, here, recorder(sent)) == 1

    assert sent == []
    assert "already runs" in capsys.readouterr().err


def test_move_runs_nothing_when_the_destination_cannot_take_it(here: Path):
    sent: list[Sequence[str]] = []
    machines = [machine(HERE, 8, ["cheap"]), fleet.Machine(FAR, error="offline")]

    assert fleet.move("cheap", FAR, machines, here, recorder(sent)) == 1

    assert sent == []


def test_rebalance_says_what_it_would_do_and_does_nothing_without_apply(
    here: Path, capsys: pytest.CaptureFixture[str]
):
    sent: list[Sequence[str]] = []
    machines = [machine(HERE, 1, ["cheap"]), machine(FAR, 10)]
    collectors = [collector(rebalance=True, memory_gb=1.0)]

    assert fleet.rebalance(machines, collectors, False, here, recorder(sent)) == 0

    assert sent == []
    assert f"cheap from {HERE} to {FAR}" in capsys.readouterr().out


def test_rebalance_applies_one_move(here: Path):
    sent: list[Sequence[str]] = []
    machines = [machine(HERE, 1, ["cheap"]), machine(FAR, 10)]
    collectors = [collector(rebalance=True)]

    assert fleet.rebalance(machines, collectors, True, here, recorder(sent)) == 0

    assert len(sent) == 1


def test_rebalance_with_no_move_says_so(here: Path, capsys: pytest.CaptureFixture[str]):
    assert fleet.rebalance([machine(HERE, 8)], [], True, here, lambda a: 1) == 0
    assert "nothing to move" in capsys.readouterr().out


# --- the command line --------------------------------------------------------------


def test_render_puts_the_shortest_first_and_the_silent_last():
    lines = fleet.render(
        [
            fleet.Machine(THIRD, error="offline"),
            machine(FAR, 8, ["cheap"]),
            machine(HERE, 1),
        ],
        [collector()],
    )
    assert HERE in lines[0]
    assert "SHORT" in lines[0]
    assert "cheap" in lines[1]
    assert "unreachable: offline" in lines[2]


def test_render_adds_what_is_wrong():
    assert fleet.render([machine(HERE, 8)], [collector()])[-1] == "problem: cheap runs nowhere"


def test_parse_args_reads_each_verb():
    assert fleet.parse_args([]).verb is None
    assert fleet.parse_args(["rebalance", "--apply"]).apply is True
    moving = fleet.parse_args(["move", "cheap", "--to", FAR])
    assert (moving.project, moving.to) == ("cheap", FAR)


def test_main_probe_prints_json(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(fleet, "probe", lambda: {"host": HERE})
    assert fleet.main(["probe"]) == 0
    assert json.loads(capsys.readouterr().out) == {"host": HERE}


def test_main_prints_the_table(monkeypatch: pytest.MonkeyPatch, capsys, here: Path):
    monkeypatch.setattr(fleet.config, "declared", lambda root: ([], ["a bad entry"]))
    monkeypatch.setattr(fleet, "survey", lambda: [machine(HERE, 8)])
    assert fleet.main([]) == 0
    said = capsys.readouterr()
    assert HERE in said.out
    assert "a bad entry" in said.err


def test_main_dispatches_move_and_rebalance(monkeypatch: pytest.MonkeyPatch, here: Path):
    monkeypatch.setattr(fleet.config, "declared", lambda root: ([], []))
    monkeypatch.setattr(fleet, "survey", lambda: [])
    monkeypatch.setattr(fleet, "move", lambda project, to, machines: 3)
    monkeypatch.setattr(fleet, "rebalance", lambda machines, collectors, apply: 4)
    assert fleet.main(["move", "cheap", "--to", FAR]) == 3
    assert fleet.main(["rebalance"]) == 4


def test_spawn_returns_stdout_on_success_and_stderr_on_failure():
    good = [sys.executable, "-c", "print('out')"]
    bad = [sys.executable, "-c", "import sys; sys.stderr.write('why'); sys.exit(3)"]
    assert fleet.spawn(good, 30) == (0, "out\n")
    assert fleet.spawn(bad, 30) == (3, "why")


def test_spawn_reports_a_missing_program():
    assert fleet.spawn(["no-such-program-here"], 5)[0] == 124


def test_inherit_returns_the_exit_code(monkeypatch: pytest.MonkeyPatch):
    done: subprocess.CompletedProcess[str] = subprocess.CompletedProcess([], 7)
    monkeypatch.setattr(fleet.subprocess, "run", lambda argv, check: done)
    assert fleet.inherit(["x"]) == 7


# --- the declaration ---------------------------------------------------------------


def test_the_workspace_lets_a_rebalance_pick_only_the_collector_whose_move_is_cheap():
    text = (REPO_ROOT / "workspace.jsonc").read_text(encoding="utf-8")
    declared, _notes = config.parse_setting(text)
    assert [c.project for c in declared if c.rebalance] == ["sports_betting"]
