import type { UseQueryResult } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { useTranslation } from 'react-i18next'
import type { VideoSource, VideoSourceStatus } from '../../types/api'

export function formatDateTime(value: string | null): string {
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

export function analysisStateText(status: string, t?: TFunction): string {
  if (status === 'analyzing') {
    return t ? t('video_sources.status_analyzing', '识别中') : '识别中'
  }
  if (status === 'paused') {
    return t ? t('video_sources.status_paused', '已暂停') : '已暂停'
  }
  return t ? t('video_sources.status_stopped', '已停止') : '已停止'
}

type VideoSourceStatusDialogProps = {
  statusSource: VideoSource
  statusQuery: UseQueryResult<VideoSourceStatus, Error>
  pausePending: boolean
  resumePending: boolean
  onPause: (id: number) => void
  onResume: (id: number) => void
  onClose: () => void
}

export function VideoSourceStatusDialog({
  statusSource,
  statusQuery,
  pausePending,
  resumePending,
  onPause,
  onResume,
  onClose,
}: VideoSourceStatusDialogProps) {
  const { t } = useTranslation()
  return (
    <div className="dialog-mask" onClick={onClose}>
      <div className="dialog" onClick={(event) => event.stopPropagation()}>
        <h3>{t('video_sources.status_dialog_title', '视频源状态：{{name}}', { name: statusSource.source_name })}</h3>
        {statusQuery.isLoading ? <p>{t('video_sources.status_loading', '加载状态中...')}</p> : null}
        {statusQuery.error ? <p>{t('video_sources.status_error', '加载失败：{{msg}}', { msg: (statusQuery.error as Error).message })}</p> : null}
        {statusQuery.data ? (
          <div className="summary-detail">
            <p>{t('video_sources.status_desc', '说明：本状态用于家庭用户查看处理进展，指标按视频时间范围计算。')}</p>
            <article>
              <p>
                <strong>{t('video_sources.status_earliest_video', '最早视频时间')}</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.video_earliest_time)}</p>
            </article>
            <article>
              <p>
                <strong>{t('video_sources.status_latest_video', '最晚视频时间')}</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.video_latest_time)}</p>
            </article>
            <article>
              <p>
                <strong>{t('video_sources.status_earliest_analyzed', '最早完成分析时间')}</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.analyzed_earliest_time)}</p>
            </article>
            <article>
              <p>
                <strong>{t('video_sources.status_latest_analyzed', '最晚完成分析时间')}</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.analyzed_latest_time)}</p>
            </article>
            <article>
              <p>
                <strong>{t('video_sources.status_coverage', '分析覆盖率')}</strong>
              </p>
              <p>
                {statusQuery.data.analyzed_coverage_percent !== null
                  ? `${statusQuery.data.analyzed_coverage_percent}%`
                  : '-'}
              </p>
            </article>
            <article>
              <p>
                <strong>{t('video_sources.status_analysis_state', '分析状态')}</strong>
              </p>
              <p>{analysisStateText(statusQuery.data.analysis_state, t)}</p>
            </article>
            <article>
              <p>
                <strong>{t('video_sources.status_time_since_new', '上次产生新视频文件已过去')}</strong>
              </p>
              <p>
                {statusQuery.data.minutes_since_last_new_video !== null
                  ? t('video_sources.minutes_format', '{{minutes}} 分钟', { minutes: statusQuery.data.minutes_since_last_new_video })
                  : '-'}
              </p>
            </article>
            <article>
              <p>
                <strong>{t('video_sources.status_full_scan', '全量扫描任务')}</strong>
              </p>
              <p>{statusQuery.data.full_build_running ? t('video_sources.scan_running', '运行中') : t('video_sources.scan_not_running', '未运行')}</p>
            </article>
            <article>
              <p>
                <strong>{t('video_sources.status_updated_at', '状态更新时间')}</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.updated_at)}</p>
            </article>
          </div>
        ) : null}
        <div className="dialog-actions">
          {statusQuery.data ? (
            <button
              className="ghost"
              disabled={pausePending || resumePending}
              onClick={() => {
                if (statusSource.source_paused) {
                  onResume(statusSource.id)
                } else {
                  onPause(statusSource.id)
                }
              }}
            >
              {statusSource.source_paused ? t('video_sources.resume_source', '恢复视频源') : t('video_sources.pause_source', '暂停视频源')}
            </button>
          ) : null}
          <button className="ghost" onClick={onClose}>
            {t('video_sources.close_dialog', '关闭')}
          </button>
        </div>
      </div>
    </div>
  )
}
