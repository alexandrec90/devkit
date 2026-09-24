---
name: triage-harness
description: Work the harness-defect backlog -- the agent reports and failed box spawns every harnessed project files on this machine's central ledger -- verifying each against current code, fixing what is still real, and recording what retired it.
disable-model-invocation: true
argument-hint: 'Optional: a project name, an event name, or a group id to work on first'
---

# Work the harness-defect backlog

Project sessions no longer file harness defects — `.claude/rules/engineering.md` takes
the harness off their plate — so what reaches this ledger now is what the harness records
about itself: a scheduled job failing (`log-wrap.py --always`), and the reports already
on it. `scripts/fix-pass.py` sends the open backlog to its devkit session with everything
else harness-shaped, and that session works it by the steps below. This skill is the
same sweep run by hand. **Read `logs/fix-pass.log` first**: a session the pass has
already sent at the backlog is on a branch of its own, and a second sweep at the same
groups is two branches at one defect.

Devkit-only, deliberately. A defect in the harness is a defect in **devkit** — the gates,
the scheduled jobs, the vendored scripts — whatever project the session that hit it was scoped to, and a
consumer repo has nothing to fix but a vendored copy to `--pull` once the fix ships here.
That is why this skill is not in `sync-devkit.py`'s `MANIFEST`.

**What does not follow is that everything on the ledger is devkit's.** A project owns some
of the scripts an agent runs — `run-tests.py`, `lint-all.py`, the harness a task
dispatches — and where those have diverged from `templates/core/`, a report about one is a
report about that project. The 2026-08-26 sweep found four in a row: an empty failure
artifact on an env skip, `lint-all` linting zero files where git is absent and calling it
success, `markdownlint --fix` over `**/*.md` while gating on the changed set. None of the
three exists in devkit's templates.

The check is one grep, and it is worth it before you start fixing: if the file the report
names has a `templates/core/` counterpart, compare them; if devkit has no such file at
all, the fix belongs in that project's checkout — `<workspace>/<project>/`, which is on
this machine and which this session can edit like any other directory.

**That changes where the fix lands, never whether it is made.** §3 says why: this skill is
the only thing that reads this ledger, so a group it declines to fix is a group nothing
will ever fix.

## Nothing is left open

This is the one rule the rest of the skill is arranged around, and it is worth stating
before the steps rather than inside them.

Every group is either **retired with a fix** or **retired as not-a-defect with the
evidence** (§2). There is no third outcome. A backlog entry that survives a sweep has been
read by an agent, judged real, and handed to the next agent — who pays the same
verification cost to reach the same conclusion, and hands it on again. That is the exact
loop the ledger replaced, restored one deferral at a time, and it is worse than the
original because each pass leaves a note saying somebody looked.

Three deferrals that read as prudence and are not:

| Reading | Why it is not a reason |
| --- | --- |
| "this belongs in its own PR" | **Bundling unrelated fixes here is correct.** A triage sweep is not a feature branch, and a reviewer reading one PR of six fixes costs less than six groups nobody fixes. Say in the PR body that it is a sweep and give each fix its own section. |
| "this is a refactor, not a defect" | A structural ceiling raised three times **is** a defect report — `.claude/rules/engineering.md` says so — and the report has already done the measuring. Cut it. |
| "this is project X's file, not devkit's" | Then fix it in project X's checkout. The ownership check routes the change; it does not excuse it. |

The only thing that ever stays the user's call is §3's two irreversibles: **discarding work
that exists only in a box**, and **cutting a release**. Ask about those; decide everything
else.

If a group is genuinely beyond one session — a fix needing a decision only the user can
make, or an outage you cannot reproduce — that is a question for the user **in the turn
you find it**, not a line in a closing report. Ask it, get the answer, fix it. A sweep
ends with the backlog empty.

> Every command below is issued bare. A wrapper here buys no second bound.

## 1. Read the backlog as groups, not as lines

```bash
python scripts/harness_triage.py
```

It prints, and writes `logs/harness-triage.log` — read from the file, per the
failure-artifact rule. Both are grouped by `(event, project, detail)`, most recurrences
first, because a backlog read flat stops being read: 24 of this machine's first 39 open
items were **one** spawn race recorded 24 times.

Three event names reach it, and they want different treatment:

| Event | What it is | Where the diagnosis starts |
| --- | --- | --- |
| `agent-report` | an agent's *judgment* — a false-positive block, an instruction that dead-ended | the `command=` field: re-run it against today's code |
| `guard-spawn-failed` | an edit was blocked and no box could be cut for it | the `detail=` field: it carries the exception |
| `codex-translation-gap` | a hook's answer did not survive Codex's schema — a member refused or stripped | the `detail=` field: it names the members |

No agent hook is wired any more, so the last two are old rows only: nothing new writes
them. Everything else on the ledger — `guard-route`, `guard-block`, `capped-bash-block`,
`lint-fix-block`, likewise historical — is forensics, not a backlog. `check-logs` §7
covers reading those.

A `codex-translation-gap` group wants one question the other two do not: **is the project
name real?** `harness_events.record(root=None)` resolves `$DEVKIT_DIR`, so a test that
drove a hook end to end used to append to this machine's ledger under whatever
`project_name` made of its `tmp_path` — and 18 of them, filed against
`test_a_lost_decision_is_refuse0`, read as a recurring failure nobody could reproduce
because the only thing reproducing them was the suite. A vendored `conftest` fixture now
isolates that, so a name like this on the open list means an *older* copy is doing it.

`project=` is normalised on read, so rows written from a box (`devkit--some-task-0824`)
group with the project they were cut from. Do not filter on the raw field.

