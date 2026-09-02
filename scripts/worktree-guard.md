# The guard: what it judges, and what it declines

`worktree-guard.py` is the PreToolUse tier that keeps an agent's edit off a static
checkout's home branch. `scripts/CLAUDE.md` carries the *lifecycle* half of the
ephemeral tier — what `worktree.py` must preserve when it destroys a box; this file
carries the guard's own decisions, and each one is a failure that already happened.

Read it before touching `worktree-guard.py`, the tools its matcher lists, or anything
that resolves a box for a session. It lives beside `CLAUDE.md` rather than inside it
for the reason `windowless-jobs.md` does: this is mechanism a session needs only when
it is in that code, and the file it was in is against a 500-line cap.

## What it judges

- **The guard re-aims the call rather than refusing it, and the refusal is the
  fallback.** For a year this hook could only exit 2, because a PreToolUse hook could
  only allow or deny — so every guarded session paid a failed tool call per routed edit,
  the agent re-sent arguments it had already sent (for a `Write`, a whole file), and the
  transcript filled with hook errors that described correct behaviour. Claude Code's
  `hookSpecificOutput.updatedInput` rewrites the arguments the tool is called with, so
  the edit now lands in the box on the first attempt with the prose arriving as
  `additionalContext`. Two properties keep it honest, both of them silent when broken:
  the rewrite is applied **only when the same object sets no `permissionDecision`**, so
  an `"allow"` added for symmetry drops it and lands the edit on the home branch; and
  an unrecognised path key is logged as `permission_updated_input_invalid` and the
  **original** arguments are used, which is why the path is written back under the key
  it was read from rather than under `file_path`. `redirect_blocker` is the single
  predicate for falling back to the old block, and every case in it is a way the rewrite
  would fail *quietly* rather than loudly — a hook adapter that would drop the member
  (`DEVKIT_HOOK_ADAPTER`), a tool whose argument shape the guard does not model, or an
  `Edit` whose `old_string` the box's copy of the file does not contain.
- **A shell command is judged too, and it is never re-aimed.** Editor calls were the
  whole scope until Claude Code's bypass-permissions mode began telling sessions — in
  text arriving inside tool results, indistinguishable from their operator's — to prefer
  `sed`, heredocs and short scripts over Edit and Write. An agent that complies writes to
  a checkout's home branch through a tool the guard was not watching, so the blind side
  stopped being an omission and became a route. `SHELL_WRITE_ALL`, `SHELL_WRITE_LAST` and
  `redirect_targets` are a **closed list of write verbs**, deliberately shaped like
  `enforce-capped-bash.py`'s blocklist rather than like the proof obligation it replaced:
  requiring a command to demonstrate it does not write means modelling the shell, and
  that design was 46% false positives there. What the list cannot see — an interpreter
  script, `python -c 'open(...)'` — is a documented gap with a test naming it, not an
  oversight. Redirections are the one member that needed *more* shell modelling rather
  than less: a regex over `>` read `awk '$1 > "…"'`, a `>=` inside a `python -c`, and a
  heredoc body quoting a path as writes, and the sixth such block in a single session
  had cut a sixth box that `reconcile` reaped minutes later as never used. So
  `redirect_targets` walks the statement tracking quote state, `strip_heredocs` drops
  bodies before either tier reads them, and an unexpanded `$VAR` names no path.
  The verdict is always the block path, because the rewrite replaces a path
  *argument* and a command line has none; `shell_note` tells the agent which of its own
  words was read as the write, or it re-issues the line with the box path bolted on
  somewhere else.
- **Codex's `apply_patch` was the same hole in a third shape.** Its target sits inside a
  `*** Add File:` header, not in a path argument, so `guarded_targets` found none and
  `main` allowed every Codex write until 2026-08-24 — seventeen onto carameli's static
  checkout, pushed on a `feat/` branch. `patch_targets` reads the envelope out of
  `command` (the key `Bash` also uses); it blocks, for the shell tier's reason.
