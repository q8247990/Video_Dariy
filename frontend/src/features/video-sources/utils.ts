import type { TFunction } from 'i18next'

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
