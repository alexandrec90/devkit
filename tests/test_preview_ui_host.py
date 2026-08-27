"""Tests for `scripts/preview-ui-host.py`.

The script is a thin host-side sibling of `preview-task.py`: the scan, the option file
and the pick grammar are that module's and are tested next door, so the weight here is
on what this script adds -- resolving a pick to a directory and a port (`plan_host`),
making the plan real without owning a subprocess of its own in tests (`apply_host`
takes its runners as arguments), and the small pure helpers around them. Everything
touching a real filesystem uses `tmp_path`, and nothing here starts npm or git. The one
socket is the offline stub's own, on an ephemeral loopback port: "the connection is
answered rather than refused" is the entire content of that helper, so a test that
faked the socket would be asserting nothing.
"""

from __future__ import annotations

import ast
import os
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from support import load_script

host = load_script("scripts/preview-ui-host.py")

Candidate = host.preview_task.Candidate
KIND_BRANCH = host.preview_task.KIND_BRANCH


def result(stdout="", returncode=0, stderr=""):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def candidate(project="demo", ref="agent/x", box="", pr=0, title=""):
    return Candidate(project=project, ref=ref, kind=KIND_BRANCH, box=box, pr=pr, title=title)


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


# --- the guardrail: this task is host Vite, and stays that way -------------------

PREVIEW_UI_HOST = Path(__file__).resolve().parents[1] / "scripts" / "preview-ui-host.py"

# Words this script's PROSE needs in order to explain itself, and its code must not
# contain. `compose` covers the file and the command; `docker`/`podman`, the engines.
NO_STACK_TOKENS = ("docker", "compose", "podman")


def code_only(source: str) -> str:
    """`source` with every docstring dropped -- `ast.unparse` has already dropped comments.

    Which is the whole trick: the file is allowed to argue at length for why it has no
    container tier, in the words that argument needs, and a change that adds one still
    fails.
    """
    tree = ast.parse(source)
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(getattr(first.value, "value", None), str):
            node.body.pop(0)
            if not node.body:
                node.body.append(ast.Pass())
    return ast.unparse(tree)


def test_the_preview_never_grows_a_docker_or_backend_tier():
    """The requirement in this script's opening paragraph, as something that can fail.

    Every round of "fix the UI preview" so far has reached for a backend, because a
    view rendering its offline state looks like the bug being reported. It is not. This
    task is one `npm run dev` per picked branch and the entire value of it is that it
    costs a port and a few seconds, against the three minutes and the RAM a stack costs
    to answer a question about CSS -- `preview-task.py` is where the stack lives, and
    keeping the two apart is why this one is worth having.
    """
    code = code_only(PREVIEW_UI_HOST.read_text(encoding="utf-8")).lower()
    for token in NO_STACK_TOKENS:
        assert token not in code, (
            f"scripts/preview-ui-host.py grew a {token!r} reference in CODE (prose and "
            "comments are exempt and were stripped before this check). This task is a "
            "bare Vite dev server per branch, deliberately -- the full-stack preview is "
            "preview-task.py, and a UI review must not have to pay for one."
        )


def test_the_only_programs_the_preview_runs_are_git_and_npm(tmp_path):
    """The other half of the guardrail, because a stack can also arrive as a subprocess.

    The token check reads the file, so a run of some engine spelled through a variable
    would pass it. This watches what `apply_host` actually shells out to.
    """
    calls, spawns = [], []

    def run(argv, **kwargs):
        calls.append(argv)
        return result()

    def spawn(argv, cwd=None, env=None):
        spawns.append(argv)
        return FakeServer()

    plan = make_plan(tmp_path, steps=((str(tmp_path), ("worktree", "add", "x", "y")),))
    assert host.apply_host(plan, "npm", run=run, spawn=spawn, environ={}) is not None
    assert [argv[0] for argv in calls] == ["git", "npm"]
    assert spawns == [
        ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5300", "--strictPort"]
    ]


# --- the offline stub: a 502, not a refused connection ---------------------------


