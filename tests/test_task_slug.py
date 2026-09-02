"""Tests for the per-session task slug.

The contract this file holds down is a *handshake between two processes*: a
UserPromptSubmit hook writes, a PreToolUse hook in a different session-directory
reads, and the only thing they share is the session id. Nothing else in the workspace
connects those two events, so if the key or the location drift apart the symptom is
not an error — it is every box quietly reverting to `ws-<hex>` names, which is what
the whole file exists to stop.

Every helper is also required to be *silent on failure*: this runs before every
prompt, and the worst honest outcome of a failure here is an uglier branch name.
"""

from __future__ import annotations

import json

from support import task_slug, worktree


def test_a_slug_written_by_the_prompt_hook_is_read_back_by_the_guard(tmp_path):
    """The handshake, end to end and in one assertion."""
    assert task_slug.record(tmp_path, "sess-1", "add-voicemail-retry") is not None
    assert task_slug.read(tmp_path, "sess-1") == "add-voicemail-retry"


def test_slugs_live_beside_the_leases_not_in_a_repo(tmp_path):
    """Both ends can compute this path; neither can compute the other's git dir."""
    assert task_slug.slugs_dir(tmp_path).parent == worktree.boxes_root(tmp_path)


def test_an_unrecorded_session_reads_as_empty_not_an_error(tmp_path):
    assert task_slug.read(tmp_path, "never-seen") == ""
    assert task_slug.read(tmp_path, "") == ""


def test_a_later_prompt_overwrites_an_earlier_one(tmp_path):
    """A session whose first prompt is "hi" should still get the real task's name."""
    task_slug.record(tmp_path, "sess-1", "hi")
    task_slug.record(tmp_path, "sess-1", "rewrite-the-scheduler")
    assert task_slug.read(tmp_path, "sess-1") == "rewrite-the-scheduler"


def test_a_session_id_cannot_escape_the_slugs_directory(tmp_path):
    """The id reaches the filesystem as a name, so it is constrained, not trusted."""
    assert task_slug.safe_session("../../etc/passwd") == "etcpasswd"
    assert task_slug.safe_session("a/b\\c") == "abc"
    assert task_slug.safe_session("") == ""
    assert len(task_slug.safe_session("x" * 500)) == 64


def test_an_unusable_session_id_records_nothing_rather_than_writing_somewhere_odd(tmp_path):
    assert task_slug.record(tmp_path, "///", "topic") is None
    assert task_slug.record(tmp_path, "sess-1", "") is None


def test_prune_keeps_the_most_recent_and_drops_the_rest(tmp_path):
    """One file per prompt per session, and nothing deletes them on the normal path --
    on a workstation short of disk that is a directory that grows forever."""
    import os
    import time

    for n in range(6):
        path = task_slug.record(tmp_path, f"sess-{n}", f"topic-{n}")
        os.utime(path, (time.time() + n, time.time() + n))

    assert task_slug.prune(tmp_path, keep=2) == 4
    survivors = {p.name for p in task_slug.slugs_dir(tmp_path).iterdir()}
    assert survivors == {"sess-4", "sess-5"}


def test_prune_on_a_directory_that_does_not_exist_is_zero_not_a_crash(tmp_path):
    assert task_slug.prune(tmp_path) == 0


def _stdin(text: str):
    class _Fake:
        def read(self):
            return text

    return _Fake()


def _workspace(tmp_path):
    path = tmp_path / "alex-projects.code-workspace"
    path.write_text(json.dumps({"folders": [{"path": "carameli"}]}), encoding="utf-8")
    return path


