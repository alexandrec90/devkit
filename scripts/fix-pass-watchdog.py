#!/usr/bin/env python3
"""Supervise the fix pass: keep it current, run it, and repair it when it cannot run.

`fix-pass.py` files everything that goes wrong *inside* a pass on the harness-defect
ledger, and sends the devkit session at it. That covers every failure but its own: a
pass that dies on an import, hangs, or refuses to start files nothing and sends no one,
and so the one component that fixes the rest was the one nothing fixed. This is what the
scheduled task runs instead, and it shares no code with the pass on purpose -- standard
library, plus the ledger writer and the tier names from `scripts/hooks/`, which are
vendored everywhere and change least.

1. **Keep the pass current.** The scheduled pass runs from the static devkit checkout, so
   a fix to it merged on GitHub did nothing until someone pulled. Before each pass the
   checkout is fast-forwarded to `origin/<default>` -- only when it is on that branch,
   clean, and not a linked worktree. When it cannot be, that is filed: the pass is
   running stale code, and nothing else would say so.
2. **Run it**, with a timeout below the task's own interval -- and once more, current
   again, when it exits `STALE`: a merge to its `scripts/` landed after step 1, so it
   held every session rather than route them with the code it started on.
3. **Judge it.** Exit 0 and 1 are the pass reporting on the world. Anything else -- a
   traceback, a timeout, a preflight refusal -- is the pass itself failing, filed as a
   `fix-pass-finding` with the output kept beside the ledger.
4. **Repair it**, in `dispatch` mode: cut a fresh devkit worktree off `origin/<default>`,
   put the output in it, and send one `claude --bg` session at the pass itself. Once
   per failure signature per devkit commit, so a pass that keeps failing the same way
   is repaired once per attempt at a fix, not once per half hour. A rescue whose intent
   is waiting while the pass still cannot run is shipped from here, since the pass that
   would ship it is the thing that is broken.

Tested in `tests/test_fix_pass_watchdog.py`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import harness_events
import worktree_tiers

REPO_ROOT = Path(__file__).resolve().parents[1]

PASS = REPO_ROOT / "scripts" / "fix-pass.py"
ARTIFACT = Path("logs") / "fix-pass.log"
FAILURE_LOG = Path("logs") / "fix-pass.watchdog.log"
STATE_NAME = "fix-pass-watchdog.json"
EVENT = "fix-pass-finding"
# Under the task's half-hour interval, so two passes never overlap.
TIMEOUT = _dt.timedelta(minutes=25)
MIN_RERUN = _dt.timedelta(minutes=5)
# `fix-pass.WINDOW_ENV`, spelled here because this file imports nothing the pass does: the
# seconds this run of the pass has before it is stopped, which it launches no session past.
WINDOW_ENV = "DEVKIT_FIX_PASS_SECONDS"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MODE = re.compile(r'"devkit\.fixPass"\s*:\s*"(\w+)"')
RESCUE_PREFIX = "agent/fix-pass-rescue"
INTENT = Path("logs") / "ship-intent.md"
# `fix_reports.ORIGIN_FILE`, spelled here because this file imports nothing the pass
# does: the mark that makes a tree's PR fixer work, which merges itself once green.
ORIGIN_FILE = Path("logs") / "fix-origin"
AUTOMERGE_LABEL = "automerge"  # sweep.AUTOMERGE_LABEL

# `fix_send.EXIT_STALE`, spelled here because this file imports nothing the pass does:
# the pass's code moved on `origin` after step 1, so it sent no one and wants a rerun.
STALE = 75
# How the pass reports on the world, as opposed to failing itself.
REPORTED = (0, 1, STALE)
# `git_trust.AUTH_SETTING`, spelled here because this file imports nothing the pass does, and
# because self-update fetches before the pass has set it anywhere: GitHub answers a
# blocked address's anonymous fetch 403, which git never takes to its credential helper,
# so a fetch of a public repository failed while pushes went through (902dad3f).
GITHUB_AUTH = ("-c", "http.https://github.com/.proactiveAuth=basic")


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *GITHUB_AUTH, *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        creationflags=NO_WINDOW,
    )


def mode_of(argv: list[str], workspace: Path) -> str:
    """`--mode` when the caller passed one, else the workspace file's switch, else off."""
    if "--mode" in argv and argv.index("--mode") + 1 < len(argv):
        return argv[argv.index("--mode") + 1]
    try:
        found = MODE.search(workspace.read_text(encoding="utf-8"))
    except OSError:
        return "off"
    return found.group(1) if found else "off"


