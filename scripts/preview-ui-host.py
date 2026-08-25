#!/usr/bin/env python3
"""Serve picked branches' frontends straight from host Vite -- no Docker, no backend.

`preview-task.py` answers "show me the thing I asked for" with a BOX: a worktree, a
port lease, a compose stack, an image build and an `npm ci` into a fresh named volume
-- about three minutes cold, all of it buying an environment this machine can already
provide for free when the question is only "what does the UI look like on this
branch". A Vite frontend needs node, the branch's files and a free port; carameli's
degrades gracefully with no backend at all (the auth probe flips `ready` in a
`.finally`, API-backed views render their offline state), so a UI-only review pays for
a stack it never calls.

This is the cheap path, as the same clickable shape: the SAME option file the preview
dropdowns read -- written by the same scan -- picked from with checkboxes
(`multiPick`), so several branches come up side by side in one run, each on its own
port, each opened in the browser once it answers.

What one run does, per picked row:

  1. **Find the files.** A row naming a live box is served from that box's own
     worktree, as it is -- unpushed work included, which is the point: "look at what
     the agent just did" is the first question this task gets asked, and it is the
     same serve-as-is decision `preview-task.py` already made for boxes. Anything
     else -- an open PR, a bare branch -- gets a detached worktree under
     `<workspace>/.ui-previews/<project>/<ref slug>`, cut from `origin/<ref>` and
     re-pointed at it on every later run, so the copy is paid for once per ref.
  2. **Install once, stamped.** `npm ci` runs only when `node_modules/.ci-stamp` is
     older than `package-lock.json` -- the same stamp the compose frontend service
     uses, so the first run of a copy costs about a minute and every later one skips
     straight to Vite, which starts in seconds.
  3. **Serve and say where.** `npm run dev` on a free port scanned upward from
     `PORT_START` -- deliberately above the registry slots, which belong to the
     Docker tier -- with `--strictPort` so the URL printed is the URL served.
     `VITE_API_BASE_URL` is forced empty so the app's calls stay same-origin and go
     through Vite's proxy, and the proxy points at the static checkout's API when
     something answers on its registered port (real data for free) or at a dead
     loopback port when nothing does: requests are refused in microseconds instead
     of stalling on `http://app:8000`, a name that only resolves inside compose.

The servers stay CHILDREN of this process: the task's terminal is the lifecycle, and
Ctrl+C -- or the terminal's trash can -- stops every one of them. Detaching instead
would need a `--down` verb, a pid file and a reaper; a terminal someone can see is
all three. The copies under `.ui-previews/` are deliberately kept between runs
(`--clean` removes them all, through git so the checkout forgets them too), and they
are invisible to `worktree.py` on purpose: no port lease, no compose project, nothing
for `reconcile` to own.

Usage:
    python preview-ui-host.py --picks="carameli:agent/foo carameli:agent/bar"
    python preview-ui-host.py --refresh      # rebuild the dropdown's option file only
    python preview-ui-host.py --clean        # remove every .ui-previews copy

The scan, the option file, the pick grammar and the probe are all `preview-task.py`'s,
loaded by path (the file is hyphenated) through the shared loader -- one branch-listing
implementation for both tasks, because a second copy is a fork that no gate would
drift-check. Tested in `tests/test_preview_ui_host.py`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import tomllib
import webbrowser
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import sweep
import worktree
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

preview_task = load_by_path("preview_task", Path(__file__).resolve().parent / "preview-task.py")

# Where the detached copies live, directly under the workspace root: NOT `.worktrees/`,
# which is `worktree.py`'s namespace -- a directory there that holds no lease would read
# as a husk to anything that scans it.
UI_PREVIEWS_DIR_NAME = ".ui-previews"

# Scanned upward for a free port per server. Above every `ports.toml` slot's range on
# purpose: the Docker tier owns those, and squatting on one would turn the next compose
# up into a bind failure this task caused.
PORT_START = 5300
PORT_SPAN = 200

# Where proxied API calls go when the static stack is down: the discard-protocol port
# on loopback, where nothing ever listens, so every request fails instantly.
DEAD_PROXY = f"http://{preview_task.LOOPBACK}:9"

# How long to wait for Vite to answer. The install has already happened synchronously
# by the time this clock starts, so this is dev-server startup alone -- seconds, with
# a margin for a cold module graph.
READY_TIMEOUT = 90.0


@dataclass(frozen=True)
class HostPlan:
    """One picked row resolved to files on disk and a port to serve them from.

    `steps` are `(cwd, git argv)` pairs that make `serve_dir` exist and point at the
    ref -- empty when the row is served from a live box, which is taken as it stands.
    A set `refusal` means nothing else in the plan is meaningful.
    """

    project: str
    ref: str
    serve_dir: str = ""
    frontend: str = ""
    port: int = 0
    proxy: str = ""
    steps: tuple[tuple[str, tuple[str, ...]], ...] = ()
    note: str = ""
    refusal: str = ""


def echo(line: str = "") -> None:
    """Print a whole line, flushed -- same contract as `preview-task.py`'s, same reason."""
    print(line, flush=True)


