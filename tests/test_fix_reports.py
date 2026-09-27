"""`scripts/fix_reports.py`: the stamp a dispatch leaves and the report a fixer writes back."""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_reports

NOW = _dt.datetime(2026, 9, 19, 9, 0, tzinfo=_dt.UTC)


def test_the_stamp_round_trips_and_a_missing_or_corrupt_one_reads_as_empty(tmp_path):
    assert fix_reports.read_stamp(tmp_path) == {}
    fix_reports.stamp(
        tmp_path,
        "pr:carameli:412:abc:d:dispatch",
        "1 check failing",
        NOW,
        problem="p",
        agent="codex",
    )
    assert fix_reports.read_stamp(tmp_path) == {
        "key": "pr:carameli:412:abc:d:dispatch",
        "what": "1 check failing",
        "when": "2026-09-19T09:00:00+00:00",
        "problem": "p",
        "agent": "codex",
    }
    (tmp_path / fix_reports.STAMP_FILE).write_text("{not json", encoding="utf-8")
    assert fix_reports.read_stamp(tmp_path) == {}


def test_a_restamp_keeps_the_mark_of_a_tree_the_pass_cut(tmp_path):
    """A fixer sent back at a fixer's branch is still on the pass's branch; one sent at a
    person's never gains the mark. Only the dispatch that cut the tree writes it."""
    fix_reports.stamp(tmp_path, "a", "n", NOW)
    assert not (tmp_path / fix_reports.ORIGIN_FILE).exists()
    (tmp_path / fix_reports.ORIGIN_FILE).write_text("fix-pass\n", encoding="utf-8")
    fix_reports.stamp(tmp_path, "b", "n", NOW)
    assert (tmp_path / fix_reports.ORIGIN_FILE).is_file()


def test_a_new_stamp_clears_the_report_an_earlier_session_left_in_the_tree(tmp_path):
    """Read against the new key, the old report marked the new dispatch blocked before
    its session had started -- and a blocked entry never expires."""
    (tmp_path / "logs").mkdir()
    (tmp_path / fix_reports.BLOCKED_FILE).write_text("needs a database\n", encoding="utf-8")
    # An earlier launch's record too: a tab dispatch writes none, and must not inherit it.
    (tmp_path / fix_reports.LAUNCH_FILE).write_text("{}", encoding="utf-8")
    fix_reports.stamp(tmp_path, "pr:carameli:412:new:d:dispatch", "n", NOW)
    assert fix_reports.blocked_reason(tmp_path) == ""
    assert not (tmp_path / fix_reports.LAUNCH_FILE).exists()
    assert fix_reports.read_stamp(tmp_path)["key"] == "pr:carameli:412:new:d:dispatch"


def test_a_launch_record_reads_back_as_one_line_and_its_absence_as_nothing(tmp_path):
    assert fix_reports.launch_line(tmp_path) == ""
    done = subprocess.CompletedProcess(["claude"], 2, "", "error: unknown option\n  --bg x\n")
    fix_reports.record_launch(tmp_path, ["claude", "--bg", "--", "the prompt"], done)
    assert fix_reports.launch_line(tmp_path) == "launcher exited 2: error: unknown option --bg x"
    (tmp_path / fix_reports.LAUNCH_FILE).write_text("[", encoding="utf-8")
    assert fix_reports.launch_line(tmp_path) == ""


def test_a_blocked_report_is_its_first_lines_trimmed_and_absent_is_empty(tmp_path):
    assert fix_reports.blocked_reason(tmp_path) == ""
    (tmp_path / "logs").mkdir()
    (tmp_path / fix_reports.BLOCKED_FILE).write_text(
        "# Blocked\n\nThe fixture needs a database the runner lacks.\n" + "x" * 900,
        encoding="utf-8",
    )
    reason = fix_reports.blocked_reason(tmp_path)
    assert reason.startswith("Blocked The fixture needs a database")
    assert len(reason) <= fix_reports.REASON_LIMIT


def listing(*entries: tuple[Path, str]) -> str:
    return "".join(
        f"worktree {path.as_posix()}\nHEAD 1\nbranch refs/heads/{branch}\n\n"
        for path, branch in entries
    )


