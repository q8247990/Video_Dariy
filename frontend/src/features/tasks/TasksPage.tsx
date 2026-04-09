import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import { deleteTaskLog, getTaskLogs, retryTaskLog, stopTaskLog } from './api'

const TASK_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'session_build', label: 'Session构建' },
  { value: 'session_analysis', label: 'Session分析' },
  { value: 'daily_summary_generation', label: '日报生成' },
  { value: 'video_pipeline_alert', label: '视频告警' },
  { value: 'webhook_push', label: 'Webhook推送' },
]

function taskTypeLabel(value: string): string {
  const found = TASK_TYPE_OPTIONS.find((item) => item.value === value)
  return found?.label ?? value
}

function formatAlertDetail(detailJson: Record<string, unknown> | null, fallback: string | null, t: TFunction): string {
  if (!detailJson) {
    return fallback ?? '-'
  }

  const alertState = String(detailJson.alert_state ?? 'triggered')
  const alertType = String(detailJson.alert_type ?? '-')
  const sourceName = String(detailJson.source_name ?? '-')
  const cameraName = String(detailJson.camera_name ?? '-')
  const metricValue = detailJson.metric_value
  const consecutiveCount = detailJson.consecutive_count

  const stateText = alertState === 'recovered' ? t('tasks.alert_recovered', '恢复') : t('tasks.alert_triggered', '触发')
  const typeText = alertType === 'latency' ? t('tasks.alert_latency', '延迟告警') : alertType

  const metricText = metricValue === undefined || metricValue === null ? '-' : String(metricValue)
  const countText =
    consecutiveCount === undefined || consecutiveCount === null ? '-' : String(consecutiveCount)

  return `${stateText} | ${typeText} | ${t('tasks.source_label', '视频源')}:${sourceName}(${cameraName}) | ${t('tasks.metric_label', '指标')}:${metricText} | ${t('tasks.consecutive_label', '连续')}:${countText}`
}

function formatSessionAnalysisDetail(
  detailJson: Record<string, unknown> | null,
  fallback: string | null,
  taskStatus: string,
  t: TFunction,
): string {
  if (taskStatus === 'running' || taskStatus === 'pending') {
    return t('tasks.analyzing', '正在识别')
  }

  if (!detailJson) {
    return fallback ?? '-'
  }

  const reason = typeof detailJson.reason === 'string' ? detailJson.reason : ''
  if (reason === 'not_found' || reason === 'not_found_after_lock') {
    return t('tasks.skip_not_found', '已跳过：Session 不存在（{{reason}}）', { reason })
  }
  if (reason === 'already_analyzing') {
    return t('tasks.skip_already_analyzing', '已跳过：已有其他分析任务抢占执行')
  }
  if (reason === 'session_open') {
    return t('tasks.skip_session_open', '已跳过：Session 仍在采集中')
  }
  if (reason.startsWith('status_')) {
    const currentStatus = typeof detailJson.current_status === 'string' ? detailJson.current_status : reason.slice(7)
    return t('tasks.skip_status', '已跳过：Session 当前状态为 {{status}}', { status: currentStatus })
  }

  const chunkIndex =
    typeof detailJson.failed_chunk_index === 'number' ? detailJson.failed_chunk_index : null
  if (chunkIndex !== null) {
    return t('tasks.fail_at_chunk', '{{fallback}}（在第 {{index}} 段中断）', { fallback: fallback ?? t('tasks.analyze_failed', '识别失败'), index: chunkIndex + 1 })
  }
  return fallback ?? t('tasks.analyze_failed_retry', '识别失败，请稍后重试')
}