**A group can span machines**, and the `host` line names the ones it was seen on. The
backlog is the union of every `harness-events-<host>.log` in the checkout's `logs/`, so on
a machine whose ledger is pooled you are triaging both. Two consequences: a group seen on
one host only is a lead worth following — a path, a shell, a scheduler that differs — and a
`--resolve-like` retires **every** machine's copy in one note, which is the point. A
machine whose `logs/` is not pooled still shows only its own, and a defect filed on the
other one is invisible here; that is a transport that was never set up, not an empty
backlog.

## 2. Verify each group before believing it

**A report is evidence that an agent was blocked, not that the block was wrong**, and
this ledger is append-only: the oldest entries predate several fixes. So for each group,
in this order — the first answer that lands ends it:

1. **Is it already fixed?** `git log --oneline -20 -- <the file the report names>`, and
   `gh pr list --state merged --limit 20 --search "<keyword>"`. A merged PR that changed
   the behaviour retires the group; go to step 4 with its number.
2. **Does it still reproduce?** Run the `command=` field, or the equivalent, against the
   working tree. An old capped-Bash or guard block reproduces against the script's
   decision function in one call, though no hook runs it any more.
3. **Was the report itself wrong?** An agent that wrapped a command the blocklist never
   named, or renamed a branch to get past a check, filed a defect against a gate doing
   its job. Retire it with a note saying so — that is a real resolution, not a dismissal.
   So is a block **no devkit hook produced** — none is wired: a refusal quoting "stays
   inside the worktree" or "cannot be shown not to be git" is Claude Code's own `claude --worktree`
   isolation guard, and one grep of `scripts/` for the quoted words settles it.
4. **`version=` says whether the reporter's copy was current.** A consumer at
   `DEVKIT_VERSION` weeks behind may be reporting something `main` already fixed. Check
   before triaging it as live.

Never resolve a group you have not answered one of those four for. Ageing out was the
hole this replaced; a note that says "probably fine" is the same hole with a command in
front of it.

## 3. Fix every group that survives, in the same turn

The execution default applies unchanged: what is worth naming is worth doing. Fix the
live groups, with a test each per `.claude/rules/engineering.md` — for a false-positive
block, the regression test is the exact command the report named.

**This skill is the last safety net.** Nothing else reads this ledger: not CI, not a
scheduled job, not a person. In a repo where every file was written by an agent and prose
has no compiler, the backlog is the only place a defect nobody had time for is written
down — so a group this skill declines is not deferred, it is dropped, with a paper trail
that makes the next sweep pay the verification cost again before dropping it again. Three
of the groups cleared on 2026-09-17 had been read and deferred by earlier sweeps; the
oldest was fifteen days old and its fix was ninety minutes of work.

So the sweep ends at zero. Bundle the fixes into **one PR** — unrelated is fine and
expected, a section per fix in the body — and prefer a large reviewed sweep to a small one
that leaves a list.

**A fix in another project's checkout is still this sweep's work.** `<workspace>/<project>`
is on this machine, but **never edit that static checkout**: it sits on its default
branch, the fix pass cannot open a PR from there, and the first sweep to do it left a
carameli fix unstaged on `master` where nothing would ever ship it. Cut a worktree on a
task branch first, from this devkit checkout:

```bash
python scripts/agent-worktree.py new --pick "carameli:master" --slug agents-md-ignore --agent none
```

It prints the path, under that project's `.claude/worktrees/`. Edit there, run its
targeted tests there, and leave a `logs/ship-intent.md` there the same way; say in this
intent's body which sibling intents the sweep left, and the fix pass opens each PR.
Nothing about the ledger being devkit's makes a carameli file unfixable from a devkit
session.

Two things stay the user's call, because both are irreversible and neither is yours to
assume: **discarding work that exists only in a box**, and **a fix that has to be
released** rather than merged (a vendored-tier change reaches consumers only through
`sync-devkit.py --pull` against a tag — say so in the PR, and see `RELEASING.md`). Neither
is a reason to leave the group open: make the change, and say in the report that a release
is what carries it.

## 4. Record what retired it — this is the step that makes the list shrink

```bash
python scripts/harness_triage.py --resolve-like <id> --note "fixed in #202: redirect_targets is quote-aware" --pr 202
```

`--resolve-like` takes one id and retires **every open item sharing its signature**, which
is the whole point of the grouping — one note for one defect, not 24. `--resolve` takes
literal ids when a group needs splitting.

- The note is **required**. `--note` refuses to be blank, exactly as `.devkit-untested.txt`
  refuses to be seeded over.
- A resolution is itself a ledger event, so the ledger stays append-only: nothing is
  edited, nothing can go stale against a second state file. It is written to **this**
  machine's shard even when it retires a row another machine filed — no machine writes to
  another's file, which is what keeps a pooled `logs/` conflict-free.
- Ids are content-addressed, not line numbers — a resolution written today still names
  its event after a thousand appends.

Resolve **after** the fix is in your intent, not before, and name the branch when there
is no PR number yet: the fix pass opens the PR from the intent, and a note naming a PR
that does not exist is the one claim on this ledger nothing can check.

## Reporting

Give the user the shape, then the work: how many groups were open, how many were already
fixed (and by what), and how many are now fixed here. A count that only went down because
things were retired is worth saying out loud — it is the failure mode this tool was built
to make visible.

**End by re-running `python scripts/harness_triage.py` and quoting the count.** The sweep
is finished when it prints zero open, and a non-zero count is the report's headline, not a
footnote: say which group, and what you need from the user to close it. "Left open with a
reason" is not an outcome this skill has.