def workspace_of(argv: list[str]) -> Path | None:
    if "--workspace" in argv and argv.index("--workspace") + 1 < len(argv):
        return Path(argv[argv.index("--workspace") + 1])
    return None


def default_branch(root: Path) -> str:
    done = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    name = done.stdout.strip().removeprefix("origin/") if done.returncode == 0 else ""
    return name or "main"


# --- 1. current ---------------------------------------------------------------------------


FETCH_ATTEMPTS = 2
FETCH_PAUSE_SECONDS = 5.0
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


def fetch(root: Path, base: str) -> str:
    """Fetch `origin/<base>`, once more after a pause; "" on success, else why not.

    Every fixer tree shares this checkout's refs, and a fixer fetching at the same moment
    holds `refs/remotes/origin/<base>`'s lock: at 06:00 on 2026-09-27, with #430 merged a
    minute before and a fixer mid-verification, the watchdog's fetch failed and was filed
    as a stale pass with no reason on record (7599a153). The reason is git's own last
    line, shas masked, so one cause is one ledger detail."""
    said = ""
    for attempt in range(FETCH_ATTEMPTS):
        if attempt:
            time.sleep(FETCH_PAUSE_SECONDS)
        done = git(root, "fetch", "--quiet", "origin", base)
        if done.returncode == 0:
            return ""
        lines = (done.stderr or "").strip().splitlines()
        said = _SHA.sub("<sha>", lines[-1].strip()) if lines else f"git exited {done.returncode}"
    return said


def self_update(root: Path) -> tuple[bool, str]:
    """Fast-forward the checkout the pass runs from; `(ok, what happened)`.

    A linked worktree is someone running the pass by hand from their own branch, and is
    left exactly as it is: that is not the stale-code failure.
    """
    if (root / ".git").is_file():
        return True, "a linked worktree -- left as it is"
    base = default_branch(root)
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != base:
        return False, f"the checkout the pass runs from is on {branch or '?'}, not {base}"
    if git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return False, "the checkout the pass runs from has uncommitted changes"
    if failed := fetch(root, base):
        return False, f"could not fetch origin/{base} -- {failed}"
    before = git(root, "rev-parse", "HEAD").stdout.strip()
    if git(root, "merge", "--ff-only", "--quiet", f"origin/{base}").returncode != 0:
        return False, f"{base} has diverged from origin/{base}"
    after = git(root, "rev-parse", "HEAD").stdout.strip()
    return True, "current" if before == after else f"updated {before[:9]}..{after[:9]}"


# --- 2 and 3. run and judge ---------------------------------------------------------------


def console_python() -> str:
    """The console interpreter beside `sys.executable`: under the scheduler that is
    `pythonw.exe`, whose children Windows gives a visible console each, flag or not.
    A copy of `sweep.console_python`, since this file imports nothing the pass does."""
    executable = Path(sys.executable)
    console = executable.with_name("python.exe")
    if executable.name.lower() == "pythonw.exe" and console.exists():
        return str(console)
    return sys.executable


