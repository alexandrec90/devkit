import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron'
import { RESTART_CHANNEL, UPDATE_CHANNEL, VERSION_CHANNEL, type UpdateState } from '../shared/updates'

/**
 * Everything the page may ask of the machine. Each entry is one narrow operation: never
 * expose `ipcRenderer` itself, or a generic invoke, which would hand the page all of main.
 */
const bridge = {
  version: (): Promise<string> => ipcRenderer.invoke(VERSION_CHANNEL),
  restartToUpdate: (): Promise<void> => ipcRenderer.invoke(RESTART_CHANNEL),
  onUpdate: (listener: (state: UpdateState) => void): (() => void) => {
    const forward = (_event: IpcRendererEvent, state: UpdateState): void => listener(state)
    ipcRenderer.on(UPDATE_CHANNEL, forward)
    return () => ipcRenderer.removeListener(UPDATE_CHANNEL, forward)
  },
}

export type Bridge = typeof bridge

contextBridge.exposeInMainWorld('app', bridge)
