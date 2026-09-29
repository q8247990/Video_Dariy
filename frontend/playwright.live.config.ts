import { defineConfig, devices } from '@playwright/test'

// 针对「已在 docker 中完整部署」的真实环境做端到端测试。
// 与 playwright.config.ts（Vite dev + 无后端）不同，这里直接访问 nginx 托管的
// 前端 + 反代 /api，不做任何 mock。
const BASE_URL = process.env.HM_BASE_URL ?? 'http://localhost:8226'

export default defineConfig({
  testDir: './e2e-live',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: 0,
  // 用例会串行操作同一套真实数据；单 worker 避免相互干扰。
  workers: 1,
  reporter: [
    ['list'],
    ['json', { outputFile: 'e2e-live-artifacts/results.json' }],
    ['html', { outputFolder: 'e2e-live-artifacts/html', open: 'never' }],
  ],
  outputDir: 'e2e-live-artifacts/test-results',
  use: {
    baseURL: BASE_URL,
    headless: true,
    locale: 'zh-CN',
    // 以 root 运行时 Chromium 需要关闭沙箱，否则无法启动。
    launchOptions: { args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'] },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})