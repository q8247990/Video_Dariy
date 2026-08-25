import i18next, { type TFunction } from 'i18next'
import { beforeAll, describe, expect, it } from 'vitest'

import zhCN from '../locales/zh-CN.json'
import { memberRoleLabel, petRoleLabel } from '../features/home-profile/labels'

let t: TFunction

beforeAll(async () => {
  const instance = i18next.createInstance()
  await instance.init({
    resources: { 'zh-CN': { translation: zhCN } },
    lng: 'zh-CN',
    interpolation: { escapeValue: false },
  })
  t = instance.t
})

describe('home-profile labels（vitest 冒烟）', () => {
  it('已知成员角色映射为 zh-CN 目录中的中文标签', () => {
    expect(memberRoleLabel(t, 'father')).toBe('爸爸')
    expect(memberRoleLabel(t, 'elder')).toBe('老人')
    expect(petRoleLabel(t, 'cat')).toBe('猫')
  })

  it('未知键回退为原始值', () => {
    expect(memberRoleLabel(t, 'unknown_role')).toBe('unknown_role')
  })

  it('jsdom 提供 localStorage 且用例间隔离', () => {
    localStorage.setItem('smoke_key', '1')
    expect(localStorage.getItem('smoke_key')).toBe('1')
  })

  it('上一个用例写入的 localStorage 已被清空', () => {
    expect(localStorage.getItem('smoke_key')).toBeNull()
  })
})
