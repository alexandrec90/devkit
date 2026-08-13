"""Tests for scripts/workspace-status.py (the SessionStart status line).

The properties that matter are all about *not* being annoying: silent when
healthy, silent when it cannot tell, and never able to fail a session start. A
status line that cries wolf is removed within a week, and then the thing it was
watching goes unwatched again.
"""

import json
import os

from support import LIVE_WORKSPACE, REPO_ROOT, load_script, needs_live_workspace, sweep

ws = load_script("scripts/workspace-status.py")


def result(name: str, verdict: str) -> sweep.Result:
    return sweep.Result(sweep.State(name=name), verdict, "reason", [])


# --- silence when there is nothing to say ------------------------------------


def test_a_clean_workspace_says_nothing():
    results = [result("carameli", sweep.CLEAN), result("devkit", sweep.SKIPPED)]
    assert ws.render(results, {}, "v0.5.3") == ""


def test_stranded_work_names_the_checkouts():
    """ "3 checkouts need action" makes you run something else to find out which."""
    results = [result("carameli", sweep.READY), result("devkit", sweep.CLEAN)]
    line = ws.render(results, {}, "v0.5.3")
    assert "carameli (ready)" in line
    assert "devkit" not in line


def test_behind_projects_are_named_with_their_version():
    line = ws.render([], {"carameli": "v0.5.2"}, "v0.5.3")
    assert "devkit v0.5.3 available" in line
    assert "carameli on v0.5.2" in line


def test_both_halves_appear_together():
    line = ws.render([result("devkit", sweep.READY)], {"carameli": "v0.5.2"}, "v0.5.3")
    assert "stranded work" in line
    assert "devkit v0.5.3 available" in line
    assert line.count("[workspace]") == 2


# --- "cannot tell" is not "behind" -------------------------------------------


def test_a_project_with_no_recorded_tag_is_not_reported_behind(tmp_path):
    """An unrecorded tag means the pull predates the receipt carrying one. Guessing
    would put every un-upgraded project in this line forever."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "DEVKIT_FILES.json").write_text(
        json.dumps({"version": 1, "files": {}}), encoding="utf-8"
    )
    assert ws.projects_behind(tmp_path, ["proj"], "v0.5.3") == {}


def test_a_project_on_the_latest_tag_is_not_behind(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "DEVKIT_FILES.json").write_text(
        json.dumps({"devkit_tag": "v0.5.3", "files": {}}), encoding="utf-8"
    )
    assert ws.projects_behind(tmp_path, ["proj"], "v0.5.3") == {}


def test_a_project_on_an_older_tag_is_behind(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "DEVKIT_FILES.json").write_text(
        json.dumps({"devkit_tag": "v0.5.2", "files": {}}), encoding="utf-8"
    )
    assert ws.projects_behind(tmp_path, ["proj"], "v0.5.3") == {"proj": "v0.5.2"}


def test_a_missing_or_malformed_receipt_is_skipped(tmp_path):
    (tmp_path / "none").mkdir()
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "DEVKIT_FILES.json").write_text("{not json", encoding="utf-8")
    assert ws.projects_behind(tmp_path, ["none", "bad", "absent"], "v0.5.3") == {}


# --- version ordering --------------------------------------------------------


def test_tags_sort_numerically_not_lexically():
    """v0.5.10 is newer than v0.5.9; string ordering says otherwise."""
    assert ws._version_key("v0.5.10") > ws._version_key("v0.5.9")
    assert ws._version_key("v0.10.0") > ws._version_key("v0.9.9")


def test_a_non_version_tag_sorts_lowest():
    assert ws._version_key("nightly") < ws._version_key("v0.0.1")


def test_the_latest_tag_is_read_from_loose_refs(tmp_path):
    tags = tmp_path / ".git" / "refs" / "tags"
    tags.mkdir(parents=True)
    for name in ("v0.5.2", "v0.5.10", "v0.5.9"):
        (tags / name).write_text("deadbeef\n", encoding="utf-8")
    assert ws.latest_devkit_tag(tmp_path) == "v0.5.10"


def test_the_latest_tag_is_read_from_packed_refs(tmp_path):
    (tmp_path / ".git").mkdir(parents=True)
    (tmp_path / ".git" / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "aaa refs/tags/v0.5.2\n"
        "bbb refs/tags/v0.5.3\n"
        "ccc refs/heads/main\n",
        encoding="utf-8",
    )
    assert ws.latest_devkit_tag(tmp_path) == "v0.5.3"


def test_a_repo_with_no_tags_reports_nothing(tmp_path):
    assert ws.latest_devkit_tag(tmp_path) == ""


# --- it must never break a session -------------------------------------------


def test_an_absent_workspace_is_silent_and_successful(tmp_path, monkeypatch, capsys):
    """The registry is workstation-local: on CI or a fresh clone there is simply
    nothing to report, which is not an error."""
    monkeypatch.setattr(ws, "DEFAULT_WORKSPACE", tmp_path / "nope.code-workspace")
    assert ws.main([]) == 0
    assert capsys.readouterr().out == ""


def test_a_failure_anywhere_still_exits_zero(tmp_path, monkeypatch):
    """A status line that can fail a session start gets removed the first time it
    is wrong -- and then nothing is watching again."""
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"folders": [{"path": "proj"}]}', encoding="utf-8")
    monkeypatch.setattr(ws, "DEFAULT_WORKSPACE", workspace)
    monkeypatch.setattr(ws.sweep, "sweep", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert ws.main([]) == 0


# --- the branch-policy half --------------------------------------------------
# The global hooks are a copy of scripts/git_policy.py, so they go stale with no
# symptom: the hooks still fire, they just enforce an older policy. Nothing else
# in the workspace would ever mention it.

installer = load_script("scripts/install-git-policy.py")


def installed(target, ref="v0.5.3"):
    """A realistic install, made through the installer's own code path."""
    installer.install(REPO_ROOT, target, ref)
    return target


