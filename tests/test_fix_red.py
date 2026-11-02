"""`scripts/fix_red.py`: step 3 of the fix pass -- what is red, and re-running a gate."""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_findings
import fix_loop
import fix_plan
import fix_red


def _regate_world(monkeypatch, code: int, err: str = "", answers: list | None = None) -> list:
    """`answers`, when given, is `(code, err)` per call in turn; else every call is `code`."""
    asked: list = []

    def gh(*args):
        asked.append(args)
        answer = answers.pop(0) if answers else (code, err)
        return subprocess.CompletedProcess(args, answer[0], "", answer[1])

    monkeypatch.setattr(fix_red.sweep, "gh_for", lambda _p: gh)
    monkeypatch.setattr(fix_red.time, "sleep", lambda _s: None)
    monkeypatch.setattr(fix_red.sweep, "git_for", lambda _p: lambda *a: None)
    monkeypatch.setattr(fix_red.tb, "detect_default_branch", lambda _git, fallback="main": "master")
    return asked


def test_regate_dispatches_the_gate_on_the_default_branch(monkeypatch, tmp_path):
    """devkit main at a merge the auto-merge workflow made: `GITHUB_TOKEN` raises no push
    event, so no gate ran at the tip and every pass held every project PR behind it."""
    asked = _regate_world(monkeypatch, 0)
    ok, line = fix_red.regate(tmp_path)
    assert ok and line.startswith("master -- ")
    assert asked == [("workflow", "run", fix_red.gate_evidence.GATE_WORKFLOW, "--ref", "master")]


def test_a_regate_gh_refuses_is_said_with_its_last_line(monkeypatch, tmp_path):
    _regate_world(monkeypatch, 1, "warn\nHTTP 422: Workflow does not have 'workflow_dispatch'\n")
    ok, line = fix_red.regate(tmp_path)
    assert not ok and line.endswith("HTTP 422: Workflow does not have 'workflow_dispatch'")
    _regate_world(monkeypatch, 1)
    assert fix_red.regate(tmp_path) == (False, "master -- FAILED to re-run the gate: ?")


_GATEWAY = "HTTP 504: We couldn't respond to your request in time. Sorry about that. (https://api.github.com/x)"


def test_a_regate_github_times_out_on_is_retried(monkeypatch, tmp_path):
    """bc1ec6d1: one `HTTP 504` from the dispatch API was filed as a harness defect, and a
    fixer sent at devkit for a GitHub hiccup the next attempt would have cleared."""
    asked = _regate_world(monkeypatch, 0, answers=[(1, _GATEWAY), (0, "")])
    ok, line = fix_red.regate(tmp_path)
    assert ok and line == "master -- no verdict at the tip; gate re-run"
    assert len(asked) == 2


def test_a_regate_github_keeps_failing_on_is_deferred_not_filed(monkeypatch, tmp_path):
    """Still a server error after every retry: the next pass re-runs it, since the tip is
    still unread, so the line carries no `FAILED` for `fix-pass.py` to file."""
    slept: list = []
    asked = _regate_world(monkeypatch, 1, _GATEWAY)
    monkeypatch.setattr(fix_red.time, "sleep", slept.append)
    ok, line = fix_red.regate(tmp_path)
    assert not ok and "FAILED" not in line
    assert line.startswith("master -- gate re-run deferred: GitHub answered HTTP 504")
    assert len(asked) == len(fix_red.REGATE_RETRY_SECONDS) + 1
    assert slept == list(fix_red.REGATE_RETRY_SECONDS)


def test_a_refusal_is_not_retried(monkeypatch, tmp_path):
    asked = _regate_world(monkeypatch, 1, "HTTP 422: Workflow does not have 'workflow_dispatch'")
    ok, line = fix_red.regate(tmp_path)
    assert not ok and "FAILED" in line
    assert len(asked) == 1


_NO_WORKFLOW = (
    "HTTP 404: workflow pr-gate.yml not found on the default branch "
    "(https://api.github.com/repos/o/skinamp/actions/workflows/pr-gate.yml)"
)


def _seed_world(monkeypatch, *, empty: str, local: int = 0, pushed: tuple = (0, "")) -> dict:
    """skinamp, 2026-11-02 (3120f1ce): `gh repo create --push` made the repo, and the run
    was ended before its push, so GitHub holds no branch and the dispatch 404s."""
    seen: dict = {"gh": [], "git": []}

    def gh(*args):
        seen["gh"].append(args)
        if args[:2] == ("repo", "view"):
            return subprocess.CompletedProcess(args, 0, f"{empty}\n", "")
        return subprocess.CompletedProcess(args, 1, "", _NO_WORKFLOW)

    def git(*args, env=None):
        seen["git"].append((args, env))
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, local, "", "")
        return subprocess.CompletedProcess(args, pushed[0], "", pushed[1])

    monkeypatch.setattr(fix_red.sweep, "gh_for", lambda _p: gh)
    monkeypatch.setattr(fix_red.sweep, "git_for", lambda _p: git)
    monkeypatch.setattr(fix_red.time, "sleep", lambda _s: None)
    monkeypatch.setattr(fix_red.tb, "detect_default_branch", lambda _git, fallback="main": "main")
    return seen


