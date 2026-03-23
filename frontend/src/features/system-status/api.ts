import { getHomeProfile } from '../home-profile/api'
import { getOnboardingStatus } from '../onboarding/api'
import { getProviders } from '../providers/api'
import { getSystemConfig } from '../system-config/api'
import { getTaskLogs } from '../tasks/api'
import { getVideoSourceStatusesBatch, getVideoSources } from '../video-sources/api'
import type { VideoSourceStatus } from '../../types/api'

type VideoPipelineHealth = {
  sourceCount: number
  attentionSourceCount: number
  pausedSourceCount: number
  avgAnalyzedCoveragePercent: number | null
  maxMinutesSinceLastNewVideo: number | null
}

type AlertSource = {
  sourceId: number
  sourceName: string
  cameraName: string
  analysisState: string
  minutesSinceLastNewVideo: number | null
}

export type SystemStatusOverview = {
  onboarding: Awaited<ReturnType<typeof getOnboardingStatus>>
  videoSources: Awaited<ReturnType<typeof getVideoSources>>['list']
  providers: Awaited<ReturnType<typeof getProviders>>['list']
  homeProfile: Awaited<ReturnType<typeof getHomeProfile>>
  systemConfig: Awaited<ReturnType<typeof getSystemConfig>>
  taskLogs: Awaited<ReturnType<typeof getTaskLogs>>['list']
  videoPipelineHealth: VideoPipelineHealth
  alertSources: AlertSource[]
}

export async function fetchSystemStatusOverview(): Promise<SystemStatusOverview> {
  const [onboarding, videoSourceData, providerData, homeProfile, systemConfig, taskLogData] =
    await Promise.all([
      getOnboardingStatus(),
      getVideoSources({ page: 1, pageSize: 100, keyword: '' }),
      getProviders({ page: 1, pageSize: 100, providerType: '' }),
      getHomeProfile(),
      getSystemConfig(),
      getTaskLogs({ page: 1, pageSize: 50, taskType: '', status: '' }),
    ])

  const enabledVideoSources = videoSourceData.list.filter((item) => item.enabled)
  const sourceIds = enabledVideoSources.map((item) => item.id)
  const statusList: VideoSourceStatus[] = await getVideoSourceStatusesBatch(sourceIds)

  const coverageValues = statusList
    .map((item) => item.analyzed_coverage_percent)
    .filter((item): item is number => item !== null)
  const freshnessValues = statusList
    .map((item) => item.minutes_since_last_new_video)
    .filter((item): item is number => item !== null)

  const avgAnalyzedCoveragePercent =
    coverageValues.length > 0
      ? Number((coverageValues.reduce((sum, item) => sum + item, 0) / coverageValues.length).toFixed(2))
      : null
  const maxMinutesSinceLastNewVideo = freshnessValues.length > 0 ? Math.max(...freshnessValues) : null
  const sourceMap = new Map(enabledVideoSources.map((item) => [item.id, item]))
  const alertSources: AlertSource[] = statusList
    .filter((item) => item.analysis_state !== 'analyzing')
    .map((item) => {
      const source = sourceMap.get(item.source_id)
      return {
        sourceId: item.source_id,
        sourceName: source?.source_name ?? `视频源 ${item.source_id}`,
        cameraName: source?.camera_name ?? '-',
        analysisState: item.analysis_state,
        minutesSinceLastNewVideo: item.minutes_since_last_new_video,
      }
    })

  return {
    onboarding,
    videoSources: videoSourceData.list,
    providers: providerData.list,
    homeProfile,
    systemConfig,
    taskLogs: taskLogData.list,
    videoPipelineHealth: {
      sourceCount: statusList.length,
      attentionSourceCount: statusList.filter((item) => item.analysis_state !== 'analyzing').length,
      pausedSourceCount: statusList.filter((item) => item.analysis_state === 'paused').length,
      avgAnalyzedCoveragePercent,
      maxMinutesSinceLastNewVideo,
    },
    alertSources,
  }
}
