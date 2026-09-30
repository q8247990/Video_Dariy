import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import type { SystemConfig } from '../../types/api'
import { getSystemConfig, updateSystemConfig } from './api'

type FormState = {
  home_timezone: string
  daily_summary_schedule: string
  scan_interval_seconds: string
  scan_hot_window_hours: string
  tag_recommendation_enabled: boolean
  mcp_enabled: boolean
  mcp_token: string
  media_manifest_ttl_seconds: string
  media_segment_ttl_seconds: string
  media_entity_image_ttl_seconds: string
}

function toFormState(data: SystemConfig): FormState {
  return {
    home_timezone: data.home_timezone ?? 'Asia/Shanghai',
    daily_summary_schedule: data.daily_summary_schedule ?? '10:00',
    scan_interval_seconds:
      typeof data.scan_interval_seconds === 'number' ? String(data.scan_interval_seconds) : '300',
    scan_hot_window_hours:
      typeof data.scan_hot_window_hours === 'number' ? String(data.scan_hot_window_hours) : '24',
    tag_recommendation_enabled: Boolean(data.tag_recommendation_enabled),
    mcp_enabled: Boolean(data.mcp_enabled),
    mcp_token: typeof data.mcp_token === 'string' ? data.mcp_token : '',
    media_manifest_ttl_seconds:
      typeof data.media_manifest_ttl_seconds === 'number'
        ? String(data.media_manifest_ttl_seconds)
        : '1800',
    media_segment_ttl_seconds:
      typeof data.media_segment_ttl_seconds === 'number'
        ? String(data.media_segment_ttl_seconds)
        : '1800',
    media_entity_image_ttl_seconds:
      typeof data.media_entity_image_ttl_seconds === 'number'
        ? String(data.media_entity_image_ttl_seconds)
        : '1800',
  }
}

export function SystemConfigPage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')

  const query = useQuery({
    queryKey: ['system-config'],
    queryFn: getSystemConfig,
  })

  const mutation = useMutation({
    mutationFn: updateSystemConfig,
    onSuccess: () => {
      setMessage(t('system_config.save_success'))
      queryClient.invalidateQueries({ queryKey: ['system-config'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  if (query.isLoading) {
    return <LoadingBlock text={t('system_config.loading')} />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  return (
    <div>
      <PageHeader title={t('system_config.title')} subtitle={t('system_config.subtitle')} />

      {message ? <div className="api-ok">{message}</div> : null}

      <SystemConfigForm
        key={JSON.stringify(query.data ?? {})}
        initialForm={toFormState(query.data ?? DEFAULT_SYSTEM_CONFIG)}
        pending={mutation.isPending}
        onSubmit={(form) => {
          mutation.mutate({
            home_timezone: form.home_timezone,
            daily_summary_schedule: form.daily_summary_schedule,
            scan_interval_seconds: Number(form.scan_interval_seconds),
            scan_hot_window_hours: Number(form.scan_hot_window_hours),
            tag_recommendation_enabled: form.tag_recommendation_enabled,
            mcp_enabled: form.mcp_enabled,
            mcp_token: form.mcp_token,
            media_manifest_ttl_seconds: Number(form.media_manifest_ttl_seconds),
            media_segment_ttl_seconds: Number(form.media_segment_ttl_seconds),
            media_entity_image_ttl_seconds: Number(form.media_entity_image_ttl_seconds),
          })
        }}
      />
    </div>
  )
}

const DEFAULT_SYSTEM_CONFIG: SystemConfig = {
  home_timezone: 'Asia/Shanghai',
  daily_summary_schedule: '10:00',
  scan_interval_seconds: 300,
  scan_hot_window_hours: 24,
  tag_recommendation_enabled: false,
  mcp_enabled: false,
  mcp_token: '',
  media_manifest_ttl_seconds: 1800,
  media_segment_ttl_seconds: 1800,
  media_entity_image_ttl_seconds: 1800,
}

type SystemConfigFormProps = {
  initialForm: FormState
  pending: boolean
  onSubmit: (form: FormState) => void
}

function SystemConfigForm({ initialForm, pending, onSubmit }: SystemConfigFormProps) {
  const { t } = useTranslation()
  const [form, setForm] = useState<FormState>(initialForm)

  return (
      <div className="card config-form">
        <label>
          {t('system_config.home_timezone')}
          <input
            value={form.home_timezone}
            onChange={(event) => setForm((old) => ({ ...old, home_timezone: event.target.value }))}
            placeholder="Asia/Shanghai"
          />
          <small>{t('system_config.home_timezone_hint')}</small>
        </label>
        <label>
          {t('system_config.daily_summary_time')}
          <input
            value={form.daily_summary_schedule}
            onChange={(event) => setForm((old) => ({ ...old, daily_summary_schedule: event.target.value }))}
            placeholder="10:00"
          />
        </label>

        <div className="inline-fields">
          <label>
            {t('system_config.scan_interval')}
            <input
              type="number"
              min={10}
              value={form.scan_interval_seconds}
              onChange={(event) =>
                setForm((old) => ({ ...old, scan_interval_seconds: event.target.value }))
              }
            />
          </label>

          <label>
            {t('system_config.scan_hot_window')}
            <input
              type="number"
              min={1}
              value={form.scan_hot_window_hours}
              onChange={(event) =>
                setForm((old) => ({ ...old, scan_hot_window_hours: event.target.value }))
              }
            />
          </label>

          <label>
            {t('system_config.media_manifest_ttl')}
            <input
              type="number"
              min={30}
              value={form.media_manifest_ttl_seconds}
              onChange={(event) =>
                setForm((old) => ({ ...old, media_manifest_ttl_seconds: event.target.value }))
              }
            />
          </label>

          <label>
            {t('system_config.media_segment_ttl')}
            <input
              type="number"
              min={30}
              value={form.media_segment_ttl_seconds}
              onChange={(event) =>
                setForm((old) => ({ ...old, media_segment_ttl_seconds: event.target.value }))
              }
            />
          </label>

          <label>
            {t('system_config.media_entity_image_ttl')}
            <input
              type="number"
              min={30}
              value={form.media_entity_image_ttl_seconds}
              onChange={(event) =>
                setForm((old) => ({ ...old, media_entity_image_ttl_seconds: event.target.value }))
              }
            />
          </label>
        </div>

        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={form.tag_recommendation_enabled}
            onChange={(event) =>
              setForm((old) => ({ ...old, tag_recommendation_enabled: event.target.checked }))
            }
          />
          {t('system_config.tag_recommendation')}
        </label>

        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={form.mcp_enabled}
            onChange={(event) => setForm((old) => ({ ...old, mcp_enabled: event.target.checked }))}
          />
          {t('system_config.mcp_enabled')}
        </label>

        <label>
          {t('system_config.mcp_token')}
          <input
            type="password"
            value={form.mcp_token}
            onChange={(event) => setForm((old) => ({ ...old, mcp_token: event.target.value }))}
            placeholder={t('system_config.mcp_token_placeholder')}
          />
        </label>

        <div className="dialog-actions">
          <button onClick={() => onSubmit(form)} disabled={pending}>
            {pending ? t('common.saving') : t('system_config.save_button')}
          </button>
        </div>
      </div>
  )
}
