"""`scripts/policy_drift.py` — is the installed git-policy runtime still its source?

Cut out of `install-git-policy.py` when that module's structural ceiling was raised a
third consecutive time, which `.claude/rules/engineering.md` makes a defect report
rather than a raise. `tests/test_install_git_policy.py` keeps the end-to-end coverage
that goes through `run_check`; what is here is the tier's own behaviour, and
particularly the three questions it answers *differently on purpose*:

- `compare_install` — does the install match its own **receipt**? Never the working
  tree: a checkout ahead of the pinned release is the normal state, and a check that
  always warns is one nobody reads.
- `worktree_drift` — for a `worktree` install only, does it match the **checkout**? The
  hole this fills cost five days of `devkit-push-gate` never running while `--check`
  printed "up to date".
- `behind_ref` — is there a newer **tag**? Silent for a worktree install, which has no
  ref to fall behind.

Nothing here spawns git or touches the network: the session-start status line calls
`compare_install`, and a status line that can hang is worse than none.
"""

from __future__ import annotations

from pathlib import Path

from support import load_script

drift = load_script("scripts/policy_drift.py")
layout = load_script("scripts/install_policy_layout.py")


def _installed(target: Path, name: str, body: str = "body") -> str:
    """Write one installed file and return its digest, the way an install would."""
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return drift.digest(body.encode("utf-8"))


def _receipt(files: dict[str, str], ref: str = "v1.2.3") -> object:
    return drift.Receipt(ref=ref, installed_at="2026-01-01T00:00:00Z", files=files)


# --- the receipt --------------------------------------------------------------


def test_digest_is_the_sha256_of_the_bytes_written():
    """Hashes rather than a version string, so the check needs no git and no network."""
    assert drift.digest(b"abc") == drift.digest(b"abc")
    assert drift.digest(b"abc") != drift.digest(b"abd")
    assert len(drift.digest(b"")) == 64


def test_a_receipt_round_trips_through_its_json():
    original = _receipt({"pre-commit": "aa"})
    assert drift.Receipt.parse(original.to_json()) == original


def test_a_receipt_is_written_sorted_so_two_installs_diff_cleanly():
    raw = _receipt({"b": "2", "a": "1"}).to_json()
    assert raw.index('"a"') < raw.index('"b"')


def test_an_unparseable_receipt_degrades_to_cannot_tell_rather_than_raising():
    """A corrupt receipt must not take down a session start: every caller reports
    "cannot tell", which is a state they already handle."""
    for raw in ("", "{", "null", "[]", '{"ref": ""}', '{"files": {}}', '{"ref": 1, "files": {}}'):
        assert drift.Receipt.parse(raw) is None


def test_a_receipt_whose_hashes_are_not_strings_is_not_a_receipt():
    """The values are compared against `digest` output, so a non-string would raise
    somewhere far from here."""
    assert drift.Receipt.parse('{"ref": "v1", "files": {"a": 3}}') is None


def test_read_receipt_answers_none_for_a_runtime_installed_before_receipts_existed(tmp_path):
    """None is a real answer, not an error -- including for the install that prompted
    receipts in the first place."""
    assert drift.read_receipt(tmp_path) is None


def test_read_receipt_reads_the_file_beside_the_runtime(tmp_path):
    written = _receipt({"pre-commit": "aa"})
    (tmp_path / drift.RECEIPT_NAME).write_text(written.to_json(), encoding="utf-8")
    assert drift.read_receipt(tmp_path) == written


# --- compare_install ----------------------------------------------------------


def test_a_runtime_with_no_receipt_is_unidentifiable_and_says_so(tmp_path):
    found = drift.compare_install(tmp_path, None)
    assert [d.name for d in found] == [drift.RECEIPT_NAME]
    assert "cannot tell" in found[0].reason


def test_an_untouched_install_is_not_drift(tmp_path):
    digest = _installed(tmp_path, "pre-commit")
    assert drift.compare_install(tmp_path, _receipt({"pre-commit": digest})) == []


def test_an_edited_install_is_reported_as_modified(tmp_path):
    _installed(tmp_path, "pre-commit", "edited by hand")
    found = drift.compare_install(tmp_path, _receipt({"pre-commit": drift.digest(b"original")}))
    assert [(d.name, d.reason) for d in found] == [
        ("pre-commit", "modified since it was installed")
    ]


def test_a_deleted_install_is_reported_as_not_installed(tmp_path):
    found = drift.compare_install(tmp_path, _receipt({"pre-commit": "aa"}))
    assert [(d.name, d.reason) for d in found] == [("pre-commit", "not installed")]


def test_a_file_the_receipt_does_not_hash_is_reported_rather_than_assumed_fine(tmp_path):
    _installed(tmp_path, "pre-commit")
    found = drift.compare_install(tmp_path, _receipt({"pre-commit": ""}))
    assert [d.reason for d in found] == ["not recorded in the receipt"]


