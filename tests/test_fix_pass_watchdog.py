"""`scripts/fix-pass-watchdog.py`: the pass's supervisor keeps it current and repairs it.

The judgement and the rationing are pure; `self_update` runs against real repositories
under `tmp_path`, since "fast-forward only when safe" is exactly what a stub would get
wrong; everything that would spawn a session is replaced.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

import pytest
from support import load_script

watchdog = load_script("scripts/fix-pass-watchdog.py")
triage = load_script("scripts/harness_triage.py")

NOW = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)
TRACE = (
    "Traceback (most recent call last):\n  File x\nTypeError: run() got an unexpected keyword 'x'"
)


# --- the judgement ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "output", "kind"),
    [
        (0, "fix-pass: mode=dispatch", ""),
        (1, "shipped  x -- failed: push: no", ""),
        (1, TRACE, "pass-crashed"),
        (2, "fix-pass: not usable from this PATH: gh", "pass-refused"),
        (2, TRACE, "pass-crashed"),
        (3, "", "pass-crashed"),
        (None, "half a record", "pass-hung"),
    ],
)
def test_the_pass_reporting_is_told_from_the_pass_failing(code, output, kind):
    assert watchdog.judge(code, output)[0] == kind


def test_a_crash_is_named_by_its_last_line():
    assert watchdog.judge(1, TRACE)[1] == "TypeError: run() got an unexpected keyword 'x'"


THREAD_TRACE = (
    "fix-pass: record at logs/fix-pass.log\n"
    "Exception in thread Thread-555 (_readerthread):\n"
    "Traceback (most recent call last):\n"
    '  File "subprocess.py", line 1599, in _readerthread\n'
    "UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d in position 4001\n"
    "  provisioning C:/w/.worktrees/roguelike failed: npm ci could not run\n"
)


def test_a_background_threads_traceback_in_a_pass_that_reported_is_not_a_crash():
    """Round four: a reader thread failed to decode a child's output, its traceback
    landed mid-record, and the watchdog sent a rescue at a pass that had finished. It is
    a defect to file -- output was lost -- but the pass did not crash."""
    kind, detail = watchdog.judge(0, THREAD_TRACE)
    assert kind == "pass-thread-error"
    assert detail == "UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d in position 4001"
    assert watchdog.judge(1, THREAD_TRACE + TRACE)[0] == "pass-crashed", "a crash after it"
    assert watchdog.judge(2, THREAD_TRACE)[0] == "pass-crashed", "the pass did not report"


def test_a_thread_error_is_filed_and_sent_no_rescue(watched):
    watched["outcome"] = (0, THREAD_TRACE)
    assert watchdog.watch(watched["argv"], NOW) == 0, "the pass itself reported"
    assert watched["rescues"] == []
    assert [f.detail.split(":")[0] for f in findings(watched["devkit"])] == ["pass-thread-error"]


def test_a_failure_signature_ignores_numbers_and_quoted_names_but_not_the_commit():
    one = watchdog.signature("pass-crashed", "KeyError: 'head' at line 12", "abc")
    assert one == watchdog.signature("pass-crashed", "KeyError: 'base' at line 40", "abc")
    assert one != watchdog.signature("pass-crashed", "KeyError: 'head' at line 12", "def"), (
        "a new devkit commit that still fails the same way gets another repair"
    )


def test_the_mode_is_the_flag_else_the_workspace_switch_else_off(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {\n  "devkit.fixPass": "dispatch"\n}}', encoding="utf-8")
    assert watchdog.mode_of(["--mode", "plan"], workspace) == "plan"
    assert watchdog.mode_of(["--scheduled"], workspace) == "dispatch"
    assert watchdog.mode_of([], tmp_path / "missing") == "off"
    assert watchdog.workspace_of(["--workspace", str(workspace)]) == workspace
    assert watchdog.workspace_of(["--scheduled"]) is None


def test_a_scheduled_interpreter_is_swapped_for_its_console_twin(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(watchdog.sys, "executable", str(tmp_path / "pythonw.exe"))
    assert watchdog.console_python() == str(tmp_path / "python.exe")
    monkeypatch.setattr(watchdog.sys, "executable", str(tmp_path / "python.exe"))
    assert watchdog.console_python() == str(tmp_path / "python.exe")


# --- the watch ----------------------------------------------------------------------------


@pytest.fixture
def watched(tmp_path, monkeypatch):
    """A workspace in `dispatch`, a devkit checkout at a fixed commit, and a log of what
    the watchdog would have done."""
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.fixPass": "dispatch"}}', encoding="utf-8")
    devkit = tmp_path / "devkit"
    devkit.mkdir()
    seen: dict = {"rescues": [], "updates": 0, "outcome": (0, "fix-pass: mode=dispatch\n")}
    monkeypatch.setattr(watchdog, "REPO_ROOT", devkit)
    monkeypatch.setattr(
        watchdog, "git", lambda root, *a: subprocess.CompletedProcess(a, 0, "abc123\n", "")
    )
    monkeypatch.setattr(watchdog, "run_pass", lambda argv: seen["outcome"])

    def update(root):
        seen["updates"] += 1
        return True, "current"

    monkeypatch.setattr(watchdog, "self_update", update)
    monkeypatch.setattr(
        watchdog,
        "rescue",
        lambda root, kind, detail, output, now: seen["rescues"].append(kind) or "sent",
    )
    monkeypatch.setattr(watchdog, "ship_rescues", lambda root: [])
    seen["argv"] = ["--scheduled", "--workspace", str(workspace)]
    seen["devkit"] = devkit
    return seen


def findings(devkit: Path) -> list:
    return triage.open_items(triage.load(devkit))


def test_a_pass_that_reports_is_passed_through_untouched(watched):
    watched["outcome"] = (1, "shipped  x -- failed: push: no\n")
    assert watchdog.watch(watched["argv"], NOW) == 1
    assert findings(watched["devkit"]) == [] and watched["rescues"] == []
    assert watched["updates"] == 1, "kept current before every pass"


def test_a_crash_is_filed_and_repaired_once_per_signature(watched):
    watched["outcome"] = (1, TRACE)
    assert watchdog.watch(watched["argv"], NOW) == 2
    assert watchdog.watch(watched["argv"], NOW) == 2
    assert watched["rescues"] == ["pass-crashed"], "one repair per failure per devkit commit"
    [found] = findings(watched["devkit"])
    assert found.event == "fix-pass-finding" and found.detail.startswith("pass-crashed: TypeError")
    record = (watched["devkit"] / watchdog.ARTIFACT).read_text(encoding="utf-8")
    assert "watchdog: the pass failed (pass-crashed)" in record
    assert "Traceback" in (watched["devkit"] / watchdog.FAILURE_LOG).read_text(encoding="utf-8")


def test_a_hang_is_repaired_too(watched):
    watched["outcome"] = (None, "half a record")
    assert watchdog.watch(watched["argv"], NOW) == 2
    assert watched["rescues"] == ["pass-hung"]


def test_a_refusal_is_filed_and_never_sent_a_session(watched):
    """A missing CLI or an expired `gh` login is outside the repository: no session can
    repair it, so it is filed -- the one thing the pass cannot hand to the devkit
    session -- and the scheduler's red result is what a person sees."""
    watched["outcome"] = (2, "fix-pass: not usable from this PATH: gh -- ... gh auth login")
    assert watchdog.watch(watched["argv"], NOW) == 2
    assert watched["rescues"] == []
    assert [f.detail.split(":")[0] for f in findings(watched["devkit"])] == ["pass-refused"]


