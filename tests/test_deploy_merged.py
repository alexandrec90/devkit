"""`scripts/deploy_merged.py`: what merged is installed within the half hour, idle or not."""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deploy_merged as dm

NOON = _dt.datetime(2026, 11, 27, 12, 0)
TIP = "b72fa83aaaaabbbbccccdddd"


def _last(status: str, sha: str = TIP, hours: float = 2.0) -> dm.Deployed:
    finished = (NOON - _dt.timedelta(hours=hours)).isoformat()
    return dm.Deployed("tvshell", sha, status, finished)


@pytest.mark.parametrize(
    ("last", "due"),
    [
        (None, True),
        (_last(dm.PASSED, sha="old"), True),
        (_last(dm.FAILED, sha="old", hours=0.1), True),
        (_last(dm.PASSED), False),
        (_last(dm.PASSED, hours=100), False),
        (_last(dm.FAILED, hours=0.5), False),
        (_last(dm.FAILED, hours=1.5), True),
        (dm.Deployed("tvshell", TIP, dm.FAILED, "not a time"), True),
    ],
)
def test_a_tip_is_deployed_once_and_a_red_one_retried_after_an_hour(last, due):
    assert dm.is_due(last, TIP, NOON) is due
    assert not dm.is_due(last, "", NOON), "origin could not be read"


def fake_git(*, fetch: int = 0, contract: bool = True, tip: str = TIP, log=None):
    def git(*args):
        if log is not None:
            log.append(args)
        out, code = "", 0
        if args[0] == "fetch":
            code = fetch
        elif args[0] == "symbolic-ref":
            out = "refs/remotes/origin/main"
        elif args[0] == "rev-parse":
            out, code = tip, 0 if tip else 1
        elif args[0] == "cat-file":
            code = 0 if contract else 1
        return subprocess.CompletedProcess(args, code, f"{out}\n", "fatal: said so" if code else "")

    return git


class Runner:
    """Records each argv; the contract's exit code is `code`, everything else succeeds."""

    def __init__(self, code: int = 0, cut: int = 0):
        self.code, self.cut = code, cut
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, _timeout, **kwargs):
        self.calls.append((list(argv), kwargs))
        code = 0
        if "add" in argv:
            code = self.cut
        elif argv[-1].endswith(dm.CONTRACT.split("/")[-1]):
            code = self.code
        return subprocess.CompletedProcess(argv, code, "installed\n", "")

    def contract_runs(self) -> list[tuple[list[str], dict]]:
        return [(a, k) for a, k in self.calls if a[-1].endswith("deploy.py")]


def _deploy(tmp_path: Path, runner: Runner, **git) -> dm.Deployed | None:
    checkout = tmp_path / "tvshell"
    checkout.mkdir(exist_ok=True)
    return dm.deploy_project(
        "tvshell",
        checkout,
        root=tmp_path / "devkit",
        git_for=lambda _p: fake_git(**git),
        runner=runner,
        now=lambda: NOON,
    )


def test_origins_tip_is_deployed_from_a_tree_of_its_own_and_recorded(tmp_path):
    runner = Runner()
    result = _deploy(tmp_path, runner)
    assert result == dm.Deployed("tvshell", TIP, dm.PASSED, NOON.isoformat(timespec="seconds"))

    def verb(argv: list[str]) -> str:
        return argv[4] if argv[:1] == ["git"] else "contract"

    assert [verb(argv) for argv, _ in runner.calls] == ["add", "contract", "remove"]
    (_argv, kwargs) = runner.contract_runs()[0]
    tree = dm.path_for(tmp_path / "tvshell", TIP)
    assert tree.name == f"{dm.PREFIX}{TIP[:12]}"
    assert kwargs["cwd"] == str(tree), "never the person's checkout"
    assert kwargs["env"][dm.ENV_FLAG] == "1"
    assert dm.read("tvshell", tmp_path / "devkit") == result
    log = dm.result_path("tvshell", tmp_path / "devkit").parent / "output.log"
    assert "installed" in log.read_text(encoding="utf-8")


def test_a_deployed_tip_is_not_deployed_again(tmp_path):
    _deploy(tmp_path, Runner())
    again = Runner()
    assert _deploy(tmp_path, again) is None
    assert again.calls == []


