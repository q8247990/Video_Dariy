import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { SessionPlaybackModal } from '../../components/common/SessionPlaybackModal'
import { getEventDetail, getSessionEvents, triggerSessionAnalyze } from './api'

function formatDateTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  const second = String(date.getSeconds()).padStart(2, '0')
  return `${year}-${month}-${day} ${hour}:${minute}:${second}`
}

export function EventDetailPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const params = useParams<{ id: string }>()
  const eventId = Number(params.id)
  const [playbackSessionId, setPlaybackSessionId] = useState<number | null>(null)

  const detailQuery = useQuery({
    queryKey: ['event-detail', eventId],
    queryFn: () => getEventDetail(eventId),
    enabled: Number.isFinite(eventId) && eventId > 0,
  })

  const sessionEventsQuery = useQuery({
    queryKey: ['session-events', detailQuery.data?.session_id],
    queryFn: () => getSessionEvents(detailQuery.data!.session_id, 'asc'),
    enabled: Boolean(detailQuery.data?.session_id),
  })

  const reanalyzeMutation = useMutation({
    mutationFn: (sessionId: number) => triggerSessionAnalyze(sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['session-events', detailQuery.data?.session_id] })
      queryClient.invalidateQueries({ queryKey: ['events'] })
    },
  })

  if (!Number.isFinite(eventId) || eventId <= 0) {
    return <ApiErrorAlert message={t('events.error_invalid_id', '无效事件ID')} />
  }

  if (detailQuery.isLoading) {
    return <LoadingBlock text={t('events.loading_detail', '正在加载事件详情')} />
  }

  if (detailQuery.error) {
    return <ApiErrorAlert message={(detailQuery.error as Error).message} />
  }

  if (!detailQuery.data) {
    return <ApiErrorAlert message={t('events.error_no_detail', '未获取到事件详情')} />
  }

  const detail = detailQuery.data
  const relatedEntities = detail.related_entities_json ?? []
  const observedActions = detail.observed_actions_json ?? []
  const interpretedStates = detail.interpreted_state_json ?? []

  return (
    <div>
      <PageHeader
        title={t('events.detail_title', '事件详情 #{{id}}', { id: detail.id })}
        subtitle={`${detail.camera_name} · ${detail.location_name}`}
        actions={
          <div className="row-actions">
            <button className="ghost" onClick={() => navigate('/events')}>
              {t('events.back_to_list', '返回事件列表')}
            </button>
            <button className="ghost" onClick={() => setPlaybackSessionId(detail.session_id)}>
              {t('events.view_playback', '查看回放')}
            </button>
            <button
              className="ghost"
              disabled={reanalyzeMutation.isPending}
              onClick={() => reanalyzeMutation.mutate(detail.session_id)}
            >
              {reanalyzeMutation.isPending ? t('events.reanalyzing', '重新识别中...') : t('events.reanalyze_session', '重新识别该Session')}
            </button>
          </div>
        }
      />

      <article className="card dashboard-text-list">
        <p>
          <strong>{t('events.detail_field_title', '标题：')}</strong>
          {detail.title ?? '-'}
        </p>
        <p>
          <strong>{t('events.detail_field_summary', '摘要：')}</strong>
          {detail.summary ?? detail.description}
        </p>
        <p>
          <strong>{t('events.detail_field_description', '详细记录：')}</strong>
          {detail.detail ?? detail.summary ?? detail.description}
        </p>
        <p>
          <strong>{t('events.detail_field_time', '发生时间：')}</strong>
          {formatDateTime(detail.event_start_time)}
        </p>
        <p>
          <strong>{t('events.detail_field_object_type', '对象类型：')}</strong>
          {detail.object_type ?? '-'}
        </p>
        <p>
          <strong>{t('events.detail_field_event_type', '事件类型：')}</strong>
          {detail.event_type ?? detail.action_type ?? '-'}
        </p>
        <p>
          <strong>{t('events.detail_field_importance', '重要性：')}</strong>
          {detail.importance_level ?? '-'}
        </p>
        <p>
          <strong>{t('events.detail_field_relative_time', '相对时间：')}</strong>
          {detail.offset_start_sec !== null && detail.offset_end_sec !== null
            ? `${detail.offset_start_sec}s ~ ${detail.offset_end_sec}s`
            : '-'}
        </p>
        <p>
          <strong>{t('events.detail_field_observed_actions', '观察动作：')}</strong>
          {observedActions.length > 0 ? observedActions.join('、') : '-'}
        </p>
        <p>
          <strong>{t('events.detail_field_interpreted_states', '解释状态：')}</strong>
          {interpretedStates.length > 0 ? interpretedStates.join('、') : '-'}
        </p>
        <p>
          <strong>{t('events.detail_field_related_entities', '关联对象：')}</strong>
          {relatedEntities.length > 0
            ? relatedEntities
                .map((item) => {
                  const name = String(item.display_name ?? item.matched_profile_name ?? t('events.unnamed_entity', '未命名对象'))
                  const status = String(item.recognition_status ?? 'unknown')
                  return `${name}(${status})`
                })
                .join('、')
            : '-'}
        </p>
        <p>
          <strong>{t('events.detail_field_session_id', '会话ID：')}</strong>
          {detail.session_id}
        </p>
        <p>
          <strong>{t('events.detail_field_tags', '标签：')}</strong>
          {detail.tags && detail.tags.length > 0
            ? detail.tags.map((tag) => tag.tag_name).join('、')
            : t('events.none', '无')}
        </p>
      </article>

      <article className="card summary-detail">
        <h3>{t('events.all_session_events', '同 Session 全部事件（Session #{{id}}）', { id: detail.session_id })}</h3>
        {sessionEventsQuery.isLoading ? <LoadingBlock text={t('events.loading_session_events', '加载同会话事件中')} /> : null}
        {sessionEventsQuery.error ? (
          <ApiErrorAlert message={(sessionEventsQuery.error as Error).message} />
        ) : null}
        {!sessionEventsQuery.isLoading && !sessionEventsQuery.error ? (
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
              {(sessionEventsQuery.data ?? []).map((item) => (
                <tr key={item.id} className={item.id === detail.id ? 'row-selected' : ''}>
                  <td>{formatDateTime(item.event_start_time)}</td>
                  <td>{item.title ?? item.summary ?? item.description}</td>
                  <td>{item.importance_level ?? '-'}</td>
                  <td>
                    <button className="ghost" onClick={() => navigate(`/events/${item.id}`)}>
                      {t('events.view_detail', '查看事件')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </article>

      <article className="card summary-detail">
        <h3>{t('events.raw_result_title', '原始识别结果')}</h3>
        <article>{JSON.stringify(detail.raw_result, null, 2)}</article>
      </article>

      <SessionPlaybackModal
        sessionId={playbackSessionId}
        open={playbackSessionId !== null}
        onClose={() => setPlaybackSessionId(null)}
      />
    </div>
  )
}
