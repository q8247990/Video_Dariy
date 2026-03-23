import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '../../components/common/PageHeader'
import { getHomeProfile } from '../home-profile/api'
import { useThemeStore } from '../../store/themeStore'

type SettingEntry = {
  key: string
  title: string
  description: string
  target: string
}

const commonEntries: SettingEntry[] = [
  {
    key: 'daily-summary',
    title: '日报与提醒',
    description: '设置日报生成时间和家庭常用提醒节奏。',
    target: '/system-config',
  },
  {
    key: 'onboarding',
    title: '初始化引导',
    description: '继续完成首次接入和个性化设置。',
    target: '/onboarding',
  },
  {
    key: 'chat',
    title: '问答助手',
    description: '查看历史问答并继续提问。',
    target: '/chat',
  },
]

const advancedEntries: SettingEntry[] = [
  {
    key: 'video-sources',
    title: '视频源管理',
    description: '配置摄像头目录、查看连接和扫描状态。',
    target: '/video-sources',
  },
  {
    key: 'providers',
    title: '模型连接',
    description: '管理用于识别和问答的模型服务。',
    target: '/providers',
  },
  {
    key: 'system-status',
    title: '运行状态详情',
    description: '查看任务失败情况和系统整体健康度。',
    target: '/system-status',
  },
  {
    key: 'tasks',
    title: '运行记录',
    description: '查看后台任务状态、失败信息和重试记录。',
    target: '/tasks',
  },
  {
    key: 'webhooks',
    title: '外部通知',
    description: '配置第三方通知回调与联调样例。',
    target: '/webhooks',
  },
]

export function SettingsPage() {
  const navigate = useNavigate()
  const theme = useThemeStore((state) => state.theme)
  const setTheme = useThemeStore((state) => state.setTheme)
  const homeProfileQuery = useQuery({
    queryKey: ['home-profile'],
    queryFn: getHomeProfile,
  })

  const tangtangEnabled = useMemo(() => {
    const name = homeProfileQuery.data?.assistant_name?.trim() ?? ''
    return name === '小糖' || name === '糖糖'
  }, [homeProfileQuery.data?.assistant_name])

  useEffect(() => {
    if (!homeProfileQuery.isSuccess) {
      return
    }
    if (!tangtangEnabled && theme === 'tangtang') {
      setTheme('light')
    }
  }, [homeProfileQuery.isSuccess, setTheme, tangtangEnabled, theme])

  return (
    <div>
      <PageHeader title="设置" subtitle="常用设置在上方，高级设置在下方" />

      <article className="card">
        <h3>常用设置</h3>
        <div className="theme-card">
          <p className="text-muted">外观皮肤（仅改变配色，不改变页面摆放）</p>
          <div className="theme-switcher">
            <button
              type="button"
              className={theme === 'light' ? 'ghost theme-btn theme-btn-active' : 'ghost theme-btn'}
              onClick={() => setTheme('light')}
            >
              浅色
            </button>
            <button
              type="button"
              className={theme === 'dark' ? 'ghost theme-btn theme-btn-active' : 'ghost theme-btn'}
              onClick={() => setTheme('dark')}
            >
              深色
            </button>
            {tangtangEnabled ? (
              <button
                type="button"
                className={theme === 'tangtang' ? 'ghost theme-btn theme-btn-active' : 'ghost theme-btn'}
                onClick={() => setTheme('tangtang')}
              >
                糖糖
              </button>
            ) : null}
          </div>
        </div>
        <div className="settings-grid">
          {commonEntries.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className="ghost settings-entry"
              onClick={() => navigate(entry.target)}
            >
              <strong>{entry.title}</strong>
              <span>{entry.description}</span>
            </button>
          ))}
        </div>
      </article>

      <article className="card settings-block-gap">
        <h3>高级设置</h3>
        <p className="text-muted">这些内容偏技术配置，通常在初次接入或排查问题时使用。</p>
        <div className="settings-grid">
          {advancedEntries.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className="ghost settings-entry"
              onClick={() => navigate(entry.target)}
            >
              <strong>{entry.title}</strong>
              <span>{entry.description}</span>
            </button>
          ))}
        </div>
      </article>
    </div>
  )
}
