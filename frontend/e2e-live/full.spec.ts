import { expect, test } from '@playwright/test'
import type { ConsoleMessage, Page } from '@playwright/test'

/**
 * 针对 docker 完整部署（frontend: http://localhost:8226, 反代 /api -> backend）
 * 的端到端功能测试。所有请求都打到真实后端与真实 PostgreSQL，不做任何 mock。
 *
 * 约定：
 * - 语言固定 zh-CN（playwright.live.config.ts 中 use.locale）。
 * - 数据来自已存在的开发库（1 视频源 / 175 Session / 197 事件 / 12 日报 / 2 Provider）。
 * - 写操作只做「幂等保存」（系统配置、家庭档案）与「弹窗开关」，不创建垃圾数据。
 */

const SHOT_DIR = 'e2e-live-artifacts/screenshots'
const ADMIN_USER = process.env.HM_ADMIN_USER ?? 'admin'
const ADMIN_PASS = process.env.HM_ADMIN_PASS ?? '123456'

type IssueBucket = {
  consoleErrors: string[]
  pageErrors: string[]
  httpErrors: { status: number; url: string }[]
}

function trackIssues(page: Page): IssueBucket {
  const bucket: IssueBucket = { consoleErrors: [], pageErrors: [], httpErrors: [] }
  page.on('console', (msg: ConsoleMessage) => {
    if (msg.type() === 'error') bucket.consoleErrors.push(msg.text())
  })
  page.on('pageerror', (err) => bucket.pageErrors.push(String(err)))
  page.on('response', (res) => {
    const status = res.status()
    if (status >= 400) bucket.httpErrors.push({ status, url: res.url() })
  })
  return bucket
}

/** 过滤掉与功能无关的噪声（favicon 之类）。 */
function meaningfulHttpErrors(bucket: IssueBucket) {
  return bucket.httpErrors.filter((e) => !/favicon\.ico/.test(e.url))
}

