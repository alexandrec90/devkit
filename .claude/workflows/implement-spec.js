export const meta = {
  name: 'implement-spec',
  description: 'Implement a large requirements doc one milestone per run: plan a task graph, build waves in parallel checkouts, integrate, audit, then ship through /ship',
  whenToUse: 'Turning a large spec or requirements document into working code across many agents. Run from a task worktree. Args: spec path, or {spec, parallel, maxAttempts, auditRounds, allMilestones}.',
  phases: [
    { title: 'Load', detail: 'read the task graph a previous run left in .spec-run/' },
    { title: 'Plan', detail: 'outline the spec, set up the architecture, decompose sections, merge into a milestone graph' },
    { title: 'Build', detail: 'waves of ready tasks, each in its own detached checkout of a snapshot' },
    { title: 'Integrate', detail: 'apply each patch to the integration tree, run its tests, record status' },
    { title: 'Audit', detail: 'check the milestone against the spec; gaps become new tasks' },
    { title: 'Ship', detail: 'write .spec-run/REPORT.md and the ship intent' },
  ],
}

// The run never commits, pushes or opens a PR (.claude/rules/session-scope.md). Everything
// it builds accumulates uncommitted in the integration tree -- the task worktree it was
// started from -- and ships as one PR through /ship at the end. That is what lets wave N
// build on wave N-1 without waiting on a merge: builders start from a snapshot commit of
// the tree that sits on no branch, and hand back a patch instead of a branch.
//
// Builders do not use `isolation: 'worktree'`: those worktrees branch from the remote
// default branch unless `worktree.baseRef` is "head", so they would not see earlier waves.
//
// The task graph lives in .spec-run/ and ships with each PR, so the next milestone can start
// in a fresh worktree on any machine once this one merges.
const opts = typeof args === 'string' ? { spec: args } : (args || {})
if (!opts.spec) throw new Error('Pass the spec path, e.g. /implement-spec docs/requirements.md')
const WAVE = opts.parallel || 4
const MAX_ATTEMPTS = opts.maxAttempts || 2
const AUDIT_ROUNDS = opts.auditRounds ?? 2
const STATE = '.spec-run'
const SCRATCH = 'logs/spec-run'

const str = { type: 'string' }
const int = { type: 'integer' }
const strs = { type: 'array', items: str }
const STATUS = { type: 'string', enum: ['pending', 'done', 'blocked'] }
const SECTION = {
  type: 'object',
  required: ['id', 'title', 'startLine', 'endLine'],
  properties: { id: str, title: str, startLine: int, endLine: int },
}
const TASK = {
  type: 'object',
  required: ['id', 'title', 'section', 'area', 'milestone', 'dependsOn', 'status', 'attempts'],
  properties: {
    id: str, title: str, section: str, area: str, milestone: int,
    dependsOn: strs, status: STATUS, attempts: int,
  },
}
const LOAD_SCHEMA = {
  type: 'object',
  required: ['branch', 'dirty', 'intentPending', 'exists', 'specPath', 'sections', 'tasks'],
  properties: {
    branch: str, dirty: { type: 'boolean' }, intentPending: { type: 'boolean' }, exists: { type: 'boolean' },
    specPath: str, sections: { type: 'array', items: SECTION }, tasks: { type: 'array', items: TASK },
  },
}
const OUTLINE_SCHEMA = {
  type: 'object', required: ['specPath', 'sections'],
  properties: { specPath: str, sections: { type: 'array', items: SECTION } },
}
const COUNT_SCHEMA = { type: 'object', required: ['count'], properties: { count: int } }
const TASKS_SCHEMA = { type: 'object', required: ['tasks'], properties: { tasks: { type: 'array', items: TASK } } }
const SNAPSHOT_SCHEMA = { type: 'object', required: ['commit'], properties: { commit: str } }
const BUILD_SCHEMA = {
  type: 'object', required: ['id', 'ok', 'summary', 'tests'],
  properties: { id: str, ok: { type: 'boolean' }, summary: str, tests: strs },
}
const INTEG_SCHEMA = {
  type: 'object', required: ['results'],
  properties: {
    results: {
      type: 'array',
      items: { type: 'object', required: ['id', 'status', 'attempts'], properties: { id: str, status: STATUS, attempts: int } },
    },
  },
}
const GAPS_SCHEMA = {
  type: 'object', required: ['gaps'],
  properties: {
    gaps: {
      type: 'array',
      items: { type: 'object', required: ['title', 'specRefs', 'reason'], properties: { title: str, specRefs: strs, reason: str } },
    },
  },
}