def test_plan_mode_files_a_crash_but_sends_no_one(watched, tmp_path):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.fixPass": "plan"}}', encoding="utf-8")
    watched["outcome"] = (1, TRACE)
    assert watchdog.watch(watched["argv"], NOW) == 2
    assert watched["rescues"] == [] and len(findings(watched["devkit"])) == 1


def test_off_neither_updates_nor_judges_beyond_the_exit(watched, tmp_path):
    (tmp_path / "w.code-workspace").write_text('{"settings": {}}', encoding="utf-8")
    assert watchdog.watch(watched["argv"], NOW) == 0
    assert watched["updates"] == 0


def test_a_pass_whose_code_moved_is_updated_and_run_once_more(watched, monkeypatch):
    """carameli #395 was misrouted by a pass that started 21s before the routing fix
    merged. The pass now holds its sessions and exits STALE; the rerun routes them."""
    outcomes = [(watchdog.STALE, "held     carameli #395\n"), (0, "sent     carameli #395\n")]
    timeouts: list = []

    def run(argv, timeout=watchdog.TIMEOUT):
        timeouts.append(timeout)
        return outcomes.pop(0)

    monkeypatch.setattr(watchdog, "run_pass", run)
    assert watchdog.watch(watched["argv"], NOW) == 0
    assert watched["updates"] == 2 and outcomes == []
    assert timeouts[1] <= watchdog.TIMEOUT, "the rerun spends what is left, never a fresh 25 min"
    assert findings(watched["devkit"]) == [], "a stale pass is the pass reporting, not failing"
    record = (watched["devkit"] / watchdog.ARTIFACT).read_text(encoding="utf-8")
    assert "the pass's code moved under it -- self-update current; ran it again" in record


