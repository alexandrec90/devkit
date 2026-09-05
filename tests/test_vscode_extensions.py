"""Tests for scripts/vscode_extensions.py (the workspace's VS Code prerequisites).

Same shape as the rest of the session-start reporters: silent when healthy, silent when
it cannot tell, and specific enough when it speaks that nobody has to go and find out
which package a missing *command* came from.
"""

import json

from support import REPO_ROOT, load_script

vsx = load_script("scripts/vscode_extensions.py")


def workspace(tmp_path, recommendations: list[str]):
    """A canonical workspace file recommending `recommendations`."""
    path = tmp_path / "workspace.jsonc"
    path.write_text(
        json.dumps({"extensions": {"recommendations": recommendations}}), encoding="utf-8"
    )
    return path


def registry(tmp_path, have: list[str]):
    """A VS Code extension registry recording `have` as installed."""
    path = tmp_path / "extensions.json"
    path.write_text(json.dumps([{"identifier": {"id": name}} for name in have]), encoding="utf-8")
    return path


# --- reading the two sides ---------------------------------------------------


def test_the_recommendations_are_read_from_the_workspace_file(tmp_path):
    assert vsx.required(workspace(tmp_path, ["a.one", "b.two"])) == ["a.one", "b.two"]


def test_an_unreadable_workspace_recommends_nothing(tmp_path):
    assert vsx.required(tmp_path / "absent.jsonc") == []


def test_the_installed_set_is_lowercased(tmp_path):
    assert vsx.installed(registry(tmp_path, ["RioJ7.Command-Variable"])) == {
        "rioj7.command-variable"
    }


def test_an_unreadable_registry_is_none_not_an_empty_set(tmp_path):
    """The two mean opposite things: no extensions really is missing every
    recommendation, while a registry this cannot find says nothing at all."""
    assert vsx.installed(tmp_path / "absent.json") is None
    assert vsx.installed(registry(tmp_path, [])) == set()


# --- what it says ------------------------------------------------------------


def test_an_installed_recommendation_says_nothing(tmp_path):
    ws = workspace(tmp_path, ["rioj7.command-variable"])
    assert vsx.report_lines(ws, registry(tmp_path, ["rioj7.command-variable"])) == []


def test_a_missing_extension_names_the_package_the_failure_does_not(tmp_path):
    """The symptom is `command 'extension.commandvariable.pickStringRemember' not found`,
    which names a command and no package -- so the fix is unguessable from the failure."""
    ws = workspace(tmp_path, ["rioj7.command-variable"])
    (line,) = vsx.report_lines(ws, registry(tmp_path, []))
    assert "rioj7.command-variable" in line
    assert "code --install-extension rioj7.command-variable" in line
    assert "reload the window" in line


def test_ids_compare_case_insensitively(tmp_path):
    """Marketplace ids are case-insensitive and the registry keeps whichever spelling
    installed it -- comparing raw would report an extension that is sitting right there."""
    ws = workspace(tmp_path, ["rioj7.command-variable"])
    assert vsx.missing(ws, registry(tmp_path, ["RioJ7.Command-Variable"])) == []


def test_a_machine_it_cannot_read_stays_silent(tmp_path):
    """A checkout with no workspace copy, or a VS Code keeping its extensions under a
    custom `--extensions-dir`, has nothing to compare against -- and a session start must
    never turn "I don't know" into a fix nobody needs."""
    ws = workspace(tmp_path, ["rioj7.command-variable"])
    assert vsx.report_lines(tmp_path / "absent.jsonc", registry(tmp_path, [])) == []
    assert vsx.report_lines(ws, tmp_path / "absent.json") == []


def test_several_missing_extensions_are_one_line_and_one_command(tmp_path):
    ws = workspace(tmp_path, ["a.one", "b.two"])
    (line,) = vsx.report_lines(ws, registry(tmp_path, ["a.one"]))
    assert "b.two" in line and "a.one" not in line


# --- the list has one source -------------------------------------------------


def test_the_default_workspace_is_this_checkouts_canonical_copy():
    """Canonical rather than the live workspace file: a fresh machine is precisely the
    one that has not rendered yet."""
    assert vsx.CANONICAL_WORKSPACE == REPO_ROOT / "workspace.jsonc"


def test_every_extension_the_real_workspace_recommends_is_required(tmp_path):
    """No second copy of the list here. A constant would be the thing that goes stale the
    day a task grows a dependency on a new extension."""
    recommended = vsx.required()
    assert "rioj7.command-variable" in recommended
    (line,) = vsx.report_lines(vsx.CANONICAL_WORKSPACE, registry(tmp_path, []))
    for name in recommended:
        assert name in line


def test_the_registry_read_costs_no_subprocess():
    """`code --list-extensions` is not on PATH on a machine where VS Code was installed
    without it, which would report every extension missing on exactly the workstation
    least able to tell that is wrong -- and a spawn at every session start is the cost
    that gets a hook disabled."""
    assert vsx.EXTENSIONS_JSON.name == "extensions.json"
    assert "subprocess" not in vsx.__dict__
