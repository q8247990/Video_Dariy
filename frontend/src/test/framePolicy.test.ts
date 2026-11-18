import { describe, expect, it } from 'vitest'

import {
  applyPreset,
  buildFrameSamplingExtraConfig,
  draftFromExtraConfig,
  emptyFramePolicyDraft,
  estimatePreview,
  isFramePolicyEmpty,
} from '../features/providers/framePolicy'

describe('framePolicy 预设填充', () => {
  it('dedupe 预设只填充其提供的默认键', () => {
    const draft = applyPreset(emptyFramePolicyDraft(), 'dedupe')
    expect(draft.preset).toBe('dedupe')
    expect(draft.dedupe_phash_distance).toBe('6')
    expect(draft.probe_fps).toBe('1.0')
    expect(draft.max_frames).toBe('60')
    expect(draft.min_frames).toBe('')
    expect(draft.max_edge).toBe('')
    expect(draft.target_context_ratio).toBe('')
  })

  it('baseline / event 预设不填充任何默认键', () => {
    for (const preset of ['baseline', 'event'] as const) {
      const draft = applyPreset(emptyFramePolicyDraft(), preset)
      expect(draft.preset).toBe(preset)
      expect(draft.max_frames).toBe('')
      expect(draft.probe_fps).toBe('')
      expect(draft.dedupe_phash_distance).toBe('')
    }
  })

  it('显式填写的值优先于预设默认值', () => {
    const draft = applyPreset(
      { ...emptyFramePolicyDraft(), max_frames: '100', probe_fps: '0.5' },
      'dedupe',
    )
    expect(draft.max_frames).toBe('100')
    expect(draft.probe_fps).toBe('0.5')
    expect(draft.dedupe_phash_distance).toBe('6')
  })

  it('切换预设不会清空已填写的值', () => {
    const draft = applyPreset(applyPreset(emptyFramePolicyDraft(), 'dedupe'), 'baseline')
    expect(draft.dedupe_phash_distance).toBe('6')
    expect(draft.max_frames).toBe('60')
    expect(draft.preset).toBe('baseline')
  })
})

describe('framePolicy 序列化', () => {
  it('未启用的空草稿序列化为 null（不写 frame_sampling 键）', () => {
    expect(buildFrameSamplingExtraConfig(emptyFramePolicyDraft())).toBeNull()
    expect(isFramePolicyEmpty(emptyFramePolicyDraft())).toBe(true)
  })

  it('非法 / 超范围值被省略，合法值保留，未知键不丢失', () => {
    const draft = draftFromExtraConfig({
      preset: 'dedupe',
      enabled: true,
      max_edge: 'abc',
      min_frames: 8,
      probe_fps: '99',
      max_frames: 0,
      custom_key: 'keep-me',
    })
    const out = buildFrameSamplingExtraConfig(draft)
    expect(out).not.toBeNull()
    expect(out).toMatchObject({ preset: 'dedupe', enabled: true, min_frames: 8 })
    expect(out).not.toHaveProperty('max_edge')
    expect(out).not.toHaveProperty('probe_fps')
    expect(out).not.toHaveProperty('max_frames')
    expect(out?.custom_key).toBe('keep-me')
  })

  it('未知键在 draft -> config -> draft 往返中保留', () => {
    const original = draftFromExtraConfig({
      enabled: true,
      max_edge: 640,
      future_key: { a: 1 },
      other_key: 'x',
    })
    const config = buildFrameSamplingExtraConfig(original)
    expect(config).not.toBeNull()
    const again = draftFromExtraConfig(config as Record<string, unknown>)
    expect(again.unknown).toEqual({ future_key: { a: 1 }, other_key: 'x' })
    expect(again.max_edge).toBe('640')
    expect(again.enabled).toBe(true)
  })

  it('dedupe 预设的 dedupe_phash_distance 序列化为数字，空串序列化为 null', () => {
    const draft = { ...applyPreset(emptyFramePolicyDraft(), 'dedupe'), enabled: true }
    expect(buildFrameSamplingExtraConfig(draft)?.dedupe_phash_distance).toBe(6)
    const blanked = { ...draft, dedupe_phash_distance: '' }
    expect(buildFrameSamplingExtraConfig(blanked)?.dedupe_phash_distance).toBeNull()
  })

  it('operator 禁用区块时 enabled 序列化为 false', () => {
    const draft = { ...emptyFramePolicyDraft(), enabled: true, max_frames: '60' }
    expect(buildFrameSamplingExtraConfig(draft)?.enabled).toBe(true)
    expect(buildFrameSamplingExtraConfig({ ...draft, enabled: false })?.enabled).toBe(false)
  })

  it('frame_sampling.max_output_tokens 输出预算解析链：显式值 > 顶层值 > 默认 4096', () => {
    const base = { ...emptyFramePolicyDraft(), enabled: true, max_edge: '640' }
    // 未配置：场景默认 4096（8192 上下文下为 8 帧）
    expect(estimatePreview(base, 8192).outputTokens).toBe(4096)
    // 顶层 max_output_tokens 生效
    expect(estimatePreview(base, 8192, 60, 640, 480, 2048).outputTokens).toBe(2048)
    // 显式 frame_sampling 值优先于顶层值
    const explicit = { ...base, max_output_tokens: '1024' }
    expect(estimatePreview(explicit, 8192, 60, 640, 480, 2048).outputTokens).toBe(1024)
  })
})

