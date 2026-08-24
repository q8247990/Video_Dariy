import { useTranslation } from 'react-i18next'

type StatusTagProps = {
  status: string
}

export function StatusTag({ status }: StatusTagProps) {
  const { t } = useTranslation()

  const statusLabelMap: Record<string, string> = {
    sealed: t('common.status_sealed'),
    pending: t('common.status_pending'),
    running: t('common.status_running'),
    open: t('common.status_open'),
    success: t('common.status_success'),
    skipped: t('common.status_skipped'),
    failed: t('common.status_failed'),
    timeout: t('common.status_timeout'),
    cancelled: t('common.status_cancelled'),
    analyzing: t('common.status_analyzing'),
    enabled: t('common.status_enabled'),
    paused: t('common.status_paused'),
    disabled: t('common.status_disabled'),
    available: t('common.status_available'),
    degrade: t('common.status_degraded'),
    degraded: t('common.status_degraded'),
    unavailable: t('common.status_unavailable'),
    unknown: t('common.status_unknown'),
  }

  return (
    <span className={`status-tag status-${status}`}>
      {statusLabelMap[status] ?? status}
    </span>
  )
}