def test_agent_trees_walks_every_worktree_of_every_checkout_on_disk(tmp_path):
    (tmp_path / "carameli").mkdir()
    tree = tmp_path / "carameli" / ".claude" / "worktrees" / "x"
    detached = tmp_path / "carameli" / ".claude" / "worktrees" / "d"
    text = listing((tmp_path / "carameli", "main"), (tree, "agent/x")) + (
        f"worktree {detached.as_posix()}\nHEAD 3\ndetached\n"
    )

    def git_for(project_dir):
        def git(*args):
            code = 0 if project_dir.name == "carameli" else 1
            return subprocess.CompletedProcess(args, code, text if code == 0 else "", "")

        return git

    found = list(fix_reports.agent_trees(tmp_path, ["carameli", "devkit", "ghost"], git_for))
    assert found == [
        ("carameli", tmp_path / "carameli", "main"),
        ("carameli", tree, "agent/x"),
        ("carameli", detached, ""),
    ]


def test_every_tree_is_read_once_with_its_stamp_and_friction(tmp_path):
    """The pass walks the trees once and reads what each holds: the stamp that matches a
    report or a dead session back to its dispatch, and the friction a session wrote."""
    (tmp_path / "carameli").mkdir()
    stamped = tmp_path / "carameli" / ".claude" / "worktrees" / "x"
    quiet = tmp_path / "carameli" / ".claude" / "worktrees" / "z"
    for tree in (stamped, quiet):
        (tree / "logs").mkdir(parents=True)
    fix_reports.stamp(stamped, "pr:carameli:412:abc:d:dispatch", "n", NOW, problem="p")
    (stamped / fix_reports.FRICTION_FILE).write_text("no .venv\n", encoding="utf-8")
    text = listing((stamped, "agent/x"), (quiet, "agent/z"))

    def git_for(_project_dir):
        return lambda *a: subprocess.CompletedProcess(a, 0, text, "")

    found = fix_reports.read_trees(tmp_path, ["carameli"], git_for)
    assert [(t.branch, t.stamp.get("problem"), t.friction) for t in found] == [
        ("agent/x", "p", ("no .venv",)),
        ("agent/z", None, ()),
    ]
    assert found[0].path == stamped and found[0].project == "carameli"


# --- what a tree says beyond a report -----------------------------------------------------


def _stamped(tree: Path, sent: _dt.datetime, agent: str = "claude") -> Path:
    (tree / "logs").mkdir(parents=True, exist_ok=True)
    fix_reports.stamp(tree, "pr:carameli:412:abc:d:dispatch", "n", sent, problem="p", agent=agent)
    return tree


def _transcript(projects: Path, tree: Path, age: _dt.timedelta, now: _dt.datetime) -> Path:
    import os

    path = fix_reports.transcript_dir(tree, projects) / "s.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    began = (now - age).isoformat()
    path.write_text(f'{{"type": "user", "timestamp": "{began}"}}\n', encoding="utf-8")
    when = (now - age).timestamp()
    os.utime(path, (when, when))
    return path


def test_the_transcript_dir_is_claude_codes_slug_of_the_tree(tmp_path):
    tree = Path(r"C:\Users\a\vs-code\devkit\.claude\worktrees\fix-vendored-0920")
    assert fix_reports.transcript_dir(tree, tmp_path) == (
        tmp_path / "C--Users-a-vs-code-devkit--claude-worktrees-fix-vendored-0920"
    )


def test_a_session_is_working_done_or_dead(tmp_path):
    now = _dt.datetime.now(_dt.UTC)
    projects = tmp_path / "projects"
    tree = _stamped(tmp_path / "t", now - _dt.timedelta(minutes=10))
    assert fix_reports.session_state(tree, now, projects) == (fix_reports.WORKING, ""), (
        "in its grace"
    )
    tree = _stamped(tmp_path / "t", now - _dt.timedelta(hours=1))
    assert fix_reports.session_state(tree, now, projects)[0] == fix_reports.NEVER_STARTED
    log = _transcript(projects, tree, _dt.timedelta(minutes=5), now)
    assert fix_reports.session_state(tree, now, projects) == (fix_reports.WORKING, str(log))
    tree = _stamped(tmp_path / "t", now - _dt.timedelta(hours=4))
    log = _transcript(projects, tree, _dt.timedelta(hours=2), now)
    assert fix_reports.session_state(tree, now, projects)[0] == fix_reports.NO_OUTCOME
    (tree / "logs" / "ship-intent.md").write_text("S\n", encoding="utf-8")
    assert fix_reports.session_state(tree, now, projects) == (fix_reports.DONE, str(log))