def run_pass(argv: list[str], timeout: _dt.timedelta = TIMEOUT) -> tuple[int | None, str]:
    """`(exit code or None on timeout, combined output)`.

    The pass runs in UTF-8 mode: dozens of its runners use `text=True` with no encoding,
    and on a cp1252 console a child's `”` killed their reader thread and lost output.
    It is told `timeout` too, so it can finish inside it rather than be stopped.
    """
    window = str(int(timeout.total_seconds()))
    try:
        done = subprocess.run(
            [console_python(), str(PASS), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", WINDOW_ENV: window},
            check=False,
            timeout=timeout.total_seconds(),
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        return None, _text(exc.stdout) + _text(exc.stderr)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def run_current(argv: list[str], mode: str, notes: list[str]) -> tuple[int | None, str]:
    """Run the pass, and once more on the new code when it exits `STALE`.

    Only inside what is left of this fire's budget, so two passes still never overlap:
    short of `MIN_RERUN`, the first run's record stands and the next fire routes it.
    """
    started = time.monotonic()
    code, output = run_pass(argv)
    if code != STALE or mode == "off":
        return code, output
    left = TIMEOUT - _dt.timedelta(seconds=time.monotonic() - started)
    moved = "watchdog: the pass's code moved under it --"
    if left < MIN_RERUN:
        notes.append(f"{moved} {int(left.total_seconds() // 60)} min left; the next fire routes")
        return code, output
    ok, what = self_update(REPO_ROOT)
    notes.append(f"{moved} self-update {what}; {'ran it again' if ok else 'not rerun'}")
    return run_pass(argv, left) if ok else (code, output)


def _text(stream: str | bytes | None) -> str:
    """A timed-out child's partial output, which `subprocess` may hand back as bytes."""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return stream or ""


THREAD_TRACE = re.compile(
    r"^Exception in thread [^\n]*\nTraceback \(most recent call last\):\n(?:[ \t][^\n]*\n)*(\S[^\n]*)",
    re.MULTILINE,
)
# What a session is sent at: the pass stopped. A refusal needs something outside the
# repository and a thread error left the pass running, so both are only filed.
RESCUED = ("pass-crashed", "pass-hung")


def judge(code: int | None, output: str) -> tuple[str, str]:
    """`(kind, detail)` when the pass itself failed; `("", "")` when it reported."""
    if code is None:
        return (
            "pass-hung",
            f"the fix pass ran past {int(TIMEOUT.total_seconds() // 60)} minutes and was stopped",
        )
    threads = THREAD_TRACE.findall(output)
    if code in REPORTED and "Traceback (most recent call last)" not in THREAD_TRACE.sub("", output):
        # A background thread's traceback -- a reader thread that could not decode a
        # child's output -- lost output but did not stop the pass: filed, not rescued.
        return ("pass-thread-error", threads[0].strip()[:240]) if threads else ("", "")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    last = lines[-1] if lines else f"exit {code}"
    kind = "pass-refused" if code == 2 and "Traceback" not in output else "pass-crashed"
    return kind, re.sub(r"\s+", " ", last)[:240]


def signature(kind: str, detail: str, head: str) -> str:
    """One failure at one devkit commit: what a rescue is rationed by."""
    shape = re.sub(r"\d+|0x[0-9a-f]+|'[^']*'", "N", detail)
    return hashlib.sha256(f"{kind}\n{shape}\n{head}".encode()).hexdigest()[:16]


def load_state(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def file_once(
    state: dict, sig: str, kind: str, detail: str, evidence: str, now: _dt.datetime
) -> bool:
    """Record the finding unless this signature already was; whether it was recorded."""
    filed = state.setdefault("filed", {})
    if sig in filed:
        return False
    fields = (("project", "devkit"), ("detail", f"{kind}: {detail}"), ("evidence", evidence))
    harness_events.record(EVENT, fields, root=REPO_ROOT)
    filed[sig] = now.isoformat(timespec="seconds")
    return True


# --- 4. repair ----------------------------------------------------------------------------


def rescue_prompt(kind: str, detail: str, branch: str) -> str:
    """What the rescue session is told. It retires the finding `file_once` filed, against
    its own branch: unretired, the pass's next ledger sweep was sent at the same defect
    while the rescue's PR was in review (755ee62b, #564)."""
    return (
        f"The scheduled fix pass itself is failing ({kind}): {detail}. Its full output is in "
        f"{FAILURE_LOG.as_posix()} in this worktree, which is devkit on the fresh branch "
        f"{branch} off the default branch. You are the fix pass's own repair: find why "
        "scripts/fix-pass.py, or a module it loads, fails this way; fix it with a regression "
        "test that fails without the fix; then ship it with the ship skill and stop. Nothing "
        "else is in scope, and nothing else is shipped until the pass runs again. Once the "
        f"fix is in your intent, retire the {EVENT} group whose detail starts `{kind}:` -- "
        "its id is in python scripts/harness_triage.py's output -- with python "
        "scripts/harness_triage.py --resolve-like <id> --note <what fixed it> --pr "
        f"{branch}, so no ledger session is sent at it while your PR is open."
    )


def home(root: Path) -> Path:
    """The main checkout `root` belongs to: where a rescue tree is cut and looked for.
    Run from a linked worktree -- the supervisor's -- the rescue landed inside it."""
    try:
        found = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except OSError:
        return root
    common = Path(found.stdout.strip())
    return common.parent if found.returncode == 0 and common.is_absolute() else root


def is_elevated() -> bool:
    """`agent_tabs.is_elevated`, spelled here because this file imports nothing the pass
    does; False off Windows."""
    if sys.platform != "win32":  # also what tells mypy `windll` exists below
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def rescue(root: Path, kind: str, detail: str, output: str, now: _dt.datetime) -> str:
    """Cut a fresh devkit worktree and send one background session at the pass; what happened.

    Refused from an elevated process, for `agent_tabs.ELEVATED`'s reason: the service a
    `claude --bg` starts runs as whoever asked, and one started elevated shuts every
    scheduled launch out until it exits -- eight hours, the one time it was measured."""
    claude = shutil.which("claude")
    if not claude:
        return "no rescue: claude is not on PATH"
    if is_elevated():
        return "no rescue: this process is elevated; run the watchdog from a shell that is not"
    base = default_branch(root)
    stamp = now.strftime("%m%d-%H%M")
    branch = f"{RESCUE_PREFIX}-{stamp}"
    tree = root / ".claude" / "worktrees" / f"fix-pass-rescue-{stamp}"
    git(root, "fetch", "--quiet", "origin", base)
    added = git(root, "worktree", "add", "--no-track", "-b", branch, str(tree), f"origin/{base}")
    if added.returncode != 0:
        return f"no rescue: could not cut {branch}: {added.stderr.strip()[-200:]}"
    (tree / FAILURE_LOG).parent.mkdir(parents=True, exist_ok=True)
    (tree / FAILURE_LOG).write_text(output, encoding="utf-8")
    (tree / ORIGIN_FILE).write_text("fix-pass\n", encoding="utf-8")
    done = subprocess.run(
        # As `agent_tabs.background_argv` launches: no question nobody will answer, no
        # MCP server, `--` so the variadic flag cannot swallow the prompt, and named for
        # its tree (`agent_tabs.session_name`) so `claude agents` says which one it is.
        [
            claude,
            "--bg",
            "--name",
            f"{root.name}/{tree.name}",
            "--strict-mcp-config",
            "--disallowedTools",
            "AskUserQuestion",
            "--",
            rescue_prompt(kind, detail, branch),
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=NO_WINDOW,
    )
    return (
        f"rescue sent in {tree}"
        if done.returncode == 0
        else f"no rescue: claude --bg exited {done.returncode}"
    )


def ship_rescues(root: Path) -> list[str]:
    """Commit, push and open the PR for every rescue tree holding an intent.

    The pass's own shipper is what is broken, so this is the minimum of it: no fixers,
    no state file. The intent is kept as `ship-intent.shipped.md`, as the pass keeps it.
    """
    shipped = []
    for tree in sorted((root / ".claude" / "worktrees").glob("fix-pass-rescue-*")):
        intent = tree / INTENT
        if not intent.is_file():
            continue
        branch = git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        subject = next(
            (
                line.strip("# ")
                for line in intent.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ),
            "",
        )
        steps = (("add", "-A"), ("commit", "-F", str(INTENT)), ("push", "-u", "origin", branch))
        failed = next((" ".join(s) for s in steps if git(tree, *s).returncode != 0), "")
        if not failed:
            pr = [
                "gh",
                "pr",
                "create",
                "--head",
                branch,
                "--title",
                subject,
                "--body-file",
                str(INTENT),
                "--label",
                AUTOMERGE_LABEL,
            ]
            made = subprocess.run(
                pr, cwd=tree, capture_output=True, text=True, check=False, creationflags=NO_WINDOW
            )
            failed = "" if made.returncode == 0 else "gh pr create"
        if not failed:
            intent.replace(intent.with_name("ship-intent.shipped.md"))
        shipped.append(f"{branch}: {'shipped' if not failed else 'FAILED at ' + failed}")
    return shipped


# --- the watch ----------------------------------------------------------------------------


def watch(argv: list[str], now: _dt.datetime | None = None) -> int:
    now = now or _dt.datetime.now(_dt.UTC)
    workspace = workspace_of(argv)
    mode = mode_of(argv, workspace) if workspace else "off"
    notes: list[str] = []
    state_path = (
        workspace.parent / worktree_tiers.BOXES_DIR_NAME / STATE_NAME if workspace else None
    )
    state = load_state(state_path) if state_path else {}
    head = git(REPO_ROOT, "rev-parse", "HEAD").stdout.strip()
    if mode != "off":
        ok, what = self_update(REPO_ROOT)
        notes.append(f"watchdog: self-update -- {what}")
        if not ok:
            file_once(
                state, signature("pass-stale", what, head), "pass-stale", what, str(REPO_ROOT), now
            )
    code, output = run_current(argv, mode, notes)
    print(output, end="")  # pythonw.exe runs this job with sys.stdout None
    kind, detail = judge(code, output)
    if kind:
        evidence = str(_write(FAILURE_LOG, output))
        head = git(REPO_ROOT, "rev-parse", "HEAD").stdout.strip()
        sig = signature(kind, detail, head)
        fresh = file_once(state, sig, kind, detail, evidence, now)
        notes.append(f"watchdog: the pass failed ({kind}): {detail}")
        if mode == "dispatch":
            notes += ship_rescues(home(REPO_ROOT))
            if fresh and kind in RESCUED:
                notes.append(f"watchdog: {rescue(home(REPO_ROOT), kind, detail, output, now)}")
    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if notes:
        _write(ARTIFACT, "\n".join(notes).rstrip() + "\n", "a")
        print("\n".join(notes))
    return exit_code(code, kind)


def exit_code(code: int | None, kind: str) -> int:
    """What the scheduler's Last Result says of this fire: the pass's own report, `2`
    for a pass that failed itself -- and `0` for a `STALE` one left to the next fire,
    which routes it: nothing failed, and `75` read as a failed run."""
    if code == STALE and not kind:
        return 0
    return code if code in REPORTED and kind not in RESCUED else 2


def _write(relative: Path, text: str, mode: str = "w") -> Path:
    """Write the failed pass's output, or (`mode="a"`) add the watchdog's lines to the
    pass's record, which the pass rewrote this run."""
    path = REPO_ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(text)
    return path


def main(argv: list[str] | None = None) -> int:
    return watch(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    # The pass prints non-ASCII; a scheduled console is cp1252, and a crash writing the
    # record of a crash is the one this file cannot report.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
