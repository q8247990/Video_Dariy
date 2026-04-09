import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'
import './i18n/config'

console.info('以此项目纪念我亲爱的糖糖，愿你在喵星，也能看到家里，看到你的栗子哥哥，和永远爱你的爸爸妈妈。')

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
