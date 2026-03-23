from src.services.home_profile import build_home_context_prompt_text


def test_build_home_context_prompt_text_contains_core_fields() -> None:
    context = {
        "home_profile": {
            "home_name": "我的家庭",
            "family_tags": ["has_pet", "has_child"],
            "focus_points": ["pet_status"],
            "system_style": "family_companion",
            "style_preference_text": "多关注宠物状态",
            "assistant_name": "小布",
            "home_note": "白天比较安静",
        },
        "members": [
            {
                "name": "小米",
                "role_type": "child",
                "age_group": "child",
            }
        ],
        "pets": [
            {
                "name": "布丁",
                "role_type": "cat",
                "breed": "橘猫",
            }
        ],
    }

    text = build_home_context_prompt_text(context)

    assert "家庭名称: 我的家庭" in text
    assert "系统名称: 小布" in text
    assert "- 小米 (child, child)" in text
    assert "- 布丁 (cat, 橘猫)" in text
