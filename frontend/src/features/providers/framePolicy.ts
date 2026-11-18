// 每模型抽帧策略（frame sampling）的纯逻辑层：表单草稿 <-> extra_config_json.frame_sampling
// 序列化，以及帧数 / token 预览。
//
// 后端契约见 src/services/frame_sampling/policy.py（键、取值范围、预设）：
//   preset / target_context_ratio / max_edge / probe_fps / min_frames / max_frames /
//   jpeg_quality / dedupe_phash_distance / window_pre_seconds / window_post_seconds /
//   max_event_windows / max_output_tokens / enabled
//
// 预设（baseline / dedupe / event）只提供默认值包，显式填写的键优先（与后端一致）。
// 序列化沿用仓库既有 parseInt 纪律：解析 -> 校验 -> 非法 / 超范围值直接省略（不写出）；
// dedupe_phash_distance 空串写 null（表示不去重）。未知键（unknown）原样保留，保证往返。

export type FramePreset = 'baseline' | 'dedupe' | 'event'

/** frame_sampling 子配置中的数值键（preset / enabled 单独处理）。 */
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
  preset: FramePreset
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

export const FRAME_PRESETS: Record<FramePreset, Partial<Record<FramePolicyNumericKey, string>>> = {
  // 与后端 src/services/frame_sampling/policy.py 的 PRESETS 一致：
  baseline: {},
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

const FRAME_POLICY_KEY_SET: ReadonlySet<string> = new Set<string>([
  'preset',
  'enabled',
  ...FRAME_POLICY_NUMERIC_KEYS,
])

/** 全新草稿：enabled=false 表示「尚未启用图像输入」（不写 frame_sampling 键）。 */
export function emptyFramePolicyDraft(): FramePolicyDraft {
  return {
    enabled: false,
    preset: 'event',
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

/**
 * 将已存储的 frame_sampling 子配置转换为表单草稿。
 *
 * - enabled：严格 bool 才采纳；非 bool（后端按视频模式处理）显示为未启用。
 * - preset：未知 / 缺失回落到后端默认 'event'。
 * - 未知键收入 draft.unknown，序列化时原样写回（往返保留）。
 */
export function draftFromExtraConfig(extra: Record<string, unknown>): FramePolicyDraft {
  const draft = emptyFramePolicyDraft()
  const preset = extra.preset
  if (preset === 'baseline' || preset === 'dedupe' || preset === 'event') {
    draft.preset = preset
  }
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
 * 应用预设：设置 preset 字段，并只填充当前为空的预设默认键（显式填写的值永不被覆盖）。
 */
export function applyPreset(draft: FramePolicyDraft, preset: FramePreset): FramePolicyDraft {
  const next: FramePolicyDraft = { ...draft, preset, unknown: { ...draft.unknown } }
  for (const [key, value] of Object.entries(FRAME_PRESETS[preset])) {
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
    draft.preset === 'event' &&
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
 * - unknown 键原样写回。
 */
export function buildFrameSamplingExtraConfig(
  draft: FramePolicyDraft,
): Record<string, unknown> | null {
  if (isFramePolicyEmpty(draft)) {
    return null
  }
  const out: Record<string, unknown> = {
    preset: draft.preset,
    enabled: draft.enabled,
  }

  const ratio = inFloatRange(draft.target_context_ratio, 0.5, 1.0)
  if (ratio !== null) out.target_context_ratio = ratio
  const maxEdge = inIntRange(draft.max_edge, 320, 1920)
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

// ---------------------------------------------------------------------------
// 帧数 / token 预览
//
// 公式与后端完全一致（镜像，勿改动数值语义）：
//   src/services/frame_sampling/tokens.py      fit_long_edge（长边缩到 max_edge，各边向下
//                                             对齐 32，不放大）+ estimate_image_tokens
//                                             （max(1, (w//32)*(h//32))）
//   src/services/frame_sampling/budget.py      usable = int(context * ratio)；
//                                             budget = usable - prompt - output；
//                                             frames = clamp(budget // tok, min, max)
//                                             再 min(ceil(duration * probe_fps))；
//                                             预算装不下 min_frames 时不可行
//   src/services/llm_provider_tester.py        代表性片段 60s / 640x480 / prompt 估算 800
//   src/services/provider_generation_limits.py 输出预算解析链：frame_sampling.max_output_tokens
//                                             -> 顶层 max_output_tokens -> 场景默认 4096，
//                                             且 clamp 到 max_model_len
// ---------------------------------------------------------------------------

const PROMPT_TOKENS_EST = 800
const IMAGE_OUTPUT_TOKENS_SCENE_DEFAULT = 4096

// 后端 FramePolicy 内置默认值（src/services/frame_sampling/policy.py _BUILTIN）。
const BUILTIN_TARGET_CONTEXT_RATIO = 0.9
const BUILTIN_MAX_EDGE = 640
const BUILTIN_PROBE_FPS = 2.0
const BUILTIN_MIN_FRAMES = 4
const BUILTIN_MAX_FRAMES = 120

export type FramePreviewNote = 'no_max_model_len' | 'plan_impossible'

export type FramePreview = {
  frames: number
  tokensPerImage: number
  imageTokens: number
  /** 本次预览使用的输出预算（token）。 */
  outputTokens: number
  /** 与后端 FramePlan.clamp_reason 同义；不可行时为 null。 */
  clampReason: 'budget' | 'max' | 'duration' | null
  note: FramePreviewNote | null
}

type PreviewPolicy = {
  targetContextRatio: number
  maxEdge: number
  probeFps: number
  minFrames: number
  maxFrames: number
  maxOutputTokens: number | null
}

/** 与后端 pick 相同：显式值（合法）> 预设默认 > 内置默认。 */
function resolvePreviewPolicy(draft: FramePolicyDraft): PreviewPolicy {
  const preset = FRAME_PRESETS[draft.preset]
  const fromPreset = (key: FramePolicyNumericKey): number | null => {
    const raw = preset[key]
    if (raw === undefined) {
      return null
    }
    const value = Number(raw)
    return Number.isFinite(value) ? value : null
  }

  const targetContextRatio =
    inFloatRange(draft.target_context_ratio, 0.5, 1.0) ?? BUILTIN_TARGET_CONTEXT_RATIO
  const maxEdge =
    inIntRange(draft.max_edge, 320, 1920) ?? fromPreset('max_edge') ?? BUILTIN_MAX_EDGE
  const probeFps =
    inFloatRange(draft.probe_fps, 0.25, 10) ?? fromPreset('probe_fps') ?? BUILTIN_PROBE_FPS
  const minFrames =
    inIntRange(draft.min_frames, 1, 60) ?? fromPreset('min_frames') ?? BUILTIN_MIN_FRAMES
  const maxFrames =
    inIntRange(draft.max_frames, 1, 600) ?? fromPreset('max_frames') ?? BUILTIN_MAX_FRAMES

  // 后端 _resolve_frame_bounds：min > max 时把 min 夹到 max。
  const resolvedMin = Math.min(minFrames, maxFrames)

  const maxOutputTokens = parseStrictInt(draft.max_output_tokens)
  return {
    targetContextRatio,
    maxEdge,
    probeFps,
    minFrames: resolvedMin,
    maxFrames,
    maxOutputTokens: maxOutputTokens !== null && maxOutputTokens > 0 ? maxOutputTokens : null,
  }
}

function fitLongEdge(srcW: number, srcH: number, maxEdge: number, align = 32): [number, number] {
  let w = srcW
  let h = srcH
  if (Math.max(srcW, srcH) > maxEdge) {
    if (srcW >= srcH) {
      w = maxEdge
      h = Math.floor((srcH * maxEdge) / srcW)
    } else {
      h = maxEdge
      w = Math.floor((srcW * maxEdge) / srcH)
    }
  }
  const snapDown = (value: number): number => Math.max(align, Math.floor(value / align) * align)
  return [snapDown(w), snapDown(h)]
}

/**
 * 按当前草稿估算一个代表性片段（默认 60s / 640x480）的帧数与 token。
 *
 * - maxModelLen 缺失 / 非正：frames=0，note='no_max_model_len'。
 * - 预算装不下 min_frames：frames=0，note='plan_impossible'（后端会拒绝保存 / 测试）。
 * - topLevelMaxOutputTokens 对应 extra_config_json 顶层 max_output_tokens（
 *   resolve_image_output_tokens 的第二层回退）；缺省时用场景默认 4096。
 */
export function estimatePreview(
  draft: FramePolicyDraft,
  maxModelLen: number | null,
  durationSeconds = 60,
  srcW = 640,
  srcH = 480,
  topLevelMaxOutputTokens: number | null = null,
): FramePreview {
  const policy = resolvePreviewPolicy(draft)
  const [fitW, fitH] = fitLongEdge(srcW, srcH, policy.maxEdge)
  const tokensPerImage = Math.max(1, Math.floor(fitW / 32) * Math.floor(fitH / 32))

  if (maxModelLen === null || !Number.isFinite(maxModelLen) || maxModelLen <= 0) {
    return {
      frames: 0,
      tokensPerImage,
      imageTokens: 0,
      outputTokens: 0,
      clampReason: null,
      note: 'no_max_model_len',
    }
  }

  // 输出预算解析链（resolve_image_output_tokens）。
  let outputTokens: number
  if (policy.maxOutputTokens !== null) {
    outputTokens = policy.maxOutputTokens
  } else if (topLevelMaxOutputTokens !== null) {
    outputTokens = topLevelMaxOutputTokens
  } else {
    outputTokens = IMAGE_OUTPUT_TOKENS_SCENE_DEFAULT
  }
  outputTokens = Math.min(outputTokens, maxModelLen)

  const usable = Math.floor(maxModelLen * policy.targetContextRatio)
  const budget = usable - PROMPT_TOKENS_EST - outputTokens
  if (budget < policy.minFrames * tokensPerImage) {
    return {
      frames: 0,
      tokensPerImage,
      imageTokens: 0,
      outputTokens,
      clampReason: null,
      note: 'plan_impossible',
    }
  }

  const byBudget = Math.floor(budget / tokensPerImage)
  let frames = Math.max(policy.minFrames, Math.min(policy.maxFrames, byBudget))
  const durationCeiling = Math.max(1, Math.ceil(durationSeconds * policy.probeFps))
  if (frames > durationCeiling) {
    frames = durationCeiling
  }

  let clampReason: 'budget' | 'max' | 'duration'
  if (byBudget > policy.maxFrames) {
    clampReason = 'max'
  } else if (durationCeiling < byBudget) {
    clampReason = 'duration'
  } else {
    clampReason = 'budget'
  }

  return {
    frames,
    tokensPerImage,
    imageTokens: frames * tokensPerImage,
    outputTokens,
    clampReason,
    note: null,
  }
}
