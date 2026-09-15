import { basename, sep } from 'node:path'

/**
 * Which port offset this checkout's dev server takes, derived from where it sits on
 * disk.
 *
 * **Vendored from devkit.** This file is in `sync-devkit.py`'s gated manifest and is
 * byte-identical in every project whose `.devkit.toml` has `[frontend] enabled = true`;
 * a local edit is reported as drift by the PR gate. Change it in devkit and let
 * projects `--pull`. `WORKTREE_DIRS` below mirrors `scripts/hooks/worktree_tiers.py`,
 * the Python list the rest of the harness reads, and a devkit test holds the two equal.
 *
 * ## The problem
 *
 * `server.port` was the bare constant 5173 with no `strictPort`, so a second
 * `npm run dev` on this machine did not fail — it slid to the next free port and said
 * so in one line of startup output. What an agent then has is a server on 5175 (5173
 * and 5174 being whatever else was up) and a habit of opening 5173, which answers
 * perfectly with *another checkout's build*. Nothing about that page says it is the
 * wrong one, so the rounds spent screenshotting it are rounds spent drawing
 * conclusions about code that was never edited.
 *
 * `strictPort: true` turns that into a startup error, and this module is what stops
 * the startup error from being the new normal: each worktree derives its own port, so
 * two dev servers coexist with nothing configured.
 *
 * ## Why derived, when this project already has a port registry
 *
 * devkit's `ports.toml` is the machine-wide registry, and it is not being bypassed
 * here — it covers a different server. What it allocates is `FRONTEND_HOST_PORT`, the
 * *host* side of `docker-compose.yml`'s `${FRONTEND_HOST_PORT:-5173}:5173`, for a
 * checkout that publishes a Docker stack. The dev server this file is about is the
 * host-Vite one: `npm --prefix frontend run dev`, the branch-preview path the config's
 * proxy comments describe, which publishes nothing and holds no lease.
 *
 * That path cannot be given a lease at cut time, because a worktree gets cut four
 * ways here and devkit's code runs in one of them:
 *
 * | how | who cuts it | devkit code runs |
 * | --- | --- | --- |
 * | `claude --worktree <topic>` | Claude Code | no |
 * | `codex --worktree` | Codex | no |
 * | devkit's `worktree.py` box (the VS Code task) | devkit | yes |
 * | Claude Remote Control with `"spawn": "worktree"` | Claude Code | no |
 *
 * So nothing done when a worktree is cut — seeding `.env`, taking a lease — reaches
 * three of the four. What all four share is where they land, and the program that
 * needs to know the port is this one, which runs identically in every one of them.
 *
 * ## Two shapes, not one
 *
 * Claude's and the box tier's worktrees sit *under* something that names them — the
 * checkout, or the workspace — with the marker directories immediately above the
 * leaf. Codex's do not: `~/.codex/worktrees/<repo-digest>/<name>` puts an opaque
 * digest between the marker and the leaf, so a marker is matched at a *depth*, and the
 * digest level itself is never mistaken for a worktree. The same distinction is what
 * `worktree_tiers.py` calls a nested tier against a detached one.
 *
 * ## The container is slot 0, and that is load-bearing
 *
 * The compose `frontend` service bind-mounts `./frontend:/app` and runs Vite there, so
 * the config sees `/app` — a path carrying no worktree segment, whatever tree it came
 * from. `portOffset` therefore returns 0 inside every container, the container keeps
 * listening on 5173, and `${FRONTEND_HOST_PORT:-5173}:5173` keeps mapping onto
 * something. If this ever derived from the branch or from an env var instead of from
 * the directory, every compose stack would publish a port with nothing behind it —
 * see the test that pins it.
 *
 * ## What it gives up
 *
 * A hash, not an allocator: two worktrees can collide, and so can a derived port and a
 * `FRONTEND_HOST_PORT` some other stack legitimately leased — both live in the same
 * 5173..5188 span by design (see `SLOT_SPAN`). That is what `strictPort: true` is for.
 * A collision is an immediate, legible startup error and `VITE_PORT=5186 npm run dev`
 * steps around it; the alternative is a second registry with a lock and a reaper,
 * which is machinery this path exists to avoid.
 *
 * An ordinary checkout always returns 0, so `localhost:5173` keeps meaning what it has
 * always meant — every bookmark, `scripts/run-e2e.py`, `scripts/run-ci.py` and
 * `CORS_ORIGINS` default included — and a given worktree keeps its port across
 * restarts, so a bookmarked `localhost:5179` stays valid for as long as that worktree.
 */