def test_a_repo_github_holds_no_branch_of_is_seeded_with_the_local_default(monkeypatch, tmp_path):
    seen = _seed_world(monkeypatch, empty="true")
    ok, line = fix_red.regate(tmp_path)
    assert ok, line
    assert "FAILED" not in line and "pushed main" in line
    (push, env) = seen["git"][-1]
    assert push == ("push", "-u", "origin", "main")
    # The generator's own waiver, and the pass's push-gate skip: CI gates the push.
    assert env["DEVKIT_SKIP_BRANCH_POLICY"] == "1"
    assert fix_red.SKIP_PUSH_GATE in env["SKIP"].split(",")


def test_a_404_on_a_repo_with_branches_is_never_seeded(monkeypatch, tmp_path):
    """A repo whose default branch lacks the workflow is a real defect, and a push there
    could land local commits nobody reviewed on a published main."""
    seen = _seed_world(monkeypatch, empty="false")
    ok, line = fix_red.regate(tmp_path)
    assert not ok and line.startswith("main -- FAILED to re-run the gate: HTTP 404")
    assert not [args for args, _ in seen["git"] if args[0] == "push"]


def test_a_seed_with_nothing_local_or_a_refused_push_is_filed(monkeypatch, tmp_path):
    _seed_world(monkeypatch, empty="true", local=1)
    ok, line = fix_red.regate(tmp_path)
    assert not ok and line == "main -- FAILED to seed the empty GitHub repo: no local main"
    _seed_world(monkeypatch, empty="true", pushed=(1, "x\n! [remote rejected] main -> main\n"))
    ok, line = fix_red.regate(tmp_path)
    assert not ok
    assert line == "main -- FAILED to seed the empty GitHub repo: ! [remote rejected] main -> main"


def test_seed_env_adds_to_a_skip_list_already_set(monkeypatch):
    monkeypatch.setenv("SKIP", "ruff")
    assert fix_red.seed_env()["SKIP"] == f"ruff,{fix_red.SKIP_PUSH_GATE}"


def test_the_seed_skips_the_push_gate_the_shipper_skips():
    import ship_intent

    assert fix_red.SKIP_PUSH_GATE == ship_intent.SKIP_PUSH_GATE


@pytest.mark.parametrize(
    ("code", "out", "empty"),
    [(0, "true\n", True), (0, "false\n", False), (1, "", False), (0, "", False)],
)
def test_unseeded_is_true_only_when_github_says_the_repo_is_empty(code, out, empty):
    """A `gh` that failed says nothing about the repo, so it never reads as empty: a seed
    pushes a default branch, and only an answer of `true` makes that safe."""
    asked: list = []

    def gh(*args):
        asked.append(args)
        return subprocess.CompletedProcess(args, code, out, "")

    assert fix_red.unseeded(gh) is empty
    assert asked == [("repo", "view", "--json", "isEmpty", "--jq", ".isEmpty")]


