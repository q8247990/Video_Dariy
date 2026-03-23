import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import {
  getDailySummaries,
  getDailySummary,
  triggerAllDailySummaries,
  triggerDailySummary,
} from './api'

function levelLabel(level: string): string {
  if (level === 'high') {
    return '高'
  }
  if (level === 'medium') {
    return '中'
  }
  if (level === 'low') {
    return '低'
  }
  return level
}

export function DailySummariesPage() {
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
      setMessage(`已触发日报生成任务：${data.task_id}`)
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const triggerAllMutation = useMutation({
    mutationFn: triggerAllDailySummaries,
    onSuccess: (data) => {
      setMessage(
        `已按 ${data.earliest_date} 到 ${data.latest_date} 的 ${data.target_dates.length} 个日期下发 ${data.queued_count} 个日报任务` +
          (data.skipped_count > 0 ? `，跳过 ${data.skipped_count} 个运行中日期` : ''),
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
        title="日报中心"
        subtitle="查看每日总结并支持手动触发生成"
        actions={
          <div className="summary-generate">
            <input
              type="date"
              value={manualDate}
              onChange={(event) => setManualDate(event.target.value)}
            />
            <button onClick={() => triggerMutation.mutate(manualDate)} disabled={triggerMutation.isPending}>
              {triggerMutation.isPending ? '触发中...' : '生成日报'}
            </button>
            <button
              className="ghost"
              onClick={() => {
                if (!window.confirm('确认按已分析完成的 Session 日期批量生成全部日报吗？')) {
                  return
                }
                triggerAllMutation.mutate()
              }}
              disabled={triggerAllMutation.isPending}
            >
              {triggerAllMutation.isPending ? '生成中...' : '生成全部日报'}
            </button>
          </div>
        }
      />

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="grid-two">
        <div className="card">
          <h3>日报列表</h3>
          {listQuery.isLoading ? <LoadingBlock text="加载日报列表中" /> : null}
          {listQuery.error ? <ApiErrorAlert message={(listQuery.error as Error).message} /> : null}
          {!listQuery.isLoading && !listQuery.error ? (
            <>
              <table className="table">
                <thead>
                  <tr>
                    <th>日期</th>
                    <th>标题</th>
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
                      <td>{item.summary_title || `${item.summary_date} 家庭日报`}</td>
                    </tr>
                  ))}
                  {(listQuery.data?.list.length ?? 0) === 0 ? (
                    <tr>
                      <td colSpan={2} className="empty-cell">
                        暂无日报
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
            </>
          ) : null}
        </div>

        <div className="card">
          <h3>日报详情</h3>
          {!resolvedSelectedDate ? <p className="text-muted">请选择左侧一条日报查看详情</p> : null}
          {detailQuery.isLoading ? <LoadingBlock text="加载详情中" /> : null}
          {detailQuery.error ? <ApiErrorAlert message={(detailQuery.error as Error).message} /> : null}
          {detail ? (
            <div className="summary-four-grid">
              <section className="summary-field-block">
                <p className="summary-field-label">标题</p>
                <article>{detail.summary_title || `${detail.summary_date} 家庭日报`}</article>
              </section>

              <section className="summary-field-block">
                <p className="summary-field-label">日期</p>
                <article>{detail.summary_date}</article>
              </section>

              <section className="summary-field-block">
                <p className="summary-field-label">总览</p>
                <article>{detail.overall_summary?.trim() || '暂无总览内容'}</article>
              </section>

              <section className="summary-field-block">
                <p className="summary-field-label">详情</p>
                {subjectSections.length > 0 ? (
                  <div className="summary-subject-list">
                    {subjectSections.map((item, index) => (
                      <article key={`${item.subject_name}-${index}`} className="summary-subject-card">
                        <header>
                          <strong>{item.subject_name}</strong>
                          <span className="text-muted">
                            {item.subject_type === 'member' ? '成员' : '宠物'} · 活动度 {item.activity_score ?? 0}
                          </span>
                        </header>
                        <p>{item.summary}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <article>暂无对象小结</article>
                )}

                <p className="summary-field-label">关注事项</p>
                {attentionItems.length > 0 ? (
                  <div className="summary-attention-list">
                    {attentionItems.map((item, index) => (
                      <article key={`${item.title}-${index}`} className="summary-attention-card">
                        <header>
                          <strong>{item.title}</strong>
                          <span className={`summary-level summary-level-${item.level}`}>
                            {levelLabel(item.level)}
                          </span>
                        </header>
                        <p>{item.summary}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <article>暂无关注事项</article>
                )}
              </section>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
