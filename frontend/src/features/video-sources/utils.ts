import type { TFunction } from 'i18next'

export function analysisStateText(status: string, t: TFunction): string {
  if (status === 'analyzing') {
    return t('video_sources.status_analyzing', '识别中')
  }
  if (status === 'paused') {
    return t('video_sources.status_paused', '已暂停')
  }
  return t('video_sources.status_stopped', '已停止')
}
