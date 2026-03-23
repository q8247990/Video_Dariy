import { apiClient, unwrapApi } from '../../lib/axios'
import type { PaginatedData, TaskLogItem } from '../../types/api'

export type TaskLogQuery = {
  page: number
  pageSize: number
  taskType: string
  status: string
}

export async function getTaskLogs(query: TaskLogQuery): Promise<PaginatedData<TaskLogItem>> {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
  })
  if (query.taskType.trim()) {
    params.set('task_type', query.taskType.trim())
  }
  if (query.status.trim()) {
    params.set('status', query.status.trim())
  }

  const response = await apiClient.get(`/tasks/logs?${params.toString()}`)
  return unwrapApi<PaginatedData<TaskLogItem>>(response)
}

export async function deleteTaskLog(id: number): Promise<Record<string, never>> {
  const response = await apiClient.delete(`/tasks/logs/${id}`)
  return unwrapApi<Record<string, never>>(response)
}

export async function stopTaskLog(id: number): Promise<{ task_log_id: number; status: string }> {
  const response = await apiClient.post(`/tasks/logs/${id}/stop`)
  return unwrapApi<{ task_log_id: number; status: string }>(response)
}

export async function retryTaskLog(id: number): Promise<{ task_id: string }> {
  const response = await apiClient.post(`/tasks/logs/${id}/retry`)
  return unwrapApi<{ task_id: string }>(response)
}
