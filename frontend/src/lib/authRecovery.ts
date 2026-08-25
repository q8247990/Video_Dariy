import { queryClient } from './queryClient'
import { useAuthStore } from '../store/authStore'

let isRecoveringAuthentication = false

export function recoverFromUnauthorized(): void {
  if (isRecoveringAuthentication) {
    return
  }
  isRecoveringAuthentication = true
  useAuthStore.getState().clearAuth()
  queryClient.clear()
  if (window.location.pathname !== '/login') {
    window.location.replace('/login')
  }
}

export function resetAuthenticationRecoveryForTests(): void {
  isRecoveringAuthentication = false
}
