import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import type { ProviderCreate } from '../../types/api'
import { createOnboardingProvider, testOnboardingProvider } from './api'
import { useOnboardingDraftStore } from './state'

export function OnboardingBasicProviderPage() {
  const { t } = useTranslation()
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
      setMessage(t('onboarding.step_basic_provider_save_success'))
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
      provider_name: t('onboarding.step_basic_provider_default'),
      api_base_url: provider.api_base_url.trim(),
      api_key: provider.api_key.trim(),
      model_name: provider.model_name.trim(),
      timeout_seconds: 60,
      retry_count: 3,
      extra_config_json: {},
      enabled: true,
      supports_vision: true,
      supports_qa: true,
      supports_tool_calling: false,
      is_default_vision: true,
      is_default_qa: true,
      video_preprocess_mode: 'keyframe',
      video_keyframe_target_n: 120,
      video_keyframe_jpeg_quality: 88,
    }
    createMutation.mutate(payload)
  }

  const canSave = provider.api_base_url.trim() && provider.api_key.trim() && provider.model_name.trim()

  return (
    <div>
      <PageHeader
        title={t('onboarding.step_basic_provider_title')}
        subtitle={t('onboarding.step_basic_provider_subtitle')}
      />
      <div className="card config-form">
        {message ? <div className={provider.tested ? 'api-ok' : 'api-error'}>{message}</div> : null}
        {provider.skipped ? (
          <div className="api-error">{t('onboarding.step_basic_provider_skip_warning')}</div>
        ) : null}
        <label>
          {t('onboarding.form_api_url')}
          <input
            value={provider.api_base_url}
            onChange={(event) => setProvider({ api_base_url: event.target.value, tested: false })}
            placeholder="https://api.openai.com/v1"
          />
        </label>
        <label>
          {t('onboarding.form_api_key')}
          <input
            type="password"
            value={provider.api_key}
            onChange={(event) => setProvider({ api_key: event.target.value, tested: false })}
          />
        </label>
        <label>
          {t('onboarding.form_model_name')}
          <input
            value={provider.model_name}
            onChange={(event) => setProvider({ model_name: event.target.value, tested: false })}
          />
        </label>

        <div className="onboarding-actions">
          <button className="ghost" onClick={() => navigate('/onboarding/basic/video')}>
            {t('onboarding.step_prev')}
          </button>
          <button
            className="ghost"
            onClick={() => {
              setProvider({ skipped: true, tested: false })
              navigate('/onboarding/basic/summary-time')
            }}
          >
            {t('onboarding.step_skip')}
          </button>
          <button onClick={onSave} disabled={!canSave || createMutation.isPending}>
            {createMutation.isPending ? t('onboarding.saving') : t('onboarding.step_save')}
          </button>
          <button
            onClick={() => {
              if (provider.provider_id) {
                testMutation.mutate(provider.provider_id)
              }
            }}
            disabled={!provider.provider_id || testMutation.isPending}
          >
            {testMutation.isPending ? t('onboarding.testing') : t('onboarding.step_test_and_next')}
          </button>
        </div>
      </div>
    </div>
  )
}
