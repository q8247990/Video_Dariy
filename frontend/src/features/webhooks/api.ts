import { apiClient, unwrapApi } from '../../lib/axios'
import type { PaginatedData, WebhookConfig, WebhookCreate, WebhookUpdate } from '../../types/api'

export async function getWebhooks(page = 1, pageSize = 20): Promise<PaginatedData<WebhookConfig>> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  const response = await apiClient.get(`/webhooks?${params.toString()}`)
  return unwrapApi<PaginatedData<WebhookConfig>>(response)
}

export async function createWebhook(payload: WebhookCreate): Promise<WebhookConfig> {
  const response = await apiClient.post('/webhooks', payload)
  return unwrapApi<WebhookConfig>(response)
}

export async function updateWebhook(id: number, payload: WebhookUpdate): Promise<WebhookConfig> {
  const response = await apiClient.put(`/webhooks/${id}`, payload)
  return unwrapApi<WebhookConfig>(response)
}

export async function deleteWebhook(id: number): Promise<void> {
  await apiClient.delete(`/webhooks/${id}`)
}

export async function testWebhook(id: number): Promise<{ success: boolean; message: string }> {
  const response = await apiClient.post(`/webhooks/${id}/test`)
  return unwrapApi<{ success: boolean; message: string }>(response)
}