async function login(page: Page): Promise<void> {
  await page.goto('/login')
  await expect(page.locator('.auth-card h1')).toBeVisible()
  await page.getByLabel('用户名').fill(ADMIN_USER)
  await page.getByLabel('密码').fill(ADMIN_PASS)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/dashboard/)
  await expect(page.locator('.page-header h1')).toBeVisible()
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${SHOT_DIR}/${name}.png`, fullPage: true })
}

async function closeDialog(page: Page): Promise<void> {
  const cancel = page.locator('.dialog').getByRole('button', { name: '取消' })
  try {
    await cancel.click({ timeout: 3_000 })
  } catch {
    await page.locator('.dialog-mask').click({ position: { x: 5, y: 5 } })
  }
  await expect(page.locator('.dialog-mask')).toHaveCount(0)
}

// ---------------------------------------------------------------------------
// 1. 登录与鉴权
// ---------------------------------------------------------------------------
test.describe.serial('登录与鉴权', () => {
  test('登录页在未登录时可渲染', async ({ page }) => {
    await page.goto('/')
    await expect(page).toHaveURL(/\/login/)
    await expect(page.locator('.auth-card h1')).toHaveText('家庭监控智能分析后台')
    await expect(page.locator('.auth-card form')).toBeVisible()
    await shot(page, '01-login')
  })

  test('错误密码给出错误提示且不进入系统', async ({ page }) => {
    await page.goto('/login')
    await page.getByLabel('用户名').fill(ADMIN_USER)
    await page.getByLabel('密码').fill('definitely-wrong-password')
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await expect(page.locator('.api-error')).toBeVisible()
    await expect(page).toHaveURL(/\/login/)
  })

  test('正确凭据登录进入仪表盘', async ({ page }) => {
    await login(page)
    await expect(page.locator('.page-header h1')).toContainText('仪表盘')
    await expect(page.locator('.topbar')).toContainText('欢迎回来')
    await shot(page, '02-dashboard-after-login')
  })

  test('未登录访问受保护路由会跳回登录页', async ({ page }) => {
    await page.goto('/events')
    await expect(page).toHaveURL(/\/login/)
  })
})

// ---------------------------------------------------------------------------
// 2. 全路由渲染巡检（页面渲染 + 无 API 错误 + 无 5xx）
// ---------------------------------------------------------------------------
type RouteSpec = { name: string; path: string; title?: string | RegExp }

const ROUTES: RouteSpec[] = [
  { name: '03-dashboard', path: '/dashboard', title: '仪表盘' },
  { name: '04-chat', path: '/chat', title: '视频问答' },
  { name: '05-events', path: '/events', title: '视频片段' },
  { name: '06-daily-summaries', path: '/daily-summaries', title: '家庭日报' },
  { name: '07-sessions', path: '/sessions', title: '录像片段列表' },
  { name: '08-tasks', path: '/tasks', title: '任务状态' },
  { name: '09-system-config', path: '/system-config', title: '系统配置' },
  { name: '10-video-sources', path: '/video-sources', title: '录像来源' },
  { name: '11-providers', path: '/providers', title: '模型服务' },
  { name: '12-system-status', path: '/system-status', title: '系统概况' },
  { name: '13-webhooks', path: '/webhooks', title: 'Webhooks' },
  { name: '14-settings', path: '/settings', title: '系统设置' },
  { name: '15-home-profile-hub', path: '/home-profile', title: '家庭画像' },
  { name: '16-home-profile-overview', path: '/home-profile/overview', title: '家庭画像' },
  { name: '17-home-members', path: '/home-profile/members', title: '家庭成员' },
  { name: '18-home-pets', path: '/home-profile/pets', title: '宠物档案' },
  { name: '19-onboarding-welcome', path: '/onboarding', title: '首次设置' },
  { name: '20-onboarding-video', path: '/onboarding/basic/video' },
  { name: '21-onboarding-provider', path: '/onboarding/basic/provider' },
  { name: '22-onboarding-summary-time', path: '/onboarding/basic/summary-time' },
  { name: '23-onboarding-basic-done', path: '/onboarding/basic/done' },
  { name: '24-onboarding-personalize-profile', path: '/onboarding/personalize/profile' },
  { name: '25-onboarding-personalize-camera', path: '/onboarding/personalize/camera-notes' },
  { name: '26-onboarding-personalize-style', path: '/onboarding/personalize/style' },
  { name: '27-onboarding-personalize-done', path: '/onboarding/personalize/done' },
]

test.describe.serial('全路由渲染巡检', () => {
  for (const route of ROUTES) {
    test(`渲染 ${route.path}`, async ({ page }) => {
      await login(page)
      const issues = trackIssues(page)

      await page.goto(route.path)
      const h1 = page.locator('.page-header h1')
      await expect(h1).toBeVisible()
      await expect(h1).not.toHaveText('')
      if (route.title) {
        await expect(h1).toContainText(route.title)
      }
      // 页面主内容已渲染，且没有顶层 API 错误提示。
      await expect(page.locator('.content-area')).toBeVisible()
      await expect(page.locator('.api-error')).toHaveCount(0)

      await shot(page, route.name)

      expect(issues.pageErrors, `未捕获的前端异常: ${issues.pageErrors.join(' | ')}`).toEqual([])
      const httpErrors = meaningfulHttpErrors(issues)
      expect(
        httpErrors.filter((e) => e.status >= 500),
        `出现 5xx 响应: ${JSON.stringify(httpErrors)}`
      ).toEqual([])
    })
  }
})

// ---------------------------------------------------------------------------
// 3. 侧边栏导航点击
// ---------------------------------------------------------------------------
const NAV_LINKS: { href: string; title: string }[] = [
  { href: '/dashboard', title: '仪表盘' },
  { href: '/chat', title: '视频问答' },
  { href: '/events', title: '视频片段' },
  { href: '/daily-summaries', title: '家庭日报' },
  { href: '/home-profile/overview', title: '家庭画像' },
  { href: '/home-profile/members', title: '家庭成员' },
  { href: '/home-profile/pets', title: '宠物档案' },
  { href: '/settings', title: '系统设置' },
  { href: '/system-config', title: '系统配置' },
  { href: '/video-sources', title: '录像来源' },
  { href: '/providers', title: '模型服务' },
  { href: '/system-status', title: '系统概况' },
  { href: '/tasks', title: '任务状态' },
  { href: '/webhooks', title: 'Webhooks' },
]

test.describe.serial('侧边栏导航点击', () => {
  test('点击每个导航项可正确切换页面', async ({ page }) => {
    await login(page)
    for (const link of NAV_LINKS) {
      await page.locator(`a.nav-item[href="${link.href}"]`).click()
      await expect(page).toHaveURL(new RegExp(`${link.href.replace(/\//g, '\\/')}$`))
      await expect(page.locator('.page-header h1')).toContainText(link.title)
    }
    await shot(page, '30-nav-final-webhooks')
  })
})