def test_the_offline_stub_answers_502_rather_than_refusing_the_connection():
    """Why the stub exists, stated as what a dev server's proxy sees.

    A refused connection is what Vite logs `http proxy error` and a full Node stack
    trace for, once per request -- so a UI that probes a session endpoint on load turns
    a working preview into a wall of red. A 502 it never mentions. The app cannot tell
    the two apart, because Vite's own error handler answered the refusal with a 502 as
    well, which is what makes this purely a noise fix.
    """
    url, server = host.start_offline_stub()
    assert server is not None
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{url}/auth/session", timeout=5)  # noqa: S310 - loopback
        assert caught.value.code == 502
        assert b"no backend" in caught.value.read()
    finally:
        server.shutdown()
        server.server_close()


def test_the_offline_stub_drains_a_posted_body_instead_of_resetting():
    """A POST with a body -- the `frontend-logs` call in the noise this fixed.

    An unread body means the socket closes with bytes still in flight, which the proxy
    reports as a reset: the same red block one layer along, for the same non-failure.
    """
    url, server = host.start_offline_stub()
    try:
        request = urllib.request.Request(  # noqa: S310 - loopback http, built above
            f"{url}/vg/1.0.0/frontend-logs",
            data=b'{"level":"error","message":"x"}',
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)  # noqa: S310 - loopback
        assert caught.value.code == 502
    finally:
        server.shutdown()
        server.server_close()


def test_the_offline_stub_falls_back_to_the_dead_port_when_it_cannot_bind():
    """A stub that will not bind costs the quiet, never the preview."""

    def factory(address, handler):
        raise OSError("address already in use")

    assert host.start_offline_stub(factory=factory) == (host.DEAD_PROXY, None)


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


def test_donor_target_hands_back_the_offline_stub_it_was_given(tmp_path, monkeypatch):
    """Every no-stack path returns the caller's `offline` URL, not the dead-port default.

    `main` passes the stub it started, so this is what decides whether a preview with
    no backend is quiet or is a stack trace per request.
    """
    stub = "http://127.0.0.1:57231"
    monkeypatch.setattr(host.worktree, "load_registry", lambda root: FakeRegistry({"demo": 0}))
    assert host.donor_target("demo", tmp_path, lambda p: False, stub) == stub
    monkeypatch.setattr(host.worktree, "load_registry", lambda root: None)
    assert host.donor_target("demo", tmp_path, lambda p: True, stub) == stub


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


def test_plan_carries_the_pr_number_and_title_through(tmp_path):
    """The closing summary's only source: nothing re-asks GitHub, so a plan that drops
    these two fields makes the block unable to say what any row is."""
    write_manifest(tmp_path, "demo", FRONTEND_MANIFEST)
    plan = host.plan_host(
        candidate(pr=42, title="Bubble chains"), tmp_path, set(), lambda p: False, host.DEAD_PROXY
    )
    assert (plan.pr, plan.title) == (42, "Bubble chains")


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


# --- the teardown nets ----------------------------------------------------------
#
# Three of them, because the one this file used to rely on -- the `finally` around
# `watch` -- turned out not to fire in the case that actually happens. Closing a VS Code
# terminal does not always deliver a Ctrl+C to the task's python, and on 2026-08-25 three
# Vite servers were found still serving on 5300/5301/5303 hours after their terminals had
# gone, with their owning pythons still sitting in the poll loop. Each test below pins one
# net; none of them can be checked by looking at a terminal, which is why they exist.


def test_watch_stops_everything_when_the_terminal_that_started_it_is_gone(monkeypatch, capsys):
    """The net for the exact failure this tier exists for: nobody interrupts.

    The departed owner is a third way out of the loop, joining "the last server exited"
    and Ctrl+C, and it leaves by the same `finally` they do -- so the stopping is written
    once and this stays a decision about *when*.
    """
    stopped = []
    monkeypatch.setattr(host, "stop", lambda server: stopped.append(server))
    servers = [(types.SimpleNamespace(ref="a"), FakeServer(polls=(None,)))]
    host.watch(servers, sleep=lambda s: None, owner=99, alive=lambda pid: False)
    assert stopped == [servers[0][1]]
    assert "terminal that started these servers is gone" in capsys.readouterr().out


