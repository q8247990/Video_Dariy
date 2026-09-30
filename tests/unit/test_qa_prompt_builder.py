from src.services.prompt_builder.v2.qa_agent import build_qa_agent_prompt


def test_build_qa_agent_prompt_orders_static_before_dynamic() -> None:
    prompt = build_qa_agent_prompt(
        now_iso="2026-03-21T12:00:00+08:00",
        timezone="Asia/Shanghai",
        home_context={
            "home_profile": {"home_name": "温馨之家", "family_tags": [], "focus_points": ["安全"]},
            "members": [{"name": "爸爸", "role_type": "father"}],
            "pets": [{"name": "布丁", "role_type": "cat"}],
        },
        data_availability={
            "earliest_event_date": "2026-03-01",
            "latest_event_date": "2026-03-20",
            "total_event_count": 42,
        },
        locale="zh-CN",
    )

    assert prompt.index("规则:") < prompt.index("当前时间:")
    assert prompt.index("事件类型说明:") < prompt.index("当前时间:")
    assert prompt.index("家庭概况") < prompt.index("当前时间:")

    assert "member_appear" in prompt
    assert "温馨之家" in prompt
    assert "爸爸" in prompt and "布丁" in prompt
    assert "2026-03-21T12:00:00+08:00" in prompt
    assert "42" in prompt


def test_build_qa_agent_prompt_injects_english_directive() -> None:
    prompt = build_qa_agent_prompt(
        now_iso="2026-03-21T12:00:00+08:00",
        timezone="Asia/Shanghai",
        home_context={"home_profile": {}, "members": [], "pets": []},
        data_availability={},
        locale="en-US",
    )

    assert "Please respond in English." in prompt
    assert "已知主体: 无" in prompt
