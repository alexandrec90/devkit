"""`/implement-spec`: the dynamic workflow that builds a large spec, vendored like `/go-nuts`.

The script has no compiler in this repo's toolchain, so this pins the clauses it exists
for and runs its control flow under Node against scripted agents. Each clause is a way the
run would break the harness: committing or pushing instead of shipping through `/ship`,
building in worktrees that start from the remote default branch and so cannot see earlier
waves, or calling the clock and breaking the runtime's resume.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from support import load_script

ROOT = Path(__file__).resolve().parents[1]
REL = ".claude/workflows/implement-spec.js"
SCRIPT = ROOT / REL

manifest = load_script("scripts/devkit_manifest.py")


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_workflow_is_vendored():
    """User-level workflows live on one machine; the operator works across several."""
    assert REL in manifest.MANIFEST


def test_meta_is_the_first_statement_and_names_the_command():
    """A `meta` that is not first, or not a literal, drops `/implement-spec` from autocomplete."""
    assert _text().startswith("export const meta = {")
    assert re.search(r"^\s*name: 'implement-spec',$", _text(), re.MULTILINE)


def test_it_leaves_commit_push_and_pr_to_the_fix_pass():
    code = _text()
    assert not re.search(r"git commit(?!-tree)", code)
    assert "git push" not in code
    assert "gh pr" not in code
    assert ".claude/skills/ship/SKILL.md" in code
    assert ".claude/skills/ship/SKILL.md" in manifest.MANIFEST
    assert ".claude/rules/session-scope.md" in manifest.MANIFEST


def test_builders_do_not_use_runtime_worktrees():
    """Those branch from the remote default branch, so wave N would not see wave N-1."""
    code = "\n".join(line for line in _text().splitlines() if not line.lstrip().startswith("//"))
    assert not re.search(r"isolation\s*:", code)


def test_it_never_reads_the_clock_or_randomness():
    """The runtime makes these throw: they would break resuming a run from its cache."""
    assert not re.search(r"Date\.now|Math\.random|new Date\(\s*\)", _text())


HARNESS = r"""
import fs from 'fs'
const [, , scriptPath, scenarioPath] = process.argv
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'))
const src = fs.readFileSync(scriptPath, 'utf8').replace(/^export const meta/m, 'const meta')
const run = new Function('args', 'agent', 'parallel', 'pipeline', 'phase', 'log',
  `return (async () => {${src}\n})()`)