def frontend_rel(manifest: dict) -> str:
    """The `[frontend] dir` a project declares, or "" when it declares no frontend."""
    front = manifest.get("frontend")
    if not isinstance(front, dict) or not front.get("enabled"):
        return ""
    return str(front.get("dir") or "")


def frontend_dir_for(project_dir: Path) -> str:
    """`frontend_rel` read from the checkout's `.devkit.toml`; "" on any failure.

    "" rather than raising, because the caller's next line is a refusal that names the
    project -- a checkout with no manifest and one with no frontend need the same
    answer, which is "this task cannot serve you".
    """
    try:
        manifest = tomllib.loads((project_dir / ".devkit.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError covers TOMLDecodeError
        return ""
    return frontend_rel(manifest)


def ref_slug(ref: str) -> str:
    """`agent/comic-book-ui-0820` -> `agent-comic-book-ui-0820`: one directory name."""
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in ref)
    return cleaned.strip("-.") or "ref"


def next_port(taken: set[int], listening, start: int = PORT_START, span: int = PORT_SPAN) -> int:
    """The first port neither allocated this run nor already answering on the host.

    `listening` is what makes two instances of the task coexist without a registry:
    the first run's servers are up by the time the second run scans, so the scan
    itself is the lease. Raises RuntimeError when the span is exhausted, which the
    planner turns into a per-row refusal rather than a dead task.
    """
    for port in range(start, start + span):
        if port in taken or listening(port):
            continue
        return port
    raise RuntimeError(f"no free port between {start} and {start + span - 1}")


def donor_target(project: str, root: Path, listening) -> str:
    """Where the dev server's API proxy points: the static stack, or nowhere fast.

    The same registry walk as `preview-task.donor_warning`, ending in a URL instead of
    a warning: when the checkout's own stack answers on its registered `app` port the
    preview gets real data for free, and when it does not the target is `DEAD_PROXY`
    so every call fails instantly instead of waiting out a connect timeout. Every
    failure path lands on `DEAD_PROXY` too -- a proxy target must always be a URL.
    """
    try:
        registry = worktree.load_registry(root)
        if registry is None:
            return DEAD_PROXY
        slot = registry.slots.get(project, -1)
        if slot < 0:
            return DEAD_PROXY
        port = registry.ports_for_slot(slot).get("app", 0)
    except (OSError, ValueError):  # ValueError covers devkit_ports.RegistryError
        return DEAD_PROXY
    if port and listening(port):
        return f"http://{preview_task.LOOPBACK}:{port}"
    return DEAD_PROXY


def dev_env(base: dict, proxy: str) -> dict:
    """The server's environment: same-origin API calls, proxied to `proxy`.

    `VITE_API_BASE_URL` is forced empty because the committed `frontend/.env` is only
    one voice in Vite's env resolution and a stray `.env.local` could aim the browser
    at a backend this task never promised; empty means every call goes through the dev
    server's own proxy, whose target this function controls.
    """
    env = dict(base)
    env["VITE_API_BASE_URL"] = ""
    env["VITE_PROXY_TARGET"] = proxy
    return env


def npm_stale(frontend: Path) -> bool:
    """Whether `npm ci` is owed, by the compose frontend service's own stamp rule:
    install unless `node_modules/.ci-stamp` is newer than `package-lock.json`.

    Any unreadable half means stale -- a missing lockfile will fail `npm ci` with its
    own message, which is better than this function guessing green.
    """
    stamp = frontend / "node_modules" / ".ci-stamp"
    lock = frontend / "package-lock.json"
    try:
        return not (stamp.is_file() and stamp.stat().st_mtime > lock.stat().st_mtime)
    except OSError:
        return True


def plan_host(candidate, root: Path, taken: set[int], listening, proxy: str) -> HostPlan:
    """Resolve one row to a `HostPlan`, claiming a port from `taken` as it goes.

    A live box wins over a fresh copy for the reason `preview-task.py` gives: the box
    is the only place unpushed work exists, and a copy of `origin/<ref>` would show a
    reviewer everything except the thing they asked to see. A box that has vanished --
    `reconcile` reaps under disk pressure -- falls through to the copy path, which is
    the best that can be served once the worktree is gone.
    """
    project_dir = root / candidate.project
    front_rel = frontend_dir_for(project_dir)
    if not front_rel:
        return HostPlan(
            project=candidate.project,
            ref=candidate.ref,
            refusal=f"{candidate.project} declares no [frontend] in .devkit.toml -- "
            "nothing to serve on the host",
        )

    steps: tuple[tuple[str, tuple[str, ...]], ...] = ()
    note = ""
    serve_dir: Path | None = None
    if candidate.box:
        box_dir = worktree.box_path(root, candidate.box)
        if box_dir.is_dir():
            serve_dir = box_dir
            note = f"serving box {candidate.box}'s worktree as it is -- unpushed work included"
    if serve_dir is None:
        serve_dir = root / UI_PREVIEWS_DIR_NAME / candidate.project / ref_slug(candidate.ref)
        target = f"origin/{candidate.ref}"
        if (serve_dir / ".git").exists():
            steps = ((str(serve_dir), ("checkout", "--detach", target)),)
        else:
            steps = ((str(project_dir), ("worktree", "add", "--detach", str(serve_dir), target)),)

    try:
        port = next_port(taken, listening)
    except RuntimeError as exc:
        return HostPlan(project=candidate.project, ref=candidate.ref, refusal=str(exc))
    taken.add(port)
    return HostPlan(
        project=candidate.project,
        ref=candidate.ref,
        serve_dir=str(serve_dir),
        frontend=str(serve_dir / front_rel),
        port=port,
        proxy=proxy,
        steps=steps,
        note=note,
    )


def apply_host(plan: HostPlan, npm: str, run=subprocess.run, spawn=subprocess.Popen, environ=None):
    """Make the plan real: git steps, the stamped install, then the server itself.

    Returns the server process, or None with the reason already printed. The install
    runs with the terminal's own streams so its progress is visible -- it is the one
    slow step, and a silent minute reads as a hang. A git step that refuses because
    the copy has local edits is reported rather than forced: the `?edit=1` layout
    editor saves into the serving copy, and discarding that silently is worse than
    asking once.
    """
    for cwd, argv in plan.steps:
        result = run(["git", "-C", cwd, *argv], capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            echo(f"  failed: git {' '.join(argv)}: {detail}")
            if "would be overwritten" in detail:
                echo(
                    f"  the copy at {plan.serve_dir} has local edits (the layout editor "
                    f"saves there) -- commit them from that directory, or discard with "
                    f"`git -C {plan.serve_dir} checkout -- .` and rerun"
                )
            return None
    frontend = Path(plan.frontend)
    if not frontend.is_dir():
        echo(f"  failed: {frontend} does not exist on {plan.ref}")
        return None
    if npm_stale(frontend):
        echo("  installing frontend dependencies (first run for this copy -- about a minute) ...")
        result = run([npm, "ci", "--no-audit", "--no-fund"], cwd=str(frontend))
        if result.returncode != 0:
            echo(f"  failed: npm ci exited {result.returncode}")
            return None
        try:
            (frontend / "node_modules" / ".ci-stamp").touch()
        except OSError:
            pass  # the stamp is an optimisation; a failed touch just re-installs next run
    env = dev_env(dict(environ if environ is not None else os.environ), plan.proxy)
    command = [
        npm,
        "run",
        "dev",
        "--",
        "--host",
        preview_task.LOOPBACK,
        "--port",
        str(plan.port),
        "--strictPort",
    ]
    return spawn(command, cwd=str(frontend), env=env)


def wait_for_server(
    url: str,
    server,
    probe=None,
    timeout: float = READY_TIMEOUT,
    poll: float = 1.0,
    clock=time.monotonic,
    sleep=time.sleep,
) -> tuple[str, float]:
    """`("ready" | "died" | "timeout", seconds waited)` for one just-spawned server.

    Not `preview-task.wait_for_ready`, and the difference is the middle verdict: that
    wait watches a container whose process it cannot see, while this one holds the
    server's own handle -- a Vite that exits (a bad config on the branch, a port
    race lost despite the scan) should be reported in seconds, not probed at for the
    whole timeout.
    """
    probe = probe or preview_task.probe
    started = clock()
    while True:
        if probe(url):
            return "ready", clock() - started
        if server.poll() is not None:
            return "died", clock() - started
        if clock() - started >= timeout:
            return "timeout", clock() - started
        sleep(poll)


def stop(server, run=subprocess.run) -> None:
    """End one server and everything it spawned.

    On Windows `npm.cmd` wraps a `node` child, and `terminate()` would kill the
    wrapper while the actual server keeps the port -- `taskkill /T` takes the tree.
    Best-effort by design: a server that already exited makes taskkill complain, and
    that is not a failure of stopping.
    """
    if os.name == "nt":
        run(["taskkill", "/T", "/F", "/PID", str(server.pid)], capture_output=True)
    else:
        server.terminate()


def watch(servers, sleep=time.sleep) -> None:
    """Hold the terminal while any server lives; Ctrl+C -- or the last exit -- ends all.

    A poll loop rather than `wait()` so the KeyboardInterrupt always lands between
    polls, where the `finally` can reach every server -- including the ones still
    healthy when one of their siblings died.
    """
    try:
        while any(server.poll() is None for _, server in servers):
            sleep(2.0)
        for plan, server in servers:
            if server.returncode:
                echo(f"  [warn] the {plan.ref} server exited with code {server.returncode}")
    except KeyboardInterrupt:
        echo("\nStopping every server ...")
    finally:
        for _, server in servers:
            stop(server)


def clean(root: Path, run=subprocess.run) -> int:
    """Remove every `.ui-previews` copy, through git so the checkouts forget them too.

    `--force` because the copies are throwaways whose only possible edits are layout-
    editor saves, and this verb is the explicit "discard them all" -- the per-run
    checkout step is the place a save gets protected, and it refuses rather than
    forces.
    """
    base = root / UI_PREVIEWS_DIR_NAME
    if not base.is_dir():
        echo(f"nothing to clean: no {UI_PREVIEWS_DIR_NAME} copies exist")
        return 0
    failures = 0
    for project_dir in sorted(path for path in base.iterdir() if path.is_dir()):
        checkout = root / project_dir.name
        for copy in sorted(path for path in project_dir.iterdir() if path.is_dir()):
            result = run(
                ["git", "-C", str(checkout), "worktree", "remove", "--force", str(copy)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                echo(f"  failed to remove {copy}: {(result.stderr or result.stdout or '').strip()}")
                failures += 1
            else:
                echo(f"  removed {copy}")
        try:
            project_dir.rmdir()
        except OSError:
            pass  # not empty: a copy refused above, and its line already says so
    try:
        base.rmdir()
    except OSError:
        pass
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preview-ui-host.py",
        description="Serve picked branches' frontends from host Vite -- no Docker, no backend.",
    )
    parser.add_argument("--workspace", type=Path, default=None, help="the .code-workspace registry")
    parser.add_argument(
        "--picks",
        default="",
        metavar="'PROJECT:REF ...'",
        help="space-joined <project>:<ref> tokens -- what the checkbox dropdown sends",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the dropdown's option file and exit, serving nothing",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=f"remove every {UI_PREVIEWS_DIR_NAME} copy and exit",
    )
    parser.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="skip `git fetch` when scanning (offline, or in a hurry)",
    )
    parser.add_argument(
        "--no-open", dest="open", action="store_false", help="print the URLs but do not open them"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = args.workspace or sweep.default_workspace(REPO_ROOT)
    if not workspace.is_file():
        echo(f"no workspace registry at {workspace}")
        return 2
    root = workspace.parent

    if args.clean:
        return clean(root)

    if args.picks and preview_task.unresolved(args.picks):
        echo("Nothing picked -- the dropdown was cancelled.")
        return 0

    if args.fetch:
        echo("Reading boxes, open PRs and recent branches ...")
    everything = preview_task.collect(workspace, fetch=args.fetch)
    written = preview_task.write_menu(
        preview_task.menu_payload(everything, preview_task.stack_projects(workspace))
    )
    if args.refresh:
        echo(
            f"Dropdown options written to {written}" if written else "Could not write the options."
        )
        return 0 if written else 1

    npm = shutil.which("npm")
    if npm is None:
        echo("npm is not on PATH -- a host preview cannot start without it.")
        return 2

    if not args.picks.strip():
        echo("Nothing picked. --picks takes the dropdown's space-joined <project>:<ref> tokens.")
        return 0
    try:
        resolved, rescanned = preview_task.resolve_picks(args.picks, everything)
    except ValueError as exc:
        echo(str(exc))
        return 2
    if rescanned:
        echo("Rescanned -- the dropdown offers the fresh list on its next run.")
    if not resolved:
        return 0

    failures = 0

    listening = preview_task.port_is_open
    taken: set[int] = set()
    servers = []
    proxies: dict[str, str] = {}
    for candidate in resolved:
        proxy = proxies.setdefault(
            candidate.project, donor_target(candidate.project, root, listening)
        )
        plan = plan_host(candidate, root, taken, listening, proxy)
        echo(f"\nServing {plan.project} {plan.ref} from the host ...")
        if plan.refusal:
            echo(f"  failed: {plan.refusal}")
            failures += 1
            continue
        if plan.note:
            echo(f"  {plan.note}")
        if plan.proxy == DEAD_PROXY:
            echo("  the static stack is down, so API-backed views will show their offline state")
        server = apply_host(plan, npm)
        if server is None:
            failures += 1
            continue
        servers.append((plan, server))

    reachable = []
    for plan, server in servers:
        url = f"http://{preview_task.LOOPBACK}:{plan.port}/"
        state, waited = wait_for_server(url, server)
        if state == "ready":
            echo(f"  {url} answered after {waited:.0f}s  ({plan.ref})")
            reachable.append((plan, url))
            if args.open:
                try:
                    webbrowser.open(url)
                except OSError:  # pragma: no cover - a headless host has no browser
                    pass
        elif state == "died":
            echo(f"  [warn] the {plan.ref} server exited before answering -- its output is above")
            failures += 1
        else:
            echo(f"  [warn] {url} still silent after {waited:.0f}s -- {plan.ref} may be wedged")
            failures += 1

    if not servers:
        return 1 if failures else 0

    rule = "=" * 66
    echo(f"\n{rule}")
    for plan, url in reachable:
        echo(f"  {url}  {plan.project}  {plan.ref}")
    echo(rule)
    echo("Serving. Ctrl+C here (or the terminal's trash can) stops every server; the")
    echo(f"copies under {UI_PREVIEWS_DIR_NAME}/ are kept for next time (--clean removes them).")
    watch(servers)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:  # pragma: no cover - a Ctrl-C is how the task is ended
        echo("\nCancelled.")
        raise SystemExit(0) from None
