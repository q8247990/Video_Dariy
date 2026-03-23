import { Fragment, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import type { VideoSession } from '../../types/api'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import { SessionPlaybackModal } from '../../components/common/SessionPlaybackModal'
import { getSessions } from '../sessions/api'
import { getSessionEvents, triggerSessionAnalyze } from './api'

function formatSessionStartTime(value: string | null): string {
  if (!value) {
    return '-'
  }

  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }

  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')

  return `${year}-${month}-${day}:${hour}:${minute}`
}

export function EventsPage() {
  const navigate = useNavigate()
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState('1')
  const [sourceId, setSourceId] = useState('')
  const [analysisStatus, setAnalysisStatus] = useState('')
  const [startTime, setStartTime] = useState('')
  const [endTime, setEndTime] = useState('')
  const [selectedSessionId, setSelectedSessionId] = useState<number | null>(null)
  const [expandedSessionId, setExpandedSessionId] = useState<number | null>(null)
  const [analyzingSessionIds, setAnalyzingSessionIds] = useState<Set<number>>(new Set())
  const [message, setMessage] = useState('')
  const queryClient = useQueryClient()

  const queryKey = useMemo(
    () => ['events', { page, sourceId, analysisStatus, startTime, endTime }],
    [page, sourceId, analysisStatus, startTime, endTime],
  )

  const query = useQuery({
    queryKey,
    queryFn: () =>
        getSessions({
          page,
          pageSize: 20,
          sourceId,
          analysisStatus,
          startTime,
          endTime,
        }),
  })

  const analyzeMutation = useMutation({
    mutationFn: (sessionId: number) => triggerSessionAnalyze(sessionId),
    onSuccess: () => {
      setMessage('已提交重新识别任务')
      queryClient.invalidateQueries({ queryKey: ['events'] })
    },
    onError: (error) => {
      setMessage(formatAnalyzeErrorMessage(error as Error))
    },
  })

  const handleReanalyze = async (sessionId: number) => {
    if (analyzingSessionIds.has(sessionId)) {
      return
    }

    setAnalyzingSessionIds((old) => {
      const next = new Set(old)
      next.add(sessionId)
      return next
    })

    try {
      await analyzeMutation.mutateAsync(sessionId)
    } finally {
      setAnalyzingSessionIds((old) => {
        const next = new Set(old)
        next.delete(sessionId)
        return next
      })
    }
  }

  if (query.isLoading) {
    return <LoadingBlock text="加载事件列表中" />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  const data = query.data
  const list = data?.list ?? []
  const total = data?.pagination.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / 20))

  const jumpToPage = () => {
    const nextPage = Number(pageInput)
    if (!Number.isFinite(nextPage)) {
      setPageInput(String(page))
      return
    }
    const normalizedPage = Math.min(totalPages, Math.max(1, Math.trunc(nextPage)))
    setPage(normalizedPage)
    setPageInput(String(normalizedPage))
  }

  return (
    <div>
      <PageHeader title="事件与回放" subtitle="按 Session 归并查看事件，再展开查看具体明细" />

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card tool-row tool-row-inline">
        <label>
          视频源编号（可选）
          <input
            value={sourceId}
            onChange={(event) => {
              setPage(1)
              setSourceId(event.target.value)
            }}
            placeholder="按视频源ID筛选"
          />
        </label>
        <label>
          分析状态
          <select
            value={analysisStatus}
            onChange={(event) => {
              setPage(1)
              setAnalysisStatus(event.target.value)
            }}
          >
            <option value="">全部</option>
            <option value="sealed">待识别</option>
            <option value="analyzing">分析中</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
          </select>
        </label>
        <label>
          开始时间
          <input
            type="datetime-local"
            value={startTime}
            onChange={(event) => {
              setPage(1)
              setPageInput('1')
              setStartTime(event.target.value)
            }}
          />
        </label>
        <label>
          结束时间
          <input
            type="datetime-local"
            value={endTime}
            onChange={(event) => {
              setPage(1)
              setPageInput('1')
              setEndTime(event.target.value)
            }}
          />
        </label>
      </div>

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>Session ID</th>
              <th>开始时间</th>
              <th>时长</th>
              <th>摘要</th>
              <th>操作</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {list.map((item) => (
              <Fragment key={item.id}>
                <tr>
                  <td>{item.id}</td>
                  <td>{formatSessionStartTime(item.session_start_time)}</td>
                  <td>{formatDurationMinutes(item.total_duration_seconds)}</td>
                  <td>{item.summary_text ?? '-'}</td>
                  <td>
                    <div className="tool-row tool-row-inline">
                      <button
                        className="ghost"
                        onClick={() =>
                          setExpandedSessionId((old) => (old === item.id ? null : item.id))
                        }
                      >
                        {expandedSessionId === item.id ? '收起事件' : '展开事件'}
                      </button>
                      <button className="ghost" onClick={() => setSelectedSessionId(item.id)}>
                        查看回放
                      </button>
                      <button
                        className="ghost"
                        onClick={() => void handleReanalyze(item.id)}
                        disabled={analyzingSessionIds.has(item.id)}
                      >
                        {analyzingSessionIds.has(item.id) ? '重新识别中...' : '重新识别'}
                      </button>
                    </div>
                  </td>
                  <td>
                    <StatusTag status={item.analysis_status} />
                  </td>
                </tr>
                {expandedSessionId === item.id ? (
                  <SessionEventsRow
                    session={item}
                    onOpenEventDetail={(eventId) => navigate(`/events/${eventId}`)}
                  />
                ) : null}
              </Fragment>
            ))}
            {list.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-cell">
                  暂无符合条件的事件
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
              setPage(nextPage)
              setPageInput(String(nextPage))
            }}
          >
            上一页
          </button>
          <span>
            第 {page} / {totalPages} 页，共 {total} 条
          </span>
          <input
            className="pager-input"
            type="number"
            min={1}
            max={totalPages}
            value={pageInput}
            onChange={(event) => setPageInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                jumpToPage()
              }
            }}
          />
          <button className="ghost" onClick={jumpToPage}>
            跳转
          </button>
          <button
            className="ghost"
            disabled={page >= totalPages}
            onClick={() => {
              const nextPage = Math.min(totalPages, page + 1)
              setPage(nextPage)
              setPageInput(String(nextPage))
            }}
          >
            下一页
          </button>
        </div>
      </div>

      <SessionPlaybackModal
        sessionId={selectedSessionId}
        open={selectedSessionId !== null}
        onClose={() => setSelectedSessionId(null)}
      />
    </div>
  )
}

