import { apiClient, unwrapApi } from '../../lib/axios'
import type {
  PaginatedData,
  Provider,
  ProviderCreate,
  ProviderUpdate,
  ProviderUsageDailyItem,
} from '../../types/api'

export type ProviderQuery = {
  page: number
  pageSize: number
  providerType: string
}

export async function getProviders(query: ProviderQuery): Promise<PaginatedData<Provider>> {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
  })
  if (query.providerType) {
    params.set('provider_type', query.providerType)
  }
  const response = await apiClient.get(`/providers?${params.toString()}`)
  return unwrapApi<PaginatedData<Provider>>(response)
}

export async function createProvider(payload: ProviderCreate): Promise<Provider> {
  const response = await apiClient.post('/providers', payload)
  return unwrapApi<Provider>(response)
}

export async function updateProvider(id: number, payload: ProviderUpdate): Promise<Provider> {
  const response = await apiClient.put(`/providers/${id}`, payload)
  return unwrapApi<Provider>(response)
}

export async function setDefaultVisionProvider(id: number): Promise<void> {
  await apiClient.post(`/providers/${id}/set-default-vision`)
}

export async function setDefaultQaProvider(id: number): Promise<void> {
  await apiClient.post(`/providers/${id}/set-default-qa`)
}

export async function testProvider(id: number): Promise<void> {
  await apiClient.post(`/providers/${id}/test`)
}

export async function deleteProvider(id: number): Promise<void> {
  await apiClient.delete(`/providers/${id}`)
}

export async function getProviderDailyUsage(days = 7): Promise<ProviderUsageDailyItem[]> {
  const response = await apiClient.get(`/providers/usage/daily?days=${days}`)
  return unwrapApi<ProviderUsageDailyItem[]>(response)
}
