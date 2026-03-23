import { apiClient, unwrapApi } from '../../lib/axios'
import type { LoginResponse } from '../../types/api'

export async function login(username: string, password: string): Promise<LoginResponse> {
  const response = await apiClient.post('/auth/login', { username, password })
  return unwrapApi<LoginResponse>(response)
}
