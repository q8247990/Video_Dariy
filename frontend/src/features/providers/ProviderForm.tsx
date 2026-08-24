import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { Provider, ProviderCreate, ProviderUpdate } from '../../types/api'
import { testProvider } from './api'
import type { TestProviderResult } from './api'

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
  enabled: boolean
  supports_vision: boolean
  supports_qa: boolean
  supports_tool_calling: boolean
  video_preprocess_mode: 'keyframe' | 'raw_mp4'
  video_keyframe_target_n: number
  video_keyframe_jpeg_quality: number
}

function getInitialState(initialValue?: Provider): FormState {
  return {
    provider_name: initialValue?.provider_name ?? '',
    api_base_url: initialValue?.api_base_url ?? '',
    api_key: '',
    model_name: initialValue?.model_name ?? '',
    timeout_seconds: initialValue?.timeout_seconds ?? 60,
    retry_count: initialValue?.retry_count ?? 3,
    enabled: initialValue?.enabled ?? true,
    supports_vision: initialValue?.supports_vision ?? false,
    supports_qa: initialValue?.supports_qa ?? true,
    supports_tool_calling: initialValue?.supports_tool_calling ?? false,
    video_preprocess_mode:
      initialValue?.video_preprocess_mode === 'raw_mp4' ? 'raw_mp4' : 'keyframe',
    video_keyframe_target_n: initialValue?.video_keyframe_target_n ?? 64,
    video_keyframe_jpeg_quality: initialValue?.video_keyframe_jpeg_quality ?? 88,
  }
}

export function ProviderForm({
  initialValue,
  pending,
  onCancel,
  onSubmit,
}: ProviderFormProps) {
  const { t } = useTranslation()
  const [form, setForm] = useState<FormState>(() => getInitialState(initialValue))
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestProviderResult | null>(null)
  const [testError, setTestError] = useState<string | null>(null)
  const submitLabel = useMemo(
    () => (initialValue ? t('providers.submit_save_edit') : t('providers.submit_create')),
    [initialValue, t],
  )
  const capabilityError = !form.supports_vision && !form.supports_qa

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
        enabled: form.enabled,
        supports_vision: form.supports_vision,
        supports_qa: form.supports_qa,
        supports_tool_calling: form.supports_tool_calling,
        video_preprocess_mode: form.video_preprocess_mode,
        video_keyframe_target_n: form.video_keyframe_target_n,
        video_keyframe_jpeg_quality: form.video_keyframe_jpeg_quality,
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
      extra_config_json: {},
      enabled: form.enabled,
      supports_vision: form.supports_vision,
      supports_qa: form.supports_qa,
      supports_tool_calling: form.supports_tool_calling,
      is_default_vision: false,
      is_default_qa: false,
      video_preprocess_mode: form.video_preprocess_mode,
      video_keyframe_target_n: form.video_keyframe_target_n,
      video_keyframe_jpeg_quality: form.video_keyframe_jpeg_quality,
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
            className={form.supports_vision ? 'capability-btn capability-btn-active' : 'capability-btn'}
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
            className={form.supports_tool_calling ? 'capability-btn capability-btn-active' : 'capability-btn'}
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
      {capabilityError ? <div className="api-error">{t('providers.capability_required')}</div> : null}

      <fieldset className="video-preprocess-group" disabled={!form.supports_vision}>
        <legend>{t('providers.video_preprocess_group')}</legend>
        {!form.supports_vision ? (
          <div className="field-hint">{t('providers.video_preprocess_vision_required')}</div>
        ) : null}
        <label>
          {t('providers.video_preprocess_mode_label')}
          <div className="capability-buttons">
            <button
              type="button"
              className={
                form.video_preprocess_mode === 'keyframe'
                  ? 'capability-btn capability-btn-active'
                  : 'capability-btn'
              }
              onClick={() =>
                setForm((old) => ({ ...old, video_preprocess_mode: 'keyframe' }))
              }
            >
              {t('providers.video_preprocess_mode_keyframe')}
            </button>
            <button
              type="button"
              className={
                form.video_preprocess_mode === 'raw_mp4'
                  ? 'capability-btn capability-btn-active'
                  : 'capability-btn'
              }
              onClick={() =>
                setForm((old) => ({ ...old, video_preprocess_mode: 'raw_mp4' }))
              }
            >
              {t('providers.video_preprocess_mode_raw_mp4')}
            </button>
          </div>
        </label>
        <div className="inline-fields">
          <label>
            {t('providers.video_keyframe_target_n_label')}
            <input
              type="number"
              min={16}
              max={256}
              value={form.video_keyframe_target_n}
              onChange={(event) =>
                setForm((old) => ({
                  ...old,
                  video_keyframe_target_n: Number(event.target.value) || 16,
                }))
              }
            />
          </label>
          <label>
            {t('providers.video_keyframe_jpeg_quality_label')}
            <input
              type="number"
              min={50}
              max={100}
              value={form.video_keyframe_jpeg_quality}
              onChange={(event) =>
                setForm((old) => ({
                  ...old,
                  video_keyframe_jpeg_quality: Number(event.target.value) || 50,
                }))
              }
            />
          </label>
        </div>
      </fieldset>

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
          <button
            type="button"
            className="ghost"
            disabled={testing}
            onClick={handleTest}
          >
            {testing ? t('providers.testing') : t('providers.test_capability')}
          </button>
          {testResult ? (
            <div className={testResult.success ? 'test-result test-result-success' : 'test-result test-result-fail'}>
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
        <button type="submit" disabled={pending || capabilityError}>
          {pending ? t('providers.submit_pending') : submitLabel}
        </button>
      </div>
    </form>
  )
}