def test_watch_keeps_going_while_the_terminal_lives(monkeypatch):
    """The other half: a live owner must never end a preview somebody is looking at."""
    monkeypatch.setattr(host, "stop", lambda server: None)
    servers = [(types.SimpleNamespace(ref="a"), FakeServer(polls=(None, None, 0)))]
    polls = []
    host.watch(servers, sleep=lambda s: polls.append(s), owner=99, alive=lambda pid: True)
    assert polls  # it waited rather than returning on the first check


def test_watch_with_no_owner_never_asks_whether_one_is_alive():
    """An owner of 0 means "started by nothing this can watch" -- an agent's detached
    shell, or a parent that had already gone by the time `main` looked."""

    def explode(pid):
        raise AssertionError("liveness must not be probed when there is no owner")

    servers = [(types.SimpleNamespace(ref="a"), FakeServer(polls=(0,)))]
    host.watch(servers, sleep=lambda s: None, owner=0, alive=explode)


def test_this_process_is_alive_and_pid_zero_is_not():
    """`pid_alive` against the two answers a machine can always be asked for."""
    assert host.pid_alive(os.getpid()) is True
    assert host.pid_alive(0) is False


def test_stop_pid_kills_the_tree_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(host.os, "name", "nt")
    host.stop_pid(77, run=lambda argv, **kwargs: calls.append(argv) or result())
    assert calls == [["taskkill", "/T", "/F", "/PID", "77"]]


def test_a_job_object_is_available_and_adopts_this_process(scratch_registry):
    """The strongest net, and the only one that survives `main` being killed outright.

    Adopting THIS process is safe -- the handle is dropped at the end of the test, and a
    job whose only member is a process that outlives it kills nothing. It goes through
    `scratch_registry.real_job` because the autouse fixture has stubbed the module
    attribute out for everyone else, this being the one test that wants the real thing.
    """
    if os.name != "nt":
        pytest.skip("job objects are a Windows facility")
    job = scratch_registry.real_job()
    assert job is not None
    assert host.adopt(job, os.getpid()) is True


def test_adopting_into_no_job_is_a_quiet_false():
    assert host.adopt(None, os.getpid()) is False


def test_the_kernel_calls_are_prototyped_for_64_bit_handles():
    """Every Win32 call in this file goes through one loader, and this is what the
    loader is for: ctypes defaults an unprototyped argument to `c_int`, which truncates
    a 64-bit HANDLE. The call then fails with a Win32 error nothing prints, and the net
    it belonged to is silently not a net -- the failure mode the whole tier exists to
    end. Asserting the prototypes is cheaper than diagnosing that twice."""
    if os.name != "nt":
        pytest.skip("kernel32 is a Windows facility")
    from ctypes import wintypes

    kernel32 = host._kernel32()
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.CreateJobObjectW.restype is wintypes.HANDLE
    assert kernel32.AssignProcessToJobObject.argtypes == [wintypes.HANDLE, wintypes.HANDLE]
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]


def test_there_is_no_kernel32_to_load_off_windows(monkeypatch):
    """The POSIX branch of every net: `stop` uses the process handle, and the job object
    and the adopt are simply absent rather than an ImportError on `ctypes.WinDLL`.

    Patches `sys.platform` and not `os.name` on purpose -- that is the guard the loader
    reads, because it is the only one mypy narrows on, and CI typechecks this file on
    Linux. A test that patched the other one would pass while the real guard went
    unexercised.
    """
    monkeypatch.setattr(host.sys, "platform", "linux")
    assert host._kernel32() is None


# --- the registry ---------------------------------------------------------------


def test_a_registry_that_will_not_read_is_empty_rather_than_fatal(tmp_path):
    """Both failure shapes, because a corrupt file must cost `--stop`, never a preview."""
    assert host.read_registry(tmp_path / "absent.json") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert host.read_registry(broken) == []


