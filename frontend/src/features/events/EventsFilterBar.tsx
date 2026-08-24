import { useTranslation } from 'react-i18next'

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
  const { t } = useTranslation()
  return (
    <div className="card tool-row tool-row-inline">
      <label>
        {t('events.filter_source_id')}
        <input
          value={sourceId}
          onChange={(event) => onSourceIdChange(event.target.value)}
          placeholder={t('events.filter_source_id_placeholder')}
        />
      </label>
      <label>
        {t('events.filter_analysis_status')}
        <select
          value={analysisStatus}
          onChange={(event) => onAnalysisStatusChange(event.target.value)}
        >
          <option value="">{t('events.filter_all')}</option>
          <option value="sealed">{t('events.status_pending')}</option>
          <option value="analyzing">{t('events.status_analyzing')}</option>
          <option value="success">{t('events.status_success')}</option>
          <option value="failed">{t('events.status_failed')}</option>
        </select>
      </label>
      <label>
        {t('events.filter_start_time')}
        <input
          type="datetime-local"
          value={startTime}
          onChange={(event) => onStartTimeChange(event.target.value)}
        />
      </label>
      <label>
        {t('events.filter_end_time')}
        <input
          type="datetime-local"
          value={endTime}
          onChange={(event) => onEndTimeChange(event.target.value)}
        />
      </label>
    </div>
  )
}
