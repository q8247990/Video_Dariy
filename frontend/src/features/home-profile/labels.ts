import type { TFunction } from 'i18next'

// 已知后端枚举对应的 i18n 键定义见 locales/zh-CN.json 与 en-US.json 的 home_profile 命名空间。
// 未知枚举值通过 i18next defaultValue（第二个参数）回退为原始值，避免渲染出断键。
function lookupLabel(t: TFunction, group: string, value: string): string {
  return t(`home_profile.${group}.${value}`, value)
}

export function familyTagLabel(t: TFunction, value: string): string {
  return lookupLabel(t, 'family_tag', value)
}

export function focusPointLabel(t: TFunction, value: string): string {
  return lookupLabel(t, 'focus_point', value)
}

export function systemStyleLabel(t: TFunction, value: string): string {
  return lookupLabel(t, 'system_style_label', value)
}

export function memberRoleLabel(t: TFunction, value: string): string {
  return lookupLabel(t, 'member_role', value)
}

export function petRoleLabel(t: TFunction, value: string): string {
  return lookupLabel(t, 'pet_role', value)
}

export function ageGroupLabel(t: TFunction, value: string): string {
  return lookupLabel(t, 'age_group', value)
}
