import { sep } from 'node:path'

import { describe, expect, it } from 'vitest'

import { SLOT_SPAN, portOffset, slotFor, worktreeName } from './worktreePort.ts'

/**
 * Vendored from devkit alongside `worktreePort.ts`, and run by each project's own
 * vitest: the module is dependency-free, so the only thing that can break it is a
 * project's path layout, which is exactly what these exercise.
 *
 * The dev port has to differ between parallel worktrees without anybody configuring
 * one, because three of the four ways a worktree is cut run no project code at the
 * time (see `worktreePort.ts`). These are the properties that has to hold.
 */

/** A path built with this platform's separator, so the assertions run on both. */
const path = (...parts: string[]) => parts.join(sep)

describe('worktreeName', () => {
  it('names the worktree for the nested tiers', () => {
    expect(worktreeName(path('C:', 'vs-code', 'carameli', '.claude', 'worktrees', 'cerf'))).toBe(
      'cerf',
    )
    expect(worktreeName(path('C:', 'vs-code', '.worktrees', 'carameli--task-0909'))).toBe(
      'carameli--task-0909',
    )
  })

  it('names a Codex worktree, which sits behind a digest outside the checkout', () => {
    // `codex --worktree` cuts `<home>/.codex/worktrees/<repo-digest>/<name>`. The
    // digest is opaque and names no repo, so the marker is matched one level further
    // up than the nested tiers' -- and the name Codex picks is the repo's own, which
    // is why a worktree here MUST derive an offset: on 0 it would take the checkout's
    // port, and `strictPort` would then refuse to start rather than slide.
    expect(
      worktreeName(path('C:', 'Users', 'a', '.codex', 'worktrees', '2e51', 'carameli')),
    ).toBe('carameli')
  })

  it('is empty for an ordinary checkout', () => {
    expect(worktreeName(path('C:', 'vs-code', 'carameli'))).toBe('')
    expect(worktreeName(path('home', 'alex', 'vs-code', 'carameli'))).toBe('')
  })

  it('is empty for the path the compose frontend service runs Vite from', () => {
    // Load-bearing, not incidental. `docker-compose.yml` bind-mounts `./frontend:/app`
    // and publishes `${FRONTEND_HOST_PORT:-5173}:5173`, so the container half of that
    // mapping is a constant. The container must therefore land on slot 0 whatever tree
    // it was started from, or every stack publishes a port with nothing behind it.
    expect(worktreeName('/app')).toBe('')
    expect(worktreeName(path('C:', 'vs-code', 'carameli', '.claude', 'worktrees', 'cerf', 'app')))
      .toBe('')
  })

  it('is empty for a subdirectory of a worktree, `frontend/` included', () => {
    // Right answer, and a trap for the caller: `vite.config.ts` lives in `frontend/`,
    // so the obvious `import.meta.dirname` is this path and not the worktree. The
    // module is not what should be loosened to accommodate that — a worktree is a
    // checkout, not any directory under one, and matching deeper would give
    // `frontend/`, `src/` and `app/` offsets of their own. The config passes the
    // checkout root instead.
    expect(
      worktreeName(path('C:', 'vs-code', 'carameli', '.claude', 'worktrees', 'cerf', 'frontend')),
    ).toBe('')
  })

  it('is empty for the container directory itself, in every tier', () => {
    // `.claude/worktrees` holds worktrees; it is not one, and treating it as one would
    // give the checkout a second offset the moment anyone ran Vite from there. Codex's
    // digest directory is the same case one level down.
    expect(worktreeName(path('C:', 'vs-code', 'carameli', '.claude', 'worktrees'))).toBe('')
    expect(worktreeName(path('C:', 'vs-code', '.worktrees'))).toBe('')
    expect(worktreeName(path('C:', 'Users', 'a', '.codex', 'worktrees'))).toBe('')
    expect(worktreeName(path('C:', 'Users', 'a', '.codex', 'worktrees', '2e51'))).toBe('')
  })

  it('matches path segments rather than a substring', () => {
    // The reason this is not `dir.includes('.worktrees')`: a checkout whose name merely
    // contains the word is an ordinary checkout, and giving it a derived port would
    // move a static server nobody asked to move.
    expect(worktreeName(path('C:', 'vs-code', 'worktrees-talk', 'demo'))).toBe('')
    expect(worktreeName(path('C:', 'vs-code', 'my.worktrees', 'demo'))).toBe('')
    expect(worktreeName(path('C:', 'vs-code', 'carameli', 'claude', 'worktrees', 'cerf'))).toBe('')
    expect(worktreeName(path('C:', 'some', 'project', 'worktrees', '2e51', 'thing'))).toBe('')
  })
})

