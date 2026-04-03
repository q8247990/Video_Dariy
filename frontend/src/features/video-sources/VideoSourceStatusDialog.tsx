import type { UseQueryResult } from '@tanstack/react-query'
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

export function analysisStateText(status: string): string {
  if (status === 'analyzing') {
    return '识别中'
  }
  if (status === 'paused') {
    return '已暂停'
  }
  return '已停止'
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
  return (
    <div className="dialog-mask" onClick={onClose}>
      <div className="dialog" onClick={(event) => event.stopPropagation()}>
        <h3>视频源状态：{statusSource.source_name}</h3>
        {statusQuery.isLoading ? <p>加载状态中...</p> : null}
        {statusQuery.error ? <p>加载失败：{(statusQuery.error as Error).message}</p> : null}
        {statusQuery.data ? (
          <div className="summary-detail">
            <p>说明：本状态用于家庭用户查看处理进展，指标按视频时间范围计算。</p>
            <article>
              <p>
                <strong>最早视频时间</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.video_earliest_time)}</p>
            </article>
            <article>
              <p>
                <strong>最晚视频时间</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.video_latest_time)}</p>
            </article>
            <article>
              <p>
                <strong>最早完成分析时间</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.analyzed_earliest_time)}</p>
            </article>
            <article>
              <p>
                <strong>最晚完成分析时间</strong>
              </p>
              <p>{formatDateTime(statusQuery.data.analyzed_latest_time)}</p>
            </article>
            <article>
              <p>
                <strong>分析覆盖率</strong>
              </p>
              <p>
                {statusQuery.data.analyzed_coverage_percent !== null
                  ? `${statusQuery.data.analyzed_coverage_percent}%`
                  : '-'}
              </p>
            </article>
            <article>
              <p>
                <strong>分析状态</strong>
              </p>
              <p>{analysisStateText(statusQuery.data.analysis_state)}</p>
            </article>
            <article>
              <p>
                <strong>上次产生新视频文件已过去</strong>
              </p>
              <p>
                {statusQuery.data.minutes_since_last_new_video !== null
                  ? `${statusQuery.data.minutes_since_last_new_video} 分钟`
                  : '-'}
              </p>
            </article>
            <article>
              <p>
                <strong>全量扫描任务</strong>
              </p>
              <p>{statusQuery.data.full_build_running ? '运行中' : '未运行'}</p>
            </article>
            <article>
              <p>
                <strong>状态更新时间</strong>
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
              {statusSource.source_paused ? '恢复视频源' : '暂停视频源'}
            </button>
          ) : null}
          <button className="ghost" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  )
}
