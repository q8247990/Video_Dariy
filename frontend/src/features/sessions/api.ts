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
  const playback = unwrapApi<SessionPlayback>(response)
  return {
    ...playback,
    playback_url: toApiMediaUrl(playback.playback_url),
    hls_url: toApiMediaUrl(playback.hls_url),
    files: playback.files.map((file) => ({ ...file, stream_url: toApiMediaUrl(file.stream_url) })),
  }
}

function toApiMediaUrl(url: string): string {
  return url.startsWith('/api/v1/') ? url : `/api/v1${url}`
}