export function TasksPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const page = Number(searchParams.get('page') ?? '1') || 1
  const taskType = searchParams.get('task_type') ?? ''
  const status = searchParams.get('status') ?? ''
  const [message, setMessage] = useState('')

  const syncSearchParams = (next: { page?: number; taskType?: string; status?: string }) => {
    const params = new URLSearchParams(searchParams)
    const nextPage = next.page ?? page
    const nextTaskType = next.taskType ?? taskType
    const nextStatus = next.status ?? status

    if (nextPage > 1) {
      params.set('page', String(nextPage))
    } else {
      params.delete('page')
    }

    if (nextTaskType.trim()) {
      params.set('task_type', nextTaskType.trim())
    } else {
      params.delete('task_type')
    }

    if (nextStatus.trim()) {
      params.set('status', nextStatus.trim())
    } else {
      params.delete('status')
    }

    setSearchParams(params, { replace: true })
  }

  const queryKey = useMemo(
    () => ['task-logs', { page, taskType, status }],
    [page, taskType, status],
  )

  const query = useQuery({
    queryKey,
    queryFn: () => getTaskLogs({ page, pageSize: 20, taskType, status }),
    refetchInterval: 10_000,
  })

  const deleteMutation = useMutation({
    mutationFn: deleteTaskLog,
    onSuccess: () => {
      setMessage(t('tasks.delete_success', '任务记录删除成功'))
      queryClient.invalidateQueries({ queryKey: ['task-logs'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const stopMutation = useMutation({
    mutationFn: stopTaskLog,
    onSuccess: () => {
      setMessage(t('tasks.stop_success', '任务已结束'))
      queryClient.invalidateQueries({ queryKey: ['task-logs'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const retryMutation = useMutation({
    mutationFn: retryTaskLog,
    onSuccess: (data) => {
      setMessage(t('tasks.retry_success', '任务已重新运行：{{id}}', { id: data.task_id }))
      queryClient.invalidateQueries({ queryKey: ['task-logs'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const hasPendingAction = deleteMutation.isPending || stopMutation.isPending || retryMutation.isPending

  if (query.isLoading) {
    return <LoadingBlock text={t('tasks.loading', '加载任务日志中')} />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  const rows = query.data?.list ?? []
  const total = query.data?.pagination.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / 20))

  return (
    <div>
      <PageHeader
        title={t('tasks.title')}
        subtitle={t('tasks.subtitle', '查看扫描、归并、分析、汇总等后台任务状态')}
        actions={<button onClick={() => query.refetch()}>{t('tasks.refresh', '立即刷新')}</button>}
      />

      <div className="card tool-row tool-row-inline">
        <label>
          {t('tasks.filter_type', '任务类型')}
          <select
            value={taskType}
            onChange={(event) => {
              const value = event.target.value
              syncSearchParams({ taskType: value, page: 1 })
            }}
          >
            {TASK_TYPE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          {t('tasks.filter_status', '状态')}
          <select
            value={status}
            onChange={(event) => {
              const value = event.target.value
              syncSearchParams({ status: value, page: 1 })
            }}
          >
            <option value="">{t('tasks.status_all', '全部')}</option>
            <option value="running">{t('tasks.status_running', '运行中')}</option>
            <option value="success">{t('tasks.status_success', '成功')}</option>
            <option value="skipped">{t('tasks.status_skipped', '已跳过')}</option>
            <option value="failed">{t('tasks.status_failed', '失败')}</option>
            <option value="timeout">{t('tasks.status_timeout', '超时')}</option>
            <option value="cancelled">{t('tasks.status_cancelled', '已取消')}</option>
            <option value="pending">{t('tasks.status_pending', '待执行')}</option>
          </select>
        </label>
      </div>

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>{t('tasks.col_type', '任务类型')}</th>
              <th>{t('tasks.col_target_id', '目标ID')}</th>
              <th>{t('tasks.col_status', '状态')}</th>
              <th>{t('tasks.col_retry', '重试')}</th>
              <th>{t('tasks.col_message', '信息')}</th>
              <th>{t('tasks.col_created', '创建时间')}</th>
              <th>{t('tasks.col_actions', '操作')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.id}</td>
                <td>{taskTypeLabel(row.task_type)}</td>
                <td>{row.task_target_id ?? '-'}</td>
                <td>
                  <StatusTag status={row.status} />
                </td>
                <td>{row.retry_count}</td>
                <td className="break-all">
                  {row.task_type === 'video_pipeline_alert'
                    ? formatAlertDetail(row.detail_json, row.message, t)
                    : row.task_type === 'session_analysis'
                      ? formatSessionAnalysisDetail(row.detail_json, row.message, row.status, t)
                      : row.message ?? '-'}
                </td>
                <td>{row.created_at}</td>
                <td>
                  {row.status === 'running' || row.status === 'pending' ? (
                    <button className="ghost" disabled={hasPendingAction} onClick={() => stopMutation.mutate(row.id)}>
                      {t('tasks.action_stop', '结束任务')}
                    </button>
                  ) : row.status === 'failed' || row.status === 'timeout' ? (
                    <>
                      <button className="ghost" disabled={hasPendingAction} onClick={() => retryMutation.mutate(row.id)}>
                        {t('tasks.action_retry', '重新运行')}
                      </button>
                      <button className="ghost" disabled={hasPendingAction} onClick={() => deleteMutation.mutate(row.id)}>
                        {t('tasks.action_delete', '删除')}
                      </button>
                    </>
                  ) : row.status === 'cancelled' || row.status === 'success' || row.status === 'skipped' ? (
                    <button className="ghost" disabled={hasPendingAction} onClick={() => deleteMutation.mutate(row.id)}>
                      {t('tasks.action_delete', '删除')}
                    </button>
                  ) : (
                    '-'
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty-cell">
                  {t('tasks.empty', '暂无任务日志')}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>

        <div className="pager">
          <button
            className="ghost"
            disabled={page <= 1}
            onClick={() => {
              const nextPage = Math.max(1, page - 1)
              syncSearchParams({ page: nextPage })
            }}
          >
            {t('tasks.pager_prev', '上一页')}
          </button>
          <span>
            {t('tasks.pager_info', '第 {{page}} / {{totalPages}} 页，共 {{total}} 条', { page, totalPages, total })}
          </span>
          <button
            className="ghost"
            disabled={page >= totalPages}
            onClick={() => {
              const nextPage = Math.min(totalPages, page + 1)
              syncSearchParams({ page: nextPage })
            }}
          >
            {t('tasks.pager_next', '下一页')}
          </button>
        </div>
      </div>
    </div>
  )
}