function formatAnalyzeErrorMessage(error: Error): string {
  const text = error.message || ''
  if (text.includes('Session is open')) {
    return '该 Session 仍在采集中，暂不能重新识别。'
  }
  if (text.includes('Session is analyzing')) {
    return '该 Session 正在识别中，请稍后再试。'
  }
  if (text.includes('Session not found')) {
    return 'Session 不存在，可能已被删除。'
  }
  return text
}

function formatDurationMinutes(seconds: number | null): string {
  if (seconds === null) {
    return '-'
  }

  const minutes = (seconds / 60).toFixed(1).replace(/\.0$/, '')
  return `${minutes} 分钟`
}

type SessionEventsRowProps = {
  session: VideoSession
  onOpenEventDetail: (eventId: number) => void
}

function SessionEventsRow({ session, onOpenEventDetail }: SessionEventsRowProps) {
  const eventsQuery = useQuery({
    queryKey: ['session-events', session.id],
    queryFn: () => getSessionEvents(session.id, 'asc'),
  })

  return (
    <tr>
      <td colSpan={6}>
        <div className="session-events-wrap">
          {eventsQuery.isLoading ? <LoadingBlock text="加载事件中" /> : null}
          {eventsQuery.error ? <ApiErrorAlert message={(eventsQuery.error as Error).message} /> : null}

          {!eventsQuery.isLoading && !eventsQuery.error ? (
            <table className="table table-sub">
              <thead>
                <tr>
                  <th>发生时间</th>
                  <th>事件</th>
                  <th>重要性</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {(eventsQuery.data ?? []).map((event) => (
                  <tr key={event.id}>
                    <td>{formatSessionStartTime(event.event_start_time)}</td>
                    <td>{event.title ?? event.summary ?? event.description}</td>
                    <td>{event.importance_level ?? '-'}</td>
                    <td>
                      <button className="ghost" onClick={() => onOpenEventDetail(event.id)}>
                        事件详情
                      </button>
                    </td>
                  </tr>
                ))}
                {(eventsQuery.data ?? []).length === 0 ? (
                  <tr>
                    <td colSpan={4} className="empty-cell">
                      该 Session 暂无事件
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          ) : null}
        </div>
      </td>
    </tr>
  )
}
