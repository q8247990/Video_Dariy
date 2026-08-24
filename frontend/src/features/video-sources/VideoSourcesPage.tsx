import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import type { VideoSource, VideoSourceCreate, VideoSourceStatus } from '../../types/api'
import {
  createVideoSource,
  deleteVideoSource,
  getVideoSourceStatusesBatch,
  getVideoSourceStatus,
  getVideoSources,
  pauseVideoSource,
  resumeVideoSource,
  testVideoSource,
  triggerFullScan,
  updateVideoSource,
} from './api'
import { VideoSourceForm } from './VideoSourceForm'
import { VideoSourceStatusDialog } from './VideoSourceStatusDialog'
import { analysisStateText } from './utils'

function mapValidateStatusLabel(status: string | null, t: (key: string) => string): string {
  if (status === 'success') {
    return t('video_sources.verify_complete_success')
  }
  if (status === 'failed') {
    return t('video_sources.verify_complete_failed')
  }
  return t('video_sources.verify_complete_unknown')
}

export function VideoSourcesPage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [keyword, setKeyword] = useState('')
  const [page] = useState(1)
  const [showCreate, setShowCreate] = useState(false)
  const [editing, setEditing] = useState<VideoSource | null>(null)
  const [selectedStatusSourceId, setSelectedStatusSourceId] = useState<number | null>(null)
  const [message, setMessage] = useState('')

  const queryKey = useMemo(() => ['video-sources', { page, keyword }], [keyword, page])

  const listQuery = useQuery({
    queryKey,
    queryFn: () => getVideoSources({ page, pageSize: 20, keyword }),
  })

  const createMutation = useMutation({
    mutationFn: createVideoSource,
    onSuccess: () => {
      setShowCreate(false)
      setMessage(t('video_sources.create_success'))
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: Partial<VideoSourceCreate> }) =>
      updateVideoSource(id, payload),
    onSuccess: (data) => {
      setEditing(null)
      if (data.last_validate_status === null) {
        setMessage(t('video_sources.save_success_reverify'))
      } else {
        setMessage(t('video_sources.update_success'))
      }
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const scanMutation = useMutation({
    mutationFn: triggerFullScan,
    onSuccess: (data) =>
      setMessage(`${t('video_sources.scan_triggered')}：${data.task_id}`),
    onError: (error) => setMessage(formatScanErrorMessage(error as Error, t)),
  })

  const deleteMutation = useMutation({
    mutationFn: deleteVideoSource,
    onSuccess: () => {
      setMessage(t('video_sources.delete_success'))
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
      queryClient.invalidateQueries({ queryKey: ['video-source-status'] })
    },
    onError: (error) => setMessage(formatDeleteErrorMessage(error as Error, t)),
  })

  const testMutation = useMutation({
    mutationFn: testVideoSource,
    onSuccess: (data) => {
      setMessage(
        t('video_sources.verify_complete_format', {
          status: mapValidateStatusLabel(data.last_validate_status ?? null, t),
          message: data.message,
        }),
      )
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-overview'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const pauseSourceMutation = useMutation({
    mutationFn: pauseVideoSource,
    onSuccess: () => {
      setMessage(t('video_sources.pause_success'))
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
      queryClient.invalidateQueries({ queryKey: ['video-source-status', statusSource?.id] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const resumeSourceMutation = useMutation({
    mutationFn: resumeVideoSource,
    onSuccess: () => {
      setMessage(t('video_sources.resume_success'))
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
      queryClient.invalidateQueries({ queryKey: ['video-source-status', statusSource?.id] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const rows = useMemo(() => listQuery.data?.list ?? [], [listQuery.data?.list])
  const statusBatchQuery = useQuery({
    queryKey: ['video-source-status-batch', rows.map((item) => item.id).join(',')],
    queryFn: () => getVideoSourceStatusesBatch(rows.map((item) => item.id)),
    enabled: rows.length > 0,
  })
  const statusMap = useMemo(() => {
    const map = new Map<number, VideoSourceStatus>()
    for (const item of statusBatchQuery.data ?? []) {
      map.set(item.source_id, item)
    }
    return map
  }, [statusBatchQuery.data])
  const sourceIdParam = searchParams.get('source_id')
  const queryStatusSourceId = Number(sourceIdParam)
  const resolvedStatusSourceId = selectedStatusSourceId ?? (Number.isFinite(queryStatusSourceId) ? queryStatusSourceId : null)
  const statusSource = rows.find((item) => item.id === resolvedStatusSourceId) ?? null
  const statusQuery = useQuery({
    queryKey: ['video-source-status', statusSource?.id],
    queryFn: () => getVideoSourceStatus(statusSource!.id),
    enabled: Boolean(statusSource),
  })

  if (listQuery.isLoading) {
    return <LoadingBlock text={t('video_sources.loading')} />
  }

  if (listQuery.error) {
    return <ApiErrorAlert message={(listQuery.error as Error).message} />
  }

  function closeStatusDialog() {
    setSelectedStatusSourceId(null)
    if (sourceIdParam) {
      const next = new URLSearchParams(searchParams)
      next.delete('source_id')
      setSearchParams(next)
    }
  }

  function sourceRowStatusText(item: VideoSource): string {
    if (!item.enabled) {
      return t('video_sources.source_row_disabled')
    }
    if (item.source_paused) {
      return t('video_sources.source_row_paused')
    }
    const status = statusMap.get(item.id)
    if (!status) {
      return t('video_sources.source_row_status_loading')
    }
    return analysisStateText(status.analysis_state)
  }

  function sourceRowFreshnessText(item: VideoSource): string {
    const status = statusMap.get(item.id)
    if (!status || status.minutes_since_last_new_video === null) {
      return t('video_sources.freshness_empty')
    }
    return t('video_sources.freshness_with_minutes', {
      minutes: status.minutes_since_last_new_video,
    })
  }

  return (
    <div>
      <PageHeader
        title={t('video_sources.title')}
        subtitle={t('video_sources.subtitle')}
        actions={<button onClick={() => setShowCreate(true)}>{t('video_sources.add_source')}</button>}
      />

      <div className="card tool-row">
        <input
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder={t('video_sources.search_placeholder')}
        />
      </div>

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>{t('video_sources.table_col_id')}</th>
              <th>{t('video_sources.table_col_source_name')}</th>
              <th>{t('video_sources.table_col_camera')}</th>
              <th>{t('video_sources.table_col_location')}</th>
              <th>{t('video_sources.table_col_status')}</th>
              <th>{t('video_sources.table_col_last_validate')}</th>
              <th>{t('video_sources.table_col_last_scan')}</th>
              <th>{t('video_sources.table_col_actions')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.source_name}</td>
                <td>{item.camera_name}</td>
                <td>{item.location_name}</td>
                <td>
                  <div>
                    <StatusTag status={item.enabled ? (item.source_paused ? 'paused' : 'enabled') : 'disabled'} />
                    <div style={{ fontSize: '0.82rem', marginTop: '0.2rem' }}>{sourceRowStatusText(item)}</div>
                    <div style={{ fontSize: '0.78rem', color: 'var(--muted)' }}>
                      {sourceRowFreshnessText(item)}
                    </div>
                  </div>
                </td>
                <td>{mapValidateStatusLabel(item.last_validate_status ?? null, t)}</td>
                <td>{item.last_scan_at ?? '-'}</td>
                <td>
                  <div className="row-actions">
                    <button className="ghost" onClick={() => setEditing(item)}>
                      {t('video_sources.action_edit')}
                    </button>
                    <button
                      className="ghost"
                      disabled={testMutation.isPending}
                      onClick={() => testMutation.mutate(item.id)}
                    >
                      {t('video_sources.action_verify')}
                    </button>
                    <button
                      className="ghost"
                      disabled={
                        scanMutation.isPending ||
                        !item.enabled ||
                        item.source_paused ||
                        item.last_validate_status !== 'success'
                      }
                      onClick={() => scanMutation.mutate(item.id)}
                    >
                      {t('video_sources.action_full_scan')}
                    </button>
                    <button className="ghost" onClick={() => setSelectedStatusSourceId(item.id)}>
                      {t('video_sources.action_view_status')}
                    </button>
                    <button
                      className="ghost"
                      disabled={deleteMutation.isPending}
                      onClick={() => {
                        if (!window.confirm(t('video_sources.delete_confirm', { name: item.source_name }))) {
                          return
                        }
                        deleteMutation.mutate(item.id)
                      }}
                    >
                      {t('video_sources.action_delete')}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {(showCreate || editing) && (
        <div className="dialog-mask" onClick={() => (showCreate ? setShowCreate(false) : setEditing(null))}>
          <div className="dialog" onClick={(event) => event.stopPropagation()}>
            <h3>{editing ? t('video_sources.edit_source') : t('video_sources.add_source')}</h3>
            <VideoSourceForm
              initialValue={editing ?? undefined}
              pending={createMutation.isPending || updateMutation.isPending}
              onCancel={() => (editing ? setEditing(null) : setShowCreate(false))}
              onSubmit={(payload) => {
                if (editing) {
                  updateMutation.mutate({ id: editing.id, payload })
                } else {
                  createMutation.mutate(payload)
                }
              }}
            />
          </div>
        </div>
      )}

      {statusSource && (
        <VideoSourceStatusDialog
          statusSource={statusSource}
          statusQuery={statusQuery}
          pausePending={pauseSourceMutation.isPending}
          resumePending={resumeSourceMutation.isPending}
          onPause={(id) => pauseSourceMutation.mutate(id)}
          onResume={(id) => resumeSourceMutation.mutate(id)}
          onClose={closeStatusDialog}
        />
      )}
    </div>
  )
}

function formatScanErrorMessage(
  error: Error,
  t: (key: string) => string,
): string {
  const text = error.message || ''
  if (text.includes('source_not_validated')) {
    return t('video_sources.verify_not_passed')
  }
  if (text.includes('source_disabled')) {
    return t('video_sources.verify_source_disabled')
  }
  if (text.includes('source_type_not_supported')) {
    return t('video_sources.verify_type_unsupported')
  }
  if (text.includes('source_paused')) {
    return t('video_sources.verify_source_paused')
  }
  return text
}

function formatDeleteErrorMessage(
  error: Error,
  t: (key: string) => string,
): string {
  const text = error.message || ''
  if (text.includes('running task')) {
    return t('video_sources.verify_running_task')
  }
  return text
}