def test_main_records_the_prompts_topic_for_the_session(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    payload = json.dumps(
        {"session_id": "sess-9", "prompt": "Please add a retry to the voicemail poller"}
    )
    monkeypatch.setattr("sys.stdin", _stdin(payload))

    assert task_slug.main(["--workspace", str(workspace)]) == 0

    # `slug_from_prompt` strips the filler, so the name is what the task is *about*.
    recorded = task_slug.read(tmp_path, "sess-9")
    assert "voicemail" in recorded
    assert "please" not in recorded


def test_main_does_not_replace_the_task_with_a_task_notification(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    task_slug.record(tmp_path, "sess-9", "serialize-auth-skin-load")
    payload = json.dumps(
        {
            "session_id": "sess-9",
            "prompt": "<task-notification>Background command completed successfully</task-notification>",
        }
    )
    monkeypatch.setattr("sys.stdin", _stdin(payload))

    assert task_slug.main(["--workspace", str(workspace)]) == 0
    assert task_slug.read(tmp_path, "sess-9") == "serialize-auth-skin-load"


def test_main_is_silent_without_a_workspace_file(tmp_path, monkeypatch):
    """A CI runner, a fresh clone, anyone else's machine: there is no box tier."""
    monkeypatch.setattr("sys.stdin", _stdin(json.dumps({"session_id": "s", "prompt": "x"})))
    assert task_slug.main(["--workspace", str(tmp_path / "nope.code-workspace")]) == 0


def test_main_never_fails_a_prompt_on_malformed_input(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    for raw in ("", "not json", "[]", "null"):
        monkeypatch.setattr("sys.stdin", _stdin(raw))
        assert task_slug.main(["--workspace", str(workspace)]) == 0


# --- the denylist -----------------------------------------------------------
#
# The defect: the slug is cut from the prompt, so a task whose whole point is a word
# turns that word into a branch name and pushes it. Reported 2026-08-24 against
# `agent/make-sure-references-<brandname>-0824` on a public repo.


def _deny_file(tmp_path, text: str):
    path = worktree.boxes_root(tmp_path) / task_slug.DENY_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_redact_drops_the_word_carrying_the_term_and_nothing_else():
    """The whole rule, without the filesystem: a slug is hyphen-separated words, and a
    term takes the word it appears in rather than the slug it appears in."""
    assert task_slug.redact("make-sure-references-cloudli", ["cloudli"]) == "make-sure-references"
    assert task_slug.redact("make-sure-references-cloudli", []) == "make-sure-references-cloudli"
    assert task_slug.redact("cloudli", ["cloudli"]) == ""


def test_a_denied_term_never_reaches_a_recorded_slug(tmp_path):
    """The exact report: the branch that would have been pushed, minus the token."""
    _deny_file(tmp_path, "cloudli\n")
    task_slug.record(tmp_path, "sess-1", "make-sure-references-cloudli")
    assert task_slug.read(tmp_path, "sess-1") == "make-sure-references"


def test_the_denylist_matches_inside_a_word_not_only_on_its_own(tmp_path):
    """Slugification glues punctuation away, so the near-miss spellings are the ones
    that would otherwise publish the term."""
    _deny_file(tmp_path, "cloudli\n")
    task_slug.record(tmp_path, "sess-1", "drop-cloudlis-and-cloudli2-refs")
    assert task_slug.read(tmp_path, "sess-1") == "drop-and-refs"


def test_the_environment_is_a_second_source_for_one_session(tmp_path, monkeypatch):
    monkeypatch.setenv(task_slug.DENY_ENV, " Acme , widgetco ")
    task_slug.record(tmp_path, "sess-1", "port-acme-to-widgetco-api")
    assert task_slug.read(tmp_path, "sess-1") == "port-to-api"


def test_comments_and_blank_lines_are_not_terms(tmp_path):
    """A `#` line that counted as a term would strip every word containing a hash --
    and a blank one is a substring of everything, which redacts the whole slug."""
    _deny_file(tmp_path, "# names we never publish\n\n   \ncloudli\n")
    task_slug.record(tmp_path, "sess-1", "rename-the-poller")
    assert task_slug.read(tmp_path, "sess-1") == "rename-the-poller"


def test_a_slug_that_is_entirely_denied_is_not_recorded(tmp_path):
    """ "" is what the guard already falls back on -- `ws-<session>`, ugly and safe."""
    _deny_file(tmp_path, "cloudli\n")
    assert task_slug.record(tmp_path, "sess-1", "cloudli") is None
    assert task_slug.read(tmp_path, "sess-1") == ""


def test_no_denylist_leaves_the_slug_exactly_as_it_was(tmp_path):
    """The overwhelmingly common case, and the one a failure here must degrade to."""
    assert task_slug.deny_terms(tmp_path, env={}) == []
    task_slug.record(tmp_path, "sess-1", "add-voicemail-retry")
    assert task_slug.read(tmp_path, "sess-1") == "add-voicemail-retry"


def test_main_redacts_the_prompt_before_it_becomes_a_branch_name(tmp_path, monkeypatch):
    """End to end from the prompt, which is where the term actually arrives."""
    workspace = _workspace(tmp_path)
    _deny_file(tmp_path, "cloudli\n")
    payload = json.dumps(
        {"session_id": "sess-9", "prompt": "Make sure references to Cloudli are removed"}
    )
    monkeypatch.setattr("sys.stdin", _stdin(payload))

    assert task_slug.main(["--workspace", str(workspace)]) == 0
    assert "cloudli" not in task_slug.read(tmp_path, "sess-9")
