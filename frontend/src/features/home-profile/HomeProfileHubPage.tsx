import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'

export function HomeProfileHubPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  
  const profileEntries = [
    {
      key: 'overview',
      title: t('home_profile.hub_overview_title', '家庭整体档案'),
      description: t('home_profile.hub_overview_desc', '维护家庭背景、关注重点和系统表达风格。'),
      target: '/home-profile/overview',
    },
    {
      key: 'members',
      title: t('home_profile.hub_members_title', '家庭成员'),
      description: t('home_profile.hub_members_desc', '维护成员称呼、关系和个体特征。'),
      target: '/home-profile/members',
    },
    {
      key: 'pets',
      title: t('home_profile.hub_pets_title', '宠物档案'),
      description: t('home_profile.hub_pets_desc', '维护宠物名称、类型和日常特征。'),
      target: '/home-profile/pets',
    },
  ]

  return (
    <div>
      <PageHeader title={t('home_profile.title', '家庭档案')} subtitle={t('home_profile.hub_subtitle', '先完善整体档案，再补充成员和宠物信息')} />

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