const CTX = `Shared state for this run lives in ${STATE}/ at the root of the integration tree:
- ${STATE}/ARCHITECTURE.md: stack, layout, conventions, and the commands to provision, build and test
- ${STATE}/tasks.json: the task graph; each task has id, title, section, area, milestone, specRefs (line ranges in the spec), acceptance, dependsOn, status, attempts, notes
- ${STATE}/progress.md: append-only log of what each wave did
Read the spec only by line range, never the whole file at once. Nothing in this run commits, pushes or opens a PR.
Run git as plain, separate commands with no command substitution or chaining: the worktree isolation check refuses git it cannot verify.`

// ---------------------------------------------------------------- Load
phase('Load')
const state = await agent(
  `Inspect this git checkout and change nothing. Report:
- branch: the current branch name (empty if HEAD is detached)
- dirty: whether \`git status --porcelain\` lists anything outside ${STATE}/
- intentPending: whether logs/ship-intent.md exists
- if ${STATE}/tasks.json and ${STATE}/outline.json both exist: exists=true, specPath and sections from outline.json, and for every task in tasks.json its id, title, section, area, milestone, dependsOn, status and attempts. Otherwise exists=false, empty arrays, specPath "${opts.spec}".`,
  { label: 'load state', phase: 'Load', schema: LOAD_SCHEMA, effort: 'low' },
)
// Mirrors `ship.is_shippable`: the intent this run ends with can only ship from a task branch.
const shippable = b => (b.startsWith('worktree-') && b.length > 'worktree-'.length) || /^[^/]+\/.+/.test(b)
if (!shippable(state.branch)) {
  throw new Error(`'${state.branch || 'detached HEAD'}' is not a task branch. Start the run from a task worktree (claude --worktree <name>).`)
}
if (state.intentPending) throw new Error('logs/ship-intent.md is waiting for the fix pass. Let it ship, then rerun.')
if (!state.exists && state.dirty) throw new Error('The tree has uncommitted changes that are not this run\'s. Start from a clean task worktree.')

let { specPath, sections, tasks } = state