describe('slotFor', () => {
  it('stays inside the span and never returns the static checkout\'s 0', () => {
    // 0 is reserved: it is what says "this is the checkout you already know", so a
    // worktree landing on it would silently take 5173 — the port `run-e2e.py`,
    // `run-ci.py` and the `CORS_ORIGINS` default all name.
    // The shapes real names take: a word, one character, empty, a Remote Control
    // worktree (`bridge-cse_` plus a session id), and something absurd.
    const sessionish = `bridge-cse_${'a1B2'.repeat(6)}`
    for (const name of ['cerf', 'a', '', sessionish, 'x'.repeat(200)]) {
      const slot = slotFor(name)
      expect(slot).toBeGreaterThanOrEqual(1)
      expect(slot).toBeLessThan(SLOT_SPAN)
    }
  })

  it('is stable for the same name', () => {
    // The property a bookmarked `localhost:5179` depends on: restarting the server, or
    // the machine, must not move it.
    expect(slotFor('twinkly-whistling-cerf')).toBe(slotFor('twinkly-whistling-cerf'))
  })

  it('stays inside devkit\'s reserved span for the frontend base', () => {
    // `ports.toml` guarantees every service base is at least `max_slots` apart, so a
    // derived port is only safe while it stays under that number. Redis at 6379 is far
    // off, but minio_console sits 40 above minio for exactly this reason — the margin
    // is the guarantee, not the distance.
    expect(SLOT_SPAN).toBe(16)
    for (const name of ['cerf', 'heron', 'zephyr', 'carameli--task-0909']) {
      expect(5173 + slotFor(name)).toBeLessThanOrEqual(5173 + SLOT_SPAN - 1)
    }
  })

  it('spreads the names actually in use', () => {
    // Not a claim about the hash in general — a claim about this workload, which is
    // Claude Code's generated worktree names. A collision is survivable (strictPort
    // reports it) but should not be the common case.
    const names = [
      'twinkly-whistling-cerf',
      'glittery-marinating-flamingo',
      'composed-imagining-harp',
      'golden-kindling-whistle',
      'modular-watching-starfish',
      'zesty-beaming-heron',
    ]
    expect(new Set(names.map(n => slotFor(n))).size).toBeGreaterThanOrEqual(names.length - 1)
  })

  it('honours a narrower span', () => {
    for (const name of ['cerf', 'heron', 'zephyr']) {
      expect(slotFor(name, 4)).toBeLessThan(4)
      expect(slotFor(name, 4)).toBeGreaterThanOrEqual(1)
    }
  })
})

describe('portOffset', () => {
  it('leaves an ordinary checkout on the conventional port', () => {
    // `localhost:5173` has to keep meaning what it always meant, or every habit,
    // bookmark and default-port script on the machine breaks to fix a problem the
    // primary checkout does not have.
    expect(portOffset(path('C:', 'vs-code', 'carameli'))).toBe(0)
  })

  it('leaves the compose container on it too', () => {
    // The other half of the mapping in `docker-compose.yml`. Paired with the
    // `worktreeName('/app')` assertion above so a change to either layer fails here.
    expect(portOffset('/app')).toBe(0)
  })

  it('moves a worktree off it, in every tier', () => {
    expect(
      portOffset(path('C:', 'vs-code', 'carameli', '.claude', 'worktrees', 'cerf')),
    ).toBeGreaterThan(0)
    expect(
      portOffset(path('C:', 'Users', 'a', '.codex', 'worktrees', '2e51', 'carameli')),
    ).toBeGreaterThan(0)
  })

  it('gives two worktrees of the same checkout different offsets', () => {
    // The whole point. These two names are real ones this repo has carried.
    const under = (name: string) =>
      portOffset(path('C:', 'vs-code', 'carameli', '.claude', 'worktrees', name))
    expect(under('twinkly-whistling-cerf')).not.toBe(under('glittery-marinating-flamingo'))
  })

  it('gives the same worktree name the same offset under any tier', () => {
    // The tiers are cut by different tools; a name is a name, and an offset that
    // depended on which tool cut it would move when a session was resumed elsewhere.
    expect(portOffset(path('C:', 'vs-code', 'carameli', '.claude', 'worktrees', 'cerf'))).toBe(
      portOffset(path('C:', 'vs-code', '.worktrees', 'cerf')),
    )
    expect(portOffset(path('C:', 'vs-code', 'carameli', '.claude', 'worktrees', 'cerf'))).toBe(
      portOffset(path('C:', 'Users', 'a', '.codex', 'worktrees', '2e51', 'cerf')),
    )
  })
})
