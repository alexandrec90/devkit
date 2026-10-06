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
  if (l === 'ship') return 'subject'
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


def _load(exists=False, branch="worktree-spec", dirty=False, intent=False):
    return {
        "branch": branch,
        "dirty": dirty,
        "intentPending": intent,
        "exists": exists,
        "specPath": "s.md",
        "sections": SECTIONS if exists else [],
    }


def _run(tmp_path: Path, **scenario) -> dict:
    node = shutil.which("node")
    # Required, not skipped: the gate's ubuntu-latest runners ship Node, and a skip here
    # would leave the workflow's control flow untested without anything turning red.
    assert node, "node is needed to run the workflow script; install Node.js"
    scenario.setdefault("args", {"spec": "s.md", "parallel": 2})
    scenario.setdefault("sections", SECTIONS)
    scenario.setdefault("failing", [])
    scenario.setdefault("gap", None)
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


def test_a_run_builds_one_milestone_and_ships_it(tmp_path):
    """Milestone 2 waits for milestone 1's PR to merge: that is the gate between phases."""
    tasks = [
        _task("a"),
        _task("b", deps=["a"], area="api"),
        _task("c", deps=["a"], area="api"),
        _task("bad", area="ui"),
        _task("d", deps=["bad"], area="ui"),
        _task("e", milestone=2),
    ]
    out = _run(tmp_path, load=_load(), tasks=tasks, failing=["bad"])
    result = out["result"]
    assert result["milestone"] == 1
    assert result["blocked"] == ["bad"]
    assert sorted(result["pending"]) == ["d", "e"]
    assert "build e" not in out["labels"]
    assert "Once this PR merges" in result["next"]
    assert out["labels"][-1] == "ship"


def test_a_wave_never_starts_before_its_dependencies_land(tmp_path):
    tasks = [_task("a"), _task("b", deps=["a"], area="api")]
    labels = _run(tmp_path, load=_load(), tasks=tasks)["labels"]
    assert labels.index("integrate m1r1w1") < labels.index("build b")


def test_an_audit_gap_becomes_a_task_built_in_the_same_run(tmp_path):
    gap = _task("m1-gap1-1")
    out = _run(tmp_path, load=_load(), tasks=[_task("a")], gap=gap)
    assert "build m1-gap1-1" in out["labels"]
    assert out["result"]["done"] == 2


def test_a_rerun_resumes_from_the_task_graph_without_planning(tmp_path):
    tasks = [_task("a", status="done"), _task("e", milestone=2)]
    out = _run(tmp_path, load=_load(exists=True, dirty=True), tasks=tasks)
    assert out["result"]["milestone"] == 2
    assert not {"outline", "architecture", "task graph"} & set(out["labels"])
    assert "build a" not in out["labels"]


@pytest.mark.parametrize(
    ("load", "message"),
    [
        (_load(branch="main"), "is not a task branch"),
        (_load(branch=""), "detached HEAD"),
        (_load(intent=True), "ship-intent.md is waiting"),
        (_load(dirty=True), "uncommitted changes"),
    ],
)
def test_it_refuses_a_tree_it_cannot_ship_from(tmp_path, load, message):
    out = _run(tmp_path, load=load, tasks=[_task("a")])
    assert message in out["error"]
    assert out["labels"] == ["load state"]