def test_a_red_deploy_is_recorded_with_its_output_and_the_tree_still_goes(tmp_path):
    runner = Runner(code=1)
    result = _deploy(tmp_path, runner)
    assert result is not None and result.status == dm.FAILED and result.reason == "exit 1"
    assert any("remove" in argv for argv, _ in runner.calls)


def test_a_tree_that_cannot_be_cut_is_red_and_runs_nothing(tmp_path):
    runner = Runner(cut=1)
    result = _deploy(tmp_path, runner)
    assert result is not None and result.status == dm.FAILED
    assert runner.contract_runs() == []


@pytest.mark.parametrize("git", [{"contract": False}, {"fetch": 1}, {"tip": ""}])
def test_no_contract_on_origin_or_no_origin_deploys_nothing(tmp_path, git):
    runner = Runner()
    assert _deploy(tmp_path, runner, **git) is None
    assert runner.calls == []
    assert dm.read("tvshell", tmp_path / "devkit") is None


def test_the_contract_is_asked_of_origin_not_of_the_checkout(tmp_path):
    log: list = []
    _deploy(tmp_path, Runner(), log=log)
    assert ("cat-file", "-e", f"origin/main:{dm.CONTRACT}") in log


def test_a_tree_a_killed_run_left_is_cleared_first(tmp_path):
    checkout = tmp_path / "tvshell"
    stale = dm.path_for(checkout, "0" * 40)
    stale.mkdir(parents=True)
    runner = Runner()
    _deploy(tmp_path, runner)
    removes = [argv for argv, _ in runner.calls if "remove" in argv]
    assert str(stale) in removes[0]


def test_clear_leftovers_removes_only_this_modules_trees(tmp_path):
    checkout = tmp_path / "tvshell"
    mine = dm.path_for(checkout, "1" * 40)
    mine.mkdir(parents=True)
    theirs = mine.parent / "tier-abc"
    theirs.mkdir()
    runner = Runner()
    dm.clear_leftovers(checkout, runner)
    removed = [argv for argv, _ in runner.calls if "remove" in argv]
    assert any(str(mine) in argv for argv in removed)
    assert not any(str(theirs) in argv for argv in removed)


def test_clear_leftovers_with_no_tree_root_does_nothing(tmp_path):
    runner = Runner()
    dm.clear_leftovers(tmp_path / "never-cut", runner)
    assert runner.calls == []


def test_run_contract_returns_the_scripts_code_and_output(tmp_path):
    code, output = dm.run_contract(tmp_path / "tvshell", TIP, Runner(code=3))
    assert code == 3
    assert "installed" in output


def test_run_contract_that_cannot_cut_says_so(tmp_path):
    code, output = dm.run_contract(tmp_path / "tvshell", TIP, Runner(cut=1))
    assert code == 1
    assert output.startswith(f"could not cut a tree at {TIP[:12]}")


def test_line_for_names_the_verdict_the_reason_and_where_to_look():
    red = dm.Deployed("tvshell", TIP, dm.FAILED, "x", "exit 2")
    assert dm.line_for(red) == (
        f"tvshell: deploy failed at {TIP[:12]} (exit 2); see logs/deploy/tvshell"
    )
    green = dm.Deployed("tvshell", TIP, dm.PASSED, "x")
    assert dm.line_for(green) == f"tvshell: deploy passed at {TIP[:12]}; see logs/deploy/tvshell"


def test_the_seams_deploy_all_reads_are_e2e_pass_seams_fields():
    from dataclasses import fields

    from support import load_script

    e2e_pass = load_script("scripts/e2e-pass.py")
    wanted = {name for name in vars(dm.Seams) if not name.startswith("_")}
    assert wanted == {"root", "git_for", "runner", "now"}
    assert wanted <= {f.name for f in fields(e2e_pass.Seams)}


def test_deploy_all_runs_each_checkout_there_is_and_says_what_ran(tmp_path, capsys):
    # The workspace file sits in the projects root, beside every checkout.
    workspace = tmp_path / "devkit.code-workspace"
    (tmp_path / "tvshell").mkdir()
    runner = Runner()
    seams = SimpleNamespace(
        root=tmp_path / "devkit", git_for=lambda _p: fake_git(), runner=runner, now=lambda: NOON
    )
    ran = dm.deploy_all(workspace, ["tvshell", "absent"], seams)
    assert [r.project for r in ran] == ["tvshell"]
    assert f"tvshell: deploy passed at {TIP[:12]}" in capsys.readouterr().out