def test_the_registry_is_moved_into_place_and_leaves_no_scratch_behind(tmp_path):
    """Named on `write_registry` itself rather than reached through `record`, because
    both properties are its own. The directory is created, so the first run on a fresh
    clone records like every later one; and the JSON arrives by a rename, so a reader
    never sees the truncated-then-refilled middle -- a session-start report that caught
    that file mid-write would call every live server dead and advertise no leak.
    """
    path = tmp_path / "logs" / "preview-servers.json"
    host.write_registry([{"pid": 7, "port": 5300}], path)
    assert host.read_registry(path) == [{"pid": 7, "port": 5300}]
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_a_registry_that_will_not_write_costs_the_record_not_the_preview(tmp_path):
    """The write half of the same totality the reader has: a server that cannot be
    written down still has its job object and its owner watch, so the preview is worth
    more than the note about it."""
    wall = tmp_path / "not-a-directory"
    wall.write_text("", encoding="utf-8")
    host.write_registry([{"pid": 7}], wall / "preview-servers.json")
    wrong_shape = tmp_path / "shape.json"
    wrong_shape.write_text('{"pid": 1}', encoding="utf-8")
    assert host.read_registry(wrong_shape) == []


def test_recording_replaces_an_entry_with_the_same_pid(scratch_registry):
    """Windows recycles pids, so the same number can name two servers over a machine's
    life. The newer record is the true one; keeping both would offer `--stop` a port that
    moved."""
    host.record([{"pid": 7, "port": 5300, "ref": "old"}])
    host.record([{"pid": 7, "port": 5301, "ref": "new"}, {"pid": 8, "port": 5302}])
    entries = host.read_registry()
    assert [entry["pid"] for entry in entries] == [7, 8]
    assert entries[0]["ref"] == "new"


def test_forgetting_drops_only_the_pids_named(scratch_registry):
    host.record([{"pid": 7, "port": 5300}, {"pid": 8, "port": 5301}])
    host.forget([7])
    assert [entry["pid"] for entry in host.read_registry()] == [8]


def test_a_server_whose_owner_still_watches_is_not_an_orphan():
    """A concurrent run's server. `instanceLimit` is 4, so this is the ordinary case."""
    entry = {"pid": 7, "owner": 8, "port": 5300}
    assert host.orphaned(entry, alive=lambda pid: True, listening=lambda port: True) is False


def test_an_owner_less_server_still_serving_its_port_is_an_orphan():
    entry = {"pid": 7, "owner": 8, "port": 5300}
    alive = {7: True, 8: False}
    assert host.orphaned(entry, alive=alive.get, listening=lambda port: True) is True


def test_a_recycled_pid_not_serving_the_recorded_port_is_left_alone():
    """The pid-recycle guard, and the reason `orphaned` asks two questions.

    An entry days old can name a pid that now belongs to a stranger's process; without
    the port check, reaping would `taskkill /T` that stranger's whole tree.
    """
    entry = {"pid": 7, "owner": 8, "port": 5300}
    alive = {7: True, 8: False}
    assert host.orphaned(entry, alive=alive.get, listening=lambda port: False) is False


def test_reaping_stops_the_orphans_and_forgets_the_dead(scratch_registry, monkeypatch):
    monkeypatch.setattr(host.os, "name", "nt")
    host.record(
        [
            {"pid": 7, "owner": 8, "port": 5300, "ref": "orphan"},
            {"pid": 9, "owner": 10, "port": 5301, "ref": "watched"},
            {"pid": 11, "owner": 12, "port": 5302, "ref": "long gone"},
        ]
    )
    alive = {7: True, 8: False, 9: True, 10: True, 11: False, 12: False}
    killed = []
    stopped = host.reap_orphans(
        alive=alive.get,
        listening=lambda port: True,
        run=lambda argv, **kwargs: killed.append(argv) or result(),
    )
    assert [entry["ref"] for entry in stopped] == ["orphan"]
    assert killed == [["taskkill", "/T", "/F", "/PID", "7"]]
    assert [entry["pid"] for entry in host.read_registry()] == [9]


def test_reaping_an_empty_registry_writes_nothing(tmp_path):
    absent = tmp_path / "absent.json"
    assert host.reap_orphans(absent, alive=lambda pid: True, run=None) == []
    assert not absent.exists()


