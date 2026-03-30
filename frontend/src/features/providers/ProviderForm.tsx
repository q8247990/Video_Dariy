import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
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
  }
}

export function ProviderForm({
  initialValue,
  pending,
  onCancel,
  onSubmit,
}: ProviderFormProps) {
  const [form, setForm] = useState<FormState>(() => getInitialState(initialValue))
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestProviderResult | null>(null)
  const [testError, setTestError] = useState<string | null>(null)
  const submitLabel = useMemo(() => (initialValue ? '保存修改' : '创建 Provider'), [initialValue])
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
      setTestError(e instanceof Error ? e.message : '测试请求失败')
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
    }
    onSubmit(payload)
  }

  return (
    <form className="dialog-form" onSubmit={handleSubmit}>
      <label>
        Provider 名称
        <input
          required
          value={form.provider_name}
          onChange={(event) => setForm((old) => ({ ...old, provider_name: event.target.value }))}
        />
      </label>

      <label>
        模型能力
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
            视觉能力
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
            问答能力
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
            工具调用
          </button>
        </div>
      </label>
      {capabilityError ? <div className="api-error">请至少选择一种模型能力</div> : null}

      <label>
        接口地址
        <input
          required
          value={form.api_base_url}
          onChange={(event) => setForm((old) => ({ ...old, api_base_url: event.target.value }))}
          placeholder="https://api.openai.com/v1"
        />
      </label>

      <label>
        模型名称
        <input
          required
          value={form.model_name}
          onChange={(event) => setForm((old) => ({ ...old, model_name: event.target.value }))}
        />
      </label>

      <label>
        API Key {initialValue ? '(留空表示不更新)' : ''}
        <input
          type="password"
          required={!initialValue}
          value={form.api_key}
          onChange={(event) => setForm((old) => ({ ...old, api_key: event.target.value }))}
        />
      </label>

      <div className="inline-fields">
        <label>
          超时时间(秒)
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
          重试次数
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
        启用该 Provider
      </label>

      {initialValue ? (
        <div className="test-section">
          <button
            type="button"
            className="ghost"
            disabled={testing}
            onClick={handleTest}
          >
            {testing ? '检测中...' : '测试能力'}
          </button>
          {testResult ? (
            <div className={testResult.success ? 'test-result test-result-success' : 'test-result test-result-fail'}>
              <span>连通性: {testResult.success ? 'OK' : '失败'}</span>
              {testResult.success ? (
                <>
                  <span>视觉: {testResult.supports_vision ? 'OK' : '不支持'}</span>
                  <span>工具调用: {testResult.supports_tool_calling ? 'OK' : '不支持'}</span>
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
          取消
        </button>
        <button type="submit" disabled={pending || capabilityError}>
          {pending ? '处理中...' : submitLabel}
        </button>
      </div>
    </form>
  )
}
