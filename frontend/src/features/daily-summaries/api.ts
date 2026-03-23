import { apiClient, unwrapApi } from '../../lib/axios'
import type { DailySummary, DailySummaryDetail, PaginatedData } from '../../types/api'

export async function getDailySummaries(page = 1, pageSize = 20): Promise<PaginatedData<DailySummary>> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  const response = await apiClient.get(`/daily-summaries?${params.toString()}`)
  return unwrapApi<PaginatedData<DailySummary>>(response)
}

export async function getDailySummary(dateStr: string): Promise<DailySummaryDetail> {
  const response = await apiClient.get(`/daily-summaries/${dateStr}`)
  return unwrapApi<DailySummaryDetail>(response)
}

export async function triggerDailySummary(dateStr?: string): Promise<{ task_id: string }> {
  const params = new URLSearchParams()
  if (dateStr) {
    params.set('target_date', dateStr)
  }
  const suffix = params.toString() ? `?${params.toString()}` : ''
  const response = await apiClient.post(`/tasks/summarize${suffix}`)
  return unwrapApi<{ task_id: string }>(response)
}

export async function triggerAllDailySummaries(): Promise<{
  earliest_date: string
  latest_date: string
  target_dates: string[]
  queued_count: number
  skipped_count: number
  task_ids: string[]
  skipped_dates: string[]
}> {
  const response = await apiClient.post('/daily-summaries/generate-all')
  return unwrapApi<{
    earliest_date: string
    latest_date: string
    target_dates: string[]
    queued_count: number
    skipped_count: number
    task_ids: string[]
    skipped_dates: string[]
  }>(response)
}
