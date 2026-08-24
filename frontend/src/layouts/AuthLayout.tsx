import { Navigate, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { LoadingBlock } from '../components/common/LoadingBlock'
import { useAuthStore } from '../store/authStore'

export function AuthLayout() {
  const { t } = useTranslation()
  const token = useAuthStore((state) => state.token)
  const bootstrapped = useAuthStore((state) => state.bootstrapped)
  if (!bootstrapped) {
    return <LoadingBlock text={t('common.init_login_loading')} />
  }
  if (token) {
    return <Navigate to="/dashboard" replace />
  }
  return <Outlet />
}