def test_a_current_install_says_nothing(tmp_path):
    target = installed(tmp_path / "hooks")
    assert ws.policy_line(REPO_ROOT, target, latest="v0.5.3") == ""


def test_a_modified_runtime_is_named(tmp_path):
    target = installed(tmp_path / "hooks")
    (target / "devkit_git_policy.py").write_text("# tampered\n", encoding="utf-8")

    line = ws.policy_line(REPO_ROOT, target, latest="v0.5.3")
    assert "branch policy" in line
    assert "devkit_git_policy.py" in line
    assert "install-git-policy.py --yes" in line


def test_an_install_from_an_older_release_is_reported_as_behind(tmp_path):
    target = installed(tmp_path / "hooks")
    line = ws.policy_line(REPO_ROOT, target, latest="v0.6.0")
    assert "installed from v0.5.3" in line
    assert "v0.6.0 available" in line


def test_a_runtime_with_no_receipt_is_reported_as_unidentifiable(tmp_path):
    """The state this machine was in: installed before receipts existed."""
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    assert "installed.json" in ws.policy_line(REPO_ROOT, target, latest="v0.5.3")


def test_an_absent_install_is_silence_not_a_warning(tmp_path):
    """A fresh clone, CI, anyone else's machine -- there is nothing to say."""
    assert ws.policy_line(REPO_ROOT, tmp_path / "never-installed", latest="v0.5.3") == ""


def test_an_unusable_source_tree_is_silence_not_an_exception(tmp_path):
    """This runs at session start, so it may never be the reason one fails -- and a
    checkout with no installer to load is exactly the shape that would raise."""
    target = installed(tmp_path / "hooks")
    assert ws.policy_line(tmp_path / "not-a-checkout", target, latest="v0.5.3") == ""


def test_an_unknown_latest_tag_does_not_invent_a_warning(tmp_path):
    target = installed(tmp_path / "hooks")
    assert ws.policy_line(REPO_ROOT, target, latest="") == ""


def test_the_policy_half_joins_the_others(tmp_path):
    line = ws.render([result("devkit", sweep.READY)], {}, "v0.5.3", "branch policy: x")
    assert "stranded work" in line
    assert "branch policy: x" in line
    assert line.count("[workspace]") == 2


def test_the_policy_half_alone_still_prints():
    assert ws.render([], {}, "", "branch policy: x") == "[workspace] branch policy: x"


# --- adoption shape ----------------------------------------------------------
# The half that answers "is this checkout a devkit project at all", which nothing asked
# before: `upgrade-project.py` skips an unadopted checkout with a reason and moves on,
# and that skip renders indistinguishably from a routine one.


