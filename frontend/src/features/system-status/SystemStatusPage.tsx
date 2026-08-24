import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { StatusTag } from '../../components/common/StatusTag'
import { onboardingRouteByAction } from '../onboarding/routes'
import { fetchSystemStatusOverview } from './api'

function mapOverallStatusLabel(status: string, t: TFunction): string {
  if (status === 'full_ready') {
    return t('system_status.full_ready')
  }
  if (status === 'basic_ready') {
    return t('system_status.basic_ready')
  }
  return t('system_status.not_ready')
}

function mapBoolLabel(value: boolean, t: TFunction): string {
  return value ? t('system_status.completed') : t('system_status.uncompleted')
}

function mapBoolStatus(value: boolean): 'success' | 'failed' {
  return value ? 'success' : 'failed'
}

function mapTaskStatus(status: string | undefined): string {
  if (!status) {
    return 'pending'
  }
  if (status === 'running') {
    return 'analyzing'
  }
  return status
}

function mapTaskTypeLabel(taskType: string, t: TFunction): string {
  if (taskType === 'session_build') {
    return t('tasks.task_session_build')
  }
  if (taskType === 'session_analysis') {
    return t('tasks.task_session_analysis')
  }
  if (taskType === 'daily_summary_generation') {
    return t('tasks.task_daily_summary_generation')
  }
  return t('tasks.task_other')
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

export function SystemStatusPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const query = useQuery({
    queryKey: ['system-status-overview'],
    queryFn: fetchSystemStatusOverview,
  })

  const latestStatus = useMemo(() => {
    if (!query.data) {
      return null
    }

    const now = query.dataUpdatedAt || 0

    const latestBuild = query.data.taskLogs.find((item) => item.task_type === 'session_build')
    const latestAnalysis = query.data.taskLogs.find((item) => item.task_type === 'session_analysis')
    const latestSummary = query.data.taskLogs.find((item) => item.task_type === 'daily_summary_generation')

    const failedCount24h = query.data.taskLogs.filter((item) => {
      if (item.status !== 'failed') {
        return false
      }
      const createdAt = new Date(item.created_at)
      if (Number.isNaN(createdAt.getTime())) {
        return false
      }
      return now - createdAt.getTime() <= 24 * 60 * 60 * 1000
    }).length

    const recentFailedTasks = query.data.taskLogs
      .filter((item) => item.status === 'failed')
      .slice(0, 3)

    return {
      latestBuild,
      latestAnalysis,
      latestSummary,
      failedCount24h,
      recentFailedTasks,
    }
  }, [query.data, query.dataUpdatedAt])

  if (query.isLoading) {
    return <LoadingBlock text={t('system_status.loading')} />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  if (!query.data || !latestStatus) {
    return <ApiErrorAlert message={t('system_status.error_fetch')} />
  }

  const { onboarding, videoSources, providers, homeProfile, systemConfig, videoPipelineHealth, alertSources } =
    query.data

  const enabledVideoSources = videoSources.filter((item) => item.enabled)
  const validatedVideoSources = enabledVideoSources.filter((item) => item.last_validate_status === 'success')
  const enabledProviders = providers.filter((item) => item.enabled)
  const testedProviders = enabledProviders.filter((item) => item.last_test_status === 'success')

  return (
    <div>
      <PageHeader
        title={t('system_status.title')}
        subtitle={t('system_status.subtitle')}
      />

      <article className="card">
        <h3>{t('system_status.onboarding_status_title')}</h3>
        <div className="dashboard-kv-list">
          <div>
            <span>{t('system_status.overall_status')}</span>
            <strong>{mapOverallStatusLabel(onboarding.overall_status, t)}</strong>
          </div>
          <div>
            <span>{t('system_status.basic_ready_status')}</span>
            <strong>{mapBoolLabel(onboarding.basic_ready, t)}</strong>
          </div>
          <div>
            <span>{t('system_status.full_ready_status')}</span>
            <strong>{mapBoolLabel(onboarding.full_ready, t)}</strong>
          </div>
        </div>

        <table className="table">
          <thead>
            <tr>
              <th>{t('system_status.col_step')}</th>
              <th>{t('system_status.col_status')}</th>
              <th>{t('system_status.col_actions')}</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>{t('system_status.step_video_source')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.video_source.configured)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/video-sources')}>
                  {t('system_status.go_video_sources')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_video_validate')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.video_source.validated)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/video-sources')}>
                  {t('system_status.go_video_sources')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_provider')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.provider.configured)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/providers')}>
                  {t('system_status.go_providers')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_provider_test')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.provider.tested)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/providers')}>
                  {t('system_status.go_providers')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_daily_summary')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.daily_summary.configured)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/system-config')}>
                  {t('system_status.go_system_config')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_home_profile')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.home_profile.configured)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/home-profile')}>
                  {t('system_status.go_home_profile')}
                </button>
              </td>
            </tr>
          </tbody>
        </table>

        <div className="row-actions" style={{ marginTop: '0.8rem' }}>
          <button onClick={() => navigate(onboardingRouteByAction(onboarding.next_action))}>
            {t('system_status.continue_onboarding')}
          </button>
          <button className="ghost" onClick={() => navigate('/onboarding')}>
            {t('system_status.view_onboarding')}
          </button>
        </div>
      </article>

      <article className="card" style={{ marginTop: '0.9rem' }}>
        <h3>{t('system_status.core_config_title')}</h3>
        <div className="dashboard-kv-list">
          <div>
            <span>{t('system_status.enabled_sources')}</span>
            <strong>{enabledVideoSources.length}</strong>
          </div>
          <div>
            <span>{t('system_status.validated_sources')}</span>
            <strong>{validatedVideoSources.length}</strong>
          </div>
          <div>
            <span>{t('system_status.enabled_providers')}</span>
            <strong>{enabledProviders.length}</strong>
          </div>
          <div>
            <span>{t('system_status.tested_providers')}</span>
            <strong>{testedProviders.length}</strong>
          </div>
          <div>
            <span>{t('system_status.system_name')}</span>
            <strong>{homeProfile.assistant_name || t('system_status.default_name')}</strong>
          </div>
          <div>
            <span>{t('system_status.daily_summary_time')}</span>
            <strong>{systemConfig.daily_summary_schedule || '-'}</strong>
          </div>
        </div>

        <div className="row-actions">
          <button className="ghost" onClick={() => navigate('/video-sources')}>
            {t('system_status.go_video_sources')}
          </button>
          <button className="ghost" onClick={() => navigate('/providers')}>
            {t('system_status.go_providers')}
          </button>
          <button className="ghost" onClick={() => navigate('/home-profile')}>
            {t('system_status.go_home_profile')}
          </button>
          <button className="ghost" onClick={() => navigate('/system-config')}>
            {t('system_status.go_system_config')}
          </button>
        </div>
      </article>

      <article className="card" style={{ marginTop: '0.9rem' }}>
        <h3>{t('system_status.recent_tasks_title')}</h3>
        <div className="dashboard-kv-list">
          <div>
            <span>{t('system_status.recent_build')}</span>
            {latestStatus.latestBuild ? (
              <StatusTag status={mapTaskStatus(latestStatus.latestBuild.status)} />
            ) : (
              <strong>-</strong>
            )}
          </div>
          <div>
            <span>{t('system_status.recent_analysis')}</span>
            {latestStatus.latestAnalysis ? (
              <StatusTag status={mapTaskStatus(latestStatus.latestAnalysis.status)} />
            ) : (
              <strong>-</strong>
            )}
          </div>
          <div>
            <span>{t('system_status.recent_summary')}</span>
            {latestStatus.latestSummary ? (
              <StatusTag status={mapTaskStatus(latestStatus.latestSummary.status)} />
            ) : (
              <strong>-</strong>
            )}
          </div>
          <div>
            <span>{t('system_status.recent_build_time')}</span>
            <strong>{formatDateTime(latestStatus.latestBuild?.created_at ?? null)}</strong>
          </div>
          <div>
            <span>{t('system_status.failed_24h')}</span>
            <strong>{latestStatus.failedCount24h}</strong>
          </div>
          <div>
            <span>{t('system_status.attention_sources')}</span>
            <strong>{videoPipelineHealth.attentionSourceCount}</strong>
          </div>
          <div>
            <span>{t('system_status.paused_sources')}</span>
            <strong>{videoPipelineHealth.pausedSourceCount}</strong>
          </div>
          <div>
            <span>{t('system_status.avg_coverage')}</span>
            <strong>
              {videoPipelineHealth.avgAnalyzedCoveragePercent !== null
                ? `${videoPipelineHealth.avgAnalyzedCoveragePercent}%`
                : '-'}
            </strong>
          </div>
          <div>
            <span>{t('system_status.max_no_video_time')}</span>
            <strong>
              {videoPipelineHealth.maxMinutesSinceLastNewVideo !== null
                ? t('common.minutes_format', { minutes: videoPipelineHealth.maxMinutesSinceLastNewVideo })
                : '-'}
            </strong>
          </div>
        </div>

        {latestStatus.recentFailedTasks.length > 0 ? (
          <div className="summary-detail" style={{ marginTop: '0.8rem' }}>
            <h4>{t('system_status.recent_failed_title')}</h4>
            {latestStatus.recentFailedTasks.map((item) => (
              <article key={item.id}>
                <p>
                  <strong>{mapTaskTypeLabel(item.task_type, t)}</strong> · {formatDateTime(item.created_at)}
                </p>
                <p>{item.message || t('system_status.no_error_msg')}</p>
              </article>
            ))}
          </div>
        ) : null}

        {alertSources.length > 0 ? (
          <div className="summary-detail" style={{ marginTop: '0.8rem' }}>
            <h4>{t('system_status.affected_sources')}</h4>
            {alertSources.map((item) => (
              <article key={item.sourceId}>
                <p>
                  <strong>{item.sourceName}</strong> · {item.cameraName}
                </p>
                <p>
                  {t('system_status.source_status')}
                  {item.analysisState === 'paused' ? t('system_status.paused') : item.analysisState === 'stopped' ? t('system_status.stopped') : t('system_status.analyzing')}
                  {item.minutesSinceLastNewVideo !== null
                    ? t('system_status.time_since_new', { minutes: item.minutesSinceLastNewVideo })
                    : ''}
                </p>
                <div className="row-actions">
                  <button
                    className="ghost"
                    onClick={() => navigate(`/video-sources?source_id=${item.sourceId}`)}
                  >
                    {t('system_status.view_source_status')}
                  </button>
                </div>
              </article>
            ))}
            <div className="row-actions">
              <button className="ghost" onClick={() => navigate('/video-sources')}>
                {t('system_status.handle_alert_sources')}
              </button>
            </div>
          </div>
        ) : null}

        <div className="row-actions">
          <button className="ghost" onClick={() => navigate('/tasks')}>
            {t('system_status.view_task_logs')}
          </button>
          <button className="ghost" onClick={() => navigate('/events')}>
            {t('system_status.view_events')}
          </button>
          <button className="ghost" onClick={() => navigate('/dashboard')}>
            {t('system_status.back_to_dashboard')}
          </button>
        </div>
      </article>
    </div>
  )
}
