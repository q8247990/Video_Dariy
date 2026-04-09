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
    return t('system_status.full_ready', '完整可运行')
  }
  if (status === 'basic_ready') {
    return t('system_status.basic_ready', '基础可运行')
  }
  return t('system_status.not_ready', '未完成基础配置')
}

function mapBoolLabel(value: boolean, t: TFunction): string {
  return value ? t('system_status.completed', '已完成') : t('system_status.uncompleted', '未完成')
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
    return <LoadingBlock text={t('system_status.loading', '正在加载运行状态详情')} />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  if (!query.data || !latestStatus) {
    return <ApiErrorAlert message={t('system_status.error_fetch', '未获取到运行状态详情')} />
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
        subtitle={t('system_status.subtitle', '查看初始化完成度、核心配置状态与最近运行状态')}
      />

      <article className="card">
        <h3>{t('system_status.onboarding_status_title', '初始化状态区')}</h3>
        <div className="dashboard-kv-list">
          <div>
            <span>{t('system_status.overall_status', '系统主状态')}</span>
            <strong>{mapOverallStatusLabel(onboarding.overall_status, t)}</strong>
          </div>
          <div>
            <span>{t('system_status.basic_ready_status', '基础可运行')}</span>
            <strong>{mapBoolLabel(onboarding.basic_ready, t)}</strong>
          </div>
          <div>
            <span>{t('system_status.full_ready_status', '完整可运行')}</span>
            <strong>{mapBoolLabel(onboarding.full_ready, t)}</strong>
          </div>
        </div>

        <table className="table">
          <thead>
            <tr>
              <th>{t('system_status.col_step', '步骤')}</th>
              <th>{t('system_status.col_status', '状态')}</th>
              <th>{t('system_status.col_actions', '操作')}</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>{t('system_status.step_video_source', '视频源配置')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.video_source.configured)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/video-sources')}>
                  {t('system_status.action_handle', '去处理')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_video_validate', '视频源校验')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.video_source.validated)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/video-sources')}>
                  {t('system_status.action_handle', '去处理')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_provider', 'Provider 配置')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.provider.configured)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/providers')}>
                  {t('system_status.action_handle', '去处理')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_provider_test', 'Provider 测试')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.provider.tested)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/providers')}>
                  {t('system_status.action_handle', '去处理')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_daily_summary', '日报配置')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.daily_summary.configured)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/system-config')}>
                  {t('system_status.action_handle', '去处理')}
                </button>
              </td>
            </tr>
            <tr>
              <td>{t('system_status.step_home_profile', '家庭档案')}</td>
              <td>
                <StatusTag status={mapBoolStatus(onboarding.steps.home_profile.configured)} />
              </td>
              <td>
                <button className="ghost" onClick={() => navigate('/home-profile')}>
                  {t('system_status.action_handle', '去处理')}
                </button>
              </td>
            </tr>
          </tbody>
        </table>

        <div className="row-actions" style={{ marginTop: '0.8rem' }}>
          <button onClick={() => navigate(onboardingRouteByAction(onboarding.next_action))}>
            {t('system_status.continue_onboarding', '继续完成初始化')}
          </button>
          <button className="ghost" onClick={() => navigate('/onboarding')}>
            {t('system_status.view_onboarding', '查看初始化引导')}
          </button>
        </div>
      </article>

      <article className="card" style={{ marginTop: '0.9rem' }}>
        <h3>{t('system_status.core_config_title', '核心配置状态区')}</h3>
        <div className="dashboard-kv-list">
          <div>
            <span>{t('system_status.enabled_sources', '启用视频源')}</span>
            <strong>{enabledVideoSources.length}</strong>
          </div>
          <div>
            <span>{t('system_status.validated_sources', '校验通过视频源')}</span>
            <strong>{validatedVideoSources.length}</strong>
          </div>
          <div>
            <span>{t('system_status.enabled_providers', '启用 Provider')}</span>
            <strong>{enabledProviders.length}</strong>
          </div>
          <div>
            <span>{t('system_status.tested_providers', '测试通过 Provider')}</span>
            <strong>{testedProviders.length}</strong>
          </div>
          <div>
            <span>{t('system_status.system_name', '系统名称')}</span>
            <strong>{homeProfile.assistant_name || t('system_status.default_name', '家庭助手')}</strong>
          </div>
          <div>
            <span>{t('system_status.daily_summary_time', '日报生成时间')}</span>
            <strong>{systemConfig.daily_summary_schedule || '-'}</strong>
          </div>
        </div>

        <div className="row-actions">
          <button className="ghost" onClick={() => navigate('/video-sources')}>
            {t('system_status.go_video_sources', '去视频源管理')}
          </button>
          <button className="ghost" onClick={() => navigate('/providers')}>
            {t('system_status.go_providers', '去 Provider 管理')}
          </button>
          <button className="ghost" onClick={() => navigate('/home-profile')}>
            {t('system_status.go_home_profile', '去家庭档案')}
          </button>
          <button className="ghost" onClick={() => navigate('/system-config')}>
            {t('system_status.go_system_config', '去系统配置')}
          </button>
        </div>
      </article>

      <article className="card" style={{ marginTop: '0.9rem' }}>
        <h3>{t('system_status.recent_tasks_title', '最近运行状态区')}</h3>
        <div className="dashboard-kv-list">
          <div>
            <span>{t('system_status.recent_build', '最近构建任务')}</span>
            {latestStatus.latestBuild ? (
              <StatusTag status={mapTaskStatus(latestStatus.latestBuild.status)} />
            ) : (
              <strong>-</strong>
            )}
          </div>
          <div>
            <span>{t('system_status.recent_analysis', '最近分析任务')}</span>
            {latestStatus.latestAnalysis ? (
              <StatusTag status={mapTaskStatus(latestStatus.latestAnalysis.status)} />
            ) : (
              <strong>-</strong>
            )}
          </div>
          <div>
            <span>{t('system_status.recent_summary', '最近日报任务')}</span>
            {latestStatus.latestSummary ? (
              <StatusTag status={mapTaskStatus(latestStatus.latestSummary.status)} />
            ) : (
              <strong>-</strong>
            )}
          </div>
          <div>
            <span>{t('system_status.recent_build_time', '最近构建时间')}</span>
            <strong>{formatDateTime(latestStatus.latestBuild?.created_at ?? null)}</strong>
          </div>
          <div>
            <span>{t('system_status.failed_24h', '24小时失败任务数')}</span>
            <strong>{latestStatus.failedCount24h}</strong>
          </div>
          <div>
            <span>{t('system_status.attention_sources', '需关注视频源数')}</span>
            <strong>{videoPipelineHealth.attentionSourceCount}</strong>
          </div>
          <div>
            <span>{t('system_status.paused_sources', '已暂停视频源数')}</span>
            <strong>{videoPipelineHealth.pausedSourceCount}</strong>
          </div>
          <div>
            <span>{t('system_status.avg_coverage', '平均分析覆盖率')}</span>
            <strong>
              {videoPipelineHealth.avgAnalyzedCoveragePercent !== null
                ? `${videoPipelineHealth.avgAnalyzedCoveragePercent}%`
                : '-'}
            </strong>
          </div>
          <div>
            <span>{t('system_status.max_no_video_time', '最长未出现新视频时长')}</span>
            <strong>
              {videoPipelineHealth.maxMinutesSinceLastNewVideo !== null
                ? t('system_status.minutes_format', '{{minutes}} 分钟', { minutes: videoPipelineHealth.maxMinutesSinceLastNewVideo })
                : '-'}
            </strong>
          </div>
        </div>

        {latestStatus.recentFailedTasks.length > 0 ? (
          <div className="summary-detail" style={{ marginTop: '0.8rem' }}>
            <h4>{t('system_status.recent_failed_title', '最近失败任务摘要')}</h4>
            {latestStatus.recentFailedTasks.map((item) => (
              <article key={item.id}>
                <p>
                  <strong>{item.task_type}</strong> · {formatDateTime(item.created_at)}
                </p>
                <p>{item.message || t('system_status.no_error_msg', '无错误消息')}</p>
              </article>
            ))}
          </div>
        ) : null}

        {alertSources.length > 0 ? (
          <div className="summary-detail" style={{ marginTop: '0.8rem' }}>
            <h4>{t('system_status.affected_sources', '受影响视频源')}</h4>
            {alertSources.map((item) => (
              <article key={item.sourceId}>
                <p>
                  <strong>{item.sourceName}</strong> · {item.cameraName}
                </p>
                <p>
                  {t('system_status.source_status', '状态：')}
                  {item.analysisState === 'paused' ? t('system_status.paused', '已暂停') : item.analysisState === 'stopped' ? t('system_status.stopped', '已停止') : t('system_status.analyzing', '识别中')}
                  {item.minutesSinceLastNewVideo !== null
                    ? t('system_status.time_since_new', '，距最近新视频 {{minutes}} 分钟', { minutes: item.minutesSinceLastNewVideo })
                    : ''}
                </p>
                <div className="row-actions">
                  <button
                    className="ghost"
                    onClick={() => navigate(`/video-sources?source_id=${item.sourceId}`)}
                  >
                    {t('system_status.view_source_status', '查看该视频源状态')}
                  </button>
                </div>
              </article>
            ))}
            <div className="row-actions">
              <button className="ghost" onClick={() => navigate('/video-sources')}>
                {t('system_status.handle_alert_sources', '处理告警视频源')}
              </button>
            </div>
          </div>
        ) : null}

        <div className="row-actions">
          <button className="ghost" onClick={() => navigate('/tasks')}>
            {t('system_status.view_task_logs', '查看任务日志')}
          </button>
          <button className="ghost" onClick={() => navigate('/events')}>
            {t('system_status.view_events', '查看事件时间轴')}
          </button>
          <button className="ghost" onClick={() => navigate('/dashboard')}>
            {t('system_status.back_to_dashboard', '返回仪表盘')}
          </button>
        </div>
      </article>
    </div>
  )
}
