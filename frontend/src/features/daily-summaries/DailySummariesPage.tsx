import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import {
  getDailySummaries,
  getDailySummary,
  triggerAllDailySummaries,
  triggerDailySummary,
} from './api'

function levelLabel(level: string, t: (key: string) => string): string {
  if (level === 'high') {
    return t('daily_summaries.level_high')
  }
  if (level === 'medium') {
    return t('daily_summaries.level_medium')
  }
  if (level === 'low') {
    return t('daily_summaries.level_low')
  }
  return level
}

export function DailySummariesPage() {
  const { t } = useTranslation()
  const [page, setPage] = useState(1)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [manualDate, setManualDate] = useState('')
  const [message, setMessage] = useState('')

  const listQuery = useQuery({
    queryKey: ['daily-summaries', { page }],
    queryFn: () => getDailySummaries(page, 20),
  })

  const resolvedSelectedDate = selectedDate ?? listQuery.data?.list?.[0]?.summary_date ?? null

  const detailQuery = useQuery({
    queryKey: ['daily-summary-detail', resolvedSelectedDate],
    queryFn: () => getDailySummary(resolvedSelectedDate as string),
    enabled: resolvedSelectedDate !== null,
  })

  const triggerMutation = useMutation({
    mutationFn: (date: string) => triggerDailySummary(date || undefined),
    onSuccess: (data) => {
      setMessage(`${t('daily_summaries.task_created')}：${data.task_id}`)
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const triggerAllMutation = useMutation({
    mutationFn: triggerAllDailySummaries,
    onSuccess: (data) => {
      setMessage(
        `${t('daily_summaries.batch_generated_part1')}${data.earliest_date}${t('daily_summaries.batch_generated_part3')}${data.latest_date}${t('daily_summaries.batch_generated_part2')}${data.target_dates.length}${t('daily_summaries.batch_generated_part4')}${data.queued_count}` +
          (data.skipped_count > 0
            ? t('daily_summaries.batch_generated_part5', { skipped: data.skipped_count })
            : ''),
      )
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const total = listQuery.data?.pagination.total ?? 0
  const totalPages = useMemo(() => Math.max(1, Math.ceil(total / 20)), [total])
  const detail = detailQuery.data
  const subjectSections = detail?.subject_sections_json ?? []
  const attentionItems = detail?.attention_items_json ?? []

  return (
    <div>
      <PageHeader
        title={t('daily_summaries.title')}
        subtitle={t('daily_summaries.subtitle')}
        actions={
          <div className="summary-generate">
            <input
              type="date"
              value={manualDate}
              onChange={(event) => setManualDate(event.target.value)}
            />
            <button onClick={() => triggerMutation.mutate(manualDate)} disabled={triggerMutation.isPending}>
              {triggerMutation.isPending ? t('daily_summaries.triggering') : t('daily_summaries.generate')}
            </button>
            <button
              className="ghost"
              onClick={() => {
                if (!window.confirm(t('daily_summaries.batch_confirm'))) {
                  return
                }
                triggerAllMutation.mutate()
              }}
              disabled={triggerAllMutation.isPending}
            >
              {triggerAllMutation.isPending ? t('daily_summaries.generating') : t('daily_summaries.generate_all')}
            </button>
          </div>
        }
      />

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="grid-two">
        <div className="card">
          <h3>{t('daily_summaries.list_title')}</h3>
          {listQuery.isLoading ? <LoadingBlock text={t('daily_summaries.loading')} /> : null}
          {listQuery.error ? <ApiErrorAlert message={(listQuery.error as Error).message} /> : null}
          {!listQuery.isLoading && !listQuery.error ? (
            <>
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('daily_summaries.col_date')}</th>
                    <th>{t('daily_summaries.col_title')}</th>
                  </tr>
                </thead>
                <tbody>
                  {(listQuery.data?.list ?? []).map((item) => (
                    <tr
                      key={item.id}
                       className={resolvedSelectedDate === item.summary_date ? 'row-selected' : ''}
                      onClick={() => setSelectedDate(item.summary_date)}
                    >
                      <td>{item.summary_date}</td>
                      <td>{item.summary_title || t('daily_summaries.default_title', { date: item.summary_date })}</td>
                    </tr>
                  ))}
                  {(listQuery.data?.list.length ?? 0) === 0 ? (
                    <tr>
                      <td colSpan={2} className="empty-cell">
                        {t('daily_summaries.empty')}
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>

              <div className="pager">
                <button className="ghost" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                  {t('daily_summaries.pager_prev')}
                </button>
                <span>
                  {t('daily_summaries.pager_info', { page, totalPages, total })}
                </span>
                <button
                  className="ghost"
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                >
                  {t('daily_summaries.pager_next')}
                </button>
              </div>
            </>
          ) : null}
        </div>

        <div className="card">
          <h3>{t('daily_summaries.detail_title')}</h3>
          {!resolvedSelectedDate ? <p className="text-muted">{t('daily_summaries.select_prompt')}</p> : null}
          {detailQuery.isLoading ? <LoadingBlock text={t('daily_summaries.loading_detail')} /> : null}
          {detailQuery.error ? <ApiErrorAlert message={(detailQuery.error as Error).message} /> : null}
          {detail ? (
            <div className="summary-four-grid">
              <section className="summary-field-block">
                <p className="summary-field-label">{t('daily_summaries.field_title')}</p>
                <article>{detail.summary_title || t('daily_summaries.default_title', { date: detail.summary_date })}</article>
              </section>

              <section className="summary-field-block">
                <p className="summary-field-label">{t('daily_summaries.field_date')}</p>
                <article>{detail.summary_date}</article>
              </section>

              <section className="summary-field-block">
                <p className="summary-field-label">{t('daily_summaries.field_overall')}</p>
                <article>{detail.overall_summary?.trim() || t('daily_summaries.empty_overall')}</article>
              </section>

              <section className="summary-field-block">
                <p className="summary-field-label">{t('daily_summaries.field_details')}</p>
                {subjectSections.length > 0 ? (
                  <div className="summary-subject-list">
                    {subjectSections.map((item, index) => (
                      <article key={`${item.subject_name}-${index}`} className="summary-subject-card">
                        <header>
                          <strong>{item.subject_name}</strong>
                          <span className="text-muted">
                            {item.subject_type === 'member'
                              ? t('daily_summaries.field_subject_type_member')
                              : t('daily_summaries.field_subject_type_pet')}
                            {' · '}
                            {t('daily_summaries.field_activity_score')} {item.activity_score ?? 0}
                          </span>
                        </header>
                        <p>{item.summary}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <article>{t('daily_summaries.empty_subject_sections')}</article>
                )}

                <p className="summary-field-label">{t('daily_summaries.field_attention_items')}</p>
                {attentionItems.length > 0 ? (
                  <div className="summary-attention-list">
                    {attentionItems.map((item, index) => (
                      <article key={`${item.title}-${index}`} className="summary-attention-card">
                        <header>
                          <strong>{item.title}</strong>
                          <span className={`summary-level summary-level-${item.level}`}>
                            {levelLabel(item.level, t)}
                          </span>
                        </header>
                        <p>{item.summary}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <article>{t('daily_summaries.empty_attention_items')}</article>
                )}
              </section>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
