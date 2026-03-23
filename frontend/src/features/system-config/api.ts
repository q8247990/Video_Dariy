import { apiClient, unwrapApi } from '../../lib/axios'
import type { SystemConfig } from '../../types/api'

export async function getSystemConfig(): Promise<SystemConfig> {
  const response = await apiClient.get('/system-config')
  return unwrapApi<SystemConfig>(response)
}

export async function updateSystemConfig(payload: SystemConfig): Promise<SystemConfig> {
  const response = await apiClient.put('/system-config', payload)
  return unwrapApi<SystemConfig>(response)
}
