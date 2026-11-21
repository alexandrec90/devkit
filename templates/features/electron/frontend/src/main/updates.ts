import { app, ipcMain, type BrowserWindow } from 'electron'
import { autoUpdater } from 'electron-updater'
import { RESTART_CHANNEL, UPDATE_CHANNEL, type UpdateState } from '../shared/updates'

/**
 * The only place the app talks to `electron-updater`: check on launch, download quietly,
 * restart only when the user asks. An unpacked run has no feed to read, so it checks
 * nothing.
 */
export function watchForUpdates(window: BrowserWindow): void {
  ipcMain.handle(RESTART_CHANNEL, () => autoUpdater.quitAndInstall())
  if (!app.isPackaged) return

  const send = (state: UpdateState): void => {
    if (!window.isDestroyed()) window.webContents.send(UPDATE_CHANNEL, state)
  }
  autoUpdater.on('checking-for-update', () => send({ kind: 'checking' }))
  autoUpdater.on('update-not-available', () => send({ kind: 'current' }))
  autoUpdater.on('download-progress', ({ percent }) => send({ kind: 'downloading', percent }))
  autoUpdater.on('update-downloaded', ({ version }) => send({ kind: 'ready', version }))
  autoUpdater.on('error', () => send({ kind: 'failed' }))
  // The `error` event above is what reports a failure; a rejection here would repeat it.
  autoUpdater.checkForUpdates().catch(() => undefined)
}
