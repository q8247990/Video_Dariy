import { defineConfig, devices } from '@playwright/test'

// 使用不常见但固定的端口，配合 strictPort，避免与本地开发服务冲突；
// 测试服务器由 webServer 自动拉起 Vite dev server，无需真实后端/Redis/NAS。
const PORT = 5199
const BASE_URL = `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: BASE_URL,
    headless: true,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
})
