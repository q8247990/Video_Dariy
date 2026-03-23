import { apiClient, unwrapApi } from '../../lib/axios'
import type { PaginatedData, SessionPlayback, VideoSession } from '../../types/api'

export type SessionQuery = {
  page: number
  pageSize: number
  sourceId: string
  analysisStatus: string
  startTime?: string
  endTime?: string
}

export async function getSessions(query: SessionQuery): Promise<PaginatedData<VideoSession>> {
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
  if (query.startTime?.trim()) {
    params.set('start_time', query.startTime.trim())
  }
  if (query.endTime?.trim()) {
    params.set('end_time', query.endTime.trim())
  }

  const response = await apiClient.get(`/sessions?${params.toString()}`)
  return unwrapApi<PaginatedData<VideoSession>>(response)
}

export async function getSessionPlayback(sessionId: number): Promise<SessionPlayback> {
  const response = await apiClient.get(`/media/sessions/${sessionId}/playback`)
  return unwrapApi<SessionPlayback>(response)
}