def project(root, name: str, *, version=True, precommit=True):
    """A checkout carrying whichever adoption markers are asked for."""
    path = root / name
    path.mkdir(parents=True)
    if version:
        (path / "DEVKIT_VERSION").write_text("abc1234\n", encoding="utf-8")
    if precommit:
        (path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    return path


def test_a_fully_adopted_workspace_says_nothing(tmp_path):
    project(tmp_path, "carameli")
    project(tmp_path, "apt-finder")
    assert ws.adoption_line(tmp_path, ["carameli", "apt-finder"]) == ""


def test_a_checkout_that_never_vendored_devkit_is_named(tmp_path):
    """ibkr_trader's actual state: registered in the workspace, outside the harness."""
    project(tmp_path, "ibkr_trader", version=False, precommit=False)
    line = ws.adoption_line(tmp_path, ["ibkr_trader"])
    assert "ibkr_trader (never vendored devkit)" in line
    assert "sync-devkit.py --pull" in line, "a standing report has to say what fixes it"


def test_a_missing_pre_commit_gate_is_named_on_an_adopted_checkout(tmp_path):
    """data-lake's actual state. Every vendored hook present and none of them running:
    no error, no red build, the checks simply never fire."""
    project(tmp_path, "data-lake", precommit=False)
    assert "data-lake (no pre-commit gate)" in ws.adoption_line(tmp_path, ["data-lake"])


def test_only_the_first_missing_marker_is_reported(tmp_path):
    """ "never vendored devkit" already implies the gate is missing. Reporting both makes
    the reader triage a list where there is one fix."""
    project(tmp_path, "ibkr_trader", version=False, precommit=False)
    assert ws.adoption_line(tmp_path, ["ibkr_trader"]).count("ibkr_trader") == 1


def test_devkit_itself_is_not_an_adopter(tmp_path):
    """It is where these files come from, and has no DEVKIT_VERSION by design. Without
    the exemption this line names devkit every session and is ignored by week two."""
    source = project(tmp_path, "devkit", version=False)
    assert ws.adoption_line(tmp_path, ["devkit"], source=source) == ""


def test_a_missing_directory_is_silence_not_a_fault(tmp_path):
    """The registry is hand-edited and can name a checkout nobody has cloned yet."""
    assert ws.adoption_line(tmp_path, ["not-cloned-here"]) == ""


def test_the_adoption_half_joins_the_others(tmp_path):
    line = ws.render([result("devkit", sweep.READY)], {}, "v0.5.3", "", "not devkit projects: x")
    assert "stranded work" in line
    assert "not devkit projects: x" in line
    assert line.count("[workspace]") == 2


def test_the_adoption_half_alone_still_prints():
    assert ws.render([], {}, "", "", "not devkit projects: x") == (
        "[workspace] not devkit projects: x"
    )


# --- the ephemeral tier ------------------------------------------------------


def row(box: str, reapable: bool) -> dict:
    return {"box": box, "reapable": reapable, "verdict": "ready", "reason": "x"}


def test_no_boxes_is_silence():
    """The common case, on a workstation that has never cut one."""
    assert ws.boxes_line([]) == ""


def test_a_box_holding_work_is_named():
    line = ws.boxes_line([row("carameli--voicemail-0806", reapable=False)])
    assert "1 ephemeral box(es)" in line
    assert "1 holding work (carameli--voicemail-0806)" in line


def test_a_reapable_box_is_reported_as_the_leak_it_is():
    """A box whose work has shipped still holds a port slot out of a fixed ceiling, and
    a container and volume set if its project has a stack. Nothing else mentions it --
    boxes are deliberately absent from the workspace file, so the sweep cannot see them."""
    line = ws.boxes_line([row("devkit--smoke-0806", reapable=True)])
    assert "1 reapable (devkit--smoke-0806)" in line
    assert "reap --all --yes" in line


def test_the_two_box_states_are_reported_separately():
    """They want opposite actions: one wants shipping, the other wants reaping."""
    line = ws.boxes_line([row("a--x-0806", reapable=False), row("b--y-0806", reapable=True)])
    assert "2 ephemeral box(es)" in line
    assert "1 holding work (a--x-0806)" in line
    assert "1 reapable (b--y-0806)" in line


def test_a_box_survey_that_blows_up_costs_the_line_not_the_session(tmp_path, monkeypatch):
    """The survey spawns git per box; one box in a state git dislikes must not take the
    status line -- let alone the session start -- down with it."""
    monkeypatch.setattr(ws.worktree, "survey", _raise)
    assert ws.box_survey(tmp_path / "nope.code-workspace") == []


def _raise(*args, **kwargs):
    raise RuntimeError("git said no")


# --- the guard that is wired outside every repo -------------------------------


def test_a_root_that_runs_the_guard_says_nothing(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"command": "python3 devkit/scripts/worktree-guard.py"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert ws.guard_line(tmp_path) == ""


def test_a_root_with_no_settings_file_at_all_is_reported(tmp_path):
    """Absent is silence everywhere else in this file, and is the wrong default here: an
    unwired guard has no symptom. The edits land on home branches and surface days later
    as a `needs-branch` backlog that looks like someone left it there by hand."""
    line = ws.guard_line(tmp_path)
    assert "not wired at the workspace root" in line
    assert "worktree-guard.py" in line


def test_a_settings_file_that_wires_other_hooks_but_not_the_guard_is_reported(tmp_path):
    """The likelier shape of the failure: a root that has settings for something else."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": []}]}}), encoding="utf-8")
    assert "not wired" in ws.guard_line(tmp_path)


def test_the_fix_names_a_forward_slash_path_on_every_platform(tmp_path):
    """The line is read on Windows, where `Path` renders backslashes that then have to be
    escaped by whoever pastes them into JSON."""
    assert ".claude/settings.json" in ws.guard_line(tmp_path)


@needs_live_workspace
def test_this_workstations_root_actually_runs_the_guard():
    """The wiring itself lives outside every repository, so this is the only place it can
    be asserted at all -- and it is the wiring that decides whether the guard exists for
    the sessions it was written for."""
    assert ws.guard_line(LIVE_WORKSPACE.parent) == ""


# --- the unattended pass, when it has stopped --------------------------------


NOW = 1_800_000_000.0


def _logged(root, age_hours: float) -> None:
    """A reconcile log last written `age_hours` before `NOW`."""
    log = root / ws.worktree.RECONCILE_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# a pass\n", encoding="utf-8")
    stamp = NOW - age_hours * 3600
    os.utime(log, (stamp, stamp))


def test_a_pass_that_ran_recently_says_nothing(tmp_path):
    _logged(tmp_path, 0.2)
    assert ws.scheduler_line(tmp_path, now=NOW) == ""


def test_a_pass_that_stopped_days_ago_is_reported(tmp_path):
    """The failure this whole line exists for: the scheduled task sat disabled for five
    days, and a stopped task is indistinguishable from a working one -- no error, no
    output, nothing red anywhere."""
    _logged(tmp_path, 24 * 5)
    line = ws.scheduler_line(tmp_path, now=NOW)
    assert "5d 0h ago" in line
    assert "install-reconcile-task.py" in line


def test_a_workstation_that_never_installed_the_task_is_left_alone(tmp_path):
    """Absent is silence here, unlike `guard_line`: a standing demand to install a
    Windows-only convenience is a line you learn to skim, which is the failure this one
    is trying to fix rather than repeat."""
    assert ws.scheduler_line(tmp_path, now=1_800_000_000.0) == ""


def test_the_age_is_coarse_because_the_question_is_days_not_minutes():
    assert ws._age(3600 * 5) == "5h"
    assert ws._age(3600 * 50) == "2d 2h"


def test_the_stopped_pass_is_reported_before_the_drift_it_causes():
    """A reader who fixes the stranded checkouts by hand and leaves the pass stopped is
    reading the same list again tomorrow."""
    results = [result("carameli", sweep.READY)]
    lines = ws.render(results, {}, "v0.5.3", scheduler="unattended pass last ran 5d 0h ago").split(
        "\n"
    )
    assert "unattended pass" in lines[0]


def test_a_specific_reconcile_schedule_failure_replaces_the_log_fallback():
    schedule = ["schedule: devkit-worktree-reconcile: disabled -- nothing is running it"]
    assert ws.scheduler_fallback("unattended pass last ran 5d 0h ago", schedule) == ""


def test_an_upgrade_failure_does_not_hide_a_stale_reconcile_log():
    schedule = ["schedule: devkit-upgrade-projects: last run failed (exit 1)"]
    fallback = "unattended pass last ran 5d 0h ago"
    assert ws.scheduler_fallback(fallback, schedule) == fallback


# --- the architectural check ------------------------------------------------


# Checkouts knowingly outside the harness, each carrying the reason it still is. This is
# a **ratchet, not an allowlist**: the test below fails both when an unlisted checkout
# drifts out of shape AND when a listed one is fixed without being removed from here.
# That second half is what stops this becoming the same silent permanent skip it exists
# to replace.
# Empty, and that is the point: every registered checkout is now inside the harness.
# Both ibkr_trader entries left first, then data-lake once it grew the
# .pre-commit-config.yaml its entry said it lacked -- and the ratchet's second half is
# what noticed each time, rather than the entry sitting here forever describing a state
# that had stopped being true.
UNADOPTED_EXCEPTIONS: dict[str, str] = {}


@needs_live_workspace
def test_every_registered_checkout_is_a_devkit_project():
    """The shape check devkit did not have. Being registered in the workspace means
    devkit's tooling manages you -- and every one of those tools (the vendored hooks,
    the commit gate, `lint-fix.py`, `upgrade-project.py`) silently does nothing for a
    checkout that never adopted. Nothing goes red; the work simply is not done."""
    names = sweep.parse_workspace(LIVE_WORKSPACE.read_text(encoding="utf-8"))
    faults = dict(ws.adoption_faults(LIVE_WORKSPACE.parent, names))

    unexpected = sorted(set(faults) - set(UNADOPTED_EXCEPTIONS))
    assert not unexpected, (
        f"{unexpected} are registered in the workspace but are not devkit projects: "
        f"{ {n: faults[n] for n in unexpected} }. Onboard them, or add each to "
        "UNADOPTED_EXCEPTIONS with the reason it is deliberate."
    )

    fixed = sorted(set(UNADOPTED_EXCEPTIONS) - set(faults))
    assert not fixed, (
        f"{fixed} are now properly adopted -- delete them from UNADOPTED_EXCEPTIONS. "
        "A stale exception is how a permanent gap goes quiet again."
    )


# --- settings still wiring a retired hook -----------------------------------
# `--pull` DELETES a retired script, so a surviving hook entry naming it makes the
# harness spawn a missing file on every prompt. `prune_settings` unwires it for every
# project that has upgraded; this line is for the ones that have not.


def _wire(root, name: str, command: str) -> None:
    path = root / name / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": command}]}]}}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_checkout_still_wiring_a_retired_hook_is_named(tmp_path):
    _wire(tmp_path, "carameli", 'python3 "x/scripts/hooks/branch-on-write.py"')
    line = ws.retired_hooks_line(tmp_path, ["carameli"])
    assert "carameli" in line
    assert "branch-on-write.py" in line
    assert "sync-devkit.py --pull" in line


def test_a_checkout_on_live_hooks_only_is_silent(tmp_path):
    _wire(tmp_path, "carameli", 'python3 "x/scripts/hooks/lint-fix.py"')
    assert ws.retired_hooks_line(tmp_path, ["carameli"]) == ""


def test_a_checkout_with_no_settings_file_is_silent(tmp_path):
    assert ws.retired_hooks_line(tmp_path, ["carameli"]) == ""


def test_every_offender_is_named_not_counted(tmp_path):
    """ "2 checkouts are stale" sends you on an errand to find out which."""
    _wire(tmp_path, "carameli", 'python3 "x/scripts/hooks/branch-on-write.py"')
    _wire(tmp_path, "apt-finder", 'python3 "x/scripts/hooks/branch-per-task.py"')
    line = ws.retired_hooks_line(tmp_path, ["carameli", "apt-finder"])
    assert "carameli" in line
    assert "apt-finder" in line


def test_the_retired_list_is_read_from_sync_devkit_not_copied(tmp_path, monkeypatch):
    """A second copy of the list is exactly the drift this whole file exists to notice."""
    _wire(tmp_path, "carameli", 'python3 "x/scripts/hooks/invented-later.py"')
    assert ws.retired_hooks_line(tmp_path, ["carameli"]) == ""

    real = ws.load_by_path

    def fake(name, path):
        module = real(name, path)
        if name == "_sync_devkit":
            module.RETIRED_PATHS = ("scripts/hooks/invented-later.py",)
        return module

    monkeypatch.setattr(ws, "load_by_path", fake)
    assert "invented-later.py" in ws.retired_hooks_line(tmp_path, ["carameli"])


def test_the_line_reaches_the_rendered_message():
    """A helper nothing calls is a check that reports nothing."""
    message = ws.render([], {}, "", retired="settings wire retired hooks: carameli (x.py)")
    assert "[workspace] settings wire retired hooks: carameli (x.py)" in message


def test_a_markdownlint_hook_naming_a_readme_is_not_a_retired_hook(tmp_path):
    """Regression. `.claude/skills/state-tools/README.md` is in RETIRED_PATHS, so
    basename matching made `README.md` a retired hook -- and carameli's markdownlint
    hook lists `"README.md"` among its arguments. Every checkout with a README in a
    hook command was named, and `--pull` would have deleted that hook."""
    _wire(
        tmp_path,
        "carameli",
        'markdownlint-cli2 --config .config.yaml "docs/roadmap.md" "README.md"',
    )
    assert ws.retired_hooks_line(tmp_path, ["carameli"]) == ""


def test_a_windows_spelled_hook_path_is_still_matched(tmp_path):
    """A settings file written on Windows can spell the path with backslashes; the
    manifest paths are always POSIX."""
    _wire(tmp_path, "carameli", 'python3 "x\\scripts\\hooks\\branch-on-write.py"')
    assert "branch-on-write.py" in ws.retired_hooks_line(tmp_path, ["carameli"])
