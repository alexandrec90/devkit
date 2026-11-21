import { defineConfig } from '@playwright/test'

// One worker: every test launches its own copy of the app. The JSON report is the failure
// artifact: an agent reads `logs/e2e-results.json`, never the terminal.
//
// Which app is named by project, never inferred from what is on disk: a `release/` left
// by an earlier run would otherwise be tested in place of the code just changed.
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  workers: 1,
  outputDir: '../logs/e2e-artifacts',
  reporter: [['line'], ['json', { outputFile: '../logs/e2e-results.json' }]],
  use: { trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [
    { name: 'source', metadata: { packaged: false } },
    { name: 'packaged', metadata: { packaged: true } },
  ],
})
