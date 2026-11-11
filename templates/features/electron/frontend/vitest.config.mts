import { defineConfig } from 'vitest/config'

// Logic only. `e2e/` is Playwright's: it drives the real app, and vitest cannot run it.
export default defineConfig({
  test: { include: ['src/**/*.test.ts', 'src/**/*.test.tsx'] },
})
