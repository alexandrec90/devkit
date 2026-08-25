"""Tests for `scripts/preview-ui-host.py`.

The script is a thin host-side sibling of `preview-task.py`: the scan, the option file
and the pick grammar are that module's and are tested next door, so the weight here is
on what this script adds -- resolving a pick to a directory and a port (`plan_host`),
making the plan real without owning a subprocess of its own in tests (`apply_host`
takes its runners as arguments), and the small pure helpers around them. Everything
touching a real filesystem uses `tmp_path`; nothing here starts npm, git or a socket.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from support import load_script

host = load_script("scripts/preview-ui-host.py")

Candidate = host.preview_task.Candidate
KIND_BRANCH = host.preview_task.KIND_BRANCH


def result(stdout="", returncode=0, stderr=""):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def candidate(project="demo", ref="agent/x", box=""):
    return Candidate(project=project, ref=ref, kind=KIND_BRANCH, box=box)


def write_manifest(root: Path, project: str, body: str) -> Path:
    project_dir = root / project
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".devkit.toml").write_text(body, encoding="utf-8")
    return project_dir


FRONTEND_MANIFEST = '[frontend]\nenabled = true\ndir = "frontend"\n'


class FakeServer:
    """A `Popen` stand-in: `poll` walks a scripted sequence, then repeats its last answer."""

    def __init__(self, polls=(None, 0), returncode=0, pid=4242):
        self._polls = list(polls)
        self.returncode = returncode
        self.pid = pid
        self.terminated = False

    def poll(self):
        if len(self._polls) > 1:
            return self._polls.pop(0)
        return self._polls[0]

    def terminate(self):
        self.terminated = True


# --- the pure helpers ----------------------------------------------------------


def test_echo_prints_a_whole_line(capsys):
    host.echo("hello")
    assert capsys.readouterr().out == "hello\n"


def test_frontend_rel_reads_an_enabled_frontend():
    assert host.frontend_rel({"frontend": {"enabled": True, "dir": "frontend"}}) == "frontend"


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"frontend": {"enabled": False, "dir": "frontend"}},
        {"frontend": "frontend"},
        {"frontend": {"enabled": True}},
    ],
)
def test_frontend_rel_is_empty_for_anything_else(manifest):
    assert host.frontend_rel(manifest) == ""


def test_frontend_dir_for_reads_the_manifest(tmp_path):
    project_dir = write_manifest(tmp_path, "demo", FRONTEND_MANIFEST)
    assert host.frontend_dir_for(project_dir) == "frontend"


def test_frontend_dir_for_is_empty_without_a_manifest(tmp_path):
    assert host.frontend_dir_for(tmp_path / "nowhere") == ""


def test_frontend_dir_for_is_empty_on_unparseable_toml(tmp_path):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    (project_dir / ".devkit.toml").write_text("[frontend\n", encoding="utf-8")
    assert host.frontend_dir_for(project_dir) == ""


def test_ref_slug_flattens_a_ref_to_one_directory_name():
    assert host.ref_slug("agent/comic-book-ui-0820") == "agent-comic-book-ui-0820"


def test_ref_slug_never_returns_an_empty_or_dot_leading_name():
    assert host.ref_slug("///") == "ref"
    assert not host.ref_slug(".hidden/x").startswith(".")


def test_next_port_skips_taken_and_listening_ports():
    port = host.next_port({5300}, lambda p: p == 5301, start=5300, span=5)
    assert port == 5302


def test_next_port_raises_when_the_span_is_exhausted():
    with pytest.raises(RuntimeError, match="no free port"):
        host.next_port(set(), lambda p: True, start=5300, span=3)


def test_dev_env_forces_same_origin_calls_through_the_proxy():
    env = host.dev_env({"PATH": "/bin"}, "http://127.0.0.1:8000")
    assert env["VITE_API_BASE_URL"] == ""
    assert env["VITE_PROXY_TARGET"] == "http://127.0.0.1:8000"
    assert env["PATH"] == "/bin"


def test_npm_stale_when_there_is_no_stamp(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert host.npm_stale(tmp_path) is True


def test_npm_fresh_when_the_stamp_is_newer_than_the_lockfile(tmp_path):
    import os

    lock = tmp_path / "package-lock.json"
    lock.write_text("{}", encoding="utf-8")
    os.utime(lock, (1_000, 1_000))
    stamp = tmp_path / "node_modules" / ".ci-stamp"
    stamp.parent.mkdir()
    stamp.touch()
    os.utime(stamp, (2_000, 2_000))
    assert host.npm_stale(tmp_path) is False
    os.utime(stamp, (500, 500))
    assert host.npm_stale(tmp_path) is True


# --- donor_target: where the proxy points --------------------------------------


class FakeRegistry:
    def __init__(self, slots, app_port=8000):
        self.slots = slots
        self._app_port = app_port

    def ports_for_slot(self, slot):
        return {"app": self._app_port}


def test_donor_target_uses_the_static_stack_when_it_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(host.worktree, "load_registry", lambda root: FakeRegistry({"demo": 0}))
    assert host.donor_target("demo", tmp_path, lambda p: p == 8000) == "http://127.0.0.1:8000"


def test_donor_target_is_dead_when_nothing_listens(tmp_path, monkeypatch):
    monkeypatch.setattr(host.worktree, "load_registry", lambda root: FakeRegistry({"demo": 0}))
    assert host.donor_target("demo", tmp_path, lambda p: False) == host.DEAD_PROXY


def test_donor_target_is_dead_without_a_registry_or_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(host.worktree, "load_registry", lambda root: None)
    assert host.donor_target("demo", tmp_path, lambda p: True) == host.DEAD_PROXY
    monkeypatch.setattr(host.worktree, "load_registry", lambda root: FakeRegistry({}))
    assert host.donor_target("demo", tmp_path, lambda p: True) == host.DEAD_PROXY


def test_donor_target_is_dead_when_the_registry_read_fails(tmp_path, monkeypatch):
    def boom(root):
        raise ValueError("mangled registry")

    monkeypatch.setattr(host.worktree, "load_registry", boom)
    assert host.donor_target("demo", tmp_path, lambda p: True) == host.DEAD_PROXY


# --- plan_host: a pick becomes a directory and a port ---------------------------


def test_plan_refuses_a_project_with_no_frontend(tmp_path):
    write_manifest(tmp_path, "demo", "[frontend]\nenabled = false\n")
    plan = host.plan_host(candidate(), tmp_path, set(), lambda p: False, host.DEAD_PROXY)
    assert "no [frontend]" in plan.refusal


def test_plan_serves_a_live_box_as_it_stands(tmp_path, monkeypatch):
    write_manifest(tmp_path, "demo", FRONTEND_MANIFEST)
    box_dir = tmp_path / ".worktrees" / "demo--x"
    box_dir.mkdir(parents=True)
    monkeypatch.setattr(host.worktree, "box_path", lambda root, name: box_dir)
    plan = host.plan_host(
        candidate(box="demo--x"), tmp_path, set(), lambda p: False, host.DEAD_PROXY
    )
    assert plan.serve_dir == str(box_dir)
    assert plan.frontend == str(box_dir / "frontend")
    assert plan.steps == ()
    assert "unpushed work included" in plan.note


def test_plan_falls_back_to_a_copy_when_the_box_is_gone(tmp_path, monkeypatch):
    write_manifest(tmp_path, "demo", FRONTEND_MANIFEST)
    monkeypatch.setattr(host.worktree, "box_path", lambda root, name: tmp_path / "reaped")
    plan = host.plan_host(
        candidate(box="demo--x"), tmp_path, set(), lambda p: False, host.DEAD_PROXY
    )
    assert host.UI_PREVIEWS_DIR_NAME in plan.serve_dir
    assert plan.steps and plan.steps[0][1][0] == "worktree"


def test_plan_cuts_a_fresh_copy_with_git_worktree_add(tmp_path):
    project_dir = write_manifest(tmp_path, "demo", FRONTEND_MANIFEST)
    plan = host.plan_host(candidate(), tmp_path, set(), lambda p: False, "http://x")
    expected = tmp_path / host.UI_PREVIEWS_DIR_NAME / "demo" / "agent-x"
    assert plan.serve_dir == str(expected)
    assert plan.steps == (
        (str(project_dir), ("worktree", "add", "--detach", str(expected), "origin/agent/x")),
    )
    assert plan.proxy == "http://x"


def test_plan_repoints_an_existing_copy_with_checkout(tmp_path):
    write_manifest(tmp_path, "demo", FRONTEND_MANIFEST)
    copy = tmp_path / host.UI_PREVIEWS_DIR_NAME / "demo" / "agent-x"
    copy.mkdir(parents=True)
    (copy / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    plan = host.plan_host(candidate(), tmp_path, set(), lambda p: False, host.DEAD_PROXY)
    assert plan.steps == ((str(copy), ("checkout", "--detach", "origin/agent/x")),)


def test_plan_claims_a_distinct_port_per_call(tmp_path):
    write_manifest(tmp_path, "demo", FRONTEND_MANIFEST)
    taken: set[int] = set()
    first = host.plan_host(candidate(), tmp_path, taken, lambda p: False, host.DEAD_PROXY)
    second = host.plan_host(
        candidate(ref="agent/y"), tmp_path, taken, lambda p: False, host.DEAD_PROXY
    )
    assert first.port == host.PORT_START
    assert second.port == host.PORT_START + 1
    assert taken == {first.port, second.port}


def test_plan_refuses_when_no_port_is_free(tmp_path):
    write_manifest(tmp_path, "demo", FRONTEND_MANIFEST)
    plan = host.plan_host(candidate(), tmp_path, set(), lambda p: True, host.DEAD_PROXY)
    assert "no free port" in plan.refusal


# --- apply_host: the plan is made real ------------------------------------------


def make_plan(tmp_path, steps=(), port=5300):
    frontend = tmp_path / "copy" / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "package-lock.json").write_text("{}", encoding="utf-8")
    return host.HostPlan(
        project="demo",
        ref="agent/x",
        serve_dir=str(tmp_path / "copy"),
        frontend=str(frontend),
        port=port,
        proxy=host.DEAD_PROXY,
        steps=steps,
    )


def test_apply_runs_steps_installs_and_spawns(tmp_path):
    calls, spawns = [], []

    def run(argv, **kwargs):
        calls.append(argv)
        return result()

    def spawn(argv, cwd=None, env=None):
        spawns.append((argv, cwd, env))
        return FakeServer()

    plan = make_plan(tmp_path, steps=((str(tmp_path), ("worktree", "add", "x", "y")),))
    server = host.apply_host(plan, "npm", run=run, spawn=spawn, environ={"PATH": "p"})
    assert server is not None
    assert calls[0][:3] == ["git", "-C", str(tmp_path)]
    assert calls[1][:2] == ["npm", "ci"]
    argv, cwd, env = spawns[0]
    assert cwd == plan.frontend
    assert "--strictPort" in argv and str(plan.port) in argv
    assert env["VITE_API_BASE_URL"] == "" and env["VITE_PROXY_TARGET"] == plan.proxy


def test_apply_skips_the_install_when_the_stamp_is_fresh(tmp_path):
    import os

    plan = make_plan(tmp_path)
    frontend = Path(plan.frontend)
    os.utime(frontend / "package-lock.json", (1_000, 1_000))
    stamp = frontend / "node_modules" / ".ci-stamp"
    stamp.parent.mkdir()
    stamp.touch()
    os.utime(stamp, (2_000, 2_000))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return result()

    server = host.apply_host(plan, "npm", run=run, spawn=lambda *a, **k: FakeServer(), environ={})
    assert server is not None
    assert calls == []


def test_apply_reports_a_refused_git_step_and_hints_at_editor_saves(tmp_path, capsys):
    plan = make_plan(tmp_path, steps=((str(tmp_path), ("checkout", "--detach", "origin/x")),))
    run = lambda argv, **kwargs: result(
        stderr="your local changes would be overwritten", returncode=1
    )
    assert host.apply_host(plan, "npm", run=run, spawn=None, environ={}) is None
    out = capsys.readouterr().out
    assert "failed: git checkout" in out
    assert "layout editor" in out


def test_apply_reports_a_branch_with_no_frontend_directory(tmp_path, capsys):
    plan = make_plan(tmp_path)
    missing = host.HostPlan(**{**plan.__dict__, "frontend": str(tmp_path / "gone")})
    assert host.apply_host(missing, "npm", run=None, spawn=None, environ={}) is None
    assert "does not exist" in capsys.readouterr().out


def test_apply_reports_a_failed_install(tmp_path, capsys):
    plan = make_plan(tmp_path)
    run = lambda argv, **kwargs: result(returncode=1)
    assert host.apply_host(plan, "npm", run=run, spawn=None, environ={}) is None
    assert "npm ci exited 1" in capsys.readouterr().out


# --- wait_for_server ------------------------------------------------------------


def make_clock(step=1.0):
    state = {"now": 0.0}

    def clock():
        state["now"] += step
        return state["now"]

    return clock


def test_wait_reports_ready_when_the_probe_answers():
    answers = iter([False, True])
    state, _ = host.wait_for_server(
        "http://x/",
        FakeServer(polls=(None,)),
        probe=lambda url: next(answers),
        clock=make_clock(),
        sleep=lambda s: None,
    )
    assert state == "ready"


def test_wait_reports_a_server_that_died_instead_of_probing_out_the_clock():
    state, _ = host.wait_for_server(
        "http://x/",
        FakeServer(polls=(1,)),
        probe=lambda url: False,
        clock=make_clock(),
        sleep=lambda s: None,
    )
    assert state == "died"


def test_wait_times_out_on_a_silent_but_living_server():
    state, waited = host.wait_for_server(
        "http://x/",
        FakeServer(polls=(None,)),
        probe=lambda url: False,
        timeout=3.0,
        clock=make_clock(),
        sleep=lambda s: None,
    )
    assert state == "timeout"
    assert waited >= 3.0


# --- stop and watch -------------------------------------------------------------


def test_stop_takes_the_whole_tree_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(host.os, "name", "nt")
    host.stop(FakeServer(pid=77), run=lambda argv, **kwargs: calls.append(argv) or result())
    assert calls == [["taskkill", "/T", "/F", "/PID", "77"]]


def test_stop_terminates_elsewhere(monkeypatch):
    monkeypatch.setattr(host.os, "name", "posix")
    server = FakeServer()
    host.stop(server, run=None)
    assert server.terminated is True


def test_watch_stops_every_server_when_the_last_one_exits(monkeypatch, capsys):
    stopped = []
    monkeypatch.setattr(host, "stop", lambda server: stopped.append(server))
    plan = types.SimpleNamespace(ref="agent/x")
    servers = [(plan, FakeServer(polls=(None, 2), returncode=2))]
    host.watch(servers, sleep=lambda s: None)
    assert stopped == [servers[0][1]]
    assert "exited with code 2" in capsys.readouterr().out


def test_watch_stops_everything_on_ctrl_c(monkeypatch, capsys):
    stopped = []
    monkeypatch.setattr(host, "stop", lambda server: stopped.append(server))

    def interrupt(seconds):
        raise KeyboardInterrupt

    servers = [
        (types.SimpleNamespace(ref="a"), FakeServer(polls=(None,))),
        (types.SimpleNamespace(ref="b"), FakeServer(polls=(None,))),
    ]
    host.watch(servers, sleep=interrupt)
    assert len(stopped) == 2
    assert "Stopping every server" in capsys.readouterr().out


# --- clean ----------------------------------------------------------------------


def test_clean_with_nothing_to_do_says_so(tmp_path, capsys):
    assert host.clean(tmp_path, run=None) == 0
    assert "nothing to clean" in capsys.readouterr().out


def test_clean_removes_every_copy_through_git(tmp_path, capsys):
    import shutil as _shutil

    copy = tmp_path / host.UI_PREVIEWS_DIR_NAME / "demo" / "agent-x"
    copy.mkdir(parents=True)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        _shutil.rmtree(argv[-1])  # what `git worktree remove` does
        return result()

    assert host.clean(tmp_path, run=run) == 0
    assert calls[0][:4] == ["git", "-C", str(tmp_path / "demo"), "worktree"]
    assert not (tmp_path / host.UI_PREVIEWS_DIR_NAME).exists()
    assert "removed" in capsys.readouterr().out


def test_clean_reports_a_copy_git_refuses_to_remove(tmp_path, capsys):
    copy = tmp_path / host.UI_PREVIEWS_DIR_NAME / "demo" / "agent-x"
    copy.mkdir(parents=True)
    run = lambda argv, **kwargs: result(stderr="locked", returncode=1)
    assert host.clean(tmp_path, run=run) == 1
    assert "failed to remove" in capsys.readouterr().out
    assert copy.is_dir()


# --- the CLI --------------------------------------------------------------------


def test_build_parser_defaults():
    args = host.build_parser().parse_args([])
    assert args.picks == "" and args.fetch and args.open
    assert not args.refresh and not args.clean and args.workspace is None


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "vs_code.code-workspace"
    ws.write_text("{}", encoding="utf-8")
    return ws


@pytest.fixture
def quiet_scan(monkeypatch):
    """Stub the collaborators `main` borrows from preview-task; return the mutable list."""
    everything: list = []
    monkeypatch.setattr(host.preview_task, "collect", lambda ws, fetch: everything)
    monkeypatch.setattr(host.preview_task, "menu_payload", lambda cands, projects: {})
    monkeypatch.setattr(host.preview_task, "stack_projects", lambda ws: [])
    monkeypatch.setattr(host.preview_task, "write_menu", lambda payload: Path("menu.json"))
    monkeypatch.setattr(host.shutil, "which", lambda name: "npm")
    return everything


def test_main_refuses_a_missing_workspace(tmp_path, capsys):
    assert host.main(["--workspace", str(tmp_path / "gone.code-workspace")]) == 2
    assert "no workspace registry" in capsys.readouterr().out


def test_main_dispatches_clean(workspace, monkeypatch):
    monkeypatch.setattr(host, "clean", lambda root: 5)
    assert host.main(["--workspace", str(workspace), "--clean"]) == 5


def test_main_reads_a_cancelled_dropdown_as_a_cancel(workspace, capsys):
    code = host.main(["--workspace", str(workspace), "--picks", "${input:previewRow}"])
    assert code == 0
    assert "cancelled" in capsys.readouterr().out


def test_main_refuses_to_serve_without_npm(workspace, quiet_scan, monkeypatch, capsys):
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    code = host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    assert code == 2
    assert "npm is not on PATH" in capsys.readouterr().out


def test_main_refresh_rewrites_the_menu_and_serves_nothing(workspace, quiet_scan, capsys):
    assert host.main(["--workspace", str(workspace), "--refresh", "--no-fetch"]) == 0
    assert "Dropdown options written" in capsys.readouterr().out


def test_main_refresh_fails_when_the_menu_cannot_be_written(workspace, quiet_scan, monkeypatch):
    monkeypatch.setattr(host.preview_task, "write_menu", lambda payload: None)
    assert host.main(["--workspace", str(workspace), "--refresh", "--no-fetch"]) == 1


def test_main_with_no_picks_serves_nothing(workspace, quiet_scan, capsys):
    assert host.main(["--workspace", str(workspace), "--no-fetch"]) == 0
    assert "Nothing picked" in capsys.readouterr().out


def test_main_fails_loudly_on_an_unservable_token(workspace, quiet_scan, capsys):
    assert host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "bareref"]) == 2
    assert "matches no row" in capsys.readouterr().out


def test_main_treats_a_lone_rescan_as_done(workspace, quiet_scan, capsys):
    rescan = f"demo:{host.preview_task.RESCAN}"
    assert host.main(["--workspace", str(workspace), "--no-fetch", "--picks", rescan]) == 0
    assert "Rescanned" in capsys.readouterr().out


def test_main_counts_a_refused_plan_as_a_failure(workspace, quiet_scan, monkeypatch, capsys):
    quiet_scan.append(candidate())
    monkeypatch.setattr(host, "donor_target", lambda project, root, listening: host.DEAD_PROXY)
    refusal = host.HostPlan(project="demo", ref="agent/x", refusal="no dice")
    monkeypatch.setattr(host, "plan_host", lambda *a: refusal)
    code = host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    assert code == 1
    assert "no dice" in capsys.readouterr().out


def test_main_serves_opens_and_watches(workspace, quiet_scan, monkeypatch, capsys):
    quiet_scan.append(candidate())
    monkeypatch.setattr(host, "donor_target", lambda project, root, listening: "http://b:1")
    plan = host.HostPlan(project="demo", ref="agent/x", serve_dir="d", frontend="f", port=5300)
    monkeypatch.setattr(host, "plan_host", lambda *a: plan)
    server = FakeServer()
    monkeypatch.setattr(host, "apply_host", lambda p, npm: server)
    monkeypatch.setattr(host, "wait_for_server", lambda url, srv: ("ready", 1.0))
    opened = []
    monkeypatch.setattr(host.webbrowser, "open", lambda url: opened.append(url))
    watched = []
    monkeypatch.setattr(host, "watch", lambda servers: watched.append(servers))
    code = host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    assert code == 0
    assert opened == ["http://127.0.0.1:5300/"]
    assert watched == [[(plan, server)]]
    assert "http://127.0.0.1:5300/" in capsys.readouterr().out


def test_main_counts_a_server_that_died_before_answering(workspace, quiet_scan, monkeypatch):
    quiet_scan.append(candidate())
    monkeypatch.setattr(host, "donor_target", lambda project, root, listening: host.DEAD_PROXY)
    plan = host.HostPlan(project="demo", ref="agent/x", serve_dir="d", frontend="f", port=5300)
    monkeypatch.setattr(host, "plan_host", lambda *a: plan)
    monkeypatch.setattr(host, "apply_host", lambda p, npm: FakeServer(polls=(1,), returncode=1))
    monkeypatch.setattr(host, "wait_for_server", lambda url, srv: ("died", 2.0))
    monkeypatch.setattr(host, "watch", lambda servers: None)
    code = host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    assert code == 1
