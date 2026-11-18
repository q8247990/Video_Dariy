import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { Provider, ProviderCreate, ProviderUpdate } from '../../types/api'
import { testProvider } from './api'
import type { TestProviderResult } from './api'
import {
  applyMode,
  buildFrameSamplingExtraConfig,
  draftFromExtraConfig,
  emptyFramePolicyDraft,
  isFramePolicyEmpty,
} from './framePolicy'
import type { FrameMode, FramePolicyDraft } from './framePolicy'

type ProviderFormProps = {
  initialValue?: Provider
  pending?: boolean
  onCancel: () => void
  onSubmit: (payload: ProviderCreate | ProviderUpdate) => void
}

type FormState = {
  provider_name: string
  api_base_url: string
  api_key: string
  model_name: string
  timeout_seconds: number
  retry_count: number
  max_model_len: string
  max_output_tokens: string
  num_frames: string
  frame_policy: FramePolicyDraft
  enabled: boolean
  supports_vision: boolean
  supports_qa: boolean
  supports_tool_calling: boolean
}

function readExtraString(value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (typeof value === 'string' && value.trim()) return value.trim()
  return ''
}

function buildExtraConfigJson(
  form: FormState,
  base?: Record<string, unknown> | null,
): Record<string, unknown> {
  const extra: Record<string, unknown> = { ...(base ?? {}) }
  const entries: [string, string][] = [
    ['max_model_len', form.max_model_len],
    ['max_output_tokens', form.max_output_tokens],
    ['num_frames', form.num_frames],
  ]
  for (const [key, raw] of entries) {
    const parsed = Number.parseInt(raw, 10)
    if (Number.isFinite(parsed) && parsed > 0) {
      extra[key] = parsed
    } else {
      delete extra[key]
    }
  }
  const frameSampling = buildFrameSamplingExtraConfig(form.frame_policy)
  if (frameSampling === null) {
    delete extra.frame_sampling
  } else {
    extra.frame_sampling = frameSampling
  }
  return extra
}

function getInitialState(initialValue?: Provider): FormState {
  const extra: Record<string, unknown> = initialValue?.extra_config_json ?? {}
  const rawFrameSampling: unknown = extra.frame_sampling
  return {
    provider_name: initialValue?.provider_name ?? '',
    api_base_url: initialValue?.api_base_url ?? '',
    api_key: '',
    model_name: initialValue?.model_name ?? '',
    timeout_seconds: initialValue?.timeout_seconds ?? 60,
    retry_count: initialValue?.retry_count ?? 3,
    max_model_len: readExtraString(extra.max_model_len),
    max_output_tokens: readExtraString(extra.max_output_tokens),
    num_frames: readExtraString(extra.num_frames) || '120',
    frame_policy:
      typeof rawFrameSampling === 'object' && rawFrameSampling !== null
        ? draftFromExtraConfig(rawFrameSampling as Record<string, unknown>)
        : emptyFramePolicyDraft(),
    enabled: initialValue?.enabled ?? true,
    supports_vision: initialValue?.supports_vision ?? false,
    supports_qa: initialValue?.supports_qa ?? true,
    supports_tool_calling: initialValue?.supports_tool_calling ?? true,
  }
}

const CLAMP_REASON_I18N_KEYS: Record<string, string> = {
  budget: 'providers.frame_policy_clamp_budget',
  max: 'providers.frame_policy_clamp_max',
  duration: 'providers.frame_policy_clamp_duration',
}

type PolicyNumberFieldProps = {
  label: string
  hint: string
  value: string
  min?: number
  max?: number
  step?: number
  onChange: (value: string) => void
}

function PolicyNumberField({
  label,
  hint,
  value,
  min,
  max,
  step,
  onChange,
}: PolicyNumberFieldProps) {
  return (
    <label>
      {label}
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <small className="text-muted">{hint}</small>
    </label>
  )
}

