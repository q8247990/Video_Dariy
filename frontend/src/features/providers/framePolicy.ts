// 每模型抽帧策略（frame sampling）的纯逻辑层：表单草稿 <-> extra_config_json.frame_sampling
// 序列化。
//
// 后端契约见 src/services/frame_sampling/policy.py（键、取值范围、模式默认值包）：
//   mode / target_context_ratio / max_edge / probe_fps / min_frames / max_frames /
//   jpeg_quality / dedupe_phash_distance / window_pre_seconds / window_post_seconds /
//   max_event_windows / max_output_tokens / enabled
//
// 模式（uniform / dedupe / event）只提供默认值包，显式填写的键优先（与后端一致）。
// 旧配置使用 `preset` 键（baseline / dedupe / event）：读取时映射到对应模式
// （baseline→uniform）以便展示；序列化时只写 `mode` 键，旧 `preset` 键不再回写。
// 帧数 / token 预览不再在前端估算（公式以后端为准）：预览只显示「测试连接」
// 返回的实测 frame_plan（见 ProviderForm）。
//
// 序列化沿用仓库既有 parseInt 纪律：解析 -> 校验 -> 非法 / 超范围值直接省略（不写出）；
// dedupe_phash_distance 空串写 null（表示不去重）。未知键（unknown）原样保留，保证往返。

export type FrameMode = 'uniform' | 'dedupe' | 'event'

/** 旧 `preset` 键取值到 `mode` 的展示映射（baseline 即均匀抽帧参考基线）。 */
const LEGACY_PRESET_TO_MODE: Record<string, FrameMode> = {
  baseline: 'uniform',
  dedupe: 'dedupe',
  event: 'event',
}

/** frame_sampling 子配置中的数值键（mode / enabled 单独处理）。 */
export type FramePolicyNumericKey =
  | 'target_context_ratio'
  | 'max_edge'
  | 'probe_fps'
  | 'min_frames'
  | 'max_frames'
  | 'jpeg_quality'
  | 'dedupe_phash_distance'
  | 'window_pre_seconds'
  | 'window_post_seconds'
  | 'max_event_windows'
  | 'max_output_tokens'

/** 表单草稿：输入框一律存字符串，enabled 存 boolean，未知键存 unknown。 */
export type FramePolicyDraft = {
  enabled: boolean
  mode: FrameMode
  target_context_ratio: string
  max_edge: string
  probe_fps: string
  min_frames: string
  max_frames: string
  jpeg_quality: string
  dedupe_phash_distance: string
  window_pre_seconds: string
  window_post_seconds: string
  max_event_windows: string
  max_output_tokens: string
  unknown: Record<string, unknown>
}

export const FRAME_MODE_DEFAULTS: Record<FrameMode, Partial<Record<FramePolicyNumericKey, string>>> = {
  // 与后端 src/services/frame_sampling/policy.py 的 PRESETS 一致：
  uniform: {},
  dedupe: { dedupe_phash_distance: '6', probe_fps: '1.0', max_frames: '60' },
  event: {},
}

const FRAME_POLICY_NUMERIC_KEYS: readonly FramePolicyNumericKey[] = [
  'target_context_ratio',
  'max_edge',
  'probe_fps',
  'min_frames',
  'max_frames',
  'jpeg_quality',
  'dedupe_phash_distance',
  'window_pre_seconds',
  'window_post_seconds',
  'max_event_windows',
  'max_output_tokens',
]

// `preset` 是旧键：读取时映射到 mode，序列化时丢弃（不写回）。
const FRAME_POLICY_KEY_SET: ReadonlySet<string> = new Set<string>([
  'mode',
  'preset',
  'enabled',
  ...FRAME_POLICY_NUMERIC_KEYS,
])

/** 全新草稿：enabled=false 表示「尚未启用图像输入」（不写 frame_sampling 键）。 */
export function emptyFramePolicyDraft(): FramePolicyDraft {
  return {
    enabled: false,
    mode: 'event',
    target_context_ratio: '',
    max_edge: '',
    probe_fps: '',
    min_frames: '',
    max_frames: '',
    jpeg_quality: '',
    dedupe_phash_distance: '',
    window_pre_seconds: '',
    window_post_seconds: '',
    max_event_windows: '',
    max_output_tokens: '',
    unknown: {},
  }
}

function readDraftValue(value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (typeof value === 'string') return value.trim()
  return ''
}

/** 解析已存储配置的 mode：优先 `mode` 键，回退旧 `preset` 键映射，均缺失 / 未知回落到 'event'。 */
function readMode(extra: Record<string, unknown>): FrameMode {
  const mode = extra.mode
  if (mode === 'uniform' || mode === 'dedupe' || mode === 'event') {
    return mode
  }
  const preset = extra.preset
  if (typeof preset === 'string') {
    return LEGACY_PRESET_TO_MODE[preset] ?? 'event'
  }
  return 'event'
}

/**
 * 将已存储的 frame_sampling 子配置转换为表单草稿。
 *
 * - enabled：严格 bool 才采纳；非 bool（后端按视频模式处理）显示为未启用。
 * - mode：`mode` 优先；旧 `preset` 键按映射展示（baseline→uniform）；未知 / 缺失回落 'event'。
 * - 未知键收入 draft.unknown，序列化时原样写回（往返保留）。
 */
