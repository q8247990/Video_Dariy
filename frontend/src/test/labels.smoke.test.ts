import { describe, expect, it } from 'vitest'

import { memberRoleLabel, petRoleLabel } from '../features/home-profile/labels'

describe('home-profile labels（vitest 冒烟）', () => {
  it('已知成员角色映射为确定性中文标签', () => {
    expect(memberRoleLabel('father')).toBe('爸爸')
    expect(memberRoleLabel('elder')).toBe('老人')
    expect(petRoleLabel('cat')).toBe('猫')
  })

  it('未知键回退为原始值', () => {
    expect(memberRoleLabel('unknown_role')).toBe('unknown_role')
  })

  it('jsdom 提供 localStorage 且用例间隔离', () => {
    localStorage.setItem('smoke_key', '1')
    expect(localStorage.getItem('smoke_key')).toBe('1')
  })

  it('上一个用例写入的 localStorage 已被清空', () => {
    expect(localStorage.getItem('smoke_key')).toBeNull()
  })
})
