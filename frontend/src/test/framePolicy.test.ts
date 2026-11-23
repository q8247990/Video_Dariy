import { describe, expect, it } from 'vitest'

import {
  applyMode,
  buildFrameSamplingExtraConfig,
  draftFromExtraConfig,
  emptyFramePolicyDraft,
  isFramePolicyEmpty,
} from '../features/providers/framePolicy'
import type { FramePolicyDraft } from '../features/providers/framePolicy'

describe('framePolicy 模式默认值填充', () => {
  it('dedupe 模式只填充其提供的默认键', () => {
    const draft = applyMode(emptyFramePolicyDraft(), 'dedupe')
    expect(draft.mode).toBe('dedupe')
    expect(draft.dedupe_phash_distance).toBe('6')
    expect(draft.probe_fps).toBe('1.0')
    expect(draft.max_frames).toBe('60')
    expect(draft.min_frames).toBe('')
    expect(draft.max_edge).toBe('')
    expect(draft.target_context_ratio).toBe('')
  })

  it('keyframe 模式只填充其提供的默认键', () => {
    const draft = applyMode(emptyFramePolicyDraft(), 'keyframe')
    expect(draft.mode).toBe('keyframe')
    expect(draft.probe_fps).toBe('2.0')
    expect(draft.max_frames).toBe('48')
    expect(draft.dedupe_phash_distance).toBe('6')
    expect(draft.min_frames).toBe('')
    expect(draft.max_edge).toBe('')
    expect(draft.target_context_ratio).toBe('')
  })

  it('uniform / event 模式不填充任何默认键', () => {
    for (const mode of ['uniform', 'event'] as const) {
      const draft = applyMode(emptyFramePolicyDraft(), mode)
      expect(draft.mode).toBe(mode)
      expect(draft.max_frames).toBe('')
      expect(draft.probe_fps).toBe('')
      expect(draft.dedupe_phash_distance).toBe('')
    }
  })

  it('显式填写的值优先于模式默认值', () => {
    const draft = applyMode(
      { ...emptyFramePolicyDraft(), max_frames: '100', probe_fps: '0.5' },
      'dedupe',
    )
    expect(draft.max_frames).toBe('100')
    expect(draft.probe_fps).toBe('0.5')
    expect(draft.dedupe_phash_distance).toBe('6')
  })

  it('切换模式不会清空已填写的值', () => {
    const draft = applyMode(applyMode(emptyFramePolicyDraft(), 'dedupe'), 'uniform')
    expect(draft.dedupe_phash_distance).toBe('6')
    expect(draft.max_frames).toBe('60')
    expect(draft.mode).toBe('uniform')
  })
})

describe('framePolicy 旧 preset 键映射', () => {
  it('旧 preset 按映射显示为 mode：baseline→uniform、dedupe→dedupe、event→event', () => {
    expect(draftFromExtraConfig({ preset: 'baseline' }).mode).toBe('uniform')
    expect(draftFromExtraConfig({ preset: 'dedupe' }).mode).toBe('dedupe')
    expect(draftFromExtraConfig({ preset: 'event' }).mode).toBe('event')
  })

  it('mode 键优先于旧 preset 键', () => {
    const draft = draftFromExtraConfig({ mode: 'uniform', preset: 'dedupe' })
    expect(draft.mode).toBe('uniform')
  })

  it('已知 mode 白名单（uniform / dedupe / event / keyframe）读取与序列化往返不降级', () => {
    for (const mode of ['uniform', 'dedupe', 'event', 'keyframe'] as const) {
      const draft = draftFromExtraConfig({ mode, enabled: true })
      expect(draft.mode).toBe(mode)
      expect(buildFrameSamplingExtraConfig(draft)?.mode).toBe(mode)
    }
  })

  it('mode / preset 均缺失或未知时回落到 event', () => {
    expect(draftFromExtraConfig({}).mode).toBe('event')
    expect(draftFromExtraConfig({ mode: 'bogus' }).mode).toBe('event')
    expect(draftFromExtraConfig({ preset: 'bogus' }).mode).toBe('event')
  })

  it('旧 preset 键不被写回（序列化只写 mode）', () => {
    const draft = draftFromExtraConfig({ preset: 'baseline', enabled: true })
    const out = buildFrameSamplingExtraConfig(draft)
    expect(out).not.toBeNull()
    expect(out?.mode).toBe('uniform')
    expect(out).not.toHaveProperty('preset')
  })
})