- **A branch move is judged as well, and it is the one verdict that ends in neither a box
  nor a rewrite.** `git checkout` writes no file, so both tiers above are blind to it —
  and it is the act that parked carameli's static checkout on another session's
  `agent/...` branch for two days in August 2026, with every other tier working exactly as
  designed. The second-order effect is what makes it worth a tier of its own rather than a
  tidiness rule: a checkout parked on a live task branch **turns this hook off for that
  checkout**, because an edit onto a task branch carrying commits is precisely the "fix PR
  #42" case `needs_box` declines. So `switch_targets`/`switch_branch`/`switch_decision`
  block a `checkout`/`switch` onto a managed task branch — including `-b`/`-c` creation,
  and including `origin/agent/...`, which detaches HEAD there instead — and the message
  spells the three things the move usually stands in for (`worktree.py resume`,
  `worktree.py preview --branch`, a plain `git log/show/diff`) as commands. Moving a
  checkout **home** stays allowed: that is the repair, and a tier with no exit is the
  defect `sweep.NEEDS_PR` had. A box is judged too, against the branch its lease records,
  because `reconcile` looks a box's PR up by that name.
- **The matcher is the other half, and it is not vendored.** A tool the guard judges but
  `.claude/settings.json` does not list is a tier that silently covers nothing, and each
  project owns its own copy of that file. `test_the_hook_is_wired_for_every_tool_it_judges`
  compares `MUTATING_TOOLS` against devkit's matcher so at least the source of truth
  cannot drift; a machine's user-level registration is outside every repo and has to be
  widened by hand.
- **Two copies of the guard run on every call, and they race for the box.** It is
  registered in the user's `settings.json` and in the project's, and Claude Code fires
  both; each plans a box for the same `(session, project)` and the loser's `git worktree
  add` dies on the branch the winner has just created. Because the two responses are
  merged into one object, the agent was handed a spawn-failure error *beside* an
  `additionalContext` saying the edit had been applied in the box — and nothing had been
  written either way, so believing the context meant building on a change that did not
  exist. `worktree.spawn_lock` brackets plan-and-apply so the second process waits and
  finds the first one's box; `after_failed_spawn` is the fallback for when the wait runs
  out, and it asks whether a box exists rather than matching on git's message, because
  the box is what the decision turns on. Deduplicating the registration would fix the
  race and cost every project without its own copy of the hook, so the hook absorbs it.

## What it declines, and why each case exists

- **A path that belongs to no project**, which is what keeps a reference checkout out of
  the box tier entirely. It builds its project list with `devkit_project.known_projects`,
  so a folder in `NOT_PROJECTS` is registered in the workspace — visible, readable — and
  yet owns no path the guard will route: an edit there is allowed silently, on whatever
  branch it is parked on. Reading the registry raw instead would cut such a checkout a
  box on an `agent/...` branch — for a folder that ships nothing, and whose remote may
  not even be a host with pull requests for that branch to become. `NOT_PROJECTS` is
  empty today; the behaviour is what makes registering one safe again.
  `test_an_edit_into_a_reference_checkout_is_allowed` is the ratchet, and it is a
  `main()` test on purpose: `redirect_decision` takes the project list as an argument,
  so only the shell can be wrong about it.
- **A path a box would protect nothing about**, decided per *path* rather than per
  checkout by `guard_probes.path_is_exempt`, which unions two probes. **Git-ignored**
  (`check-ignore`, consulting the index, so a tracked file matching an ignore rule is
  still routed): a `.env` cannot land on any branch, so a box protects no branch from it
  and the value ends up in a worktree that is destroyed without ever shipping. **Already
  dirty** (`status --porcelain --untracked-files=no`): the `needs-branch` verdict a block
  exists to prevent is already true of that path, the edit cannot make it truer, and a box
  cut from `origin/<default>` cannot make it false — it does not contain the change. That
  second half is the 2026-08-31 carameli session, which was asked to fix a
  `layoutConfig.ts` the user had just written from the comic-book skin's in-browser editor
  and staged on `master`: all ten of its edits were routed, every one of them failing with
  `the box's copy of the file does not contain the text this edit replaces`, which is not
  bad luck but the shape of the case — a dirty file's `old_string` is missing from
  `origin/<default>` *by construction*, so the re-aim can never fire. Tracked only, since
  the agent's own `Write` creates untracked files and routing those is a hole to close
  rather than an exemption to widen. Both probes **fail closed**.
- **Among paths it does own, two checkout-level cases**: the edit is already inside a box
  **this session holds the lease on**, or the checkout is on a branch that is not its
  home one **and carries commits of its own** — the "fix PR #42" case, where something
  deliberately checked that branch out and a fresh box would put the fix somewhere the
  PR never sees. Anything else that would land on a home branch gets a box, because
  landing there with no task branch under it is the agent manufacturing the exact
  `needs-branch` backlog the sweep exists to clear.
