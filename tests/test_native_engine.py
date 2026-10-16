import json
from types import SimpleNamespace

from custom_components.ha_ai_tools import models
from custom_components.ha_ai_tools.engine import ConversationEngine
from tests.test_engine import Client, reply, user
from tests.test_manager import manager


async def test_native_profile_controls_model_parameters_and_prompt():
    m = manager()
    await m.async_load()
    client = Client([reply("好的")])
    await ConversationEngine(m).async_run(
        user("你好"),
        client,
        "chosen",
        [],
        profile={
            "id": "default",
            "prompt": "请回答得很简短。",
            "temperature": 0.7,
            "top_p": 0.8,
            "max_tokens": 321,
            "max_function_calls_per_conversation": 3,
        },
    )
    request = client.requests[0]
    assert request["model"] == "chosen"
    assert request["temperature"] == 0.7 and request["top_p"] == 0.8
    assert request["max_tokens"] == 321
    assert "请回答得很简短。" in request["messages"][0]["content"]


async def test_assistant_profiles_isolate_context_but_default_keeps_legacy_key():
    m = manager()
    await m.async_load()
    client = Client([reply("好的"), reply("请说明对象")])
    engine = ConversationEngine(m)
    await engine.async_run(user("先谈客厅灯"), client, "m", [], profile={"id": "default"})
    await engine.async_run(user("把它关闭"), client, "m", [], profile={"id": "second"})
    assert models.session_key("a", None) in m.data["sessions"]
    assert all(x.get("content") != "先谈客厅灯" for x in client.requests[1]["messages"])
    assert len(m.data["sessions"]) == 2


async def test_legacy_function_protocol_returns_function_result_and_closes_budget():
    m = manager()
    await m.async_load()
    value = {
        "role": "assistant",
        "content": None,
        "function_call": {"name": "get_weather", "arguments": json.dumps({"location": "家"})},
    }
    first = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(model_dump=lambda **kwargs: value))]
    )
    client = Client([first, reply("天气已查询")])
    answer = await ConversationEngine(m).async_run(
        user("查询天气"),
        client,
        "m",
        [],
        profile={"id": "default", "use_tools": False, "max_function_calls_per_conversation": 1},
    )
    assert answer == "天气已查询"
    assert "functions" in client.requests[0] and "tools" not in client.requests[0]
    assert client.requests[1]["function_call"] == "none"
    assert client.requests[1]["messages"][-1]["role"] == "function"
    assert client.requests[1]["messages"][-1]["name"] == "get_weather"


async def test_context_clear_threshold_preserves_reminders_and_memory():
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "喝水", "in_minutes": 60}, "stable")
    key = models.session_key("a", None)
    m.data["sessions"][key] = {
        "updated_at": models.now().isoformat(),
        "summary": "旧摘要",
        "turns": [{"user": "旧问题", "assistant": "旧回答", "actions": []}],
    }
    client = Client([reply("好")])
    await ConversationEngine(m).async_run(
        user("继续"),
        client,
        "m",
        [],
        profile={"id": "default", "context_threshold": 1, "context_truncate_strategy": "clear"},
    )
    assert not any(x.get("content") == "旧问题" for x in client.requests[0]["messages"])
    assert "旧摘要" not in client.requests[0]["messages"][0]["content"]
    assert len(m.data["sessions"][key]["turns"]) == 1
    assert m.data["reminders"] == [r]


async def test_completion_token_parameter_for_reasoning_model():
    m = manager()
    await m.async_load()
    client = Client([reply("好")])
    await ConversationEngine(m).async_run(
        user("你好"), client, "o3-mini", [], profile={"id": "default", "max_tokens": 345}
    )
    assert client.requests[0]["max_completion_tokens"] == 345
    assert "max_tokens" not in client.requests[0]
    assert "temperature" not in client.requests[0]


async def test_reasoning_model_auxiliary_memory_and_summary_parameters():
    m = manager()
    await m.async_load()
    engine = ConversationEngine(m)
    client = Client([reply('{"memories":[]}'), reply("摘要")])
    await engine._auto_memory(client, "o3-mini", "我喜欢26度", "family", 0)
    session = {"turns": [{"user": "u", "assistant": "a"}] * 13, "summary": ""}
    await engine._summarize(client, "o3-mini", session)
    for request in client.requests:
        assert "max_completion_tokens" in request
        assert "temperature" not in request and "max_tokens" not in request


async def test_completion_event_contains_compatible_messages_and_response():
    m = manager()
    await m.async_load()
    fired = []
    m.hass.bus = SimpleNamespace(async_fire=lambda name, data: fired.append((name, data)))
    client = Client([reply("好的")])
    await ConversationEngine(m).async_run(user("你好"), client, "m", [])
    name, data = fired[-1]
    assert name == "ha_ai_tools.conversation.finished"
    assert data["messages"][-1]["content"] == "好的"
    assert data["user_input"]["text"] == "你好"
    assert data["response"]["choices"][0]["message"]["content"] == "好的"


async def test_failed_mutation_is_not_replayed_within_same_turn(monkeypatch):
    from custom_components.ha_ai_tools import engine as module

    m = manager()
    await m.async_load()
    side_effects = []

    async def partial(*args):
        side_effects.append("changed")
        raise ValueError("后续操作失败")

    monkeypatch.setattr(module, "async_tool", partial)
    call = {
        "id": "one",
        "type": "function",
        "function": {"name": "execute_services", "arguments": "{}"},
    }
    client = Client([reply(None, [call]), reply(None, [{**call, "id": "two"}]), reply("部分失败")])
    await ConversationEngine(m).async_run(user("执行"), client, "m", [])
    assert side_effects == ["changed"]
