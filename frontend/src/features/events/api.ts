import { apiClient, unwrapApi } from '../../lib/axios'
import type { EventDetail, EventRecord, PaginatedData } from '../../types/api'

export type EventQuery = {
  page: number
  pageSize: number
  sourceId: string
  analysisStatus: string
}

export async function getEvents(query: EventQuery): Promise<PaginatedData<EventRecord>> {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
  })

  if (query.sourceId.trim()) {
    params.set('source_id', query.sourceId.trim())
  }
  if (query.analysisStatus.trim()) {
    params.set('analysis_status', query.analysisStatus.trim())
  }

  const response = await apiClient.get(`/events?${params.toString()}`)
  return unwrapApi<PaginatedData<EventRecord>>(response)
}

export async function triggerSessionAnalyze(sessionId: number): Promise<{ task_id: string }> {
  const response = await apiClient.post(`/tasks/analyze/${sessionId}`)
  return unwrapApi<{ task_id: string }>(response)
}

export async function getEventDetail(eventId: number): Promise<EventDetail> {
  const response = await apiClient.get(`/events/${eventId}`)
  return unwrapApi<EventDetail>(response)
}

export async function getSessionEvents(
  sessionId: number,
  order: 'asc' | 'desc' = 'asc',
): Promise<EventRecord[]> {
  const params = new URLSearchParams({ order })
  const response = await apiClient.get(`/sessions/${sessionId}/events?${params.toString()}`)
  return unwrapApi<EventRecord[]>(response)
}