const tasks = structuredClone(scenario.tasks)
const labels = []
let gapsGiven = false
const agent = async (prompt, o) => {
  const l = o.label
  labels.push(l)
  if (l === 'load state') return { ...scenario.load, tasks: scenario.load.exists ? structuredClone(tasks) : [] }
  if (l.startsWith('handoff m')) {
    return scenario.handoff || { outcome: 'shipped', detail: 'https://pr', branch: 'spec/s-m' + l.slice(9) }
  }
  if (l === 'outline') return { specPath: 's.md', sections: scenario.sections }
  if (l === 'architecture') return 'summary'
  if (l.startsWith('decompose')) return { count: 1 }
  if (l === 'task graph') return { tasks: structuredClone(tasks) }
  if (l.startsWith('snapshot')) return { commit: 'abc123' }
  if (l.startsWith('build ')) {
    const id = l.slice(6)
    return { id, ok: !scenario.failing.includes(id), summary: '', tests: [] }
  }
  if (l.startsWith('integrate')) {
    const reports = JSON.parse(prompt.slice(prompt.indexOf('Build reports:') + 14, prompt.indexOf('For each report')))
    return { results: reports.map(r => {
      const t = tasks.find(x => x.id === r.id)
      if (r.ok) t.status = 'done'
      else { t.attempts += 1; t.status = t.attempts >= 2 ? 'blocked' : 'pending' }
      return { id: t.id, status: t.status, attempts: t.attempts }
    }) }
  }
  if (l.startsWith('audit')) {
    if (gapsGiven || !scenario.gap) return { gaps: [] }
    gapsGiven = true
    return { gaps: [{ title: 'gap', specRefs: ['1-2'], reason: 'missing' }] }
  }
  if (l.startsWith('gap tasks')) {
    const t = { ...scenario.gap, status: 'pending', attempts: 0 }
    tasks.push({ ...t })
    return { tasks: [t] }
  }
  if (l.startsWith('ship m')) return 'subject'
  throw new Error('unexpected agent ' + l)
}
const parallel = thunks => Promise.all(thunks.map(t => t().catch(() => null)))
const pipeline = (items, ...stages) => Promise.all(items.map(async (item, i) => {
  let r = item
  for (const s of stages) { try { r = await s(r, item, i) } catch { return null } }
  return r
}))
try {
  const result = await run(scenario.args, agent, parallel, pipeline, () => {}, () => {})
  console.log(JSON.stringify({ result, labels }))
} catch (e) {
  console.log(JSON.stringify({ error: e.message, labels }))
}
"""

SECTIONS = [{"id": "s1", "title": "S1", "startLine": 1, "endLine": 100}]


def _task(tid, milestone=1, deps=(), area="core", status="pending"):
    return {
        "id": tid,
        "title": tid.upper(),
        "section": "s1",
        "area": area,
        "milestone": milestone,
        "dependsOn": list(deps),
        "status": status,
        "attempts": 0,
    }


def _load(exists=False, branch="worktree-spec", dirty=False, intent=False, shipped=()):
    return {
        "branch": branch,
        "dirty": dirty,
        "intentPending": intent,
        "exists": exists,
        "specPath": "s.md",
        "sections": SECTIONS if exists else [],
        "shipped": [{"milestone": m, "branch": b} for m, b in shipped],
    }


def _run(tmp_path: Path, **scenario) -> dict:
    node = shutil.which("node")
    # Required, not skipped: the gate's ubuntu-latest runners ship Node, and a skip here
    # would leave the workflow's control flow untested without anything turning red.
    assert node, "node is needed to run the workflow script; install Node.js"
    scenario.setdefault("args", {"spec": "docs/s.md", "parallel": 2})
    scenario.setdefault("sections", SECTIONS)
    scenario.setdefault("failing", [])
    scenario.setdefault("gap", None)
    scenario.setdefault("handoff", None)
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    data = tmp_path / "scenario.json"
    data.write_text(json.dumps(scenario), encoding="utf-8")
    out = subprocess.run(
        [node, str(harness), str(SCRIPT), str(data)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


TWO_MILESTONES = [
    _task("a"),
    _task("b", deps=["a"], area="api"),
    _task("bad", area="ui"),
    _task("d", deps=["bad"], area="ui"),
    _task("e", milestone=2, deps=["a"]),
]


def test_one_run_ships_every_milestone_as_a_stack(tmp_path):
    """Milestone 2 is built on a branch cut from the commit the fix pass made for milestone 1."""
    out = _run(tmp_path, load=_load(), tasks=TWO_MILESTONES, failing=["bad"])
    labels, result = out["labels"], out["result"]
    assert labels.index("ship m1") < labels.index("handoff m2") < labels.index("build e")
    assert labels[-1] == "ship m2"
    assert result["shipped"] == [
        {"milestone": 1, "branch": "worktree-spec"},
        {"milestone": 2, "branch": "spec/s-m2"},
    ]
    assert result["blocked"] == ["bad"]
    assert result["pending"] == ["d"]
    assert "Every milestone is shipped" in result["next"]


def test_a_wave_never_starts_before_its_dependencies_land(tmp_path):
    tasks = [_task("a"), _task("b", deps=["a"], area="api")]
    labels = _run(tmp_path, load=_load(), tasks=tasks)["labels"]
    assert labels.index("integrate m1r1w1") < labels.index("build b")


def test_an_audit_gap_becomes_a_task_built_in_the_same_milestone(tmp_path):
    gap = _task("m1-gap1-1")
    out = _run(tmp_path, load=_load(), tasks=[_task("a")], gap=gap)
    assert out["labels"].index("build m1-gap1-1") < out["labels"].index("ship m1")
    assert out["result"]["done"] == 2


@pytest.mark.parametrize("intent", [True, False])
def test_a_rerun_stacks_on_what_already_shipped_without_planning(tmp_path, intent):
    """Whether milestone 1's intent is still waiting or the pass has shipped it already."""
    tasks = [_task("a", status="done"), _task("e", milestone=2)]
    load = _load(exists=True, intent=intent, shipped=[(1, "worktree-spec")])
    labels = _run(tmp_path, load=load, tasks=tasks)["labels"]
    assert labels[:2] == ["load state", "handoff m2"]
    assert not {"outline", "architecture", "task graph", "build a"} & set(labels)
    assert labels[-1] == "ship m2"


def test_a_refused_milestone_stops_the_stack(tmp_path):
    refused = {"outcome": "refused", "detail": "ruff", "branch": ""}
    out = _run(tmp_path, load=_load(), tasks=TWO_MILESTONES, handoff=refused)
    assert "has not shipped (refused: ruff)" in out["error"]
    assert "build e" not in out["labels"]


def test_max_milestones_stops_after_shipping(tmp_path):
    args = {"spec": "docs/s.md", "maxMilestones": 1}
    out = _run(tmp_path, load=_load(), tasks=TWO_MILESTONES, args=args)
    assert "handoff m2" not in out["labels"]
    assert out["labels"][-1] == "ship m1"
    assert "Rerun /implement-spec" in out["result"]["next"]


def test_one_pr_builds_everything_into_a_single_ship(tmp_path):
    args = {"spec": "docs/s.md", "onePr": True}
    out = _run(tmp_path, load=_load(), tasks=TWO_MILESTONES, args=args)
    ships = [label for label in out["labels"] if label.startswith(("ship", "handoff"))]
    assert ships == ["ship m1"]
    assert "build e" in out["labels"]
    assert [s["milestone"] for s in out["result"]["shipped"]] == [1, 2]


@pytest.mark.parametrize(
    ("load", "message"),
    [
        (_load(branch="main"), "is not a task branch"),
        (_load(branch=""), "detached HEAD"),
        (_load(dirty=True), "uncommitted changes"),
    ],
)
def test_it_refuses_a_tree_it_cannot_ship_from(tmp_path, load, message):
    out = _run(tmp_path, load=load, tasks=[_task("a")])
    assert message in out["error"]
    assert out["labels"] == ["load state"]
