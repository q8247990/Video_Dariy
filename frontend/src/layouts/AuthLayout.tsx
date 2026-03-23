import { Navigate, Outlet } from 'react-router-dom'
import { LoadingBlock } from '../components/common/LoadingBlock'
import { useAuthStore } from '../store/authStore'

export function AuthLayout() {
  const token = useAuthStore((state) => state.token)
  const bootstrapped = useAuthStore((state) => state.bootstrapped)
  if (!bootstrapped) {
    return <LoadingBlock text="登录状态初始化中..." />
  }
  if (token) {
    return <Navigate to="/dashboard" replace />
  }
  return <Outlet />
}
