import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { HlsVideoPlayer } from '../../components/common/HlsVideoPlayer'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import { getSessionPlayback, getSessions } from './api'

export function SessionsPage() {
  const [page, setPage] = useState(1)
  const [sourceId, setSourceId] = useState('')
  const [analysisStatus, setAnalysisStatus] = useState('')
  const [selectedSessionId, setSelectedSessionId] = useState<number | null>(null)

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
    return <LoadingBlock text="加载 Session 列表中" />
  }

  if (listQuery.error) {
    return <ApiErrorAlert message={(listQuery.error as Error).message} />
  }

  const list = listQuery.data?.list ?? []
  const total = listQuery.data?.pagination.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / 20))

  return (
    <div>
      <PageHeader title="Session 列表" subtitle="查看连续视频片段与分析状态" />

      <div className="card tool-row tool-row-inline">
        <label>
          source_id
          <input
            value={sourceId}
            onChange={(event) => {
              setSourceId(event.target.value)
              setPage(1)
            }}
            placeholder="按视频源ID筛选"
          />
        </label>
        <label>
          分析状态
          <select
            value={analysisStatus}
            onChange={(event) => {
              setAnalysisStatus(event.target.value)
              setPage(1)
            }}
          >
            <option value="">全部</option>
            <option value="open">采集中</option>
            <option value="sealed">待识别</option>
            <option value="analyzing">分析中</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
          </select>
        </label>
      </div>

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>来源</th>
              <th>开始时间</th>
              <th>结束时间</th>
              <th>时长(秒)</th>
              <th>活动级别</th>
              <th>重要事件</th>
              <th>摘要</th>
              <th>状态</th>
              <th>操作</th>
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
                <td>{item.has_important_event === null ? '-' : item.has_important_event ? '是' : '否'}</td>
                <td>{item.summary_text ?? '-'}</td>
                <td>
                  <StatusTag status={item.analysis_status} />
                </td>
                <td>
                  <button className="ghost" onClick={() => setSelectedSessionId(item.id)}>
                    查看回放
                  </button>
                </td>
              </tr>
            ))}
            {list.length === 0 ? (
              <tr>
                <td colSpan={10} className="empty-cell">
                  暂无符合条件的 Session
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>

        <div className="pager">
          <button className="ghost" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
            上一页
          </button>
          <span>
            第 {page} / {totalPages} 页，共 {total} 条
          </span>
          <button
            className="ghost"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          >
            下一页
          </button>
        </div>
      </div>

      {selectedSessionId !== null ? (
        <div className="card playback-card">
          <div className="playback-head">
            <h3>Session #{selectedSessionId} 回放</h3>
            <button className="ghost" onClick={() => setSelectedSessionId(null)}>
              关闭
            </button>
          </div>

          {playbackQuery.isLoading ? <LoadingBlock text="加载回放列表中" /> : null}
          {playbackQuery.error ? <ApiErrorAlert message={(playbackQuery.error as Error).message} /> : null}

          {!playbackQuery.isLoading && !playbackQuery.error ? (
            <div className="playback-grid">
              {playbackQuery.data?.playback_url ? (
                <article className="playback-item">
                  <h4>拼接回放</h4>
                  <HlsVideoPlayer
                    src={
                      playbackQuery.data.playback_url.startsWith('/api/v1')
                        ? playbackQuery.data.playback_url
                        : `/api/v1${playbackQuery.data.playback_url}`
                    }
                  />
                </article>
              ) : (
                <div className="empty-cell">当前 Session 暂无可播放文件</div>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
