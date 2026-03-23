import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '../../components/common/PageHeader'
import type { ProviderCreate } from '../../types/api'
import { createOnboardingProvider, testOnboardingProvider } from './api'
import { useOnboardingDraftStore } from './state'

export function OnboardingBasicProviderPage() {
  const navigate = useNavigate()
  const provider = useOnboardingDraftStore((state) => state.provider)
  const setProvider = useOnboardingDraftStore((state) => state.setProvider)
  const hydrate = useOnboardingDraftStore((state) => state.hydrate)
  const [message, setMessage] = useState('')

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const createMutation = useMutation({
    mutationFn: createOnboardingProvider,
    onSuccess: (data) => {
      setProvider({ provider_id: data.id, skipped: false })
      setMessage('Provider 保存成功，请测试连接。')
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const testMutation = useMutation({
    mutationFn: testOnboardingProvider,
    onSuccess: (data) => {
      setProvider({ tested: data.success, skipped: false })
      setMessage(data.message)
      if (data.success) {
        navigate('/onboarding/basic/summary-time')
      }
    },
    onError: (error) => {
      setProvider({ tested: false })
      setMessage((error as Error).message)
    },
  })

  const onSave = () => {
    const payload: ProviderCreate = {
      provider_name: '默认 Provider',
      api_base_url: provider.api_base_url.trim(),
      api_key: provider.api_key.trim(),
      model_name: provider.model_name.trim(),
      timeout_seconds: 60,
      retry_count: 3,
      extra_config_json: {},
      enabled: true,
      supports_vision: true,
      supports_qa: true,
      is_default_vision: true,
      is_default_qa: true,
    }
    createMutation.mutate(payload)
  }

  const canSave = provider.api_base_url.trim() && provider.api_key.trim() && provider.model_name.trim()

  return (
    <div>
      <PageHeader title="阶段一 · 配置 Provider" subtitle="这一步决定分析、日报和问答是否可用" />
      <div className="card config-form">
        {message ? <div className={provider.tested ? 'api-ok' : 'api-error'}>{message}</div> : null}
        {provider.skipped ? (
          <div className="api-error">你已跳过 Provider，系统当前不算基础可运行，分析/日报/问答不可用。</div>
        ) : null}
        <label>
          API URL
          <input
            value={provider.api_base_url}
            onChange={(event) => setProvider({ api_base_url: event.target.value, tested: false })}
            placeholder="https://api.openai.com/v1"
          />
        </label>
        <label>
          API Key
          <input
            type="password"
            value={provider.api_key}
            onChange={(event) => setProvider({ api_key: event.target.value, tested: false })}
          />
        </label>
        <label>
          模型名称
          <input
            value={provider.model_name}
            onChange={(event) => setProvider({ model_name: event.target.value, tested: false })}
          />
        </label>

        <div className="onboarding-actions">
          <button className="ghost" onClick={() => navigate('/onboarding/basic/video')}>
            上一步
          </button>
          <button
            className="ghost"
            onClick={() => {
              setProvider({ skipped: true, tested: false })
              navigate('/onboarding/basic/summary-time')
            }}
          >
            跳过此步
          </button>
          <button onClick={onSave} disabled={!canSave || createMutation.isPending}>
            {createMutation.isPending ? '保存中...' : '保存'}
          </button>
          <button
            onClick={() => {
              if (provider.provider_id) {
                testMutation.mutate(provider.provider_id)
              }
            }}
            disabled={!provider.provider_id || testMutation.isPending}
          >
            {testMutation.isPending ? '测试中...' : '测试连接并下一步'}
          </button>
        </div>
      </div>
    </div>
  )
}
