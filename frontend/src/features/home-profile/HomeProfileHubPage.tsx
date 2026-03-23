import { useNavigate } from 'react-router-dom'
import { PageHeader } from '../../components/common/PageHeader'

const profileEntries = [
  {
    key: 'overview',
    title: '家庭整体档案',
    description: '维护家庭背景、关注重点和系统表达风格。',
    target: '/home-profile/overview',
  },
  {
    key: 'members',
    title: '家庭成员',
    description: '维护成员称呼、关系和个体特征。',
    target: '/home-profile/members',
  },
  {
    key: 'pets',
    title: '宠物档案',
    description: '维护宠物名称、类型和日常特征。',
    target: '/home-profile/pets',
  },
]

export function HomeProfileHubPage() {
  const navigate = useNavigate()

  return (
    <div>
      <PageHeader title="家庭档案" subtitle="先完善整体档案，再补充成员和宠物信息" />

      <article className="card">
        <div className="settings-grid">
          {profileEntries.map((entry) => (
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
