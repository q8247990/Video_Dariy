import { describe, expect, it } from 'vitest'

import { parseRelatedEntities, parseRelatedEntity, type RelatedEntity } from '../types/api'

const EMPTY: RelatedEntity = {
  entity_type: null,
  display_name: null,
  matched_profile_name: null,
  recognition_status: null,
  confidence: null,
}

describe('parseRelatedEntity', () => {
  it('完整有效载荷原样保留各字段', () => {
    const parsed = parseRelatedEntity({
      entity_type: 'member',
      display_name: '爸爸',
      matched_profile_name: 'father',
      recognition_status: 'matched',
      confidence: 0.92,
    })
    expect(parsed).toEqual({
      entity_type: 'member',
      display_name: '爸爸',
      matched_profile_name: 'father',
      recognition_status: 'matched',
      confidence: 0.92,
    })
  })

  it('畸形载荷中的非字符串/非数字字段安全置空', () => {
    const parsed = parseRelatedEntity({
      entity_type: 42,
      display_name: { name: 'x' },
      matched_profile_name: ['a'],
      recognition_status: true,
      confidence: 'high',
    })
    expect(parsed).toEqual(EMPTY)
  })

  it('null / undefined / 非对象输入返回安全默认值且不抛异常', () => {
    expect(parseRelatedEntity(null)).toEqual(EMPTY)
    expect(parseRelatedEntity(undefined)).toEqual(EMPTY)
    expect(parseRelatedEntity('陌生人')).toEqual(EMPTY)
    expect(parseRelatedEntity(123)).toEqual(EMPTY)
    expect(parseRelatedEntity([])).toEqual(EMPTY)
  })

  it('未知 recognition_status 枚举值保留原始字符串（由渲染层兜底展示）', () => {
    const parsed = parseRelatedEntity({ recognition_status: 'brand_new_status' })
    expect(parsed.recognition_status).toBe('brand_new_status')
  })

  it('空字符串字段视为缺失', () => {
    const parsed = parseRelatedEntity({ display_name: '', matched_profile_name: '' })
    expect(parsed.display_name).toBeNull()
    expect(parsed.matched_profile_name).toBeNull()
  })

  it('confidence 非有限数字时置空', () => {
    expect(parseRelatedEntity({ confidence: Number.NaN }).confidence).toBeNull()
    expect(parseRelatedEntity({ confidence: Number.POSITIVE_INFINITY }).confidence).toBeNull()
  })
})

describe('parseRelatedEntities', () => {
  it('数组载荷逐项归一化', () => {
    const parsed = parseRelatedEntities([
      { entity_type: 'pet', display_name: '咪咪', recognition_status: 'matched' },
      'garbage',
    ])
    expect(parsed).toHaveLength(2)
    expect(parsed?.[0].display_name).toBe('咪咪')
    expect(parsed?.[1]).toEqual(EMPTY)
  })

  it('null / undefined / 非数组输入返回 null', () => {
    expect(parseRelatedEntities(null)).toBeNull()
    expect(parseRelatedEntities(undefined)).toBeNull()
    expect(parseRelatedEntities('oops')).toBeNull()
    expect(parseRelatedEntities({})).toBeNull()
  })
})