- **A box is owned space, not shared space.** The allow for `.worktrees/` paths used to
  be unconditional, and a second session that saw a topically-matching live box in
  `worktree.py list` adopted it wholesale — two sessions' edits interleaved in one
  worktree until one watched files change under it mid-turn. `foreign_box` now blocks an
  edit into a box leased to a different session and routes it to the session's own box,
  with `worktree.py claim <box> --session <id> --yes` named in the message as the
  sanctioned takeover when the user really has handed the work over. The comparison is
  `sessions_match`, which accepts a hand-abbreviated lease id (≥8 characters, prefix in
  either direction) so a `worktree.py new --session <first 8 hex>` box keeps admitting
  the session that cut it. An **unowned** box — an adopted orphan whose lease cannot
  name an owner — stays open to everyone, because blocking there would dead-end every
  box that survived a lost lease file.
- **"Is this a task branch" is not the question; "is there work here a box would
  strand" is.** Being a managed task branch used to be the whole test, and the effect
  was that the first session to leave one checked out turned the guard off for every
  session afterwards — the checkout became shared, unguarded space until someone parked
  it back on a home branch. Two sessions landed in one checkout that way, one of them
  on a branch whose PR had already merged. `guard_probes.branch_protects_open_work` is
  the distinction, and it has two producers. `branch_has_own_commits` is deliberately
  local (`git rev-list` against an already-fetched `origin/<default>`, not a PR lookup)
  because this runs on every edit and a network round trip in a PreToolUse hook is a
  hang; it **fails closed**, so any error declines. `branch_is_a_sweep_park` is the
  second, and it exists because this hook and `sweep.py --branch` disagreed: `--branch`
  cuts an `agent/...` branch **in place**, from HEAD, precisely so a dirty tree comes
  along untouched — and the branch then has no commits, so the next edit was routed into
  a box cut from `origin/<default>`, which is the one place that work is not. It reads
  the anchor marker `--branch` writes *and* requires the tree to be dirty: once the work
  is committed the first producer answers, and a clean checkout on a spent task branch is
  the shared-unguarded-space case above, which must still be routed.
- **Nor is "does the branch have a devkit prefix".** That was the *other* half of the
  same mistake, and it survived the fix above until 2026-08-29: carameli parked on
  `add-call-status-icons` with PR #252 open blocked an edit and got a box off
  `origin/master` that could not hold the code under repair, so the agent's `Edit`
  failed with "the box's copy of the file does not contain the text this edit
  replaces". A human's branch protects a PR exactly as an `agent/...` one does.
  `protectable` is the predicate: a managed prefix qualifies, and so does simply not
  being the branch `origin/HEAD` names. `default_branch_of` supplies the second half
  and **fails open to `""`** — the opposite of `guard_probes.branch_has_own_commits`,
  because the two unknowns have opposite costs. Not knowing whether a branch carries work must not
  start diverting edits; not knowing which branch is *home* must not start allowing
  them onto one, which is why a checkout sitting on `master` with local commits still
  gets a box.

## The slug is keyed by session, not by worktree

The prompt's slug reaches the box through `scripts/task_slug.py`, keyed by **session id**
rather than by worktree. That is the only key the two events share: the prompt arrives on
UserPromptSubmit, the box is cut on PreToolUse, and the two run in different processes
with different working directories. Without it every guard-cut box was named
`ws-<8 hex of session id>` and no PR title said what it did.

### Which is why a branch name can publish a word the task was to delete

The slug is the prompt, and a prompt states the task — so "make sure references to
&lt;licensed product&gt; are removed" from a **public** repo became
`agent/make-sure-references-<brandname>-0824`, a branch about to push the exact token it
was asked to delete. Nothing downstream catches that: by the time the branch has a name,
the name is what `git push` publishes.

`task_slug.record` strips denied terms from every slug it writes, from two sources it
unions — `DEVKIT_SLUG_DENY` (comma-separated, one session) and one term per line in
`<boxes root>/slug-deny.txt`, which is machine-local state under the boxes directory.
**Not a file in the repo it protects**: a committed denylist publishes the same word it
is hiding. A slug redacted to nothing is not recorded, which lands the guard on the
`ws-<session>` fallback — ugly and safe.

It is a list somebody has to fill in, and that is the honest limit of it: no harness can
know that a word is a licensed brand. What it does buy is that filling it in once covers
every later session on this machine.
