import { describe, expect, it } from 'vitest'

import enUS from '../locales/en-US.json'
import zhCN from '../locales/zh-CN.json'

type JsonTree = { [key: string]: string | JsonTree }

function flattenKeys(tree: JsonTree, prefix = ''): string[] {
  return Object.entries(tree).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') {
      return flattenKeys(value, path)
    }
    return [path]
  })
}

describe('locale parity（zh-CN / en-US）', () => {
  const zhKeys = flattenKeys(zhCN).sort()
  const enKeys = flattenKeys(enUS).sort()

  it('两份语言目录键集合完全一致', () => {
    const missingInEn = zhKeys.filter((key) => !enKeys.includes(key))
    const missingInZh = enKeys.filter((key) => !zhKeys.includes(key))
    expect({ missingInEn, missingInZh }).toEqual({ missingInEn: [], missingInZh: [] })
  })

  it('所有词条均为非空字符串', () => {
    for (const tree of [zhCN, enUS]) {
      for (const key of flattenKeys(tree)) {
        const value = key.split('.').reduce<unknown>((node, part) => {
          if (node !== null && typeof node === 'object') {
            return (node as Record<string, unknown>)[part]
          }
          return undefined
        }, tree)
        expect(typeof value, `key ${key} 应为字符串`).toBe('string')
        expect((value as string).length, `key ${key} 不应为空`).toBeGreaterThan(0)
      }
    }
  })
})
