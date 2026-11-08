import { describe, expect, it } from 'vitest'
import { describeUpdate } from './updates'

describe('describeUpdate', () => {
  it('says nothing before a check starts', () => {
    expect(describeUpdate({ kind: 'idle' })).toBe('')
  })

  it('rounds the download to a whole percent', () => {
    expect(describeUpdate({ kind: 'downloading', percent: 41.6 })).toBe('Downloading update (42%)')
  })

  it('names the version a restart will apply', () => {
    expect(describeUpdate({ kind: 'ready', version: '1.2.3' })).toBe('Version 1.2.3 is ready')
  })

  it('reports a failed check without treating it as an app failure', () => {
    expect(describeUpdate({ kind: 'failed' })).toBe('Could not check for updates')
  })
})
