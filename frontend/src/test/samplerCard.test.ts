import { describe, expect, it } from 'vitest'

import enUS from '../locales/en-US.json'
import zhCN from '../locales/zh-CN.json'
import {
  SAMPLER_DEFAULTS,
  SAMPLER_ERROR_I18N_KEYS,
  SAMPLER_I18N_KEYS,
  toDraft,
  toUpdate,
  type FrameSamplerConfig,
  type SamplerDraft,
} from '../features/providers/samplerForm'

function baseDraft(overrides: Partial<SamplerDraft> = {}): SamplerDraft {
  return { ...toDraft({}), ...overrides }
}

describe('samplerForm.toDraft', () => {
  it('uses backend registry defaults for missing keys', () => {
    const draft = toDraft({})
    expect(draft.frame_sampler_enabled).toBe(false)
    expect(draft.frame_sampler_url).toBe('')
    expect(draft.frame_sampler_timeout_seconds).toBe('120')
    expect(draft.frame_sampler_queue_limit).toBe('8')
    expect(draft.frame_sampling_session_frame_limit).toBe('0')
  })

  it('maps stored config values into draft strings', () => {
    const config: FrameSamplerConfig = {
      frame_sampler_enabled: true,
      frame_sampler_url: 'http://10.0.0.5:18080',
      frame_sampler_timeout_seconds: 90,
      frame_sampler_queue_limit: 4,
      frame_sampling_session_frame_limit: 64,
    }
    const draft = toDraft(config)
    expect(draft.frame_sampler_enabled).toBe(true)
    expect(draft.frame_sampler_url).toBe('http://10.0.0.5:18080')
    expect(draft.frame_sampler_timeout_seconds).toBe('90')
    expect(draft.frame_sampler_queue_limit).toBe('4')
    expect(draft.frame_sampling_session_frame_limit).toBe('64')
  })

  it('falls back to defaults for non-number values', () => {
    const draft = toDraft({
      frame_sampler_timeout_seconds: Number.NaN,
      frame_sampler_queue_limit: Number.POSITIVE_INFINITY,
    })
    expect(draft.frame_sampler_timeout_seconds).toBe(String(SAMPLER_DEFAULTS.frame_sampler_timeout_seconds))
    expect(draft.frame_sampler_queue_limit).toBe(String(SAMPLER_DEFAULTS.frame_sampler_queue_limit))
  })
})

describe('samplerForm.toUpdate', () => {
  it('accepts a valid draft and returns a typed payload', () => {
    const result = toUpdate(
      baseDraft({
        frame_sampler_enabled: true,
        frame_sampler_url: ' http://10.0.0.5:18080 ',
      }),
    )
    expect(result.ok).toBe(true)
    expect(result.errors).toEqual({})
    expect(result.payload.frame_sampler_enabled).toBe(true)
    expect(result.payload.frame_sampler_url).toBe('http://10.0.0.5:18080')
    expect(result.payload.frame_sampler_timeout_seconds).toBe(120)
    expect(result.payload.frame_sampler_queue_limit).toBe(8)
    expect(result.payload.frame_sampling_session_frame_limit).toBe(0)
  })

  it('allows blank url (registry _optional_string)', () => {
    const result = toUpdate(baseDraft({ frame_sampler_url: '' }))
    expect(result.ok).toBe(true)
    expect(result.payload.frame_sampler_url).toBe('')
  })

  it('rejects non-numeric and non-finite input as not_a_number', () => {
    const result = toUpdate(baseDraft({ frame_sampler_timeout_seconds: 'abc' }))
    expect(result.ok).toBe(false)
    expect(result.errors.frame_sampler_timeout_seconds).toBe('not_a_number')

    const empty = toUpdate(baseDraft({ frame_sampler_queue_limit: '' }))
    expect(empty.errors.frame_sampler_queue_limit).toBe('not_a_number')

    const inf = toUpdate(baseDraft({ frame_sampler_queue_limit: 'Infinity' }))
    expect(inf.errors.frame_sampler_queue_limit).toBe('not_a_number')
  })

  it('rejects fractional values on integer fields as not_an_integer', () => {
    const result = toUpdate(baseDraft({ frame_sampler_queue_limit: '2.5' }))
    expect(result.errors.frame_sampler_queue_limit).toBe('not_an_integer')
  })

  it('enforces positive ints for timeout and queue limit', () => {
    const zero = toUpdate(baseDraft({ frame_sampler_timeout_seconds: '0' }))
    expect(zero.errors.frame_sampler_timeout_seconds).toBe('positive_int')

    const negative = toUpdate(baseDraft({ frame_sampler_queue_limit: '-1' }))
    expect(negative.errors.frame_sampler_queue_limit).toBe('positive_int')
  })

  it('allows 0 but rejects negatives on session frame limit', () => {
    const zero = toUpdate(baseDraft({ frame_sampling_session_frame_limit: '0' }))
    expect(zero.ok).toBe(true)
    expect(zero.payload.frame_sampling_session_frame_limit).toBe(0)

    const negative = toUpdate(baseDraft({ frame_sampling_session_frame_limit: '-1' }))
    expect(negative.errors.frame_sampling_session_frame_limit).toBe('non_negative_int')
  })

  it('collects multiple errors at once', () => {
    const result = toUpdate(
      baseDraft({
        frame_sampler_timeout_seconds: '0',
        frame_sampler_queue_limit: '-1',
      }),
    )
    expect(result.ok).toBe(false)
    expect(Object.keys(result.errors).sort()).toEqual([
      'frame_sampler_queue_limit',
      'frame_sampler_timeout_seconds',
    ])
  })
})

describe('sampler i18n keys（zh-CN / en-US）', () => {
  function lookup(tree: unknown, path: string): unknown {
    return path.split('.').reduce<unknown>((node, part) => {
      if (node !== null && typeof node === 'object') {
        return (node as Record<string, unknown>)[part]
      }
      return undefined
    }, tree)
  }

  it('component keys exist as non-empty strings in both locales', () => {
    for (const tree of [zhCN, enUS]) {
      for (const key of SAMPLER_I18N_KEYS) {
        const value = lookup(tree, key)
        expect(typeof value, `${key} 应存在于语言目录`).toBe('string')
        expect((value as string).length, `${key} 不应为空`).toBeGreaterThan(0)
      }
    }
  })

  it('every error code maps to a key that exists in both locales', () => {
    for (const key of Object.values(SAMPLER_ERROR_I18N_KEYS)) {
      for (const tree of [zhCN, enUS]) {
        const value = lookup(tree, key)
        expect(typeof value, `${key} 应存在于语言目录`).toBe('string')
        expect((value as string).length, `${key} 不应为空`).toBeGreaterThan(0)
      }
    }
  })
})