def test_a_stale_pass_is_not_rerun_past_the_fires_budget_or_off_its_branch(watched, monkeypatch):
    runs: list = []
    first = (watchdog.STALE, "held\n")
    monkeypatch.setattr(watchdog, "run_pass", lambda *a: runs.append(a) or first)
    monkeypatch.setattr(watchdog, "MIN_RERUN", watchdog.TIMEOUT * 2)
    notes: list[str] = []
    assert watchdog.run_current([], "dispatch", notes) == first and len(runs) == 1
    assert "the next fire routes" in notes[0] and watched["updates"] == 0
    monkeypatch.setattr(watchdog, "MIN_RERUN", _dt.timedelta(0))
    monkeypatch.setattr(watchdog, "self_update", lambda root: (False, "on agent/x, not main"))
    assert watchdog.run_current([], "dispatch", notes) == first and len(runs) == 2
    assert notes[1].endswith("self-update on agent/x, not main; not rerun")
    assert watchdog.run_current([], "off", notes) == first and len(notes) == 2


def test_a_checkout_that_cannot_be_kept_current_is_filed(watched, monkeypatch):
    monkeypatch.setattr(
        watchdog, "self_update", lambda root: (False, "the checkout is on agent/x, not main")
    )
    assert watchdog.watch(watched["argv"], NOW) == 0
    [found] = findings(watched["devkit"])
    assert found.detail == "pass-stale: the checkout is on agent/x, not main"


# --- repair -------------------------------------------------------------------------------


def test_a_rescue_cuts_a_fresh_tree_and_sends_one_background_session(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog.shutil, "which", lambda _c: None)
    assert (
        watchdog.rescue(tmp_path, "pass-crashed", "x", "out", NOW)
        == "no rescue: claude is not on PATH"
    )
    monkeypatch.setattr(watchdog.shutil, "which", lambda _c: "claude")
    gits = []
    monkeypatch.setattr(
        watchdog,
        "git",
        lambda root, *a: gits.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
    )
    spawned = []
    monkeypatch.setattr(
        watchdog.subprocess,
        "run",
        lambda argv, **k: spawned.append((argv, k["cwd"])) or subprocess.CompletedProcess(argv, 0),
    )
    note = watchdog.rescue(tmp_path, "pass-crashed", "TypeError: x", TRACE, NOW)
    tree = tmp_path / ".claude" / "worktrees" / "fix-pass-rescue-0926-1200"
    assert note == f"rescue sent in {tree}"
    assert (
        "worktree",
        "add",
        "--no-track",
        "-b",
        "agent/fix-pass-rescue-0926-1200",
        str(tree),
        "origin/main",
    ) in gits
    [(argv, cwd)] = spawned
    assert argv[:2] == ["claude", "--bg"] and cwd == tree
    assert "fix pass itself is failing (pass-crashed): TypeError: x" in argv[-1]
    assert (tree / watchdog.FAILURE_LOG).read_text(encoding="utf-8") == TRACE
    # Launched like every other background session (`agent_tabs.background_argv`), which
    # this stdlib-only file may not import: it may not ask, loads no MCP server, and the
    # variadic flag cannot swallow the prompt.
    assert "--strict-mcp-config" in argv and argv[-2] == "--"
    assert argv[argv.index("--disallowedTools") + 1] == "AskUserQuestion"
    assert (tree / watchdog.ORIGIN_FILE).is_file(), "fixer work: its PR merges once green"


