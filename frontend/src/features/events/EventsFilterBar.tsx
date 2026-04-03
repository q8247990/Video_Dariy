type EventsFilterBarProps = {
  sourceId: string
  analysisStatus: string
  startTime: string
  endTime: string
  onSourceIdChange: (value: string) => void
  onAnalysisStatusChange: (value: string) => void
  onStartTimeChange: (value: string) => void
  onEndTimeChange: (value: string) => void
}

export function EventsFilterBar({
  sourceId,
  analysisStatus,
  startTime,
  endTime,
  onSourceIdChange,
  onAnalysisStatusChange,
  onStartTimeChange,
  onEndTimeChange,
}: EventsFilterBarProps) {
  return (
    <div className="card tool-row tool-row-inline">
      <label>
        视频源编号（可选）
        <input
          value={sourceId}
          onChange={(event) => onSourceIdChange(event.target.value)}
          placeholder="按视频源ID筛选"
        />
      </label>
      <label>
        分析状态
        <select
          value={analysisStatus}
          onChange={(event) => onAnalysisStatusChange(event.target.value)}
        >
          <option value="">全部</option>
          <option value="sealed">待识别</option>
          <option value="analyzing">分析中</option>
          <option value="success">成功</option>
          <option value="failed">失败</option>
        </select>
      </label>
      <label>
        开始时间
        <input
          type="datetime-local"
          value={startTime}
          onChange={(event) => onStartTimeChange(event.target.value)}
        />
      </label>
      <label>
        结束时间
        <input
          type="datetime-local"
          value={endTime}
          onChange={(event) => onEndTimeChange(event.target.value)}
        />
      </label>
    </div>
  )
}