export function ProviderForm({ initialValue, pending, onCancel, onSubmit }: ProviderFormProps) {
  const { t } = useTranslation()
  const [form, setForm] = useState<FormState>(() => getInitialState(initialValue))
  const [framePolicyOpen] = useState(() => !isFramePolicyEmpty(form.frame_policy))
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestProviderResult | null>(null)
  const [testError, setTestError] = useState<string | null>(null)
  const submitLabel = useMemo(
    () => (initialValue ? t('providers.submit_save_edit') : t('providers.submit_create')),
    [initialValue, t],
  )
  const capabilityError = !form.supports_vision && !form.supports_qa
  const apiKeyRequiredError = !initialValue && !form.api_key.trim()

  const framePolicyActive = !isFramePolicyEmpty(form.frame_policy)
  // 帧数 / token 预览只显示「测试连接」返回的实测 frame_plan（公式以后端为准，
  // 前端不再本地估算）；无测试结果时展示引导提示。
  const framePlan =
    testResult?.frame_plan &&
    Number.isFinite(testResult.frame_plan.frames) &&
    testResult.frame_plan.frames > 0
      ? testResult.frame_plan
      : null
  const framePlanClampKey = framePlan ? CLAMP_REASON_I18N_KEYS[framePlan.clamp_reason] : undefined
  const framePlanClampReason = framePlanClampKey ? t(framePlanClampKey) : null

  const handleTest = async () => {
    if (!initialValue) return
    setTesting(true)
    setTestResult(null)
    setTestError(null)
    try {
      const result = await testProvider(initialValue.id)
      setTestResult(result)
      if (result.success) {
        setForm((old) => ({
          ...old,
          supports_vision: result.supports_vision,
          supports_tool_calling: result.supports_tool_calling,
        }))
      }
    } catch (e: unknown) {
      setTestError(e instanceof Error ? e.message : t('providers.test_request_failed'))
    } finally {
      setTesting(false)
    }
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (initialValue) {
      const payload: ProviderUpdate = {
        provider_name: form.provider_name,
        api_base_url: form.api_base_url,
        model_name: form.model_name,
        timeout_seconds: form.timeout_seconds,
        retry_count: form.retry_count,
        extra_config_json: buildExtraConfigJson(form, initialValue?.extra_config_json),
        enabled: form.enabled,
        supports_vision: form.supports_vision,
        supports_qa: form.supports_qa,
        supports_tool_calling: form.supports_tool_calling,
      }
      if (form.api_key.trim()) {
        payload.api_key = form.api_key.trim()
      }
      onSubmit(payload)
      return
    }

    const payload: ProviderCreate = {
      provider_name: form.provider_name,
      api_base_url: form.api_base_url,
      api_key: form.api_key.trim(),
      model_name: form.model_name,
      timeout_seconds: form.timeout_seconds,
      retry_count: form.retry_count,
      extra_config_json: buildExtraConfigJson(form),
      enabled: form.enabled,
      supports_vision: form.supports_vision,
      supports_qa: form.supports_qa,
      supports_tool_calling: form.supports_tool_calling,
      is_default_vision: false,
      is_default_qa: false,
    }
    onSubmit(payload)
  }

  return (
    <form className="dialog-form" onSubmit={handleSubmit}>
      <label>
        {t('providers.form_capability_label')}
        <div className="capability-buttons">
          <button
            type="button"
            className={
              form.supports_vision ? 'capability-btn capability-btn-active' : 'capability-btn'
            }
            onClick={() =>
              setForm((old) => ({
                ...old,
                supports_vision: !old.supports_vision,
              }))
            }
          >
            {t('providers.form_capability_vision')}
          </button>
          <button
            type="button"
            className={form.supports_qa ? 'capability-btn capability-btn-active' : 'capability-btn'}
            onClick={() =>
              setForm((old) => ({
                ...old,
                supports_qa: !old.supports_qa,
              }))
            }
          >
            {t('providers.form_capability_qa')}
          </button>
          <button
            type="button"
            className={
              form.supports_tool_calling ? 'capability-btn capability-btn-active' : 'capability-btn'
            }
            onClick={() =>
              setForm((old) => ({
                ...old,
                supports_tool_calling: !old.supports_tool_calling,
              }))
            }
          >
            {t('providers.form_capability_tool_calling')}
          </button>
        </div>
      </label>
      {capabilityError ? (
        <div className="api-error">{t('providers.capability_required')}</div>
      ) : null}

      <label>
        {t('providers.provider_name_label')}
        <input
          required
          value={form.provider_name}
          onChange={(event) => setForm((old) => ({ ...old, provider_name: event.target.value }))}
        />
      </label>

      <label>
        {t('providers.form_api_base_url')}
        <input
          required
          value={form.api_base_url}
          onChange={(event) => setForm((old) => ({ ...old, api_base_url: event.target.value }))}
          placeholder={t('providers.form_api_base_url_placeholder')}
        />
      </label>

      <label>
        {t('providers.form_model_name')}
        <input
          required
          value={form.model_name}
          onChange={(event) => setForm((old) => ({ ...old, model_name: event.target.value }))}
        />
      </label>

      <label>
        {t('providers.form_api_key_label')}
        {initialValue ? ` ${t('providers.form_api_key_keep_blank')}` : ''}
        <input
          type="password"
          required={!initialValue}
          value={form.api_key}
          onChange={(event) => setForm((old) => ({ ...old, api_key: event.target.value }))}
        />
      </label>

      <div className="inline-fields">
        <label>
          {t('providers.form_timeout')}
          <input
            type="number"
            min={1}
            value={form.timeout_seconds}
            onChange={(event) =>
              setForm((old) => ({ ...old, timeout_seconds: Number(event.target.value) || 1 }))
            }
          />
        </label>
        <label>
          {t('providers.form_retry_count')}
          <input
            type="number"
            min={0}
            value={form.retry_count}
            onChange={(event) =>
              setForm((old) => ({ ...old, retry_count: Number(event.target.value) || 0 }))
            }
          />
        </label>
      </div>

      <div className="inline-fields">
        <label>
          {t('providers.form_max_model_len')}
          <input
            type="number"
            min={1}
            value={form.max_model_len}
            onChange={(event) => setForm((old) => ({ ...old, max_model_len: event.target.value }))}
            placeholder={t('providers.form_max_model_len_placeholder')}
          />
        </label>
        <label>
          {t('providers.form_max_output_tokens')}
          <input
            type="number"
            min={1}
            value={form.max_output_tokens}
            onChange={(event) =>
              setForm((old) => ({ ...old, max_output_tokens: event.target.value }))
            }
            placeholder={t('providers.form_max_output_tokens_placeholder')}
          />
        </label>
      </div>

      <label>
        {t('providers.form_num_frames')}
        <input
          type="number"
          min={1}
          max={600}
          value={form.num_frames}
          onChange={(event) => setForm((old) => ({ ...old, num_frames: event.target.value }))}
          placeholder={t('providers.form_num_frames_placeholder')}
        />
      </label>

      <details className="policy-block" open={framePolicyOpen}>
        <summary>
          <strong>{t('providers.frame_policy_title')}</strong>
        </summary>
        <div className="dialog-form">
          <div className="text-muted">{t('providers.frame_policy_explainer')}</div>

          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={form.frame_policy.enabled}
              onChange={(event) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, enabled: event.target.checked },
                }))
              }
            />
            {t('providers.frame_policy_enabled')}
          </label>

          <label>
            {t('providers.frame_policy_mode')}
            <select
              value={form.frame_policy.mode}
              onChange={(event) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: applyMode(old.frame_policy, event.target.value as FrameMode),
                }))
              }
            >
              <option value="uniform">{t('providers.frame_policy_mode_uniform')}</option>
              <option value="dedupe">{t('providers.frame_policy_mode_dedupe')}</option>
              <option value="event">{t('providers.frame_policy_mode_event')}</option>
            </select>
          </label>

          <div className="inline-fields">
            <PolicyNumberField
              label={t('providers.frame_policy_target_context_ratio')}
              hint={t('providers.frame_policy_hint_ratio', { value: 0.9 })}
              value={form.frame_policy.target_context_ratio}
              min={0.5}
              max={1}
              step={0.05}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, target_context_ratio: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_max_edge')}
              hint={t('providers.frame_policy_hint_max_edge', { value: 640 })}
              value={form.frame_policy.max_edge}
              min={320}
              max={1920}
              step={32}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, max_edge: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_probe_fps')}
              hint={t('providers.frame_policy_hint_probe_fps', { value: 2 })}
              value={form.frame_policy.probe_fps}
              min={0.25}
              max={10}
              step={0.25}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, probe_fps: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_min_frames')}
              hint={t('providers.frame_policy_hint_min_frames', { value: 4 })}
              value={form.frame_policy.min_frames}
              min={1}
              max={60}
              step={1}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, min_frames: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_max_frames')}
              hint={t('providers.frame_policy_hint_max_frames', { value: 120 })}
              value={form.frame_policy.max_frames}
              min={1}
              max={600}
              step={1}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, max_frames: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_jpeg_quality')}
              hint={t('providers.frame_policy_hint_jpeg_quality', { value: 85 })}
              value={form.frame_policy.jpeg_quality}
              min={60}
              max={95}
              step={1}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, jpeg_quality: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_dedupe_phash_distance')}
              hint={t('providers.frame_policy_hint_dedupe', { value: 6 })}
              value={form.frame_policy.dedupe_phash_distance}
              min={1}
              max={32}
              step={1}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, dedupe_phash_distance: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_window_pre_seconds')}
              hint={t('providers.frame_policy_hint_window_pre', { value: 5 })}
              value={form.frame_policy.window_pre_seconds}
              min={0}
              max={60}
              step={1}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, window_pre_seconds: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_window_post_seconds')}
              hint={t('providers.frame_policy_hint_window_post', { value: 10 })}
              value={form.frame_policy.window_post_seconds}
              min={0}
              max={120}
              step={1}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, window_post_seconds: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_max_event_windows')}
              hint={t('providers.frame_policy_hint_max_event_windows', { value: 4 })}
              value={form.frame_policy.max_event_windows}
              min={1}
              max={10}
              step={1}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, max_event_windows: value },
                }))
              }
            />
            <PolicyNumberField
              label={t('providers.frame_policy_max_output_tokens')}
              hint={t('providers.frame_policy_hint_max_output_tokens', { value: 4096 })}
              value={form.frame_policy.max_output_tokens}
              min={1}
              step={1}
              onChange={(value) =>
                setForm((old) => ({
                  ...old,
                  frame_policy: { ...old.frame_policy, max_output_tokens: value },
                }))
              }
            />
          </div>

          {framePolicyActive ? (
            <div>
              <strong>{t('providers.frame_policy_preview_title')}</strong>
              {framePlan ? (
                <>
                  <div className="text-muted">
                    {t('providers.frame_policy_preview', {
                      frames: framePlan.frames,
                      tokens: framePlan.image_tokens,
                      output: framePlan.max_output_tokens,
                    })}
                    {framePlanClampReason ? ` — ${framePlanClampReason}` : ''}
                  </div>
                  <div className="text-muted">{t('providers.frame_policy_test_plan_note')}</div>
                </>
              ) : (
                <div className="text-muted">{t('providers.frame_policy_preview_need_test')}</div>
              )}
            </div>
          ) : null}
        </div>
      </details>

      <label className="checkbox-field">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(event) => setForm((old) => ({ ...old, enabled: event.target.checked }))}
        />
        {t('providers.form_enabled_label')}
      </label>

      {initialValue ? (
        <div className="test-section">
          <button type="button" className="ghost" disabled={testing} onClick={handleTest}>
            {testing ? t('providers.testing') : t('providers.test_capability')}
          </button>
          {testResult ? (
            <div
              className={
                testResult.success
                  ? 'test-result test-result-success'
                  : 'test-result test-result-fail'
              }
            >
              <span>
                {testResult.success
                  ? t('providers.test_connectivity_ok')
                  : t('providers.test_connectivity_failed')}
              </span>
              {testResult.success ? (
                <>
                  <span>
                    {testResult.supports_vision
                      ? t('providers.test_vision_ok')
                      : t('providers.test_vision_unsupported')}
                  </span>
                  <span>
                    {testResult.supports_tool_calling
                      ? t('providers.test_tool_calling_ok')
                      : t('providers.test_tool_calling_unsupported')}
                  </span>
                </>
              ) : null}
              <span className="test-result-message">{testResult.message}</span>
            </div>
          ) : null}
          {testError ? <div className="api-error">{testError}</div> : null}
        </div>
      ) : null}

      <div className="dialog-actions">
        <button type="button" className="ghost" onClick={onCancel}>
          {t('common.cancel')}
        </button>
        <button type="submit" disabled={pending || capabilityError || apiKeyRequiredError}>
          {pending ? t('providers.submit_pending') : submitLabel}
        </button>
      </div>
    </form>
  )
}
