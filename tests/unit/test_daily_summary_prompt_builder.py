from src.services.prompt_builder.v2.daily_summary import build_daily_data_input_prompt


def test_build_daily_data_input_prompt_compacts_repeated_subject_events() -> None:
    repeated_events = [
        {
            "event_id": idx,
            "event_type": "pet_activity",
            "title": "玳瑁猫行走",
            "summary": "一只玳瑁猫进入画面并行走。",
            "importance_level": "medium",
            "recognition_status": "confirmed",
        }
        for idx in range(1, 31)
    ]
    subject_sections = [
        {
            "subject_name": "嘟嘟",
            "subject_type": "pet",
            "related_event_count": len(repeated_events),
            "related_event_summaries": repeated_events,
        }
    ]
    attention_candidates = [
        {
            "event_id": 901,
            "event_type": "scene_attention_needed",
            "title": "植物遮挡",
            "summary": "画面左侧和中间被植物叶片遮挡，影响观察。",
            "importance_level": "medium",
        },
        {
            "event_id": 902,
            "event_type": "scene_attention_needed",
            "title": "植物遮挡",
            "summary": "画面左侧和中间被植物叶片遮挡，影响观察。",
            "importance_level": "medium",
        },
    ]

    prompt = build_daily_data_input_prompt(
        subject_sections=subject_sections,
        missing_subjects=["西瓜"],
        attention_candidates=attention_candidates,
    )

    assert "数据输入（紧凑版）" in prompt
    assert "对象=嘟嘟(pet)" in prompt
    assert "次数=30" in prompt
    assert prompt.count("玳瑁猫行走") == 1
    assert "未命中对象：西瓜" in prompt
    assert "植物遮挡" in prompt
    assert "次数=2" in prompt


def test_build_daily_data_input_prompt_limits_subject_clusters() -> None:
    events = [
        {
            "event_id": idx,
            "event_type": f"pet_activity_{idx}",
            "title": f"事件标题{idx}",
            "summary": f"事件摘要{idx}",
            "importance_level": "medium" if idx % 2 else "low",
            "recognition_status": "confirmed",
        }
        for idx in range(1, 40)
    ]
    prompt = build_daily_data_input_prompt(
        subject_sections=[
            {
                "subject_name": "栗子",
                "subject_type": "pet",
                "related_event_count": len(events),
                "related_event_summaries": events,
            }
        ],
        missing_subjects=[],
        attention_candidates=[],
    )

    cluster_lines = [line for line in prompt.splitlines() if line.strip().startswith("01.")]
    assert cluster_lines

    numbered_cluster_lines = [
        line for line in prompt.splitlines() if line.startswith("  ") and ". [" in line
    ]
    assert len(numbered_cluster_lines) <= 16