def test_stopping_ends_even_a_server_whose_owner_is_still_watching(scratch_registry, monkeypatch):
    """The difference between `--stop` and the reap: `--stop` is the request itself.

    The owner needs no separate kill -- `watch` returns the moment its last server exits.
    """
    monkeypatch.setattr(host.os, "name", "nt")
    host.record(
        [
            {"pid": 7, "owner": 8, "port": 5300, "ref": "watched"},
            {"pid": 9, "owner": 10, "port": 5301, "ref": "already dead"},
        ]
    )
    alive = {7: True, 8: True, 9: False, 10: True}
    stopped = host.stop_recorded(
        alive=alive.get, listening=lambda port: True, run=lambda argv, **kwargs: result()
    )
    assert [entry["ref"] for entry in stopped] == ["watched"]
    assert host.read_registry() == []


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
    assert not args.stop


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "vs_code.code-workspace"
    ws.write_text("{}", encoding="utf-8")
    return ws


@pytest.fixture(autouse=True)
def scratch_registry(tmp_path, monkeypatch):
    """Point the server registry at a throwaway file for every test in this module.

    Autouse because forgetting it once is not a failing test -- it is a run that edits
    the developer's real `logs/preview-ui-servers.json` and, through `reap_orphans`, can
    `taskkill` a preview they are looking at. The default has to be the safe one.
    """
    monkeypatch.setattr(host, "SERVER_REGISTRY", tmp_path / "servers.json")
    # And no real job object, for a sharper version of the same hazard: `FakeServer.pid`
    # is a made-up number, and on a machine where it happens to name a live process,
    # `adopt` would put a stranger into a job that kills its members when this test
    # process exits. The job object is tested directly, with this process's own pid.
    real_job = host.kill_on_close_job
    monkeypatch.setattr(host, "kill_on_close_job", lambda: None)
    return types.SimpleNamespace(path=tmp_path / "servers.json", real_job=real_job)


@pytest.fixture
def quiet_scan(monkeypatch):
    """Stub the collaborators `main` borrows from preview-task; return the mutable list."""
    everything: list = []
    monkeypatch.setattr(host.preview_task, "collect", lambda ws, fetch, projects: everything)
    monkeypatch.setattr(host.preview_task, "menu_payload", lambda cands, projects: {})
    monkeypatch.setattr(host.preview_task, "ui_projects", lambda ws: [])
    monkeypatch.setattr(host.preview_task, "write_menu", lambda payload: Path("menu.json"))
    monkeypatch.setattr(host.shutil, "which", lambda name: "npm")
    # No listening socket in a `main` test: the real one binds a port for the run.
    monkeypatch.setattr(host, "start_offline_stub", lambda: ("http://127.0.0.1:57231", None))
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


def test_main_scans_and_draws_only_the_checkouts_it_can_serve(workspace, quiet_scan, monkeypatch):
    """The dropdown must not offer a checkout this script would refuse to serve.

    Both halves of that in one test because they are one decision: `collect` is asked for
    the frontend-declaring checkouts alone -- so a backend-only checkout costs no `git
    fetch` and no `gh pr list` on a pass that runs every fifteen minutes -- and the
    payload is grouped by the same list, so nothing wider can reach the file.
    """
    seen: dict[str, list[str]] = {}

    def fake_collect(ws, fetch, projects):
        seen["scanned"] = projects
        return []

    def fake_payload(candidates, projects):
        seen["drawn"] = projects
        return {}

    monkeypatch.setattr(host.preview_task, "ui_projects", lambda ws: ["carameli"])
    monkeypatch.setattr(host.preview_task, "collect", fake_collect)
    monkeypatch.setattr(host.preview_task, "menu_payload", fake_payload)
    assert host.main(["--workspace", str(workspace), "--refresh", "--no-fetch"]) == 0
    assert seen == {"scanned": ["carameli"], "drawn": ["carameli"]}


def test_main_refresh_fails_when_the_menu_cannot_be_written(workspace, quiet_scan, monkeypatch):
    monkeypatch.setattr(host.preview_task, "write_menu", lambda payload: None)
    assert host.main(["--workspace", str(workspace), "--refresh", "--no-fetch"]) == 1


def test_main_with_no_picks_serves_nothing(workspace, quiet_scan, capsys):
    assert host.main(["--workspace", str(workspace), "--no-fetch"]) == 0
    assert "Nothing picked" in capsys.readouterr().out


def test_main_fails_loudly_on_an_unservable_token(workspace, quiet_scan, capsys):
    assert host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "bareref"]) == 2
    assert "matches no row" in capsys.readouterr().out


