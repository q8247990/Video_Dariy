import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { Pager } from '../../components/common/Pager'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import { deleteTaskLog, getTaskLogs, retryTaskLog, stopTaskLog } from './api'

const TASK_TYPE_VALUES = [
  '',
  'session_build',
  'session_analysis',
  'daily_summary_generation',
  'webhook_push',
] as const

function taskTypeLabel(value: string, t: (key: string) => string): string {
  if (value === '') {
    return t('tasks.status_all')
  }
  if (value === 'session_build') {
    return t('tasks.task_session_build')
  }
  if (value === 'session_analysis') {
    return t('tasks.task_session_analysis')
  }
  if (value === 'daily_summary_generation') {
    return t('tasks.task_daily_summary_generation')
  }
  if (value === 'webhook_push') {
    return t('tasks.task_webhook_push')
  }
  return t('tasks.task_other')
}

function formatSessionAnalysisDetail(
  detailJson: Record<string, unknown> | null,
  fallback: string | null,
  taskStatus: string,
  t: TFunction,
): string {
  if (taskStatus === 'running' || taskStatus === 'pending') {
    return t('tasks.status_analyzing')
  }

  if (!detailJson) {
    return fallback ?? '-'
  }

  const reason = typeof detailJson.reason === 'string' ? detailJson.reason : ''
  if (reason === 'not_found' || reason === 'not_found_after_lock') {
    return t('tasks.skip_not_found', { reason })
  }
  if (reason === 'already_analyzing') {
    return t('tasks.skip_already_analyzing')
  }
  if (reason === 'session_open') {
    return t('tasks.skip_session_open')
  }
  if (reason.startsWith('status_')) {
    const currentStatus =
      typeof detailJson.current_status === 'string' ? detailJson.current_status : reason.slice(7)
    return t('tasks.skip_status', { status: currentStatus })
  }

  const chunkIndex =
    typeof detailJson.failed_chunk_index === 'number' ? detailJson.failed_chunk_index : null
  if (chunkIndex !== null) {
    return t('tasks.fail_at_chunk', {
      fallback: fallback ?? t('tasks.analyze_failed'),
      index: chunkIndex + 1,
    })
  }
  return fallback ?? t('tasks.analyze_failed_retry')
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
      setMessage(t('tasks.delete_success'))
      queryClient.invalidateQueries({ queryKey: ['task-logs'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const stopMutation = useMutation({
    mutationFn: stopTaskLog,
    onSuccess: () => {
      setMessage(t('tasks.stop_success'))
      queryClient.invalidateQueries({ queryKey: ['task-logs'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const retryMutation = useMutation({
    mutationFn: retryTaskLog,
    onSuccess: (data) => {
      setMessage(t('tasks.retry_success', { id: data.task_id }))
      queryClient.invalidateQueries({ queryKey: ['task-logs'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const hasPendingAction =
    deleteMutation.isPending || stopMutation.isPending || retryMutation.isPending

  if (query.isLoading) {
    return <LoadingBlock text={t('tasks.loading')} />
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
        subtitle={t('tasks.subtitle')}
        actions={<button onClick={() => query.refetch()}>{t('tasks.refresh')}</button>}
      />

      <div className="card tool-row tool-row-inline">
        <label>
          {t('tasks.filter_type')}
          <select
            value={taskType}
            onChange={(event) => {
              const value = event.target.value
              syncSearchParams({ taskType: value, page: 1 })
            }}
          >
            {TASK_TYPE_VALUES.map((value) => (
              <option key={value} value={value}>
                {taskTypeLabel(value, t)}
              </option>
            ))}
          </select>
        </label>

        <label>
          {t('tasks.filter_status')}
          <select
            value={status}
            onChange={(event) => {
              const value = event.target.value
              syncSearchParams({ status: value, page: 1 })
            }}
          >
            <option value="">{t('tasks.status_all')}</option>
            <option value="running">{t('tasks.status_running')}</option>
            <option value="success">{t('tasks.status_success')}</option>
            <option value="skipped">{t('tasks.status_skipped')}</option>
            <option value="failed">{t('tasks.status_failed')}</option>
            <option value="timeout">{t('tasks.status_timeout')}</option>
            <option value="cancelled">{t('tasks.status_cancelled')}</option>
            <option value="pending">{t('tasks.status_pending')}</option>
          </select>
        </label>
      </div>

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>{t('tasks.col_id')}</th>
              <th>{t('tasks.col_type')}</th>
              <th>{t('tasks.col_target_id')}</th>
              <th>{t('tasks.col_status')}</th>
              <th>{t('tasks.col_retry')}</th>
              <th>{t('tasks.col_message')}</th>
              <th>{t('tasks.col_created')}</th>
              <th>{t('tasks.col_actions')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.id}</td>
                <td>{taskTypeLabel(row.task_type, t)}</td>
                <td>{row.task_target_id ?? '-'}</td>
                <td>
                  <StatusTag status={row.status} />
                </td>
                <td>{row.retry_count}</td>
                <td className="break-all">
                  {row.task_type === 'session_analysis'
                    ? formatSessionAnalysisDetail(row.detail_json, row.message, row.status, t)
                    : (row.message ?? '-')}
                </td>
                <td>{row.created_at}</td>
                <td>
                  {row.status === 'running' || row.status === 'pending' ? (
                    <button
                      className="ghost"
                      disabled={hasPendingAction}
                      onClick={() => stopMutation.mutate(row.id)}
                    >
                      {t('tasks.action_stop')}
                    </button>
                  ) : row.status === 'failed' ||
                    row.status === 'timeout' ||
                    row.status === 'cancelled' ? (
                    <>
                      <button
                        className="ghost"
                        disabled={hasPendingAction}
                        onClick={() => retryMutation.mutate(row.id)}
                      >
                        {t('tasks.action_retry')}
                      </button>
                      <button
                        className="ghost"
                        disabled={hasPendingAction}
                        onClick={() => deleteMutation.mutate(row.id)}
                      >
                        {t('tasks.action_delete')}
                      </button>
                    </>
                  ) : row.status === 'success' || row.status === 'skipped' ? (
                    <button
                      className="ghost"
                      disabled={hasPendingAction}
                      onClick={() => deleteMutation.mutate(row.id)}
                    >
                      {t('tasks.action_delete')}
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
                  {t('tasks.empty')}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>

        <Pager
          page={page}
          totalPages={totalPages}
          total={total}
          onPrev={() => syncSearchParams({ page: Math.max(1, page - 1) })}
          onNext={() => syncSearchParams({ page: Math.min(totalPages, page + 1) })}
        />
      </div>
    </div>
  )
}