def test_a_leftover_of_the_other_layout_is_asked_about_separately(tmp_path):
    """The receipt can only describe what it wrote, so a leftover of the OTHER
    entrypoint layout is invisible to the file loop -- and it is the one difference
    that changes which code actually runs."""
    stale = next(iter(layout.POLICY_ENTRYPOINTS))
    kept = next(name for name in layout.POLICY_ENTRYPOINTS if name != stale)
    digest = _installed(tmp_path, kept)
    # `entrypoint_path` is what the check looks for, and for the package layout that is
    # the DIRECTORY, not its `__init__.py`: an empty `devkit_git_policy/` is a namespace
    # package and shadows a flat module just the same.
    shadow = layout.entrypoint_path(tmp_path, stale)
    shadow.parent.mkdir(parents=True, exist_ok=True)
    if stale.endswith("/__init__.py"):
        shadow.mkdir(exist_ok=True)
    else:
        shadow.write_text("older install", encoding="utf-8")

    found = drift.compare_install(tmp_path, _receipt({kept: digest}))
    assert [d.name for d in found] == [stale]
    assert "older install" in found[0].reason


# --- worktree_drift -----------------------------------------------------------


def test_a_tagged_install_is_never_compared_against_the_working_tree(tmp_path):
    """`compare_install`'s reasoning: a checkout ahead of a pinned release is the
    normal state, and flagging it would make this warn constantly."""
    assert drift.worktree_drift(tmp_path, tmp_path, _receipt({"pre-commit": "aa"})) == []


def test_no_receipt_means_no_worktree_comparison(tmp_path):
    assert drift.worktree_drift(tmp_path, tmp_path, None) == []


def test_a_worktree_install_the_checkout_has_moved_past_is_drift(tmp_path):
    """The hole this fills. `compare_install` asks whether the install matches its own
    receipt and `behind_ref` is silent for a worktree install, so neither had anything
    to say -- and `--check` printed "up to date" over a dispatcher installed from a
    checkout that had since gained the whole pre-push wiring. `devkit-push-gate` had
    not run on any push for five days and nothing on the machine reported it.
    """
    source_name, destination_name = next(iter(drift.RUNTIME_FILES.items()))
    source = tmp_path / "checkout" / source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("the checkout has moved on", encoding="utf-8")
    receipt = _receipt({destination_name: drift.digest(b"what was installed")}, ref="worktree")

    found = drift.worktree_drift(tmp_path / "checkout", tmp_path / "hooks", receipt)
    assert [(d.name, d.reason) for d in found] == [
        (destination_name, "older than the working tree it came from")
    ]


def test_a_worktree_install_that_matches_the_checkout_is_clean(tmp_path):
    source_name, destination_name = next(iter(drift.RUNTIME_FILES.items()))
    source = tmp_path / "checkout" / source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("identical", encoding="utf-8")
    receipt = _receipt({destination_name: drift.digest(b"identical")}, ref="worktree")

    assert drift.worktree_drift(tmp_path / "checkout", tmp_path / "hooks", receipt) == []


def test_a_source_file_that_has_since_disappeared_is_not_drift(tmp_path):
    """`install_files` skips what a ref does not hold, so reporting a file the next
    install would not write either would be a failure nothing could clear."""
    destination_name = next(iter(drift.RUNTIME_FILES.values()))
    receipt = _receipt({destination_name: drift.digest(b"anything")}, ref="worktree")
    assert drift.worktree_drift(tmp_path / "gone", tmp_path / "hooks", receipt) == []


# --- behind_ref ---------------------------------------------------------------


def test_being_behind_a_release_names_the_installed_ref():
    assert drift.behind_ref(_receipt({}, ref="v1.0.0"), "v2.0.0") == "v1.0.0"


def test_the_current_release_is_not_behind():
    assert drift.behind_ref(_receipt({}, ref="v2.0.0"), "v2.0.0") == ""


def test_behind_is_silent_when_either_side_is_unknown():
    assert drift.behind_ref(None, "v2.0.0") == ""
    assert drift.behind_ref(_receipt({}, ref="v1.0.0"), "") == ""


def test_a_working_tree_install_is_never_reported_behind():
    """It has no ref to fall behind, so a tag comparison says nothing true about it.
    That silence used to be the whole answer and was read as "current";
    `worktree_drift` is what actually answers the question, by bytes."""
    assert drift.behind_ref(_receipt({}, ref=drift.WORKTREE_REF), "v2.0.0") == ""


# --- render_drift -------------------------------------------------------------


def test_the_report_names_the_file_the_reason_and_the_one_command_that_fixes_it(tmp_path):
    report = drift.render_drift(tmp_path, [drift.Drift("pre-commit", "modified")])
    assert str(tmp_path) in report
    assert "pre-commit -- modified" in report
    assert "install-git-policy.py --yes" in report
    assert "the policy being enforced" in report


def test_being_behind_is_rendered_even_with_no_modified_file(tmp_path):
    report = drift.render_drift(tmp_path, [], behind="v1.0.0", latest="v2.0.0")
    assert "installed from v1.0.0; v2.0.0 is available" in report