def test_main_stops_every_recorded_server_and_names_them(workspace, monkeypatch, capsys):
    """`--stop` is the fourth net, and the only one a person can reach on purpose."""
    entry = {"pid": 4242, "owner": 1, "port": 5300, "project": "demo", "ref": "agent/x"}
    monkeypatch.setattr(host, "stop_recorded", lambda: [entry])
    assert host.main(["--workspace", str(workspace), "--stop"]) == 0
    out = capsys.readouterr().out
    assert "demo agent/x on port 5300" in out
    assert "1 host preview server(s) stopped" in out


def test_main_stop_runs_before_the_scan(workspace, monkeypatch):
    """No fetch, no menu, no npm check: stopping must work on a machine mid-anything.

    `quiet_scan` is deliberately absent here -- if `--stop` ever grew a dependency on the
    scan, this test would hit the real `collect` and hang or fail rather than pass.
    """
    monkeypatch.setattr(host, "stop_recorded", lambda: [])
    assert host.main(["--workspace", str(workspace), "--stop"]) == 0


def test_main_counts_a_refused_plan_as_a_failure(workspace, quiet_scan, monkeypatch, capsys):
    quiet_scan.append(candidate())
    monkeypatch.setattr(host, "donor_target", lambda *a: host.DEAD_PROXY)
    refusal = host.HostPlan(project="demo", ref="agent/x", refusal="no dice")
    monkeypatch.setattr(host, "plan_host", lambda *a: refusal)
    code = host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    assert code == 1
    assert "no dice" in capsys.readouterr().out


def test_main_gives_the_started_stub_to_every_proxy_decision(workspace, quiet_scan, monkeypatch):
    """The wiring that IS the noise fix: what the stub bound is what a stackless project
    proxies to, so its API calls are answered 502 rather than refused."""
    quiet_scan.append(candidate())
    seen: list[str] = []

    def donor(project, root, listening, offline):
        seen.append(offline)
        return offline

    monkeypatch.setattr(host, "donor_target", donor)
    monkeypatch.setattr(
        host, "plan_host", lambda *a: host.HostPlan(project="demo", ref="agent/x", refusal="stop")
    )
    host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    assert seen == ["http://127.0.0.1:57231"]


def test_main_serves_opens_and_watches(workspace, quiet_scan, monkeypatch, capsys):
    quiet_scan.append(candidate())
    monkeypatch.setattr(host, "donor_target", lambda *a: "http://b:1")
    plan = host.HostPlan(project="demo", ref="agent/x", serve_dir="d", frontend="f", port=5300)
    monkeypatch.setattr(host, "plan_host", lambda *a: plan)
    server = FakeServer()
    monkeypatch.setattr(host, "apply_host", lambda p, npm: server)
    monkeypatch.setattr(host, "wait_for_server", lambda url, srv: ("ready", 1.0))
    opened = []
    monkeypatch.setattr(host.webbrowser, "open", lambda url: opened.append(url))
    watched = []
    monkeypatch.setattr(host, "watch", lambda servers, **kwargs: watched.append(servers))
    code = host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    assert code == 0
    assert opened == ["http://127.0.0.1:5300/"]
    assert watched == [[(plan, server)]]
    assert "http://127.0.0.1:5300/" in capsys.readouterr().out


def test_main_counts_a_server_that_died_before_answering(workspace, quiet_scan, monkeypatch):
    quiet_scan.append(candidate())
    monkeypatch.setattr(host, "donor_target", lambda *a: host.DEAD_PROXY)
    plan = host.HostPlan(project="demo", ref="agent/x", serve_dir="d", frontend="f", port=5300)
    monkeypatch.setattr(host, "plan_host", lambda *a: plan)
    monkeypatch.setattr(host, "apply_host", lambda p, npm: FakeServer(polls=(1,), returncode=1))
    monkeypatch.setattr(host, "wait_for_server", lambda url, srv: ("died", 2.0))
    monkeypatch.setattr(host, "watch", lambda servers, **kwargs: None)
    code = host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    assert code == 1


# --- the closing summary block --------------------------------------------------