def test_seed_pushes_the_local_default_branch_and_says_so():
    calls: list = []

    def git(*args, env=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    assert fix_red.seed(git, "main") == (
        True,
        "main -- GitHub held no branch; pushed main, which raises its gate",
    )
    assert calls == [
        ("rev-parse", "--verify", "--quiet", "refs/heads/main"),
        ("push", "-u", "origin", "main"),
    ]


@pytest.mark.parametrize(
    ("said", "transient"),
    [
        (_GATEWAY, True),
        ("HTTP 502: Bad Gateway", True),
        ("HTTP 503: No server is currently available", True),
        ('Post "https://api.github.com/x": net/http: TLS handshake timeout', True),
        ("error connecting to api.github.com", True),
        # 4d942641: how a push words GitHub's 500.
        ("remote: Internal Server Error", True),
        ("fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host", True),
        ("! [remote rejected] main -> main (protected branch hook declined)", False),
        ("HTTP 422: Workflow does not have 'workflow_dispatch'", False),
        ("HTTP 404: Not Found", False),
        ("HTTP 5000 widgets", False),
        ("", False),
    ],
)
def test_transient_names_only_a_server_or_network_failure(said, transient):
    assert fix_red.transient(said) is transient


def test_regate_unread_re_runs_each_and_reports_only_the_dispatched(monkeypatch, tmp_path):
    answers = {"devkit": (True, "main -- gate re-run"), "carameli": (False, "main -- FAILED x")}
    monkeypatch.setattr(fix_red, "regate", lambda project_dir: answers[project_dir.name])
    lines, rerun = fix_red.regate_unread(tmp_path, ["devkit", "carameli"], fix_cycle.DISPATCH)
    assert lines == ["devkit main -- gate re-run", "carameli main -- FAILED x"]
    assert rerun == {"devkit"}


def test_regate_unread_outside_dispatch_only_says_what_it_would_do(monkeypatch, tmp_path):
    monkeypatch.setattr(fix_red, "regate", lambda _d: pytest.fail("plan re-gated"))
    lines, rerun = fix_red.regate_unread(tmp_path, ["devkit"], fix_cycle.PLAN)
    assert lines == ["devkit -- would re-run the gate: no verdict at the tip"]
    # Counted as re-run all the same: a rehearsal that held every project fixer behind
    # "devkit's gate could not be read" showed holds the dispatch pass never makes.
    assert rerun == {"devkit"}
    assert fix_red.regate_unread(tmp_path, [], fix_cycle.DISPATCH) == ([], set())


# --- a gate GitHub refused to start over billing -------------------------------------

BILLED = (f"Lint / {fix_plan.NO_BILLING}",)


def _billing_ctx(tmp_path: Path, mode: str = fix_cycle.DISPATCH) -> fix_loop.Context:
    devkit = tmp_path / "devkit"
    devkit.mkdir(exist_ok=True)
    now = _dt.datetime(2026, 10, 29, 23, 0, tzinfo=_dt.UTC)
    return fix_loop.Context(tmp_path, [], devkit, tmp_path / "l.json", tmp_path / "h", mode, now)


def _skipped(signature: tuple[str, ...], project: str = "productivity-tracker"):
    failure = fix_plan.Failure(
        kind=fix_plan.PR,
        project=project,
        number=3,
        title="T",
        url=f"u/{project}",
        signature=signature,
    )
    return fix_plan.Decision(fix_plan.SKIP, "n", (failure,))


def test_a_gate_refused_over_billing_files_one_finding_awaiting_a_person(tmp_path):
    """productivity-tracker #3, social-scraper #108: the plan's skip line alone is a
    record line a person has to notice; the finding puts the step in the morning toast."""
    ctx = _billing_ctx(tmp_path)
    journal = fix_findings.Journal(ctx.devkit_dir)
    skipped = [_skipped(BILLED), _skipped(BILLED, "social-scraper"), _skipped(("x::y",))]
    assert fix_red.billing(ctx, journal, skipped) == []
    [found] = journal.findings
    assert (found.kind, found.project, found.evidence) == (
        fix_red.BILLING_KIND,
        "devkit",
        "u/productivity-tracker",
    )
    assert found.detail == fix_red.BILLING_DETAIL
    fix_loop.record(ctx, journal)
    items = fix_findings.triage.open_items(fix_findings.triage.load(ctx.devkit_dir))
    assert list(fix_findings.triage.awaiting_person(items).values()) == [fix_plan.BILLING_STEP]


def test_a_pass_with_no_billing_refusal_retires_the_open_finding(tmp_path):
    """Nothing else would: the person who settled the bill rarely writes the resolution."""
    ctx = _billing_ctx(tmp_path)
    journal = fix_findings.Journal(ctx.devkit_dir)
    fix_red.billing(ctx, journal, [_skipped(BILLED)])
    fix_loop.record(ctx, journal)
    [item] = fix_findings.triage.open_items(fix_findings.triage.load(ctx.devkit_dir))
    crashed = fix_findings.Journal(ctx.devkit_dir, crashed=["collect"])
    assert fix_red.billing(ctx, crashed, []) == [], "a pass that read no gate proves nothing"
    rehearsal = _billing_ctx(tmp_path, fix_cycle.PLAN)
    assert fix_red.billing(rehearsal, journal, [])[0].startswith(f"would retire [{item.id}]")
    assert fix_findings.triage.open_items(fix_findings.triage.load(ctx.devkit_dir)) != []
    [line] = fix_red.billing(ctx, journal, [_skipped(("x::y",))])
    assert line == f"retired [{item.id}] -- no gate was refused over billing on this pass"
    assert fix_findings.triage.open_items(fix_findings.triage.load(ctx.devkit_dir)) == []
    assert fix_red.billing(ctx, journal, []) == []
