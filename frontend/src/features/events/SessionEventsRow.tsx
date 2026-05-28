import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import type { VideoSession } from '../../types/api'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { getSessionEvents } from './api'
import { formatSessionStartTime } from './utils'

type SessionEventsRowProps = {
  session: VideoSession
  onOpenEventDetail: (eventId: number) => void
}

export function SessionEventsRow({ session, onOpenEventDetail }: SessionEventsRowProps) {
  const { t } = useTranslation()
  const eventsQuery = useQuery({
    queryKey: ['session-events', session.id],
    queryFn: () => getSessionEvents(session.id, 'asc'),
  })

  return (
    <tr>
      <td colSpan={6}>
        <div className="session-events-wrap">
          {eventsQuery.isLoading ? <LoadingBlock text={t('events.loading_session_events', '加载事件中')} /> : null}
          {eventsQuery.error ? <ApiErrorAlert message={(eventsQuery.error as Error).message} /> : null}

          {!eventsQuery.isLoading && !eventsQuery.error ? (
            <table className="table table-sub">
              <thead>
                <tr>
                  <th>{t('events.col_event_time', '发生时间')}</th>
                  <th>{t('events.col_event_title', '事件')}</th>
                  <th>{t('events.col_importance', '重要性')}</th>
                  <th>{t('events.col_actions', '操作')}</th>
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
                        {t('events.view_detail', '事件详情')}
                      </button>
                    </td>
                  </tr>
                ))}
                {(eventsQuery.data ?? []).length === 0 ? (
                  <tr>
                    <td colSpan={4} className="empty-cell">
                      {t('events.empty_session_events', '该 Session 暂无事件')}
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
