// 帧采样器设置卡片（模型服务页）。
//
// 复用 features/system-config 的现有查询与写入（queryKey ['system-config']、
// getSystemConfig / updateSystemConfig），不新增请求层。
//
// 采样器是仅后端可达的独立服务（浏览器直连会引入 CORS / 网络可达性假设），
// 前端只维护配置，不直接调用 {frame_sampler_url}/sample；端到端采样由
// 分析任务与系统状态校验验证。

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import type { SystemConfig } from '../../types/api'
import { getSystemConfig, updateSystemConfig } from '../system-config/api'
import {
  SAMPLER_DEFAULTS,
  SAMPLER_ERROR_I18N_KEYS,
  toDraft,
  toUpdate,
  type FrameSamplerConfig,
  type SamplerDraft,
  type SamplerErrorCode,
  type SamplerFieldKey,
} from './samplerForm'

export function SamplerCard() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [success, setSuccess] = useState('')
  const [failure, setFailure] = useState('')

  const query = useQuery({
    queryKey: ['system-config'],
    queryFn: getSystemConfig,
  })

  const mutation = useMutation({
    mutationFn: (payload: Record<SamplerFieldKey, string | number | boolean>) =>
      // 共享的 SystemConfig 类型（types/api.ts，由其他任务维护）尚未包含
      // 9 个 frame_* 键；后端 PUT 契约（SystemConfigUpdate）恰好接受这些键。
      updateSystemConfig(payload as unknown as SystemConfig),
    onSuccess: () => {
      setSuccess(t('providers.sampler_save_success'))
      setFailure('')
      queryClient.invalidateQueries({ queryKey: ['system-config'] })
    },
    onError: (error) => {
      // 业务错误 4000 → HTTP 400，axios 拦截器已把服务端 message 放入 Error.message
      setFailure((error as Error).message)
      setSuccess('')
    },
  })

  return (
    <div className="card">
      <div>
        <strong>{t('providers.sampler_title')}</strong>
        <div className="text-muted">{t('providers.sampler_help')}</div>
      </div>

      {success ? <div className="api-ok">{success}</div> : null}
      {failure ? <div className="api-error">{failure}</div> : null}

      {query.isLoading ? (
        <LoadingBlock text={t('providers.sampler_loading')} />
      ) : query.error ? (
        <ApiErrorAlert message={(query.error as Error).message} />
      ) : (
        <SamplerCardForm
          key={query.dataUpdatedAt}
          initialDraft={toDraft((query.data ?? {}) as FrameSamplerConfig)}
          pending={mutation.isPending}
          onSubmit={(draft) => {
            const result = toUpdate(draft)
            if (!result.ok) {
              return
            }
            mutation.mutate(result.payload)
          }}
          validate={toUpdate}
        />
      )}
    </div>
  )
}

type SamplerCardFormProps = {
  initialDraft: SamplerDraft
  pending: boolean
  onSubmit: (draft: SamplerDraft) => void
  validate: (draft: SamplerDraft) => { errors: Partial<Record<SamplerFieldKey, SamplerErrorCode>> }
}

function SamplerCardForm({ initialDraft, pending, onSubmit, validate }: SamplerCardFormProps) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState<SamplerDraft>(initialDraft)
  const [errors, setErrors] = useState<Partial<Record<SamplerFieldKey, SamplerErrorCode>>>({})

  const enabled = draft.frame_sampler_enabled

  function setField<K extends SamplerFieldKey>(key: K, value: SamplerDraft[K]) {
    setDraft((old) => ({ ...old, [key]: value }))
    setErrors((old) => {
      if (!old[key]) {
        return old
      }
      const next = { ...old }
      delete next[key]
      return next
    })
  }

  function handleSubmit() {
    const result = validate(draft)
    if (Object.keys(result.errors).length > 0) {
      setErrors(result.errors)
      return
    }
    setErrors({})
    onSubmit(draft)
  }

  function errorText(key: SamplerFieldKey): string | null {
    const code = errors[key]
    return code ? t(SAMPLER_ERROR_I18N_KEYS[code]) : null
  }

  return (
    <div className="config-form">
      {!enabled ? <div className="api-error">{t('providers.sampler_disabled_warning')}</div> : null}

      <label className="checkbox-field">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setField('frame_sampler_enabled', event.target.checked)}
        />
        {t('providers.sampler_enabled')}
      </label>

      <label>
        {t('providers.sampler_url')}
        <input
          value={draft.frame_sampler_url}
          onChange={(event) => setField('frame_sampler_url', event.target.value)}
        />
        <small className="text-muted">{t('providers.sampler_url_hint')}</small>
      </label>

      <div className="inline-fields">
        <label>
          {t('providers.sampler_timeout')}
          <input
            type="number"
            min={1}
            step={1}
            value={draft.frame_sampler_timeout_seconds}
            onChange={(event) => setField('frame_sampler_timeout_seconds', event.target.value)}
          />
          {errorText('frame_sampler_timeout_seconds') ? (
            <small className="field-error">
              {errorText('frame_sampler_timeout_seconds') as string}
            </small>
          ) : (
            <small className="text-muted">
              {t('providers.sampler_int_positive_hint', {
                value: SAMPLER_DEFAULTS.frame_sampler_timeout_seconds,
              })}
            </small>
          )}
        </label>

        <label>
          {t('providers.sampler_session_frame_limit')}
          <input
            type="number"
            min={0}
            step={1}
            value={draft.frame_sampling_session_frame_limit}
            onChange={(event) =>
              setField('frame_sampling_session_frame_limit', event.target.value)
            }
          />
          {errorText('frame_sampling_session_frame_limit') ? (
            <small className="field-error">
              {errorText('frame_sampling_session_frame_limit') as string}
            </small>
          ) : (
            <small className="text-muted">
              {t('providers.sampler_int_non_negative_hint', {
                value: SAMPLER_DEFAULTS.frame_sampling_session_frame_limit,
              })}
            </small>
          )}
        </label>
      </div>

      <div>
        <strong>{t('providers.sampler_impact_title')}</strong>
        <div className="text-muted">
          {t('providers.sampler_impact_defaults', {
            timeout: SAMPLER_DEFAULTS.frame_sampler_timeout_seconds,
          })}
        </div>
        <div className="text-muted">{t('providers.sampler_impact_no_cap')}</div>
      </div>

      <div className="dialog-actions">
        <button type="button" onClick={handleSubmit} disabled={pending}>
          {pending ? t('common.saving') : t('providers.sampler_save_button')}
        </button>
      </div>
    </div>
  )
}