describe('framePolicy 序列化', () => {
  it('未启用的空草稿序列化为 null（不写 frame_sampling 键）', () => {
    expect(buildFrameSamplingExtraConfig(emptyFramePolicyDraft())).toBeNull()
    expect(isFramePolicyEmpty(emptyFramePolicyDraft())).toBe(true)
    expect(emptyFramePolicyDraft().hasEnabledKey).toBe(false)
  })

  it('启用且仅改模式时只写 mode（缺省 enabled 即图像模式）', () => {
    const draft: FramePolicyDraft = { ...emptyFramePolicyDraft(), enabled: true, mode: 'uniform' }
    const out = buildFrameSamplingExtraConfig(draft)
    expect(out).toMatchObject({ mode: 'uniform' })
    expect(out).not.toHaveProperty('enabled')
  })

  it('非法 / 超范围值被省略，合法值保留，未知键不丢失', () => {
    const draft = draftFromExtraConfig({
      mode: 'dedupe',
      enabled: true,
      max_edge: 'abc',
      min_frames: 8,
      probe_fps: '99',
      max_frames: 0,
      custom_key: 'keep-me',
    })
    const out = buildFrameSamplingExtraConfig(draft)
    expect(out).not.toBeNull()
    expect(out).toMatchObject({ mode: 'dedupe', enabled: true, min_frames: 8 })
    expect(out).not.toHaveProperty('max_edge')
    expect(out).not.toHaveProperty('probe_fps')
    expect(out).not.toHaveProperty('max_frames')
    expect(out?.custom_key).toBe('keep-me')
  })

  describe('max_edge 范围 320–2560', () => {
    it('2560 / 320 边界值原样写出', () => {
      expect(buildFrameSamplingExtraConfig({ ...emptyFramePolicyDraft(), enabled: true, max_edge: '2560' })?.max_edge).toBe(2560)
      expect(buildFrameSamplingExtraConfig({ ...emptyFramePolicyDraft(), enabled: true, max_edge: '320' })?.max_edge).toBe(320)
    })

    it('超出 320–2560 的值被省略（不写出）', () => {
      expect(buildFrameSamplingExtraConfig({ ...emptyFramePolicyDraft(), enabled: true, max_edge: '2561' })).not.toHaveProperty('max_edge')
      expect(buildFrameSamplingExtraConfig({ ...emptyFramePolicyDraft(), enabled: true, max_edge: '319' })).not.toHaveProperty('max_edge')
      expect(buildFrameSamplingExtraConfig({ ...emptyFramePolicyDraft(), enabled: true, max_edge: '4000' })).not.toHaveProperty('max_edge')
    })
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

  it('dedupe 模式的 dedupe_phash_distance 序列化为数字，空串序列化为 null', () => {
    const draft = { ...applyMode(emptyFramePolicyDraft(), 'dedupe'), enabled: true }
    expect(buildFrameSamplingExtraConfig(draft)?.dedupe_phash_distance).toBe(6)
    const blanked = { ...draft, dedupe_phash_distance: '' }
    expect(buildFrameSamplingExtraConfig(blanked)?.dedupe_phash_distance).toBeNull()
  })

  it('禁用时 enabled 序列化为 false，原配置显式写过 true 时保留 true', () => {
    const draft = { ...emptyFramePolicyDraft(), enabled: true, max_frames: '60' }
    expect(buildFrameSamplingExtraConfig(draft)).not.toHaveProperty('enabled')
    expect(buildFrameSamplingExtraConfig({ ...draft, enabled: false })?.enabled).toBe(false)
    const explicitTrue = draftFromExtraConfig({ enabled: true, max_frames: 60 })
    expect(buildFrameSamplingExtraConfig(explicitTrue)?.enabled).toBe(true)
  })
})

describe('framePolicy enabled 与后端 resolve_input_mode 对齐', () => {
  it('缺省 enabled 的已存配置显示为启用（后端视为图像模式）', () => {
    expect(draftFromExtraConfig({}).enabled).toBe(true)
    const draft = draftFromExtraConfig({ mode: 'uniform', min_frames: 3, max_frames: 8 })
    expect(draft.enabled).toBe(true)
    expect(draft.hasEnabledKey).toBe(false)
  })

  it('显式 enabled:false 显示为未启用且往返保留 false', () => {
    const withPolicy = draftFromExtraConfig({ mode: 'uniform', enabled: false, max_frames: 8 })
    expect(withPolicy.enabled).toBe(false)
    expect(buildFrameSamplingExtraConfig(withPolicy)?.enabled).toBe(false)

    const bare = draftFromExtraConfig({ enabled: false })
    expect(bare.enabled).toBe(false)
    expect(buildFrameSamplingExtraConfig(bare)?.enabled).toBe(false)
  })

  it('非 bool 的 enabled（后端按视频模式处理）显示为未启用', () => {
    expect(draftFromExtraConfig({ enabled: 'yes' }).enabled).toBe(false)
    expect(draftFromExtraConfig({ enabled: null }).enabled).toBe(false)
  })

  it('未触碰开关保存缺省 enabled 配置：不写 enabled 键，图像模式保持不变', () => {
    const original = { mode: 'uniform', min_frames: 3, max_frames: 8 }
    const out = buildFrameSamplingExtraConfig(draftFromExtraConfig(original))
    expect(out).not.toBeNull()
    expect(out).not.toHaveProperty('enabled')
    expect(out).toMatchObject(original)
    // frame_sampling 仍存在且 enabled 缺失 => 后端 resolve_input_mode 仍为 image
    expect(draftFromExtraConfig(out as Record<string, unknown>).enabled).toBe(true)
  })
})
