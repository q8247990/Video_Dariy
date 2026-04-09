import { useMemo, useState } from 'react'
import { NavLink, Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  LayoutDashboard,
  MessagesSquare,
  FileSearch2,
  ScrollText,
  Settings,
  House,
  Users,
  Camera,
  Bot,
  BellRing,
  ListChecks,
  Activity,
  ChevronDown,
  SlidersHorizontal,
  PawPrint,
} from 'lucide-react'
import { LoadingBlock } from '../components/common/LoadingBlock'
import { useAuthStore } from '../store/authStore'
import { useThemeStore } from '../store/themeStore'

export function DashboardLayout() {
  const { t } = useTranslation()
  const location = useLocation()
  const navigate = useNavigate()
  const token = useAuthStore((state) => state.token)
  const bootstrapped = useAuthStore((state) => state.bootstrapped)
  const username = useAuthStore((state) => state.username)
  const clearAuth = useAuthStore((state) => state.clearAuth)
  const theme = useThemeStore((state) => state.theme)

  const topLinks = useMemo(() => [
    { to: '/dashboard', label: t('layouts.dashboard'), icon: LayoutDashboard },
    { to: '/chat', label: t('layouts.chat'), icon: MessagesSquare },
    { to: '/events', label: t('layouts.events'), icon: FileSearch2 },
    { to: '/daily-summaries', label: t('layouts.daily_summary'), icon: ScrollText },
  ], [t])

  const homeProfileChildren = useMemo(() => [
    { to: '/home-profile/overview', label: t('home_profile.title'), icon: House },
    { to: '/home-profile/members', label: '家庭成员', icon: Users },
    { to: '/home-profile/pets', label: '宠物档案', icon: PawPrint },
  ], [t])

  const settingsChildren = useMemo(() => [
    { to: '/system-config', label: '日报与提醒', icon: SlidersHorizontal },
    { to: '/video-sources', label: t('video_sources.title'), icon: Camera },
    { to: '/providers', label: t('providers.title'), icon: Bot },
    { to: '/system-status', label: t('system_status.title'), icon: Activity },
    { to: '/tasks', label: t('tasks.title'), icon: ListChecks },
    { to: '/webhooks', label: t('webhooks.title'), icon: BellRing },
  ], [t])

  const homeProfileActive = useMemo(() => {
    return location.pathname === '/home-profile' || homeProfileChildren.some((item) => location.pathname.startsWith(item.to))
  }, [location.pathname, homeProfileChildren])
  const settingsActive = useMemo(() => {
    if (location.pathname === '/settings') {
      return true
    }
    return settingsChildren.some((item) => location.pathname.startsWith(item.to))
  }, [location.pathname, settingsChildren])
  const [homeProfileExpandedState, setHomeProfileExpanded] = useState(true)
  const [settingsExpandedState, setSettingsExpanded] = useState(true)

  const homeProfileExpanded = homeProfileActive || homeProfileExpandedState
  const settingsExpanded = settingsActive || settingsExpandedState

  if (!bootstrapped) {
    return <LoadingBlock text="登录状态初始化中..." />
  }

  if (!token) {
    return <Navigate to="/login" replace />
  }

  const onLogout = () => {
    clearAuth()
    navigate('/login', { replace: true })
  }

  return (
    <div className="admin-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-title">
            {theme === 'tangtang' ? (
              <span className="brand-paw" aria-hidden="true">
                <PawPrint size={13} />
              </span>
            ) : null}
            <span>Video Diary</span>
          </span>
          <small>家庭监控智能平台</small>
        </div>
        <nav>
          {topLinks.map((link) => {
            const Icon = link.icon
            return (
              <NavLink
                to={link.to}
                key={link.to}
                className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}
              >
                <Icon size={16} />
                <span>{link.label}</span>
              </NavLink>
            )
          })}

          <button
            type="button"
            className={homeProfileActive ? 'nav-item nav-parent active' : 'nav-item nav-parent'}
            onClick={() => setHomeProfileExpanded((old) => !old)}
          >
            <House size={16} />
            <span>家庭档案</span>
            <ChevronDown size={14} className={homeProfileExpanded ? 'nav-chevron open' : 'nav-chevron'} />
          </button>

          {homeProfileExpanded ? (
            <div className="nav-sub-list">
              {homeProfileChildren.map((link) => {
                const Icon = link.icon
                return (
                  <NavLink
                    key={link.to}
                    to={link.to}
                    className={({ isActive }) =>
                      isActive ? 'nav-item nav-sub-item active' : 'nav-item nav-sub-item'
                    }
                  >
                    <Icon size={14} />
                    <span>{link.label}</span>
                  </NavLink>
                )
              })}
            </div>
          ) : null}

          <button
            type="button"
            className={settingsActive ? 'nav-item nav-parent active' : 'nav-item nav-parent'}
            onClick={() => setSettingsExpanded((old) => !old)}
          >
            <Settings size={16} />
            <span>设置</span>
            <ChevronDown size={14} className={settingsExpanded ? 'nav-chevron open' : 'nav-chevron'} />
          </button>

          {settingsExpanded ? (
            <div className="nav-sub-list">
              <NavLink
                to="/settings"
                className={({ isActive }) => (isActive ? 'nav-item nav-sub-item active' : 'nav-item nav-sub-item')}
              >
                <Settings size={14} />
                <span>{t('layouts.settings')}</span>
              </NavLink>
              {settingsChildren.map((link) => {
                const Icon = link.icon
                return (
                  <NavLink
                    key={link.to}
                    to={link.to}
                    className={({ isActive }) =>
                      isActive ? 'nav-item nav-sub-item active' : 'nav-item nav-sub-item'
                    }
                  >
                    <Icon size={14} />
                    <span>{link.label}</span>
                  </NavLink>
                )
              })}
            </div>
          ) : null}
        </nav>
      </aside>
      <main className="content-area">
        <header className="topbar">
          <div>
            <h2>家庭助手</h2>
            <p>欢迎回来，{username ?? '家人'}</p>
          </div>
          <button onClick={onLogout}>{t('layouts.logout')}</button>
        </header>
        <section className="page-content">
          <Outlet />
        </section>
      </main>
    </div>
  )
}
