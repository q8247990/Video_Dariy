import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
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
import { VideoSourceStatusDialog, analysisStateText } from './VideoSourceStatusDialog'

export function VideoSourcesPage() {
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
      setMessage('视频源创建成功')
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
        setMessage('视频源更新成功。配置已变更，请先重新校验，再执行扫描。')
      } else {
        setMessage('视频源更新成功')
      }
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const scanMutation = useMutation({
    mutationFn: triggerFullScan,
    onSuccess: (data) => setMessage(`已触发全量扫描任务：${data.task_id}`),
    onError: (error) => setMessage(formatScanErrorMessage(error as Error)),
  })

  const deleteMutation = useMutation({
    mutationFn: deleteVideoSource,
    onSuccess: () => {
      setMessage('视频源删除成功')
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
      queryClient.invalidateQueries({ queryKey: ['video-source-status'] })
    },
    onError: (error) => setMessage(formatDeleteErrorMessage(error as Error)),
  })

  const testMutation = useMutation({
    mutationFn: testVideoSource,
    onSuccess: (data) => {
      setMessage(`校验完成：${data.last_validate_status ?? 'unknown'}，${data.message}`)
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-overview'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const pauseSourceMutation = useMutation({
    mutationFn: pauseVideoSource,
    onSuccess: () => {
      setMessage('已暂停视频源，停止接收新文件与自动处理')
      queryClient.invalidateQueries({ queryKey: ['video-sources'] })
      queryClient.invalidateQueries({ queryKey: ['video-source-status', statusSource?.id] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const resumeSourceMutation = useMutation({
    mutationFn: resumeVideoSource,
    onSuccess: () => {
      setMessage('已恢复视频源，重新开始接收与自动处理')
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
    return <LoadingBlock text="加载视频源中" />
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
      return '已禁用'
    }
    if (item.source_paused) {
      return '已暂停'
    }
    const status = statusMap.get(item.id)
    if (!status) {
      return '状态加载中'
    }
    return analysisStateText(status.analysis_state)
  }

  function sourceRowFreshnessText(item: VideoSource): string {
    const status = statusMap.get(item.id)
    if (!status || status.minutes_since_last_new_video === null) {
      return '最近新视频：-'
    }
    return `最近新视频：${status.minutes_since_last_new_video} 分钟前`
  }

  return (
    <div>
      <PageHeader
        title="视频源管理"
        subtitle="配置摄像头目录和识别上下文"
        actions={<button onClick={() => setShowCreate(true)}>新增视频源</button>}
      />

      <div className="card tool-row">
        <input
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder="按视频源或摄像头名称搜索"
        />
      </div>

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>视频源名称</th>
              <th>摄像头</th>
              <th>位置</th>
              <th>状态</th>
              <th>最近校验</th>
              <th>最近扫描</th>
              <th>操作</th>
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
                <td>{item.last_validate_status ?? '-'}</td>
                <td>{item.last_scan_at ?? '-'}</td>
                <td>
                  <div className="row-actions">
                    <button className="ghost" onClick={() => setEditing(item)}>
                      编辑
                    </button>
                    <button
                      className="ghost"
                      disabled={testMutation.isPending}
                      onClick={() => testMutation.mutate(item.id)}
                    >
                      校验
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
                      全量扫描
                    </button>
                    <button className="ghost" onClick={() => setSelectedStatusSourceId(item.id)}>
                      查看状态
                    </button>
                    <button
                      className="ghost"
                      disabled={deleteMutation.isPending}
                      onClick={() => {
                        if (!window.confirm(`确认删除视频源「${item.source_name}」吗？`)) {
                          return
                        }
                        deleteMutation.mutate(item.id)
                      }}
                    >
                      删除
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
            <h3>{editing ? '编辑视频源' : '新增视频源'}</h3>
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

function formatScanErrorMessage(error: Error): string {
  const text = error.message || ''
  if (text.includes('source_not_validated')) {
    return '该视频源尚未校验通过，请先执行校验。'
  }
  if (text.includes('source_disabled')) {
    return '该视频源已禁用，请先启用后再扫描。'
  }
  if (text.includes('source_type_not_supported')) {
    return '该视频源类型当前不支持扫描。'
  }
  if (text.includes('source_paused')) {
    return '该视频源已暂停，请先恢复后再扫描。'
  }
  return text
}

function formatDeleteErrorMessage(error: Error): string {
  const text = error.message || ''
  if (text.includes('running task')) {
    return '该视频源仍有运行中的任务，请稍后重试删除。'
  }
  return text
}