export function draftFromExtraConfig(extra: Record<string, unknown>): FramePolicyDraft {
  const draft = emptyFramePolicyDraft()
  draft.mode = readMode(extra)
  const enabled = extra.enabled
  draft.enabled = typeof enabled === 'boolean' ? enabled : false
  for (const key of FRAME_POLICY_NUMERIC_KEYS) {
    draft[key] = readDraftValue(extra[key])
  }
  const unknown: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(extra)) {
    if (!FRAME_POLICY_KEY_SET.has(key)) {
      unknown[key] = value
    }
  }
  draft.unknown = unknown
  return draft
}

/**
 * 应用模式：设置 mode 字段，并只填充当前为空的模式默认键（显式填写的值永不被覆盖）。
 */
export function applyMode(draft: FramePolicyDraft, mode: FrameMode): FramePolicyDraft {
  const next: FramePolicyDraft = { ...draft, mode, unknown: { ...draft.unknown } }
  for (const [key, value] of Object.entries(FRAME_MODE_DEFAULTS[mode])) {
    const field = key as FramePolicyNumericKey
    if (next[field] === '') {
      next[field] = value
    }
  }
  return next
}

/** 草稿是否「未启用且未填写任何内容」——此时不写 frame_sampling 键（保持视频模式）。 */
export function isFramePolicyEmpty(draft: FramePolicyDraft): boolean {
  return (
    draft.enabled === false &&
    draft.mode === 'event' &&
    FRAME_POLICY_NUMERIC_KEYS.every((key) => draft[key].trim() === '') &&
    Object.keys(draft.unknown).length === 0
  )
}

function parseStrictInt(text: string): number | null {
  const trimmed = text.trim()
  if (!/^[+-]?\d+$/.test(trimmed)) {
    return null
  }
  return Number(trimmed)
}

function parseFiniteFloat(text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === '') {
    return null
  }
  const value = Number(trimmed)
  return Number.isFinite(value) ? value : null
}

function inIntRange(text: string, low: number, high: number): number | null {
  const parsed = parseStrictInt(text)
  if (parsed === null) {
    return null
  }
  return parsed >= low && parsed <= high ? parsed : null
}

function inFloatRange(text: string, low: number, high: number): number | null {
  const parsed = parseFiniteFloat(text)
  if (parsed === null) {
    return null
  }
  return parsed >= low && parsed <= high ? parsed : null
}

/**
 * 序列化 frame_sampling 子配置。
 *
 * - 未启用且未填写任何内容的草稿返回 null（ProviderForm 据此删除 / 不写该键）。
 * - 非法 / 超范围值省略（不写出）；dedupe_phash_distance 空串写 null。
 * - 模式写 `mode` 键；旧 `preset` 键不写回。
 * - unknown 键原样写回。
 */
export function buildFrameSamplingExtraConfig(
  draft: FramePolicyDraft,
): Record<string, unknown> | null {
  if (isFramePolicyEmpty(draft)) {
    return null
  }
  const out: Record<string, unknown> = {
    mode: draft.mode,
    enabled: draft.enabled,
  }

  const ratio = inFloatRange(draft.target_context_ratio, 0.5, 1.0)
  if (ratio !== null) out.target_context_ratio = ratio
  const maxEdge = inIntRange(draft.max_edge, 320, 2560)
  if (maxEdge !== null) out.max_edge = maxEdge
  const probeFps = inFloatRange(draft.probe_fps, 0.25, 10)
  if (probeFps !== null) out.probe_fps = probeFps
  const minFrames = inIntRange(draft.min_frames, 1, 60)
  if (minFrames !== null) out.min_frames = minFrames
  const maxFrames = inIntRange(draft.max_frames, 1, 600)
  if (maxFrames !== null) out.max_frames = maxFrames
  const jpegQuality = inIntRange(draft.jpeg_quality, 60, 95)
  if (jpegQuality !== null) out.jpeg_quality = jpegQuality

  const phashText = draft.dedupe_phash_distance.trim()
  if (phashText === '') {
    out.dedupe_phash_distance = null
  } else {
    const phash = inIntRange(draft.dedupe_phash_distance, 1, 32)
    if (phash !== null) out.dedupe_phash_distance = phash
  }

  const windowPre = inFloatRange(draft.window_pre_seconds, 0, 60)
  if (windowPre !== null) out.window_pre_seconds = windowPre
  const windowPost = inFloatRange(draft.window_post_seconds, 0, 120)
  if (windowPost !== null) out.window_post_seconds = windowPost
  const maxEventWindows = inIntRange(draft.max_event_windows, 1, 10)
  if (maxEventWindows !== null) out.max_event_windows = maxEventWindows
  const maxOutputTokens = parseStrictInt(draft.max_output_tokens)
  if (maxOutputTokens !== null && maxOutputTokens > 0) out.max_output_tokens = maxOutputTokens

  for (const [key, value] of Object.entries(draft.unknown)) {
    out[key] = value
  }
  return out
}