def test_the_stamped_session_is_told_from_another_session_in_the_same_tree(tmp_path):
    """The first supervised run sent a fixer into a worktree an interactive session also
    lived in, and judged the fixer by the interactive session's transcript -- the newest
    file there. The dispatched session is the first one to start after the stamp."""
    now = _dt.datetime.now(_dt.UTC)
    projects = tmp_path / "projects"
    tree = _stamped(tmp_path / "t", now - _dt.timedelta(hours=2))
    folder = fix_reports.transcript_dir(tree, projects)
    folder.mkdir(parents=True)
    resident = folder / "resident.jsonl"
    resident.write_text(
        f'{{"timestamp": "{(now - _dt.timedelta(days=1)).isoformat()}"}}\n'
        f'{{"timestamp": "{(now - _dt.timedelta(minutes=5)).isoformat()}"}}\n',
        encoding="utf-8",
    )
    fixer = folder / "fixer.jsonl"
    fixer.write_text(
        '{"type": "file-history-snapshot"}\n'
        f'{{"type": "user", "timestamp": "{(now - _dt.timedelta(hours=2)).isoformat()}"}}\n',
        encoding="utf-8",
    )
    import os

    old = (now - _dt.timedelta(hours=2)).timestamp()
    os.utime(fixer, (old, old))
    sent = now - _dt.timedelta(hours=2)
    assert fix_reports.session_transcript(tree, sent, projects) == fixer
    assert fix_reports.newest_transcript(tree, projects) == resident, "the resident spoke last"
    assert fix_reports.session_state(tree, now, projects) == (fix_reports.NO_OUTCOME, str(fixer))
    assert fix_reports.active_transcript(tree, now, projects) == resident
    assert fix_reports.active_transcript(tree, now + _dt.timedelta(hours=3), projects) is None
    assert fix_reports.started_at(tmp_path / "missing.jsonl") is None


def test_bookkeeping_appended_after_a_session_ended_is_not_the_session_speaking(tmp_path):
    """`claude stop` appends `last-prompt`/`cost-state` rows to a finished session's file,
    which refreshed its mtime and held its tree as "a session is working in" for another
    90 minutes. Activity is the last record that carries a `timestamp`."""
    now = _dt.datetime.now(_dt.UTC)
    projects = tmp_path / "projects"
    tree = _stamped(tmp_path / "t", now - _dt.timedelta(hours=4))
    log = _transcript(projects, tree, _dt.timedelta(hours=2), now)
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "last-prompt", "sessionId": "s"}\n{"type": "cost-state"}\n')
    assert fix_reports.last_spoke(log) == now - _dt.timedelta(hours=2)
    assert fix_reports.active_transcript(tree, now, projects) is None
    assert fix_reports.session_state(tree, now, projects) == (fix_reports.NO_OUTCOME, str(log))


