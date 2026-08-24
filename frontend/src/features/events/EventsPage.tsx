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
import { SessionEventsRow } from './SessionEventsRow'
import { formatSessionStartTime } from './utils'
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
      setMessage(t('events.reanalyze_submitted'))
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
    return <LoadingBlock text={t('events.loading')} />
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
      <PageHeader title={t('events.title')} subtitle={t('events.subtitle')} />

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
              <th>{t('events.col_session_id')}</th>
              <th>{t('events.col_start_time')}</th>
              <th>{t('events.col_duration')}</th>
              <th>{t('events.col_summary')}</th>
              <th>{t('events.col_actions')}</th>
              <th>{t('events.col_status')}</th>
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
                        {expandedSessionId === item.id ? t('events.collapse_events') : t('events.expand_events')}
                      </button>
                      <button className="ghost" onClick={() => setSelectedSessionId(item.id)}>
                        {t('events.view_playback')}
                      </button>
                      <button
                        className="ghost"
                        onClick={() => void handleReanalyze(item.id)}
                        disabled={analyzingSessionIds.has(item.id)}
                      >
                        {analyzingSessionIds.has(item.id) ? t('events.reanalyzing') : t('events.reanalyze')}
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
    return t('events.error_session_open')
  }
  if (text.includes('Session is analyzing')) {
    return t('events.error_session_analyzing')
  }
  if (text.includes('Session not found')) {
    return t('events.error_session_not_found')
  }
  return text
}

function formatDurationMinutes(seconds: number | null, t: TFunction): string {
  if (seconds === null) {
    return '-'
  }

  const minutes = (seconds / 60).toFixed(1).replace(/\.0$/, '')
  return t('events.minutes_format', { minutes })
}
