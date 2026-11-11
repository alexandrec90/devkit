import { join } from 'node:path'
import { app, BrowserWindow, ipcMain, shell } from 'electron'
import { VERSION_CHANNEL } from '../shared/updates'
import { watchForUpdates } from './updates'

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 900,
    height: 600,
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      // CommonJS, which is why `package.json` has no `"type": "module"`: a sandboxed
      // preload cannot be an ES module.
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  window.once('ready-to-show', () => window.show())

  // The page only ever shows this app. A link goes to the user's browser, and only an
  // https one: `openExternal` hands its argument to the OS, which runs other schemes.
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) void shell.openExternal(url)
    return { action: 'deny' }
  })
  window.webContents.on('will-navigate', (event, url) => {
    if (url !== window.webContents.getURL()) event.preventDefault()
  })

  const devServer = process.env.ELECTRON_RENDERER_URL
  if (devServer && !app.isPackaged) void window.loadURL(devServer)
  else void window.loadFile(join(__dirname, '../renderer/index.html'))
  return window
}

void app.whenReady().then(() => {
  ipcMain.handle(VERSION_CHANNEL, () => app.getVersion())
  watchForUpdates(createWindow())
})

app.on('window-all-closed', () => app.quit())
