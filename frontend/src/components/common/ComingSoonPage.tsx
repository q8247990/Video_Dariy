import { PageHeader } from './PageHeader'

type ComingSoonPageProps = {
  title: string
  subtitle: string
}

export function ComingSoonPage({ title, subtitle }: ComingSoonPageProps) {
  return (
    <div>
      <PageHeader title={title} subtitle={subtitle} />
      <div className="card coming-soon">
        <p>该模块正在开发中，下一步将对接真实接口与详细交互。</p>
      </div>
    </div>
  )
}