/**
 * Directories a worktree is cut into, as `[marker, depth]`: the directory names that
 * sit above the worktree, and how many levels above the leaf the marker starts. Depth
 * 1 is the marker immediately above; depth 2 leaves one opaque level between them.
 *
 * Mirrors `TIERS` in devkit's `scripts/hooks/worktree_tiers.py` plus the box tier;
 * `tests/test_worktree_port.py` there fails if the two lists disagree. One deliberate
 * narrowing: the Python side anchors Codex's tier on `$CODEX_HOME`, while this file —
 * which never asks the environment — spells that home's default directory name. A
 * machine that has moved `CODEX_HOME` gets offset 0 there and a `strictPort` refusal,
 * which is loud; `VITE_PORT=<n> npm run dev` is the way past it.
 */
const WORKTREE_DIRS: [string[], number][] = [
  [['.claude', 'worktrees'], 1],
  [['.worktrees'], 1],
  [['.codex', 'worktrees'], 2],
]

/**
 * How many distinct offsets exist, including 0. Matches `registry.max_slots` in
 * devkit's `ports.toml`, which is also the minimum spacing that file enforces between
 * any two service bases. Sharing the number keeps a derived dev port inside the
 * frontend base's reserved span (5173..5188) rather than wandering into the next
 * service's — the property `ports.toml`'s `validate()` exists to guarantee, and one a
 * second, wider span here would quietly break.
 */
export const SLOT_SPAN = 16

/**
 * The worktree name in `dir`, or '' when `dir` is an ordinary checkout.
 *
 * Matched on the path segments above the leaf rather than on a substring, so a
 * checkout that merely *contains* the words — `~/code/worktrees-talk/demo` — is not
 * mistaken for one, and a repository legitimately named `.worktrees` is not either.
 */
export function worktreeName(dir: string): string {
  const parts = dir.split(sep).filter(Boolean)
  return WORKTREE_DIRS.some(
    ([marker, depth]) =>
      parts.length >= marker.length + depth &&
      marker.every(
        (segment, i) => parts[parts.length - depth - marker.length + i] === segment,
      ),
  )
    ? basename(dir)
    : ''
}

/**
 * A stable non-zero number for `name`, in `1..span-1`.
 *
 * FNV-1a, written out rather than imported: this file is loaded by the Vite config, so
 * it has to stay dependency-free, and the hash only needs to spread short directory
 * names evenly. `>>> 0` after each step keeps it in unsigned 32-bit space, which is
 * what makes the result identical on every platform — the property a bookmarked port
 * depends on.
 */
export function slotFor(name: string, span: number = SLOT_SPAN): number {
  let hash = 0x811c9dc5
  for (let i = 0; i < name.length; i += 1) {
    hash = ((hash ^ name.charCodeAt(i)) >>> 0) * 0x01000193
    hash >>>= 0
  }
  return 1 + (hash % (span - 1))
}

/**
 * The offset to add to every conventional base for the checkout at `dir`.
 *
 * 0 for an ordinary checkout and for the container, so the familiar port keeps
 * working; a stable `1..span-1` for a worktree. Applied to the dev and preview bases
 * together, so one worktree's pair moves as a pair and stays easy to reason about.
 */
export function portOffset(dir: string, span: number = SLOT_SPAN): number {
  const name = worktreeName(dir)
  return name === '' ? 0 : slotFor(name, span)
}
