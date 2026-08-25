import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { HlsVideoPlayer } from '../../components/common/HlsVideoPlayer'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import { getSessionPlayback, getSessions } from './api'

export function SessionsPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedPage = Number(searchParams.get('page'))
  const page = Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1
  const [sourceId, setSourceId] = useState('')
  const [analysisStatus, setAnalysisStatus] = useState('')
  const [selectedSessionId, setSelectedSessionId] = useState<number | null>(null)

  const setPage = (nextPage: number | ((currentPage: number) => number)) => {
    const resolvedPage = typeof nextPage === 'function' ? nextPage(page) : nextPage
    const next = new URLSearchParams(searchParams)
    if (resolvedPage <= 1) {
      next.delete('page')
    } else {
      next.set('page', String(resolvedPage))
    }
    setSearchParams(next)
  }

  const queryKey = useMemo(
    () => ['sessions', { page, sourceId, analysisStatus }],
    [page, sourceId, analysisStatus],
  )

  const listQuery = useQuery({
    queryKey,
    queryFn: () => getSessions({ page, pageSize: 20, sourceId, analysisStatus }),
  })

  const playbackQuery = useQuery({
    queryKey: ['session-playback', selectedSessionId],
    queryFn: () => getSessionPlayback(selectedSessionId as number),
    enabled: selectedSessionId !== null,
  })

  if (listQuery.isLoading) {
    return <LoadingBlock text={t('sessions.loading')} />
  }

  if (listQuery.error) {
    return <ApiErrorAlert message={(listQuery.error as Error).message} />
  }

  const list = listQuery.data?.list ?? []
  const total = listQuery.data?.pagination.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / 20))

  return (
    <div>
      <PageHeader title={t('sessions.title')} subtitle={t('sessions.subtitle')} />

      <div className="card tool-row tool-row-inline">
        <label>
          {t('sessions.filter_by_source_id')}
          <input
            value={sourceId}
            onChange={(event) => {
              setSourceId(event.target.value)
              setPage(1)
            }}
            placeholder={t('sessions.filter_by_source_id_placeholder')}
          />
        </label>
        <label>
          {t('sessions.filter_analysis_status')}
          <select
            value={analysisStatus}
            onChange={(event) => {
              setAnalysisStatus(event.target.value)
              setPage(1)
            }}
          >
            <option value="">{t('sessions.filter_all')}</option>
            <option value="open">{t('common.status_open')}</option>
            <option value="sealed">{t('common.status_sealed')}</option>
            <option value="analyzing">{t('common.status_analyzing')}</option>
            <option value="partial">{t('common.status_partial')}</option>
            <option value="success">{t('common.status_success')}</option>
            <option value="failed">{t('common.status_failed')}</option>
          </select>
        </label>
      </div>

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>{t('sessions.table_col_id')}</th>
              <th>{t('sessions.table_col_source')}</th>
              <th>{t('sessions.table_col_start_time')}</th>
              <th>{t('sessions.table_col_end_time')}</th>
              <th>{t('sessions.table_col_duration')}</th>
              <th>{t('sessions.table_col_activity')}</th>
              <th>{t('sessions.table_col_important')}</th>
              <th>{t('sessions.table_col_summary')}</th>
              <th>{t('sessions.table_col_status')}</th>
              <th>{t('sessions.table_col_actions')}</th>
            </tr>
          </thead>
          <tbody>
            {list.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.source_id}</td>
                <td>{item.session_start_time}</td>
                <td>{item.session_end_time}</td>
                <td>{item.total_duration_seconds ?? '-'}</td>
                <td>{item.activity_level ?? '-'}</td>
                <td>
                  {item.has_important_event === null
                    ? '-'
                    : item.has_important_event
                    ? t('sessions.yes')
                    : t('sessions.no')}
                </td>
                <td>{item.summary_text ?? '-'}</td>
                <td>
                  <StatusTag status={item.analysis_status} />
                </td>
                <td>
                  <button className="ghost" onClick={() => setSelectedSessionId(item.id)}>
                    {t('sessions.action_view_playback')}
                  </button>
                  {item.analysis_status === 'partial' ? (
                    <span className="status-tag status-partial">{t('common.status_retryable')}</span>
                  ) : null}
                </td>
              </tr>
            ))}
            {list.length === 0 ? (
              <tr>
                <td colSpan={10} className="empty-cell">
                  {t('sessions.empty')}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>

        <div className="pager">
          <button className="ghost" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
            {t('sessions.pager_prev')}
          </button>
          <span>
            {t('sessions.pager_format', { page, totalPages, total })}
          </span>
          <button
            className="ghost"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          >
            {t('sessions.pager_next')}
          </button>
        </div>
      </div>

      {selectedSessionId !== null ? (
        <div className="card playback-card">
          <div className="playback-head">
            <h3>{t('sessions.playback_title', { id: selectedSessionId })}</h3>
            <button className="ghost" onClick={() => setSelectedSessionId(null)}>
              {t('sessions.playback_close')}
            </button>
          </div>

          {playbackQuery.isLoading ? <LoadingBlock text={t('sessions.playback_loading')} /> : null}
          {playbackQuery.error ? <ApiErrorAlert message={(playbackQuery.error as Error).message} /> : null}

          {!playbackQuery.isLoading && !playbackQuery.error ? (
            <div className="playback-grid">
              {playbackQuery.data?.playback_url ? (
                <article className="playback-item">
                  <h4>{t('sessions.merged_playback')}</h4>
                  <HlsVideoPlayer
                    src={playbackQuery.data.playback_url}
                  />
                </article>
              ) : (
                <div className="empty-cell">{t('sessions.playback_empty')}</div>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
