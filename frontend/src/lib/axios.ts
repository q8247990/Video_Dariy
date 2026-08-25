import axios from 'axios'
import { recoverFromUnauthorized } from './authRecovery'
import { useAuthStore } from '../store/authStore'
import { useLocaleStore } from '../store/localeStore'

type ApiResponse<T> = {
  code: number
  message: string
  data: T
}

type ApiErrorBody = {
  message?: unknown
  detail?: unknown
}

export class ApiError extends Error {
  readonly status: number | undefined

  constructor(message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function getApiErrorMessage(data: unknown, fallback: string): string {
  if (typeof data !== 'object' || data === null) {
    return fallback
  }

  const body = data as ApiErrorBody
  if (typeof body.message === 'string' && body.message) {
    return body.message
  }
  if (typeof body.detail === 'string' && body.detail) {
    return body.detail
  }
  return fallback
}

export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 12_000,
})

apiClient.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  const locale = useLocaleStore.getState().locale
  if (locale) {
    config.headers['Accept-Language'] = locale
  }
  return config
})

apiClient.interceptors.response.use(
  (response) => {
    const payload = response.data as ApiResponse<unknown>
    if (typeof payload?.code === 'number' && payload.code !== 0) {
      if (payload.code === 4011) {
        recoverFromUnauthorized()
      }
        return Promise.reject(new ApiError(payload.message || '请求失败', response.status))
    }
    return response
  },
  (error: unknown) => {
    if (axios.isAxiosError(error)) {
      if (error.response?.status === 401) {
        recoverFromUnauthorized()
      }
      const message = getApiErrorMessage(error.response?.data, error.message || '网络错误')
      return Promise.reject(new ApiError(message, error.response?.status))
    }
    return Promise.reject(new Error('未知错误'))
  },
)

export function unwrapApi<T>(response: { data: ApiResponse<T> }): T {
  return response.data.data
}