def test_a_rescue_is_cut_beside_the_main_checkout_not_inside_a_linked_worktree(tmp_path):
    """Run from the supervisor's worktree, the watchdog cut its rescue tree *inside*
    that worktree, where the pass then found and shipped it as a devkit branch."""
    main = tmp_path / "devkit"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    linked = main / ".claude" / "worktrees" / "supervisor"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(linked)], check=True)
    assert watchdog.home(linked).resolve() == main.resolve()
    assert watchdog.home(main).resolve() == main.resolve()
    assert watchdog.home(tmp_path / "not-a-repo") == tmp_path / "not-a-repo"


def test_a_rescue_intent_is_shipped_by_the_watchdog_while_the_pass_cannot(tmp_path, monkeypatch):
    tree = tmp_path / ".claude" / "worktrees" / "fix-pass-rescue-0926-1200"
    (tree / "logs").mkdir(parents=True)
    (tree / watchdog.INTENT).write_text("Stop the pass crashing on x\n\nWhy.\n", encoding="utf-8")
    (tmp_path / ".claude" / "worktrees" / "fix-pass-rescue-idle").mkdir()
    steps = []

    def git(root, *args):
        steps.append(args[0])
        out = "agent/fix-pass-rescue-0926-1200\n" if args[0] == "rev-parse" else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(watchdog, "git", git)
    made = []
    monkeypatch.setattr(
        watchdog.subprocess,
        "run",
        lambda argv, **k: made.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    assert watchdog.ship_rescues(tmp_path) == ["agent/fix-pass-rescue-0926-1200: shipped"]
    assert steps == ["rev-parse", "add", "commit", "push"]
    assert made[0][:3] == ["gh", "pr", "create"] and "Stop the pass crashing on x" in made[0]
    assert made[0][made[0].index("--label") + 1] == "automerge", "fixer PRs merge themselves"
    assert (tree / "logs" / "ship-intent.shipped.md").exists() and not (
        tree / watchdog.INTENT
    ).exists()


# --- keeping current, against real repositories ------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _quiet(repo: Path, tmp_path: Path) -> None:
    """An author, no signing, and no hooks: `install-git-policy.py` sets `core.hooksPath`
    globally, and its branch policy refuses a commit on `main` in a throwaway repo."""
    empty = tmp_path / "no-hooks"
    empty.mkdir(exist_ok=True)
    for key, value in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(empty)),
    ):
        _git(repo, "config", key, value)


