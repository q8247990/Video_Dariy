import { useMemo, useState } from 'react'
import { NavLink, Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
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

const topLinks = [
  { to: '/dashboard', label: '首页', icon: LayoutDashboard },
  { to: '/chat', label: '问答助手', icon: MessagesSquare },
  { to: '/events', label: '事件与回放', icon: FileSearch2 },
  { to: '/daily-summaries', label: '每日日报', icon: ScrollText },
]

const homeProfileChildren = [
  { to: '/home-profile/overview', label: '家庭整体档案', icon: House },
  { to: '/home-profile/members', label: '家庭成员', icon: Users },
  { to: '/home-profile/pets', label: '宠物档案', icon: PawPrint },
]

const settingsChildren = [
  { to: '/system-config', label: '日报与提醒', icon: SlidersHorizontal },
  { to: '/video-sources', label: '视频源管理', icon: Camera },
  { to: '/providers', label: '模型连接', icon: Bot },
  { to: '/system-status', label: '运行状态详情', icon: Activity },
  { to: '/tasks', label: '运行记录', icon: ListChecks },
  { to: '/webhooks', label: '外部通知', icon: BellRing },
]

export function DashboardLayout() {
  const location = useLocation()
  const navigate = useNavigate()
  const token = useAuthStore((state) => state.token)
  const bootstrapped = useAuthStore((state) => state.bootstrapped)
  const username = useAuthStore((state) => state.username)
  const clearAuth = useAuthStore((state) => state.clearAuth)
  const theme = useThemeStore((state) => state.theme)
  const homeProfileActive = useMemo(() => {
    return location.pathname === '/home-profile' || homeProfileChildren.some((item) => location.pathname.startsWith(item.to))
  }, [location.pathname])
  const settingsActive = useMemo(() => {
    if (location.pathname === '/settings') {
      return true
    }
    return settingsChildren.some((item) => location.pathname.startsWith(item.to))
  }, [location.pathname])
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
                <span>设置总览</span>
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
          <button onClick={onLogout}>退出登录</button>
        </header>
        <section className="page-content">
          <Outlet />
        </section>
      </main>
    </div>
  )
}