describe('estimatePreview（公式镜像 budget.py + tokens.py）', () => {
  const baseDraft = () => ({ ...emptyFramePolicyDraft(), enabled: true, max_edge: '640' })

  it('640x480 @ max_edge 640：单帧 300 tokens', () => {
    const preview = estimatePreview(baseDraft(), 65536)
    expect(preview.tokensPerImage).toBe(300)
  })

  it('max_model_len 65536 -> 120 帧（受最多帧数 / 时长上限夹紧）', () => {
    const preview = estimatePreview(baseDraft(), 65536)
    expect(preview.frames).toBe(120)
    expect(preview.clampReason).toBe('max')
    expect(preview.imageTokens).toBe(120 * 300)
    expect(preview.note).toBeNull()
  })

  it('max_model_len 32768 -> 81 帧', () => {
    expect(estimatePreview(baseDraft(), 32768).frames).toBe(81)
  })

  it('max_model_len 8192 -> 8 帧', () => {
    expect(estimatePreview(baseDraft(), 8192).frames).toBe(8)
  })

  it('max_model_len 8192 且 max_output_tokens 8192 -> 0 帧 + 不可行提示', () => {
    const draft = { ...baseDraft(), max_output_tokens: '8192' }
    const preview = estimatePreview(draft, 8192)
    expect(preview.frames).toBe(0)
    expect(preview.note).toBe('plan_impossible')
  })

  it('max_model_len 缺失 -> 0 帧 + 提示', () => {
    const preview = estimatePreview(baseDraft(), null)
    expect(preview.frames).toBe(0)
    expect(preview.note).toBe('no_max_model_len')
  })

  it('probe_fps 参与时长上限：60s @ 1fps -> 最多 60 帧', () => {
    const draft = { ...baseDraft(), probe_fps: '1', max_frames: '600' }
    const preview = estimatePreview(draft, 65536)
    expect(preview.frames).toBe(60)
    expect(preview.clampReason).toBe('duration')
  })

  it('dedupe 预设默认值参与预览（max_frames 60 / probe_fps 1.0）', () => {
    const draft = applyPreset(
      { ...emptyFramePolicyDraft(), enabled: true, max_edge: '640' },
      'dedupe',
    )
    const preview = estimatePreview(draft, 65536)
    expect(preview.frames).toBe(60)
    expect(preview.clampReason).toBe('max')
  })
})
