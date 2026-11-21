import { mkdirSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { _electron as electron, expect, test, type ElectronApplication, type Page } from '@playwright/test'

const FRONTEND = join(__dirname, '..')
const SCREENSHOT = join(FRONTEND, '..', 'logs', 'ui', 'main-window.png')
const UNPACKED = join(FRONTEND, 'release', 'win-unpacked')

/** The app `electron-builder --dir` packed. Throws when it is not there, rather than
 * falling back to the source and reporting on a build nobody ships. */
function packagedExe(): string {
  const exe = readdirSync(UNPACKED).find((name) => name.endsWith('.exe'))
  if (!exe) throw new Error(`no .exe in ${UNPACKED}: run npm run test:e2e:packaged`)
  return join(UNPACKED, exe)
}

let app: ElectronApplication
let page: Page
const problems: string[] = []

test.beforeEach(async ({}, testInfo) => {
  problems.length = 0
  const packaged = testInfo.project.metadata.packaged === true
  app = await electron.launch(packaged ? { executablePath: packagedExe() } : { args: [FRONTEND] })
  page = await app.firstWindow()
  page.on('pageerror', (error) => problems.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') problems.push(message.text())
  })
  await page.waitForLoadState('domcontentloaded')
})

test.afterEach(async () => {
  await app.close()
})

test('the window opens, draws the app and logs no error', async () => {
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  await expect(page.getByTestId('version')).toContainText(/\d+\.\d+\.\d+/)
  // The picture an agent looks at after a UI change; the PR gate uploads it.
  mkdirSync(dirname(SCREENSHOT), { recursive: true })
  await page.screenshot({ path: SCREENSHOT })
  expect(problems).toEqual([])
})

test('the page cannot reach Node', async () => {
  const reachable = await page.evaluate(() => ({
    require: 'require' in globalThis,
    process: 'process' in globalThis,
  }))
  expect(reachable).toEqual({ require: false, process: false })
})

test('a page cannot open a window of its own', async () => {
  // `about:blank`, not an https URL: the handler sends those to the user's real browser.
  // `window.open` answers null only when the handler denied it, which counting windows
  // cannot show: the count is still one until a new window finishes opening.
  const opened = await page.evaluate(() => window.open('about:blank') !== null)
  expect(opened).toBe(false)
})
