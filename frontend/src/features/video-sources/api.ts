import { apiClient, unwrapApi } from '../../lib/axios'
import type { PaginatedData, VideoSource, VideoSourceCreate, VideoSourceStatus } from '../../types/api'

export type VideoSourceQuery = {
  page: number
  pageSize: number
  keyword: string
}

export async function getVideoSources(query: VideoSourceQuery): Promise<PaginatedData<VideoSource>> {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
  })

  if (query.keyword.trim()) {
    params.set('keyword', query.keyword.trim())
  }

  const response = await apiClient.get(`/video-sources?${params.toString()}`)
  return unwrapApi<PaginatedData<VideoSource>>(response)
}

export async function createVideoSource(payload: VideoSourceCreate): Promise<VideoSource> {
  const response = await apiClient.post('/video-sources', payload)
  return unwrapApi<VideoSource>(response)
}

export async function updateVideoSource(id: number, payload: Partial<VideoSourceCreate>): Promise<VideoSource> {
  const response = await apiClient.put(`/video-sources/${id}`, payload)
  return unwrapApi<VideoSource>(response)
}

export async function deleteVideoSource(id: number): Promise<Record<string, never>> {
  const response = await apiClient.delete(`/video-sources/${id}`)
  return unwrapApi<Record<string, never>>(response)
}

export async function triggerFullScan(sourceId: number): Promise<{ task_id: string }> {
  const response = await apiClient.post(`/tasks/${sourceId}/build/full`)
  return unwrapApi<{ task_id: string }>(response)
}

export type VideoSourceTestResult = {
  success: boolean
  message: string
  file_count: number
  latest_file_time: string | null
  earliest_file_time: string | null
  last_validate_status: string | null
  last_validate_message: string | null
  last_validate_at: string | null
}

export async function testVideoSource(sourceId: number): Promise<VideoSourceTestResult> {
  const response = await apiClient.post(`/video-sources/${sourceId}/test`)
  return unwrapApi<VideoSourceTestResult>(response)
}

export async function getVideoSourceStatus(sourceId: number): Promise<VideoSourceStatus> {
  const response = await apiClient.get(`/video-sources/${sourceId}/status`)
  return unwrapApi<VideoSourceStatus>(response)
}

export async function getVideoSourceStatusesBatch(sourceIds: number[]): Promise<VideoSourceStatus[]> {
  if (sourceIds.length === 0) {
    return []
  }
  const response = await apiClient.get(`/video-sources/status/batch?source_ids=${sourceIds.join(',')}`)
  return unwrapApi<VideoSourceStatus[]>(response)
}

export async function pauseVideoSource(
  sourceId: number,
): Promise<{ source_id: number; source_paused: boolean }> {
  const response = await apiClient.post(`/video-sources/${sourceId}/pause`)
  return unwrapApi<{ source_id: number; source_paused: boolean }>(response)
}

export async function resumeVideoSource(
  sourceId: number,
): Promise<{ source_id: number; source_paused: boolean }> {
  const response = await apiClient.post(`/video-sources/${sourceId}/resume`)
  return unwrapApi<{ source_id: number; source_paused: boolean }>(response)
}
