import { useTranslation } from 'react-i18next'

type StatusTagProps = {
  status: string
}

export function StatusTag({ status }: StatusTagProps) {
  const { t } = useTranslation()

  const statusLabelMap: Record<string, string> = {
    sealed: t('common.status_sealed', '待识别'),
    pending: t('common.status_pending', '待处理'),
    running: t('common.status_running', '处理中'),
    open: t('common.status_open', '采集中'),
    success: t('common.status_success', '成功'),
    skipped: t('common.status_skipped', '已跳过'),
    failed: t('common.status_failed', '失败'),
    timeout: t('common.status_timeout', '超时'),
    cancelled: t('common.status_cancelled', '已取消'),
    analyzing: t('common.status_analyzing', '分析中'),
    enabled: t('common.status_enabled', '启用'),
    paused: t('common.status_paused', '已暂停'),
    disabled: t('common.status_disabled', '禁用'),
    available: t('common.status_available', '可用'),
    degraded: t('common.status_degraded', '降级'),
    unavailable: t('common.status_unavailable', '不可用'),
    unknown: t('common.status_unknown', '未知'),
  }

  return (
    <span className={`status-tag status-${status}`}>
      {statusLabelMap[status] ?? status}
    </span>
  )
}
