import { Fragment, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import { SessionPlaybackModal } from '../../components/common/SessionPlaybackModal'
import { usePagination } from '../../hooks/usePagination'
import { getSessions } from '../sessions/api'
import { triggerSessionAnalyze } from './api'
import { EventsFilterBar } from './EventsFilterBar'
import { SessionEventsRow, formatSessionStartTime } from './SessionEventsRow'
import { Pager } from './Pager'

export function EventsPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const pagination = usePagination()
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
    () => ['events', { page: pagination.page, sourceId, analysisStatus, startTime, endTime }],
    [pagination.page, sourceId, analysisStatus, startTime, endTime],
  )

  const query = useQuery({
    queryKey,
    queryFn: () =>
        getSessions({
          page: pagination.page,
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
      setMessage(t('events.reanalyze_submitted', '已提交重新识别任务'))
      queryClient.invalidateQueries({ queryKey: ['events'] })
    },
    onError: (error) => {
      setMessage(formatAnalyzeErrorMessage(error as Error, t))
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
    return <LoadingBlock text={t('events.loading', '加载事件列表中')} />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  const data = query.data
  const list = data?.list ?? []
  const total = data?.pagination.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / 20))

  return (
    <div>
      <PageHeader title={t('events.title')} subtitle={t('events.subtitle', '按 Session 归并查看事件，再展开查看具体明细')} />

      {message ? <div className="api-ok">{message}</div> : null}

      <EventsFilterBar
        sourceId={sourceId}
        analysisStatus={analysisStatus}
        startTime={startTime}
        endTime={endTime}
        onSourceIdChange={(value) => {
          pagination.resetPage()
          setSourceId(value)
        }}
        onAnalysisStatusChange={(value) => {
          pagination.resetPage()
          setAnalysisStatus(value)
        }}
        onStartTimeChange={(value) => {
          pagination.resetPage()
          setStartTime(value)
        }}
        onEndTimeChange={(value) => {
          pagination.resetPage()
          setEndTime(value)
        }}
      />

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>Session ID</th>
              <th>{t('events.col_start_time', '开始时间')}</th>
              <th>{t('events.col_duration', '时长')}</th>
              <th>{t('events.col_summary', '摘要')}</th>
              <th>{t('events.col_actions', '操作')}</th>
              <th>{t('events.col_status', '状态')}</th>
            </tr>
          </thead>
          <tbody>
            {list.map((item) => (
              <Fragment key={item.id}>
                <tr>
                  <td>{item.id}</td>
                  <td>{formatSessionStartTime(item.session_start_time)}</td>
                  <td>{formatDurationMinutes(item.total_duration_seconds, t)}</td>
                  <td>{item.summary_text ?? '-'}</td>
                  <td>
                    <div className="tool-row tool-row-inline">
                      <button
                        className="ghost"
                        onClick={() =>
                          setExpandedSessionId((old) => (old === item.id ? null : item.id))
                        }
                      >
                        {expandedSessionId === item.id ? t('events.collapse_events', '收起事件') : t('events.expand_events', '展开事件')}
                      </button>
                      <button className="ghost" onClick={() => setSelectedSessionId(item.id)}>
                        {t('events.view_playback', '查看回放')}
                      </button>
                      <button
                        className="ghost"
                        onClick={() => void handleReanalyze(item.id)}
                        disabled={analyzingSessionIds.has(item.id)}
                      >
                        {analyzingSessionIds.has(item.id) ? t('events.reanalyzing', '重新识别中...') : t('events.reanalyze', '重新识别')}
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
                  {t('events.empty')}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>

        <Pager
          page={pagination.page}
          totalPages={totalPages}
          total={total}
          pageInput={pagination.pageInput}
          onPageInputChange={pagination.setPageInput}
          onJumpToPage={() => pagination.jumpToPage(totalPages)}
          onPrev={pagination.goToPrev}
          onNext={() => pagination.goToNext(totalPages)}
        />
      </div>

      <SessionPlaybackModal
        sessionId={selectedSessionId}
        open={selectedSessionId !== null}
        onClose={() => setSelectedSessionId(null)}
      />
    </div>
  )
}

function formatAnalyzeErrorMessage(error: Error, t: TFunction): string {
  const text = error.message || ''
  if (text.includes('Session is open')) {
    return t('events.error_session_open', '该 Session 仍在采集中，暂不能重新识别。')
  }
  if (text.includes('Session is analyzing')) {
    return t('events.error_session_analyzing', '该 Session 正在识别中，请稍后再试。')
  }
  if (text.includes('Session not found')) {
    return t('events.error_session_not_found', 'Session 不存在，可能已被删除。')
  }
  return text
}

function formatDurationMinutes(seconds: number | null, t: TFunction): string {
  if (seconds === null) {
    return '-'
  }

  const minutes = (seconds / 60).toFixed(1).replace(/\.0$/, '')
  return t('events.minutes_format', '{{minutes}} 分钟', { minutes })
}
