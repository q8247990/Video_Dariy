// 帧采样器（frame sampler）设置的纯逻辑层：表单草稿 <-> 校验后的更新载荷。
//
// 校验规则与后端注册表 src/services/system_config_registry.py 保持一致：
//   frame_sampler_enabled              bool
//   frame_sampler_url                  可选字符串（空串 = 未配置）
//   frame_sampler_timeout_seconds      正整数（默认 120）
//   frame_sampling_session_frame_limit 非负整数（默认 0 = 不限制）
//
// 错误值为稳定的错误码（不是文案），由 SamplerCard 映射到 i18n 词条，
// 便于在无 i18n 环境下做单元测试。

export const SAMPLER_DEFAULTS = {
  frame_sampler_enabled: false,
  frame_sampler_url: '',
  frame_sampler_timeout_seconds: 120,
  frame_sampling_session_frame_limit: 0,
} as const

export type FrameSamplerConfig = {
  frame_sampler_enabled?: boolean
  frame_sampler_url?: string
  frame_sampler_timeout_seconds?: number
  frame_sampling_session_frame_limit?: number
}

/** 表单草稿：输入框一律存字符串，布尔开关存 boolean。 */
export type SamplerDraft = {
  frame_sampler_enabled: boolean
  frame_sampler_url: string
  frame_sampler_timeout_seconds: string
  frame_sampling_session_frame_limit: string
}

export type SamplerFieldKey = keyof SamplerDraft

/** 与后端解析器对应的稳定错误码。 */
export type SamplerErrorCode =
  | 'not_a_number'
  | 'not_an_integer'
  | 'positive_int'
  | 'non_negative_int'

export type SamplerUpdateResult = {
  ok: boolean
  payload: Record<SamplerFieldKey, string | number | boolean>
  errors: Partial<Record<SamplerFieldKey, SamplerErrorCode>>
}

/** SamplerCard 用到的全部 i18n 键（供测试校验 zh-CN / en-US 键完整性）。 */
export const SAMPLER_I18N_KEYS = [
  'providers.sampler_title',
  'providers.sampler_help',
  'providers.sampler_loading',
  'providers.sampler_enabled',
  'providers.sampler_url',
  'providers.sampler_url_hint',
  'providers.sampler_timeout',
  'providers.sampler_session_frame_limit',
  'providers.sampler_int_positive_hint',
  'providers.sampler_int_non_negative_hint',
  'providers.sampler_disabled_warning',
  'providers.sampler_impact_title',
  'providers.sampler_impact_defaults',
  'providers.sampler_impact_no_cap',
  'providers.sampler_save_button',
  'providers.sampler_save_success',
  'providers.sampler_error_not_a_number',
  'providers.sampler_error_not_an_integer',
  'providers.sampler_error_positive_int',
  'providers.sampler_error_non_negative_int',
] as const

export const SAMPLER_ERROR_I18N_KEYS: Record<SamplerErrorCode, string> = {
  not_a_number: 'providers.sampler_error_not_a_number',
  not_an_integer: 'providers.sampler_error_not_an_integer',
  positive_int: 'providers.sampler_error_positive_int',
  non_negative_int: 'providers.sampler_error_non_negative_int',
}

type Parsed = { ok: true; value: number } | { ok: false; code: SamplerErrorCode }

function toFiniteNumber(text: string): Parsed {
  const trimmed = text.trim()
  if (trimmed === '') {
    return { ok: false, code: 'not_a_number' }
  }
  const value = Number(trimmed)
  if (!Number.isFinite(value)) {
    return { ok: false, code: 'not_a_number' }
  }
  return { ok: true, value }
}

/** 整数校验：min = 1 对应正整数（_positive_int），min = 0 对应非负整数（_non_negative_int）。 */
function toInteger(text: string, min: 0 | 1): Parsed {
  const parsed = toFiniteNumber(text)
  if (!parsed.ok) {
    return parsed
  }
  if (!Number.isInteger(parsed.value)) {
    return { ok: false, code: 'not_an_integer' }
  }
  if (parsed.value < min) {
    return { ok: false, code: min === 1 ? 'positive_int' : 'non_negative_int' }
  }
  return { ok: true, value: parsed.value }
}

function numberToString(value: unknown, fallback: number): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : String(fallback)
}

function stringOrEmpty(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** 将后端返回的系统配置（缺失键按注册表默认值）转换为表单草稿。 */
export function toDraft(config: FrameSamplerConfig): SamplerDraft {
  return {
    frame_sampler_enabled: Boolean(config.frame_sampler_enabled),
    frame_sampler_url: stringOrEmpty(config.frame_sampler_url),
    frame_sampler_timeout_seconds: numberToString(
      config.frame_sampler_timeout_seconds,
      SAMPLER_DEFAULTS.frame_sampler_timeout_seconds,
    ),
    frame_sampling_session_frame_limit: numberToString(
      config.frame_sampling_session_frame_limit,
      SAMPLER_DEFAULTS.frame_sampling_session_frame_limit,
    ),
  }
}

/**
 * 校验草稿并生成 PUT /api/v1/system-config 的载荷。
 * 非法字段只出现在 errors 中，payload 不应在 ok=false 时发送。
 */
export function toUpdate(draft: SamplerDraft): SamplerUpdateResult {
  const errors: Partial<Record<SamplerFieldKey, SamplerErrorCode>> = {}
  // 非法字段保留原始字符串：ok=false 时 payload 不应发送，见函数说明。
  const payload: Record<SamplerFieldKey, string | number | boolean> = {
    frame_sampler_enabled: draft.frame_sampler_enabled,
    frame_sampler_url: draft.frame_sampler_url.trim(),
    frame_sampler_timeout_seconds: draft.frame_sampler_timeout_seconds,
    frame_sampling_session_frame_limit: draft.frame_sampling_session_frame_limit,
  }

  const timeout = toInteger(draft.frame_sampler_timeout_seconds, 1)
  if (timeout.ok) {
    payload.frame_sampler_timeout_seconds = timeout.value
  } else {
    errors.frame_sampler_timeout_seconds = timeout.code
  }

  const sessionLimit = toInteger(draft.frame_sampling_session_frame_limit, 0)
  if (sessionLimit.ok) {
    payload.frame_sampling_session_frame_limit = sessionLimit.value
  } else {
    errors.frame_sampling_session_frame_limit = sessionLimit.code
  }

  return { ok: Object.keys(errors).length === 0, payload, errors }
}