def test_review_columns_pair_the_pr_number_with_the_title():
    plan = host.HostPlan(project="demo", ref="agent/x", pr=7, title="Ship button")
    assert host.review_columns(plan) == ("PR #7", '"Ship button"')


def test_review_columns_keep_a_titled_branch_that_has_no_pr():
    """A branch with no PR is titled by its commit subject, which is still the only
    sentence anybody wrote about the row -- dropping it for want of a number says
    nothing about a branch whose author did."""
    plan = host.HostPlan(project="demo", ref="agent/x", title="WIP bubbles")
    assert host.review_columns(plan) == ("", '"WIP bubbles"')


def test_review_columns_are_empty_when_the_row_has_neither():
    assert host.review_columns(host.HostPlan(project="demo", ref="agent/x")) == ("", "")


def test_summary_lines_align_the_titles_of_a_row_with_a_pr_and_a_row_without():
    """The regression the column split exists for: a bare branch's title used to slide
    left into the PR column, so the one thing read downwards started in two places."""
    reachable = [
        (host.HostPlan(project="demo", ref="agent/x", pr=251, title="Panel shapes"), "http://a:1/"),
        (host.HostPlan(project="demo", ref="agent/y", title="wip: bubbles"), "http://a:2/"),
    ]
    lines = host.summary_lines(reachable)
    assert lines[0].index('"Panel shapes"') == lines[1].index('"wip: bubbles"')


def test_summary_lines_drop_a_pr_column_no_row_can_fill():
    """Padding it instead would put a gap in every line to align nothing."""
    reachable = [
        (host.HostPlan(project="demo", ref="agent/x", title="one"), "http://a:1/"),
        (host.HostPlan(project="demo", ref="agent/y", title="two"), "http://a:2/"),
    ]
    assert host.summary_lines(reachable)[0] == '  http://a:1/  demo  agent/x  "one"'


def test_summary_lines_size_their_columns_to_the_widest_row():
    reachable = [
        (host.HostPlan(project="demo", ref="agent/x", pr=7, title="Ship button"), "http://a:1/"),
        (host.HostPlan(project="a-longer-project", ref="agent/much-longer-ref"), "http://b:22/"),
    ]
    lines = host.summary_lines(reachable)
    assert lines[0].index("demo") == lines[1].index("a-longer-project")
    assert lines[0].endswith('PR #7  "Ship button"')


def test_summary_lines_do_not_trail_the_padding_of_a_row_with_nothing_to_say():
    reachable = [
        (host.HostPlan(project="demo", ref="agent/x"), "http://a:1/"),
        (host.HostPlan(project="demo", ref="agent/much-longer-ref"), "http://b:22/"),
    ]
    assert [line for line in host.summary_lines(reachable) if line != line.rstrip()] == []


def test_summary_lines_of_nothing_is_nothing():
    """Every server can time out, and `main` draws its rule around whatever comes back."""
    assert host.summary_lines([]) == []


def test_main_names_the_pr_and_title_in_the_closing_block(
    workspace, quiet_scan, monkeypatch, capsys
):
    """The whole path, end to end: a picked row's PR number and title reach the block a
    reviewer clicks out of, and the rule is drawn wide enough to frame them."""
    quiet_scan.append(candidate())
    monkeypatch.setattr(host, "donor_target", lambda *a: "http://b:1")
    plan = host.HostPlan(
        project="demo",
        ref="agent/x",
        serve_dir="d",
        frontend="f",
        port=5300,
        pr=99,
        title="Editor ship button",
    )
    monkeypatch.setattr(host, "plan_host", lambda *a: plan)
    monkeypatch.setattr(host, "apply_host", lambda p, npm: FakeServer())
    monkeypatch.setattr(host, "wait_for_server", lambda url, srv: ("ready", 1.0))
    monkeypatch.setattr(host.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(host, "watch", lambda servers, **kwargs: None)
    host.main(["--workspace", str(workspace), "--no-fetch", "--picks", "demo:agent/x"])
    row = '  http://127.0.0.1:5300/  demo  agent/x  PR #99  "Editor ship button"'
    rule = "=" * len(row)
    assert f"{rule}\n{row}\n{rule}" in capsys.readouterr().out