// ---------------------------------------------------------------- Plan
if (!state.exists) {
  phase('Plan')
  const outline = await agent(
    `You are planning the implementation of a large requirements document: ${opts.spec}.
If ${STATE}/outline.json already exists and is valid, return it unchanged. Otherwise:
1. If the spec is not plain text or Markdown (.docx, .pdf, .html...), convert it to Markdown at ${STATE}/spec.md and use that path from now on.
2. Map its structure without reading it whole: count lines, grep for headings and requirement ids, skim where needed. Split it along its own headings into sections of roughly 200-800 lines, each a coherent area of functionality, covering every line exactly once. Give each a short kebab-case id.
3. Write {specPath, sections} to ${STATE}/outline.json and return the same.`,
    { label: 'outline', phase: 'Plan', schema: OUTLINE_SCHEMA },
  )
  specPath = outline.specPath
  sections = outline.sections
  log(`${sections.length} spec sections`)

  await agent(
    `${CTX}
Spec: ${specPath}. Outline: ${JSON.stringify(sections)}
If ${STATE}/ARCHITECTURE.md already exists, stop and return a one-line summary. Otherwise:
- Read the spec's overview, non-functional and technical-constraint sections (by line range) and the existing code and instruction files.
- Where the spec leaves the stack open, prefer what the repo already uses. Add only dependencies the work needs.
- Write ${STATE}/ARCHITECTURE.md: stack, module layout mapped to spec sections, conventions, and the exact commands to provision a fresh checkout, build, run all tests, and run the tests for one module. Tests must run concurrently from separate checkouts (no fixed ports, no shared temp paths).
- Scaffold whatever the layout needs before any task can start, so the build and a smoke test pass. Start ${STATE}/progress.md.
Return a one-paragraph summary.`,
    { label: 'architecture', phase: 'Plan' },
  )

  await pipeline(sections, s => agent(
    `${CTX}
Decompose one section of the spec into implementation tasks: "${s.title}" (id ${s.id}), lines ${s.startLine}-${s.endLine} of ${specPath}.
If ${STATE}/sections/${s.id}.json already exists and is valid, return its task count without redoing it.
Read ${STATE}/ARCHITECTURE.md and those lines. Each task is one coherent, testable behavior a single engineer could finish and test in one sitting (roughly 50-400 lines of change). Every requirement in the range maps to at least one task; invent nothing. Fields: localId (${s.id}-1, ${s.id}-2, ...), title, area (the ARCHITECTURE.md module it mostly touches), specRefs (line ranges), acceptance (concrete checks, automatable where possible), dependsOn (localIds in this section), externalDeps (plain-language needs from other sections, e.g. "user authentication"; may be empty).
Write the array to ${STATE}/sections/${s.id}.json. Return the count.`,
    { label: `decompose ${s.id}`, phase: 'Plan', schema: COUNT_SCHEMA },
  ))

  const graph = await agent(
    `${CTX}
Build the task graph from every file in ${STATE}/sections/ (written by parallel planners) and ${STATE}/ARCHITECTURE.md.
- Merge duplicate or overlapping tasks across sections, keeping all their specRefs and acceptance criteria. Record the section each task came from.
- Assign final ids (keep the localId where unique).
- Resolve each externalDeps entry to concrete task ids in dependsOn. Break cycles by splitting tasks.
- Group the tasks into numbered milestones, 1 upward. Each milestone is one pull request a person reviews and merges before the next starts, so make each a coherent, reviewable slice of roughly 10-25 tasks, foundational work (data model, shared types, auth, config) first. A task depends only on tasks in its own or an earlier milestone.
- Every task: status "pending", attempts 0, notes "".
Write ${STATE}/tasks.json as {"spec": "${specPath}", "tasks": [...]} with full fields.
Return id, title, section, area, milestone, dependsOn, status and attempts for every task.`,
    { label: 'task graph', phase: 'Plan', schema: TASKS_SCHEMA },
  )
  tasks = graph.tasks
}

const byId = new Map(tasks.map(t => [t.id, t]))
const all = () => [...byId.values()]
const pendingMilestones = all().filter(t => t.status === 'pending').map(t => t.milestone)
const lastMilestone = Math.max(1, ...all().map(t => t.milestone))
const target = opts.allMilestones ? lastMilestone : Math.min(lastMilestone, ...pendingMilestones)
const inScope = t => t.milestone <= target
log(`${byId.size} tasks, ${all().filter(t => t.status === 'done').length} done; this run builds milestone ${opts.allMilestones ? `1-${target}` : target} of ${lastMilestone}`)
if (all().filter(inScope).length > 600) log('Large milestone: one run may hit the 1,000-agent cap. Rerun /implement-spec in this tree to continue.')

// ---------------------------------------------------------------- Build + Integrate
const isDone = id => byId.get(id)?.status === 'done'

