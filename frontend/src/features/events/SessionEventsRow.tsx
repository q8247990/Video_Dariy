import { useQuery } from '@tanstack/react-query'
import type { VideoSession } from '../../types/api'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { getSessionEvents } from './api'

export function formatSessionStartTime(value: string | null): string {
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

type SessionEventsRowProps = {
  session: VideoSession
  onOpenEventDetail: (eventId: number) => void
}

export function SessionEventsRow({ session, onOpenEventDetail }: SessionEventsRowProps) {
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
