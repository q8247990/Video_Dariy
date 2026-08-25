import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { fetchDashboardOverview } from './api'
import type { DashboardSystemStatusItem } from '../../types/api'

function statusText(status: DashboardSystemStatusItem['status'], t: (key: string) => string): string {
  if (status === 'ok') {
    return t('dashboard.status_ok')
  }
  if (status === 'partial') {
    return t('dashboard.status_partial')
  }
  if (status === 'error') {
    return t('dashboard.status_error')
  }
  return t('dashboard.status_uncompleted')
}

function formatDateTime(value: string | null): string {
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
  return `${year}-${month}-${day} ${hour}:${minute}`
}

export function DashboardPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { data, isLoading, error } = useQuery({
    queryKey: ['dashboard-overview'],
    queryFn: fetchDashboardOverview,
  })

  const quickActions = [
    { key: 'events', label: t('layouts.events'), target: '/events' },
    { key: 'daily_summary', label: t('layouts.daily_summary'), target: '/daily-summaries' },
    { key: 'qa', label: t('layouts.chat'), target: '/chat' },
    { key: 'home_profile', label: t('layouts.profile'), target: '/home-profile' },
    { key: 'settings', label: t('layouts.settings'), target: '/settings' },
    { key: 'onboarding', label: t('dashboard.onboarding_guide'), target: '/onboarding' },
  ]

  if (isLoading) {
    return <LoadingBlock text={t('dashboard.loading_overview')} />
  }

  if (error) {
    return <ApiErrorAlert message={(error as Error).message} />
  }

  if (!data) {
    return <ApiErrorAlert message={t('common.load_failed_retry')} />
  }

  const showDetailAction =
    data.system_status.primary_action.label !== data.system_status.detail_action.label ||
    data.system_status.primary_action.target !== data.system_status.detail_action.target

  return (
    <div>
      <PageHeader title={`${data.assistant_name} · ${t('dashboard.title')}`} />

      <article className="card dashboard-status-card">
        <div className="dashboard-status-head">
          <div>
            <h3>{data.system_status.title}</h3>
            <p className="text-muted">{data.system_status.description}</p>
          </div>
          <div className="dashboard-status-actions">
            <button onClick={() => navigate(data.system_status.primary_action.target)}>
              {data.system_status.primary_action.label}
            </button>
            {showDetailAction ? (
              <button
                className="ghost"
                onClick={() => navigate(data.system_status.detail_action.target)}
              >
                {data.system_status.detail_action.label}
              </button>
            ) : null}
          </div>
        </div>

        <ul className="dashboard-status-items">
          {data.system_status.items.map((item) => (
            <li key={item.key}>
              <span>{item.label}</span>
              <strong className={`dashboard-item-status dashboard-item-${item.status}`}>
                {statusText(item.status, t)}
              </strong>
            </li>
          ))}
        </ul>
      </article>

      {data.alert.show ? (
        <article className="card dashboard-alert-card">
          <div>
            <h3>{data.alert.title}</h3>
            <p>{data.alert.description}</p>
          </div>
          {(() => {
            const action = data.alert.action
            if (!action) {
              return null
            }
            return <button onClick={() => navigate(action.target)}>{action.label}</button>
          })()}
        </article>
      ) : null}

      <div className="grid-two dashboard-main-grid">
        <article className="card">
          <h3>{t('dashboard.events_overview_title')}</h3>
          <div className="dashboard-kv-list">
            <div>
              <span>{t('dashboard.today_event_count')}</span>
              <strong>{data.event_summary.today_event_count}</strong>
            </div>
            <div>
              <span>{t('dashboard.yesterday_event_count')}</span>
              <strong>{data.event_summary.yesterday_event_count}</strong>
            </div>
            <div>
              <span>{t('dashboard.important_events_24h')}</span>
              <strong>{data.event_summary.important_event_count_24h}</strong>
            </div>
          </div>
          <button className="ghost" onClick={() => navigate('/events')}>
            {t('dashboard.go_events')}
          </button>
        </article>

        <article className="card">
          <h3>{t('dashboard.recent_tasks_title')}</h3>
          <ul className="list-simple dashboard-text-list">
            <li>
              <span>{t('dashboard.last_scan_at')}</span>
              <strong>{formatDateTime(data.task_summary.last_scan_at)}</strong>
            </li>
            <li>
              <span>{t('dashboard.last_analysis_status')}</span>
              <strong>{data.task_summary.last_analysis_status ?? '-'}</strong>
            </li>
            <li>
              <span>{t('dashboard.last_daily_summary_status')}</span>
              <strong>{data.task_summary.last_daily_summary_status ?? '-'}</strong>
            </li>
            <li>
              <span>{t('dashboard.failed_tasks_24h')}</span>
              <strong>{data.task_summary.failed_task_count_24h}</strong>
            </li>
          </ul>
        </article>

        <article className="card">
          <h3>{t('dashboard.important_events_title')}</h3>
          {data.important_events.length > 0 ? (
            <ul className="list-simple dashboard-important-list">
              {data.important_events.map((item) => (
                <li key={item.id}>
                  <button className="ghost" onClick={() => navigate(`/events/${item.id}`)}>
                    {t('dashboard.view_detail')}
                  </button>
                  <p>{item.title}</p>
                  <small>{item.summary}</small>
                  <small>
                    {formatDateTime(item.event_time)} · {item.camera_name}
                  </small>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted">{t('dashboard.no_important_events')}</p>
          )}
        </article>

        <article className="card">
          <h3>{t('dashboard.latest_summary_title')}</h3>
          {data.latest_daily_summary.exists ? (
            <div className="dashboard-latest-summary">
              <small>{data.latest_daily_summary.date}</small>
              <p>{data.latest_daily_summary.summary_preview}</p>
              <button className="ghost" onClick={() => navigate('/daily-summaries')}>
                {t('dashboard.view_summary')}
              </button>
            </div>
          ) : (
            <div className="dashboard-latest-summary">
              <p>{t('dashboard.no_summary')}</p>
              <small>
                {data.latest_daily_summary.empty_reason === 'failed'
                  ? t('dashboard.summary_failed_reason')
                  : t('dashboard.summary_pending_reason')}
              </small>
              <button className="ghost" onClick={() => navigate('/daily-summaries')}>
                {t('dashboard.go_daily_summaries')}
              </button>
            </div>
          )}
        </article>
      </div>

      <article className="card dashboard-actions-card">
        <h3>{t('dashboard.quick_actions_title')}</h3>
        <div className="dashboard-actions-grid">
          {quickActions.map((action) => (
            <button key={action.key} className="ghost" onClick={() => navigate(action.target)}>
              {action.label}
            </button>
          ))}
        </div>
      </article>
    </div>
  )
}
