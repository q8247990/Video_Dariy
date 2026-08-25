import { QueryClient } from '@tanstack/react-query'
import { ApiError } from './axios'

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) {
    return false
  }

  if (error instanceof ApiError) {
    return error.status === 429 || (error.status !== undefined && error.status >= 500)
  }

  return true
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: shouldRetry,
      refetchOnWindowFocus: false,
    },
  },
})
