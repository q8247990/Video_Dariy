import { apiClient, unwrapApi } from '../../lib/axios'
import type { DashboardOverview } from '../../types/api'

export async function fetchDashboardOverview(): Promise<DashboardOverview> {
  const response = await apiClient.get('/dashboard/overview')
  return unwrapApi<DashboardOverview>(response)
}