@pytest.fixture
def checkout(tmp_path):
    """A static checkout on `main` whose origin has moved one commit ahead of it."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    author = tmp_path / "author"
    _git(tmp_path, "clone", str(origin), str(author))
    _quiet(author, tmp_path)
    (author / "a.txt").write_text("1\n", encoding="utf-8")
    _git(author, "add", "a.txt")
    _git(author, "commit", "-m", "one")
    _git(author, "push", "origin", "main")
    static = tmp_path / "devkit"
    _git(tmp_path, "clone", str(origin), str(static))
    _git(static, "remote", "set-head", "origin", "main")
    _quiet(static, tmp_path)
    (author / "a.txt").write_text("2\n", encoding="utf-8")
    _git(author, "commit", "-am", "two")
    _git(author, "push", "origin", "main")
    return static


def test_a_clean_checkout_on_its_default_branch_is_fast_forwarded(checkout):
    ok, what = watchdog.self_update(checkout)
    assert ok and what.startswith("updated")
    assert (checkout / "a.txt").read_text(encoding="utf-8") == "2\n"
    assert watchdog.self_update(checkout) == (True, "current")


def test_a_dirty_checkout_is_left_and_said(checkout):
    (checkout / "a.txt").write_text("local\n", encoding="utf-8")
    assert watchdog.self_update(checkout) == (
        False,
        "the checkout the pass runs from has uncommitted changes",
    )


def test_a_checkout_on_another_branch_is_left_and_said(checkout):
    _git(checkout, "switch", "-c", "agent/x")
    ok, what = watchdog.self_update(checkout)
    assert not ok and what == "the checkout the pass runs from is on agent/x, not main"


def test_a_linked_worktree_is_someone_running_by_hand_and_left_alone(tmp_path):
    (tmp_path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert watchdog.self_update(tmp_path) == (True, "a linked worktree -- left as it is")


def test_the_pass_is_run_and_a_hang_is_cut_off(tmp_path, monkeypatch):
    script = tmp_path / "pass.py"
    script.write_text("import sys\nprint('record')\nsys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(watchdog, "PASS", script)
    assert watchdog.run_pass([]) == (3, "record\n")
    script.write_text(
        "import time\nprint('started', flush=True)\ntime.sleep(30)\n", encoding="utf-8"
    )
    code, _ = watchdog.run_pass([], _dt.timedelta(seconds=2))
    assert code is None


def test_the_pass_runs_in_utf8_mode_so_no_runner_decodes_with_the_console_page(
    tmp_path, monkeypatch
):
    """Dozens of the pass's runners use `text=True` with no encoding; on a cp1252 console
    a child's `\u201d` (0x9d in UTF-8) killed their reader thread twice in one evening.
    UTF-8 mode fixes every one at the process that starts them all."""
    script = tmp_path / "pass.py"
    script.write_text(
        "import subprocess, sys\n"
        "out = subprocess.run([sys.executable, '-c', \"import sys; sys.stdout.buffer.write("
        "'\\u201d'.encode())\"], capture_output=True, text=True).stdout\n"
        "print(sys.flags.utf8_mode, ascii(out))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(watchdog, "PASS", script)
    assert watchdog.run_pass([]) == (0, "1 '\\u201d'\n")


def test_a_signature_is_filed_once_and_the_state_survives_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "REPO_ROOT", tmp_path)
    state = watchdog.load_state(tmp_path / "missing.json")
    assert state == {}
    assert watchdog.file_once(state, "s1", "pass-crashed", "x", "e", NOW)
    assert not watchdog.file_once(state, "s1", "pass-crashed", "x", "e", NOW)
    assert len(findings(tmp_path)) == 1
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert watchdog.load_state(tmp_path / "bad.json") == {}


def test_the_default_branch_falls_back_to_main(monkeypatch, tmp_path):
    answers = iter(["origin/master\n", ""])
    monkeypatch.setattr(
        watchdog, "git", lambda root, *a: subprocess.CompletedProcess(a, 0, next(answers), "")
    )
    assert watchdog.default_branch(tmp_path) == "master"
    assert watchdog.default_branch(tmp_path) == "main"


def test_the_rescue_prompt_names_the_failure_the_log_and_the_one_way_out():
    text = watchdog.rescue_prompt("pass-hung", "ran past 25 minutes", "agent/fix-pass-rescue-x")
    assert "(pass-hung): ran past 25 minutes" in text
    assert "logs/fix-pass.watchdog.log" in text and "agent/fix-pass-rescue-x" in text
    assert "regression test" in text and "ship skill" in text


def test_the_marks_spelled_here_are_the_ones_the_pass_reads():
    """Stdlib-only, so it cannot import them; this is what keeps the copies equal."""
    fix_reports = load_script("scripts/fix_reports.py")
    sweep = load_script("scripts/sweep.py")
    assert watchdog.ORIGIN_FILE == fix_reports.ORIGIN_FILE
    assert watchdog.AUTOMERGE_LABEL == sweep.AUTOMERGE_LABEL
