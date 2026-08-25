import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import type { SystemConfig } from '../../types/api'
import { getSystemConfig, updateSystemConfig } from './api'

type FormState = {
  daily_summary_schedule: string
  scan_interval_seconds: string
  scan_hot_window_hours: string
  scan_late_tolerance_seconds: string
  latency_alert_threshold_seconds: string
  alert_consecutive_required: string
  alert_notify_cooldown_minutes: string
  default_session_merge_gap_seconds: string
  tag_recommendation_enabled: boolean
  mcp_enabled: boolean
  mcp_token: string
}

function toFormState(data: SystemConfig): FormState {
  return {
    daily_summary_schedule: data.daily_summary_schedule ?? '10:00',
    scan_interval_seconds:
      typeof data.scan_interval_seconds === 'number' ? String(data.scan_interval_seconds) : '300',
    scan_hot_window_hours:
      typeof data.scan_hot_window_hours === 'number' ? String(data.scan_hot_window_hours) : '24',
    scan_late_tolerance_seconds:
      typeof data.scan_late_tolerance_seconds === 'number'
        ? String(data.scan_late_tolerance_seconds)
        : '180',
    latency_alert_threshold_seconds:
      typeof data.latency_alert_threshold_seconds === 'number'
        ? String(data.latency_alert_threshold_seconds)
        : '600',
    alert_consecutive_required:
      typeof data.alert_consecutive_required === 'number' ? String(data.alert_consecutive_required) : '3',
    alert_notify_cooldown_minutes:
      typeof data.alert_notify_cooldown_minutes === 'number'
        ? String(data.alert_notify_cooldown_minutes)
        : '60',
    default_session_merge_gap_seconds:
      typeof data.default_session_merge_gap_seconds === 'number'
        ? String(data.default_session_merge_gap_seconds)
        : '61',
    tag_recommendation_enabled: Boolean(data.tag_recommendation_enabled),
    mcp_enabled: Boolean(data.mcp_enabled),
    mcp_token: typeof data.mcp_token === 'string' ? data.mcp_token : '',
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
            daily_summary_schedule: form.daily_summary_schedule,
            scan_interval_seconds: Number(form.scan_interval_seconds),
            scan_hot_window_hours: Number(form.scan_hot_window_hours),
            scan_late_tolerance_seconds: Number(form.scan_late_tolerance_seconds),
            latency_alert_threshold_seconds: Number(form.latency_alert_threshold_seconds),
            alert_consecutive_required: Number(form.alert_consecutive_required),
            alert_notify_cooldown_minutes: Number(form.alert_notify_cooldown_minutes),
            default_session_merge_gap_seconds: Number(form.default_session_merge_gap_seconds),
            tag_recommendation_enabled: form.tag_recommendation_enabled,
            mcp_enabled: form.mcp_enabled,
            mcp_token: form.mcp_token,
          })
        }}
      />
    </div>
  )
}

const DEFAULT_SYSTEM_CONFIG: SystemConfig = {
  daily_summary_schedule: '10:00',
  scan_interval_seconds: 300,
  scan_hot_window_hours: 24,
  scan_late_tolerance_seconds: 180,
  latency_alert_threshold_seconds: 600,
  alert_consecutive_required: 3,
  alert_notify_cooldown_minutes: 60,
  default_session_merge_gap_seconds: 61,
  tag_recommendation_enabled: false,
  mcp_enabled: false,
  mcp_token: '',
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
            {t('system_config.scan_late_tolerance')}
            <input
              type="number"
              min={0}
              value={form.scan_late_tolerance_seconds}
              onChange={(event) =>
                setForm((old) => ({ ...old, scan_late_tolerance_seconds: event.target.value }))
              }
            />
          </label>

          <label>
            {t('system_config.latency_alert_threshold')}
            <input
              type="number"
              min={30}
              value={form.latency_alert_threshold_seconds}
              onChange={(event) =>
                setForm((old) => ({ ...old, latency_alert_threshold_seconds: event.target.value }))
              }
            />
          </label>

          <label>
            {t('system_config.alert_consecutive_required')}
            <input
              type="number"
              min={1}
              value={form.alert_consecutive_required}
              onChange={(event) =>
                setForm((old) => ({ ...old, alert_consecutive_required: event.target.value }))
              }
            />
          </label>

          <label>
            {t('system_config.alert_notify_cooldown')}
            <input
              type="number"
              min={1}
              value={form.alert_notify_cooldown_minutes}
              onChange={(event) =>
                setForm((old) => ({ ...old, alert_notify_cooldown_minutes: event.target.value }))
              }
            />
          </label>

          <label>
            {t('system_config.session_merge_gap')}
            <input
              type="number"
              min={1}
              value={form.default_session_merge_gap_seconds}
              onChange={(event) =>
                setForm((old) => ({ ...old, default_session_merge_gap_seconds: event.target.value }))
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