async function build(round) {
  for (let wave = 1; ; wave++) {
    const pending = all().filter(t => t.status === 'pending' && inScope(t))
    if (!pending.length) return
    const ready = pending.filter(t => t.dependsOn.every(isDone))
    if (!ready.length) {
      log(`${pending.length} pending tasks wait on blocked dependencies: ${pending.slice(0, 20).map(t => t.id).join(', ')}`)
      return
    }
    // Spread a wave across areas first to keep conflicts down.
    const picked = []
    const areas = new Set()
    for (const t of ready) if (picked.length < WAVE && !areas.has(t.area)) { picked.push(t); areas.add(t.area) }
    for (const t of ready) if (picked.length < WAVE && !picked.includes(t)) picked.push(t)

    const label = `m${target}r${round}w${wave}`
    const tree = t => `${SCRATCH}/trees/${t.id}`
    log(`Wave ${label}: ${picked.map(t => t.id).join(', ')} (${pending.length} pending)`)

    const snap = await agent(
      `Prepare build wave ${label} in this checkout (the integration tree). Run each command on its own:
1. \`git add -A\`, then \`git write-tree\`, and note the tree id it prints.
2. \`git commit-tree <tree id> -p HEAD -m "spec-run snapshot ${label}"\`, and note the commit id. That commit is on no branch; HEAD and the branch do not move.
3. For each of ${picked.map(tree).join(', ')}: if it is already a worktree, \`git worktree remove --force <path>\`; then \`git worktree add --detach <path> <commit id>\`.
Return the commit id.`,
      { label: `snapshot ${label}`, phase: 'Build', schema: SNAPSHOT_SCHEMA, effort: 'low' },
    )
    if (!snap) throw new Error(`Snapshot for wave ${label} failed. Rerun /implement-spec to continue.`)

    // Barrier: the integrator applies a whole wave serially to the one integration tree.
    const built = await parallel(picked.map(t => () => agent(
      `${CTX}
Implement task ${t.id}: "${t.title}" (milestone ${t.milestone}).
Your checkout is ${tree(t)}: a detached worktree of snapshot ${snap.commit}, nested inside the integration tree. Other agents build other tasks in sibling checkouts at the same time.
- Make every edit inside ${tree(t)} and run every command from there. Run git as \`git -C ${tree(t)} ...\`.
- Leave ${STATE}/ alone in your checkout; the integrator owns it.
- A fresh checkout has nothing installed: provision it first, as .claude/rules/session-scope.md says.
1. Read ${STATE}/ARCHITECTURE.md, task ${t.id} in ${STATE}/tasks.json (acceptance, specRefs, notes from earlier attempts) and its spec lines in ${specPath}. Read code narrowly.
2. Implement it, with automated tests for each acceptance criterion. Run the tests for what you touched, not the whole suite.
3. Create ${tree(t)}/logs/, run \`git -C ${tree(t)} add -A\`, then write \`git -C ${tree(t)} diff --cached --binary ${snap.commit}\` to ${tree(t)}/logs/task.patch. Do not commit.
Return ok=true only if your tests pass and the patch is not empty; otherwise ok=false with the reason in summary. List the test files you ran.`,
      { label: `build ${t.id}`, phase: 'Build', schema: BUILD_SCHEMA },
    )))
    const reports = picked.map((t, i) => ({
      ...(built[i] || { id: t.id, ok: false, summary: 'build agent did not return', tests: [] }),
      patch: `${tree(t)}/logs/task.patch`,
    }))

    const integ = await agent(
      `${CTX}
Integrate build wave ${label} into this checkout, the integration tree. Build reports:
${JSON.stringify(reports, null, 1)}
For each report, in order:
- ok=true: run \`git add -A\` and \`git write-tree\`; that tree id is your rollback point. Apply with \`git apply --3way --index --exclude='${STATE}/*' <patch>\`. Resolve any conflict markers keeping both sides' intent, guided by both tasks' acceptance criteria in ${STATE}/tasks.json. Run the report's tests plus the tests for any file you changed while resolving. If they pass, set the task's status to "done" in ${STATE}/tasks.json. If they fail and a small fix does not solve it, roll back with \`git read-tree --reset -u <rollback tree id>\` and treat the task as failed.
- ok=false, or failed above: in ${STATE}/tasks.json increment attempts, append the reason to notes, and set status "blocked" if attempts >= ${MAX_ATTEMPTS}, else "pending".
Then remove this wave's checkouts with \`git worktree remove --force <path>\` and append a short entry for the wave to ${STATE}/progress.md.
Return id, final status and attempts for every task in the reports.`,
      { label: `integrate ${label}`, phase: 'Integrate', schema: INTEG_SCHEMA },
    )
    if (!integ) throw new Error(`Integration of wave ${label} failed. Rerun /implement-spec in this tree: ${STATE}/tasks.json records every task that landed.`)
    for (const r of integ.results) {
      const t = byId.get(r.id)
      if (!t) continue
      t.attempts = r.attempts
      t.status = r.status === 'pending' && r.attempts >= MAX_ATTEMPTS ? 'blocked' : r.status
    }
  }
}

