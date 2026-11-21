/** What the updater last reported, as main hands it to the page. */
export type UpdateState =
  | { kind: 'idle' }
  | { kind: 'checking' }
  | { kind: 'current' }
  | { kind: 'downloading'; percent: number }
  | { kind: 'ready'; version: string }
  | { kind: 'failed' }

export const UPDATE_CHANNEL = 'updates:state'
export const RESTART_CHANNEL = 'updates:restart'
export const VERSION_CHANNEL = 'app:version'

/** The sentence the window shows. A failed check says nothing alarming: the app works. */
export function describeUpdate(state: UpdateState): string {
  switch (state.kind) {
    case 'idle':
      return ''
    case 'checking':
      return 'Checking for updates'
    case 'current':
      return 'Up to date'
    case 'downloading':
      return `Downloading update (${Math.round(state.percent)}%)`
    case 'ready':
      return `Version ${state.version} is ready`
    case 'failed':
      return 'Could not check for updates'
  }
}
