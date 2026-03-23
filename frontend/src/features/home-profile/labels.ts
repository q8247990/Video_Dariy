const FAMILY_TAG_LABELS: Record<string, string> = {
  has_pet: '有宠物',
  has_child: '有孩子',
  has_elder: '有老人',
}

const FOCUS_POINT_LABELS: Record<string, string> = {
  pet_status: '宠物状态',
  member_inout: '成员出入',
  stranger_or_stay: '陌生人/异常停留',
  elder_safety: '老人安全',
  child_activity: '孩子活动',
  daily_summary: '日常摘要',
}

const SYSTEM_STYLE_LABELS: Record<string, string> = {
  concise_summary: '简洁纪要型',
  family_companion: '家庭陪伴型',
  focus_alert: '重点关注型',
}

const MEMBER_ROLE_LABELS: Record<string, string> = {
  father: '爸爸',
  mother: '妈妈',
  child: '孩子',
  elder: '老人',
  other_member: '其他成员',
}

const PET_ROLE_LABELS: Record<string, string> = {
  cat: '猫',
  dog: '狗',
  other_pet: '其他',
}

const AGE_GROUP_LABELS: Record<string, string> = {
  child: '儿童',
  adult: '成人',
  elder: '老人',
}

function withFallback(dict: Record<string, string>, value: string): string {
  return dict[value] ?? value
}

export function familyTagLabel(value: string): string {
  return withFallback(FAMILY_TAG_LABELS, value)
}

export function focusPointLabel(value: string): string {
  return withFallback(FOCUS_POINT_LABELS, value)
}

export function systemStyleLabel(value: string): string {
  return withFallback(SYSTEM_STYLE_LABELS, value)
}

export function memberRoleLabel(value: string): string {
  return withFallback(MEMBER_ROLE_LABELS, value)
}

export function petRoleLabel(value: string): string {
  return withFallback(PET_ROLE_LABELS, value)
}

export function ageGroupLabel(value: string): string {
  return withFallback(AGE_GROUP_LABELS, value)
}
