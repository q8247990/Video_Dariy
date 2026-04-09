import axios from 'axios'
import { useAuthStore } from '../store/authStore'
import { useLocaleStore } from '../store/localeStore'

type ApiResponse<T> = {
  code: number
  message: string
  data: T
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
      return Promise.reject(new Error(payload.message || '请求失败'))
    }
    return response
  },
  (error: unknown) => {
    if (axios.isAxiosError(error)) {
      const message = error.response?.data?.message || error.message || '网络错误'
      return Promise.reject(new Error(message))
    }
    return Promise.reject(new Error('未知错误'))
  },
)

export function unwrapApi<T>(response: { data: ApiResponse<T> }): T {
  return response.data.data
}