// ---------------------------------------------------------------- Audit loop
for (let round = 1; ; round++) {
  await build(round)
  if (round > AUDIT_ROUNDS) break

  // The last milestone also audits for requirements no task covers at all.
  const final = !all().some(t => t.milestone > target && t.status === 'pending')
  const scoped = sections
    .map(s => ({ s, ids: all().filter(t => t.section === s.id && t.milestone === target && t.status === 'done').map(t => t.id) }))
    .filter(x => final || x.ids.length)
  const audits = await pipeline(scoped, ({ s, ids }) => agent(
    `${CTX}
Audit spec section "${s.title}" (lines ${s.startLine}-${s.endLine} of ${specPath}) against this checkout's working tree, uncommitted work included. Change nothing; running tests is fine.
- For tasks ${ids.join(', ') || '(none)'}: does the code meet every acceptance criterion in ${STATE}/tasks.json, with tests that check it?${final ? `
- Which requirements in the range does no task in ${STATE}/tasks.json cover?` : ''}
Skip anything a task with status "blocked" covers; it is already tracked. Report only concrete, spec-grounded gaps, each with specRefs and a one-sentence reason. An empty list is a good answer.`,
    { label: `audit ${s.id}`, phase: 'Audit', schema: GAPS_SCHEMA },
  ))
  const gaps = audits.flatMap((a, i) => (a ? a.gaps.map(g => ({ ...g, section: scoped[i].s.id })) : []))
  const unaudited = audits.filter(a => !a).length
  if (unaudited) log(`${unaudited} section audits did not return and were skipped this round`)
  if (!gaps.length) { log(`Audit round ${round}: no gaps`); break }
  log(`Audit round ${round}: ${gaps.length} gaps`)

  const added = await agent(
    `${CTX}
Audit round ${round} of milestone ${target} found these gaps:
${JSON.stringify(gaps, null, 1)}
Add tasks for them to ${STATE}/tasks.json. Drop gaps an existing task (any status) already covers, merge overlapping gaps, and size new tasks like the existing ones. Ids m${target}-gap${round}-1, m${target}-gap${round}-2, ...; section as given; milestone ${target}; area, specRefs, acceptance, dependsOn (existing ids allowed); status "pending", attempts 0, notes "".
Return only the tasks you added.`,
    { label: `gap tasks r${round}`, phase: 'Audit', schema: TASKS_SCHEMA },
  )
  if (!added || !added.tasks.length) break
  for (const t of added.tasks) byId.set(t.id, t)
}

// ---------------------------------------------------------------- Ship
const ids = s => all().filter(t => t.status === s).map(t => t.id)
const remaining = all().filter(t => t.milestone > target && t.status === 'pending').length
const shipped = await agent(
  `${CTX}
This run has finished milestone ${target} of ${lastMilestone}.
1. Write ${STATE}/REPORT.md: what this milestone built per spec section, every blocked or pending task with its notes, and anything a person must decide.
2. Ship as .claude/skills/ship/SKILL.md says, writing logs/ship-intent.md last. The subject says what milestone ${target} delivers; the body says why, which spec sections it covers, which tasks are blocked, and that the task graph is in ${STATE}/tasks.json.
Return the subject and the report in under 300 words.`,
  { label: 'ship', phase: 'Ship' },
)
return {
  milestone: target,
  done: ids('done').length,
  blocked: ids('blocked'),
  pending: ids('pending'),
  next: remaining
    ? `${remaining} tasks remain in later milestones. Once this PR merges, start a fresh task worktree and run /implement-spec ${opts.spec} again.`
    : 'Every milestone is built.',
  shipped,
}
