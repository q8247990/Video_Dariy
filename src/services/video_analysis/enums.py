VIDEO_EVENT_TYPES = {
    "member_appear",
    "member_enter",
    "member_leave",
    "member_stay",
    "unknown_person_appear",
    "pet_appear",
    "pet_enter",
    "pet_stay",
    "pet_activity",
    "pet_rest",
    "pet_leave",
    "member_pet_interaction",
    "multi_entity_interaction",
    "abnormal_stay",
    "scene_attention_needed",
    "other",
}

ANALYSIS_NOTE_TYPES = {
    "identity_uncertain",
    "low_visibility",
    "occlusion",
    "scene_noise",
    "possible_false_positive",
    "no_significant_event",
}

EVENT_TYPE_DEFINITIONS: list[dict[str, str]] = [
    {"type": "member_appear", "desc": "家庭成员出现在画面中"},
    {"type": "member_enter", "desc": "成员进入画面或区域"},
    {"type": "member_leave", "desc": "成员离开画面或区域"},
    {"type": "member_stay", "desc": "成员持续停留"},
    {"type": "unknown_person_appear", "desc": "陌生人/未知人员出现"},
    {"type": "pet_appear", "desc": "宠物出现在画面中"},
    {"type": "pet_activity", "desc": "宠物活动（走动、玩耍等）"},
    {"type": "pet_rest", "desc": "宠物休息或静止"},
    {"type": "member_pet_interaction", "desc": "成员与宠物互动"},
    {"type": "multi_entity_interaction", "desc": "多个对象之间互动"},
    {"type": "abnormal_stay", "desc": "异常停留（长时间不动、异常位置等）"},
    {"type": "scene_attention_needed", "desc": "场景需要关注（异常光线、物品移动等）"},
]

IMPORTANCE_LEVELS = {"low", "medium", "high"}
RECOGNITION_STATUSES = {"confirmed", "suspected", "unknown"}
ACTIVITY_LEVELS = {"low", "medium", "high"}

EVENT_TYPE_ALIASES = {
    "pet_stay": "pet_rest",
    "pet_enter": "pet_appear",
    "person_appear": "unknown_person_appear",
    "person_enter": "unknown_person_appear",
    "person_leave": "unknown_person_appear",
    "member_activity": "member_stay",
    "pet_interact": "member_pet_interaction",
    "pet_interaction": "member_pet_interaction",
}

ANALYSIS_NOTE_TYPE_ALIASES = {
    "visibility_low": "low_visibility",
    "low_confidence": "possible_false_positive",
    "uncertain_identity": "identity_uncertain",
}


def normalize_event_type(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in VIDEO_EVENT_TYPES:
        return normalized
    return EVENT_TYPE_ALIASES.get(normalized, "other")


def normalize_analysis_note_type(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in ANALYSIS_NOTE_TYPES:
        return normalized
    return ANALYSIS_NOTE_TYPE_ALIASES.get(normalized, "scene_noise")


def normalize_activity_level(value: str | None, *, has_events: bool) -> str:
    normalized = (value or "").strip().lower()
    if normalized in ACTIVITY_LEVELS:
        return normalized
    return "medium" if has_events else "low"
