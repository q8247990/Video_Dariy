import { expect, test } from '@playwright/test'

// 冒烟：应用根路径可加载，未登录时落到登录页并渲染登录卡片。
// 文案存在 zh-CN/en-US 两种可能（取决于浏览器 locale 检测），两者皆为合法基线。
test('app root renders login shell without backend dependency', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login/)
  const heading = page.locator('.auth-card h1')
  await expect(heading).toBeVisible()
  await expect(heading).toHaveText(/家庭监控智能分析后台|Home Video AI Analysis Console/)
  await expect(page.locator('.auth-card form')).toBeVisible()
})
