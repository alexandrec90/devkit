# Keeping a scheduled job window-less on Windows

Reference material split out of [`CLAUDE.md`](CLAUDE.md) when that file reached the
500-line instruction cap. It is the same three sections — the mechanics behind
`devkit_schtasks.windowless()` / `console_python()` and the checks in
`tests/test_scheduled_jobs.py` — with the cross-references that pointed at neighbouring
paragraphs re-anchored to name their target instead.

Read it when you are touching an installer, a spawn inside an `UNATTENDED` module, or
anything that resolves an interpreter for a scheduled task. The policy that sends you
here — artifacts, `<Settings>` order, `--devkit` — stays in `CLAUDE.md`.

## A job's reach is longer than the script the scheduler names

`pythonw.exe` stops the job opening a console. It does nothing about the console child
that job spawns: a process with no console of its own makes Windows allocate a **brand
new console window** for each console child, so `creationflags=CREATE_NO_WINDOW` has to
be on the spawn as well. That much was known, and `sweep.py`, `worktree.py` and
`worktree-guard.py` were fixed for it — with a check that read exactly those three files.

The flicker came back anyway, from `git` spawned by `sync-devkit.py` on the nightly
upgrade pass, and the shape of the miss is the part worth keeping. **The flag stops at a
process boundary**: `upgrade-project.py` flags its spawn of `sync-devkit.py`, Windows
*ignores* the flag because the interpreter it launches is `pythonw.exe` and the flag
applies only to console-subsystem children, and the fresh console-less process then
spawns `git` per project with nothing set. A check scoped to one job's own scripts could
not have seen it, because the script at fault belongs to no job.

So the rule is about the reachable set, and `tests/test_scheduled_jobs.py` holds both
halves: every module in `UNATTENDED` flags every spawn, and every script an installer
names has to be in `UNATTENDED`. Only the outermost spawn strictly needs the flag — a
window-less console *is* inherited, which is why nothing below `git` needs to know — but
"outermost" is not checkable, so every site carries it.

## The flag is half of it. The interpreter is the other half

Everything above was in place, every spawn carried `NO_WINDOW`, and a console window
still opened every night for about sixteen seconds. The section above even names the
mechanism in passing — *Windows ignores the flag for a GUI-subsystem child* — without
drawing the conclusion, and the wrapper paragraph in `CLAUDE.md` drew the opposite one.

`CREATE_NO_WINDOW` is a **console** flag. Passing it alongside `pythonw.exe` suppresses
nothing, because there is no console to suppress; the child is left console-*less*,
which is the precise condition that makes Windows allocate a fresh visible console for
each of *its* children. The flag protects the hop it is passed to and loses the whole
subtree behind it. Pass it with a console `python.exe` instead and the child gets a real
console that is merely hidden — and **every descendant inherits that**, including the
`ensurepip` that `python -m venv` re-spawns, and every hook `python -m pre_commit` runs.

Measured under `pythonw.exe`, flag set on both spawns:

| Spawn | Result |
| --- | --- |
| `sys.executable -m venv X` | rc 0 in 16.5s, **and a console window** for `ensurepip` |
| `python.exe -m venv Y` | rc 0 in 11.7s, no window |

So the pair is the rule, and neither half works alone:

- **`pythonw.exe` only at the scheduler boundary** — the task's own `<Command>`, and
  nowhere else. That is what `windowless()` is for in each installer.
- **`console_python()` plus `creationflags=NO_WINDOW` for every Python child a job
  spawns**, including the interpreter *inside* a `log-wrap.py --always ... -- <python>`
  argv, which is what `console()` is for in the three wrapped installers.

`sys.executable` is therefore banned outright in `UNATTENDED`, not merely inspected at
spawn sites: `git_policy` builds its argv in one helper and spawns it three functions
away through an injected `runner`, so a site-scoped check reads it as clean. The ban and
its one exemption — the body of `console_python` itself — are
`test_no_scheduled_job_spawns_the_interpreter_that_is_running_it`.

Three further checks in that file exist because each was a way this could come back
unseen: the `creationflags` **value** is now compared against a `NO_WINDOW` spelling
rather than the keyword merely being present; `os.system` and the other spawns that
accept no `creationflags` at all are refused; and the **import closure** of `UNATTENDED`
is walked, so a helper module that gains a spawn joins the check by being imported
rather than by being remembered. `IMPORTED_NOT_ENTERED` is the one exemption to that
last, and it is mechanically narrow: the module's spawns must all be inside its own
`main()`, which nothing imports its way into.

Its cost is the second half, and it is the one that trades a visible bug for an invisible
one: **the flag binds a child that captures nothing to the console it was just given**,
so such a child's output stops reaching the handles it inherited. Every spawn in the
reconcile path captures, which is why this never bit there. `docker-maint.py` streams,
and flagging it alone would have emptied `logs/scheduled-docker-prune.log` of everything
docker said — an artifact reporting an exit code and nothing to diagnose it with, which
is the failure that made artifacts mandatory in `CLAUDE.md`. Capture, or name the
streams (`docker-maint.inherited_streams`); never just add the flag.

## A file named `pythonw.exe` need not be an interpreter

Both sections above were in place — the pair was the rule, every spawn carried the flag,
and the checks were in `tests/test_scheduled_jobs.py` — when a console window came back
at boot on 2026-08-21. **Every guard for this bug was a source scan of devkit's own
files, and every one of them was checking a name.**

Inside a virtualenv, `Scripts\pythonw.exe` is not an interpreter. It is a stub deferring
to the base install named in `pyvenv.cfg`, and the two builders differ in the one way
that matters here: CPython's stub loads the base **in-process**, while uv's is a
trampoline that **spawns it as a child**. So `devkit-global-tools` — the only job whose
interpreter comes from a `.venv`, because `install-global-tools.interpreter` prefers the
checkout's — was registered against a GUI-subsystem file correctly named `pythonw.exe`,
opened no console of its own, and handed its console child a brand new visible one. That
is the same mechanism as the section above, arriving through the *scheduler boundary*
rather than through a spawn site.

Two consequences, both now enforced rather than written down:

- **`devkit_schtasks.windowless` owns the resolution, and no installer keeps a copy.**
  Six of them did, identically, on the stated reasoning that six lines are cheaper to
  repeat than to couple — and all six were wrong at once for as long as it took one
  job's interpreter to come from a venv. It resolves through `home`, which also settles
  the hazard `interpreter` warned about from the other end: a box's `.venv` disappears
  when its PR merges, and the base install outlives every venv.
- **A source scan cannot close this class of bug, so one check reads the machine.**
  `schedule_health.virtualenv_interpreter` compares each registered task's `Task To Run`
  against `pyvenv.cfg` and reports it at session start. All three rounds of this bug were
  found by a human watching windows flash; that is the loop this replaces.