def test_the_last_record_is_found_past_a_line_split_by_the_read_window(tmp_path, monkeypatch):
    now = _dt.datetime.now(_dt.UTC)
    path = tmp_path / "s.jsonl"
    path.write_text(
        f'{{"type": "user", "timestamp": "{now.isoformat()}", "pad": "{"x" * 400}"}}\n'
        '{"type": "last-prompt"}\nnot json\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(fix_reports, "TAIL_BYTES", 100)
    assert fix_reports.last_spoke(path) is not None, "falls back to the file's mtime"
    monkeypatch.setattr(fix_reports, "TAIL_BYTES", 10_000)
    assert fix_reports.last_spoke(path) == now
    assert fix_reports.last_spoke(tmp_path / "missing.jsonl") is None


def test_a_session_judged_once_or_one_that_leaves_no_transcript_is_not_judged(tmp_path):
    now = _dt.datetime.now(_dt.UTC)
    tree = _stamped(tmp_path / "t", now - _dt.timedelta(hours=4), agent="codex")
    assert fix_reports.session_state(tree, now, tmp_path) == ("", ""), "a Codex tab has a person"
    tree = _stamped(tmp_path / "u", now - _dt.timedelta(hours=4))
    fix_reports.note_on_stamp(tree, "dead", "never started")
    assert fix_reports.session_state(tree, now, tmp_path) == ("", "")
    assert fix_reports.session_state(tmp_path / "nothing", now, tmp_path) == ("", "")


def test_friction_lines_drop_markup_and_blanks_and_filing_away_reads_them_once(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / fix_reports.FRICTION_FILE).write_text(
        "# Friction\n\n- the evidence dir was empty\n* 2. no .venv in the tree\n", encoding="utf-8"
    )
    assert fix_reports.friction_lines(tmp_path) == (
        "the evidence dir was empty",
        "no .venv in the tree",
    ), "a heading is not a thing the harness cost"
    fix_reports.file_away(tmp_path, fix_reports.FRICTION_FILE)
    assert fix_reports.friction_lines(tmp_path) == ()
    assert (tmp_path / "logs" / "friction.filed.md").exists()
    fix_reports.file_away(tmp_path, fix_reports.FRICTION_FILE)  # nothing there: no error


def test_a_line_the_tree_already_had_filed_is_not_read_again(tmp_path):
    """a174d816: the session wrote its friction file whole after the pass had filed the
    first copy away, so the same line came back, was filed a second time, and reopened a
    group already retired with its fix as a recurrence. A new line beside it still reads."""
    (tmp_path / "logs").mkdir()
    friction = tmp_path / fix_reports.FRICTION_FILE
    friction.write_text("- no .venv in the tree\n", encoding="utf-8")
    fix_reports.file_away(tmp_path, fix_reports.FRICTION_FILE)
    friction.write_text("- no .venv in the tree\n- the rehearsal exited 1\n", encoding="utf-8")
    assert fix_reports.friction_lines(tmp_path) == ("the rehearsal exited 1",)
    friction.write_text("* no .venv in the tree\n", encoding="utf-8")  # markup aside, same
    assert fix_reports.friction_lines(tmp_path) == ()


def test_a_line_about_claude_codes_worktree_guard_is_not_filed(tmp_path):
    """ced1c085: the guard is Claude Code's, `.claude/rules/engineering.md` already says
    so and names the spellings that pass it, and `session_friction` never files its
    refusals -- but written into a friction file, one still became a group to retire."""
    (tmp_path / "logs").mkdir()
    (tmp_path / fix_reports.FRICTION_FILE).write_text(
        "- a compound Bash line was refused by Claude Code's worktree isolation guard as "
        '"names git in a form too complex to verify"; PowerShell ran it\n'
        "- the command cannot be shown not to be git\n"
        "- `harness_triage.py --resolve-like` crashed on a group with no host\n",
        encoding="utf-8",
    )
    assert fix_reports.friction_lines(tmp_path) == (
        "`harness_triage.py --resolve-like` crashed on a group with no host",
    )


def test_the_newest_transcript_is_the_latest_written(tmp_path):
    now = _dt.datetime.now(_dt.UTC)
    tree = tmp_path / "t"
    assert fix_reports.newest_transcript(tree, tmp_path / "p") is None
    older = _transcript(tmp_path / "p", tree, _dt.timedelta(hours=2), now)
    newer = older.with_name("b.jsonl")
    newer.write_text("{}\n", encoding="utf-8")
    assert fix_reports.newest_transcript(tree, tmp_path / "p") == newer


def test_inherit_origin_copies_only_a_mark_that_exists(tmp_path):
    """What `agent-worktree.py new` calls, so a sibling tree a fixer cuts is fixer work."""
    home, tree = tmp_path / "home", tmp_path / "tree"
    assert fix_reports.inherit_origin(tree, home) is False and not tree.exists()
    (home / "logs").mkdir(parents=True)
    (home / fix_reports.ORIGIN_FILE).write_text("fix-pass\n", encoding="utf-8")
    assert fix_reports.inherit_origin(tree, home) is True
    assert (tree / fix_reports.ORIGIN_FILE).read_text(encoding="utf-8") == "fix-pass\n"
