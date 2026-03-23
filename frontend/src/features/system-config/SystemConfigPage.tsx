import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')

  const query = useQuery({
    queryKey: ['system-config'],
    queryFn: getSystemConfig,
  })

  const mutation = useMutation({
    mutationFn: updateSystemConfig,
    onSuccess: () => {
      setMessage('系统配置已保存')
      queryClient.invalidateQueries({ queryKey: ['system-config'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  if (query.isLoading) {
    return <LoadingBlock text="加载系统配置中" />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  return (
    <div>
      <PageHeader title="系统配置" subtitle="设置扫描、归并和功能开关" />

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
  const [form, setForm] = useState<FormState>(initialForm)

  return (
    <div className="card config-form">
        <label>
          日报生成时间（HH:mm）
          <input
            value={form.daily_summary_schedule}
            onChange={(event) => setForm((old) => ({ ...old, daily_summary_schedule: event.target.value }))}
            placeholder="10:00"
          />
        </label>

        <div className="inline-fields">
          <label>
            扫描间隔（秒）
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
            热窗口（小时）
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
            迟到容忍（秒）
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
            延迟告警阈值（秒）
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
            告警连续触发次数
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
            告警通知冷却（分钟）
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
            Session 归并间隔（秒）
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
          启用标签推荐
        </label>

        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={form.mcp_enabled}
            onChange={(event) => setForm((old) => ({ ...old, mcp_enabled: event.target.checked }))}
          />
          启用 MCP 接口（AstrBot 通过 /mcp 接入）
        </label>

        <label>
          MCP Token（供 Streamable HTTP 鉴权）
          <input
            value={form.mcp_token}
            onChange={(event) => setForm((old) => ({ ...old, mcp_token: event.target.value }))}
            placeholder="请输入 MCP Token"
          />
        </label>

        <div className="dialog-actions">
          <button onClick={() => onSubmit(form)} disabled={pending}>
            {pending ? '保存中...' : '保存配置'}
          </button>
        </div>
      </div>
  )
}