// ---------------------------------------------------------------------------
// 4. 各功能页交互
// ---------------------------------------------------------------------------
test.describe.serial('功能交互', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('视频片段：筛选、展开事件、打开回放', async ({ page }) => {
    await page.goto('/events')
    await expect(page.locator('table.table')).toBeVisible()

    // 分析状态筛选 -> 成功
    await page.locator('.card.tool-row select').selectOption('success')
    await expect(page.locator('table.table')).toBeVisible()

    // 展开第一行事件
    const expandBtn = page.getByRole('button', { name: '展开事件' }).first()
    if (await expandBtn.count()) {
      await expandBtn.click()
      await expect(page.locator('table.table-sub').first()).toBeVisible({ timeout: 15_000 })
      await shot(page, '31-events-expanded')

      const detailBtn = page.getByRole('button', { name: '事件详情' }).first()
      if (await detailBtn.count()) {
        await detailBtn.click()
        await expect(page.locator('.page-header h1')).toContainText('事件详情')
        await shot(page, '32-event-detail')
        await page.getByRole('button', { name: '返回事件列表' }).click()
        await expect(page).toHaveURL(/\/events$/)
      }
    }

    // 打开回放弹窗
    const playbackBtn = page.getByRole('button', { name: '查看回放' }).first()
    if (await playbackBtn.count()) {
      await playbackBtn.click()
      await expect(page.locator('.dialog, .playback-card, [class*="playback"]').first()).toBeVisible({
        timeout: 15_000,
      })
      await shot(page, '33-events-playback')
    }
  })

  test('录像片段列表：回放与分页', async ({ page }) => {
    await page.goto('/sessions')
    await expect(page.locator('table.table')).toBeVisible()
    await expect(page.locator('.page-header h1')).toContainText('录像片段列表')

    const playbackBtn = page.getByRole('button', { name: '查看回放' }).first()
    if (await playbackBtn.count()) {
      await playbackBtn.click()
      await expect(page.locator('.playback-card')).toBeVisible({ timeout: 15_000 })
      await shot(page, '34-sessions-playback')
      await page.getByRole('button', { name: '关闭' }).click()
      await expect(page.locator('.playback-card')).toHaveCount(0)
    }
  })

  test('家庭日报：选择某日查看详情', async ({ page }) => {
    await page.goto('/daily-summaries')
    await expect(page.locator('.page-header h1')).toContainText('家庭日报')
    const rows = page.locator('table.table tbody tr')
    if ((await rows.count()) > 0) {
      await rows.first().click()
      await expect(page.locator('.summary-four-grid')).toBeVisible({ timeout: 15_000 })
      await shot(page, '35-daily-summary-detail')
    }
  })

  test('任务状态：状态筛选与立即刷新', async ({ page }) => {
    await page.goto('/tasks')
    await expect(page.locator('.page-header h1')).toContainText('任务状态')
    await expect(page.locator('table.table')).toBeVisible()
    // 任务页有两个下拉：第 1 个是「任务类型」，第 2 个才是「状态」。
    await page.locator('.card.tool-row select').nth(1).selectOption('failed')
    await expect(page.locator('table.table')).toBeVisible()
    await page.getByRole('button', { name: '立即刷新' }).click()
    await expect(page.locator('table.table')).toBeVisible()
    await shot(page, '36-tasks-filtered')
  })

  test('系统配置：幂等保存', async ({ page }) => {
    await page.goto('/system-config')
    await expect(page.locator('.config-form')).toBeVisible()
    await page.getByRole('button', { name: '保存配置' }).click()
    await expect(page.locator('.api-ok')).toContainText('系统配置已保存', { timeout: 15_000 })
    await shot(page, '37-system-config-saved')
  })

  test('录像来源：新增弹窗开关、查看状态、检查', async ({ page }) => {
    await page.goto('/video-sources')
    await expect(page.locator('table.table')).toBeVisible()

    // 新增弹窗
    await page.getByRole('button', { name: '添加录像来源' }).click()
    await expect(page.locator('.dialog-mask .dialog')).toBeVisible()
    await expect(page.locator('.dialog h3')).toContainText('添加录像来源')
    await shot(page, '38-video-source-dialog')
    await closeDialog(page)

    // 查看状态弹窗
    const statusBtn = page.getByRole('button', { name: '查看状态' }).first()
    if (await statusBtn.count()) {
      await statusBtn.click()
      await expect(page.locator('.dialog-mask')).toBeVisible({ timeout: 15_000 })
      await shot(page, '39-video-source-status')
      // 关闭：点击遮罩
      await page.locator('.dialog-mask').click({ position: { x: 5, y: 5 } })
      await expect(page.locator('.dialog-mask')).toHaveCount(0)
    }

    // 检查（真实调用后端 verify）
    const verifyBtn = page.getByRole('button', { name: '检查' }).first()
    if (await verifyBtn.count()) {
      await verifyBtn.click()
      await expect(page.locator('.api-ok')).toBeVisible({ timeout: 30_000 })
      await shot(page, '40-video-source-verified')
    }
  })

  test('模型服务：新增弹窗开关与测试连接', async ({ page }) => {
    await page.goto('/providers')
    await expect(page.locator('table.table')).toBeVisible()

    await page.getByRole('button', { name: '添加模型服务' }).click()
    await expect(page.locator('.dialog h3')).toContainText('添加模型服务')
    const dialogBox = await page.locator('.dialog').boundingBox()
    const viewport = page.viewportSize()!
    expect(dialogBox!.height, '弹窗高度必须不超过视口，底部按钮才可达').toBeLessThanOrEqual(viewport.height)
    await shot(page, '41-provider-dialog')
    await page.locator('.dialog').getByRole('button', { name: '取消' }).click({ timeout: 5_000 })
    await expect(page.locator('.dialog-mask')).toHaveCount(0)

    // 测试连接（外部 Provider，允许返回成功或失败，重点是不崩溃）
    const testBtn = page.getByRole('button', { name: '测试连接' }).first()
    if (await testBtn.count()) {
      await testBtn.click()
      await page.waitForTimeout(4_000)
      await expect(page.locator('.page-header h1')).toContainText('模型服务')
      await shot(page, '42-provider-tested')
    }
  })

  test('Webhooks：新增弹窗开关与样例卡片', async ({ page }) => {
    await page.goto('/webhooks')
    await expect(page.locator('.page-header h1')).toContainText('Webhooks')
    await expect(page.locator('.debug-sample-card').first()).toBeVisible()

    await page.getByRole('button', { name: '添加 Webhook' }).click()
    await expect(page.locator('.dialog h3')).toContainText('添加 Webhook')
    await shot(page, '43-webhook-dialog')
    await closeDialog(page)
  })

  test('家庭成员：新增弹窗开关', async ({ page }) => {
    await page.goto('/home-profile/members')
    await expect(page.locator('.page-header h1')).toContainText('家庭成员')
    await page.getByRole('button', { name: '新增成员' }).click()
    await expect(page.locator('.dialog h3')).toContainText('新增成员')
    await shot(page, '44-member-dialog')
    await closeDialog(page)
  })

  test('宠物档案：新增弹窗开关', async ({ page }) => {
    await page.goto('/home-profile/pets')
    await expect(page.locator('.page-header h1')).toContainText('宠物档案')
    await page.getByRole('button', { name: '新增宠物' }).click()
    await expect(page.locator('.dialog h3')).toContainText('新增宠物')
    await shot(page, '45-pet-dialog')
    await closeDialog(page)
  })

  test('家庭画像：幂等保存家庭档案', async ({ page }) => {
    await page.goto('/home-profile/overview')
    await expect(page.locator('.config-form')).toBeVisible()
    const saveBtn = page.getByRole('button', { name: '保存家庭档案' })
    if (await saveBtn.count()) {
      await saveBtn.click()
      await expect(page.locator('.api-ok')).toBeVisible({ timeout: 15_000 })
      await shot(page, '46-home-profile-saved')
    }
  })

  test('系统设置：主题与语言切换', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.locator('.page-header h1')).toContainText('系统设置')

    // 主题：深色
    await page.getByRole('button', { name: '深色', exact: true }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await shot(page, '47-theme-dark')

    // 主题：浅色
    await page.getByRole('button', { name: '浅色', exact: true }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

    // 语言：English -> 标题变为 Settings，再切回简体中文
    await page.getByRole('button', { name: 'English', exact: true }).click()
    await expect(page.locator('.page-header h1')).toContainText('Settings')
    await shot(page, '48-language-en')
    await page.getByRole('button', { name: '简体中文', exact: true }).click()
    await expect(page.locator('.page-header h1')).toContainText('系统设置')
  })

  test('视频问答：空问题校验 + 提交一次提问', async ({ page }) => {
    await page.goto('/chat')
    await expect(page.locator('.page-header h1')).toContainText('视频问答')
    const input = page.locator('.chat-ask-card textarea')
    await expect(input).toBeVisible()

    // 空问题：HTML required 阻止提交，URL 不变
    await page.getByRole('button', { name: '发送' }).click()
    await expect(page).toHaveURL(/\/chat$/)

    // 真实提问一次（模型不可达时允许出现错误提示，但不允许崩溃）
    await input.fill('今天家里有没有人？')
    await page.getByRole('button', { name: '发送' }).click()
    await expect(page.locator('.chat-answer-card')).toBeVisible({ timeout: 90_000 })
    await expect(page.locator('.chat-ask-card .api-error')).toHaveCount(0)
    await expect(page.locator('.chat-answer-card .chat-a')).not.toHaveText('')
    await shot(page, '49-chat-answer')
  })

  test('未知路由展示 404 页面', async ({ page }) => {
    await page.goto('/this-route-does-not-exist')
    await expect(page.locator('.auth-card h1')).toContainText('页面未找到')
    await shot(page, '50-not-found')
  })

  test('退出登录回到登录页', async ({ page }) => {
    await page.goto('/dashboard')
    await page.getByRole('button', { name: '退出登录' }).click()
    await expect(page).toHaveURL(/\/login/)
    await expect(page.locator('.auth-card form')).toBeVisible()
  })
})

// ---------------------------------------------------------------------------
// 5. 后端探针（经 nginx 反代）
// ---------------------------------------------------------------------------
test.describe.serial('后端探针', () => {
  test('公共入口 /health /livez /readyz /metrics 可用', async ({ request }) => {
    for (const path of ['/health', '/livez', '/readyz', '/metrics']) {
      const res = await request.get(path)
      expect(res.status(), `${path} 状态码`).toBe(200)
    }
    const ready = await (await request.get('/readyz')).json()
    expect(ready.checks.database).toBe(true)
    expect(ready.checks.redis).toBe(true)
    expect(ready.checks.alembic_head).toBe(true)
  })

  test('登录接口返回 token', async ({ request }) => {
    const res = await request.post('/api/v1/auth/login', {
      data: { username: ADMIN_USER, password: ADMIN_PASS },
    })
    expect(res.status()).toBe(200)
    const body = await res.json()
    expect(body.code).toBe(0)
    expect(body.data.token).toBeTruthy()
  })
})