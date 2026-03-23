type StatusTagProps = {
  status: string
}

const statusLabelMap: Record<string, string> = {
  sealed: '待识别',
  pending: '待处理',
  running: '处理中',
  open: '采集中',
  success: '成功',
  skipped: '已跳过',
  failed: '失败',
  timeout: '超时',
  cancelled: '已取消',
  analyzing: '分析中',
  enabled: '启用',
  paused: '已暂停',
  disabled: '禁用',
  available: '可用',
  degraded: '降级',
  unavailable: '不可用',
  unknown: '未知',
}

export function StatusTag({ status }: StatusTagProps) {
  return (
    <span className={`status-tag status-${status}`}>
      {statusLabelMap[status] ?? status}
    </span>
  )
}
