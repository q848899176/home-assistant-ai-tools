import asyncio
from datetime import timedelta
from types import SimpleNamespace

from custom_components.ha_ai_tools import models
from custom_components.ha_ai_tools.engine import ConversationEngine
from tests.test_manager import manager


async def test_all_declared_tools_get_response_even_over_budget():
    m = manager()
    await m.async_load()
    calls = [
        {
            "id": f"c{i}",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"location":"家"}'},
        }
        for i in range(21)
    ]
    client = Client([reply(None, calls), reply("查询完成")])
    await ConversationEngine(m).async_run(user("天气如何"), client, "deepseek", [])
    returned = {x["tool_call_id"] for x in client.requests[1]["messages"] if x["role"] == "tool"}
    assert returned == {f"c{i}" for i in range(21)}


async def test_sensitive_tool_result_is_recursively_removed_before_persist():
    m = manager()
    await m.async_load()
    m.hass.states.get("weather.home").attributes["attribution"] = "password: FAKE_NEVER_STORE"
    calls = [
        {
            "id": "c",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"location":"家"}'},
        }
    ]
    await ConversationEngine(m).async_run(
        user("天气如何"), Client([reply(None, calls), reply("晴天")]), "deepseek", []
    )
    assert "FAKE_NEVER_STORE" not in str(m.store.data)


async def test_forget_invalidates_inflight_history():
    m = manager()
    await m.async_load()
    memory = await m.async_remember(
        {"key": "temp", "text": "喜欢26度", "evidence": "我喜欢26度"}, "我喜欢26度", "family"
    )
    key = models.session_key("a", None)
    m.data["sessions"][key] = {
        "updated_at": models.now().isoformat(),
        "summary": "喜欢26度",
        "turns": [{"user": "我喜欢26度", "assistant": "好", "actions": []}],
    }
    started, release = asyncio.Event(), asyncio.Event()

    class SlowClient(Client):
        async def create(self, **kwargs):
            started.set()
            await release.wait()
            return reply("好的")

    task = asyncio.create_task(
        ConversationEngine(m).async_run(user("接着说"), SlowClient([]), "deepseek", [])
    )
    await started.wait()
    await m.async_forget(memory["id"])
    release.set()
    await task
    assert "26度" not in str(m.store.data)


def reply(text, calls=None):
    value = {"role": "assistant", "content": text}
    if calls:
        value["tool_calls"] = calls
    msg = SimpleNamespace(content=text, model_dump=lambda **_: value)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class Client:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.replies)


def user(text, device="a", user_id=None):
    return SimpleNamespace(
        text=text, device_id=device, conversation_id=None, context=SimpleNamespace(user_id=user_id)
    )


async def test_voice_model_receives_speaker_names_beyond_default_selection():
    m = manager()
    await m.async_load()
    client = Client([reply("好的")])
    await ConversationEngine(m).async_run(
        user("在卧室播报"), client, "deepseek", [],
        [{"entity_id": "assist_satellite.voice", "name": "Nabu", "area": "卧室"}],
    )
    assert '"name": "Nabu"' in client.requests[0]["messages"][0]["content"]
    assert '"broadcast_devices"' in client.requests[0]["messages"][0]["content"]


async def test_voice_changes_only_requested_reminder_devices():
    import json

    m = manager()
    await m.async_load()
    first = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "first")
    await m.async_add_reminder({"message": "吃药", "in_minutes": 10}, "second")
    calls = [{"id": "change-speaker", "type": "function", "function": {
        "name": "update_reminder_outputs",
        "arguments": json.dumps({"id": first["id"], "outputs": ["assist_satellite.voice"]}),
    }}]
    await ConversationEngine(m).async_run(
        user("把喝水提醒改到Nabu播报"),
        Client([reply(None, calls), reply("已修改")]), "deepseek", [],
    )
    assert m.store.data["reminders"][0]["outputs"] == ["assist_satellite.voice"]
    assert m.store.data["reminders"][1]["outputs"] == ["text.speaker_play_text"]


async def test_followup_reuses_recent_context_after_new_wake_word():
    m = manager()
    await m.async_load()
    client = Client([reply("已选择客厅灯。"), reply("好的。")])
    engine = ConversationEngine(m)
    await engine.async_run(user("客厅灯"), client, "deepseek", [])
    await engine.async_run(user("把它关掉"), client, "deepseek", [])
    contents = [x["content"] for x in client.requests[1]["messages"]]
    assert "客厅灯" in contents


async def test_different_devices_do_not_share_recent_conversation():
    m = manager()
    await m.async_load()
    client = Client([reply("好的"), reply("哪个设备？")])
    engine = ConversationEngine(m)
    await engine.async_run(user("客厅灯", "a"), client, "deepseek", [])
    await engine.async_run(user("把它关掉", "b"), client, "deepseek", [])
    assert all(x.get("content") != "客厅灯" for x in client.requests[1]["messages"])


async def test_auto_preference_tool_persists_real_user_evidence():
    m = manager()
    await m.async_load()
    call = {
        "id": "call1",
        "type": "function",
        "function": {
            "name": "remember",
            "arguments": '{"key":"temperature","text":"喜欢26度","evidence":"我喜欢26度"}',
        },
    }
    client = Client([reply(None, [call]), reply("记住了。")])
    answer = await ConversationEngine(m).async_run(user("我喜欢26度"), client, "deepseek", [])
    assert answer == "记住了。"
    assert m.store.data["memories"][0]["text"] == "喜欢26度"
    assert client.requests[1]["messages"][-1]["role"] == "tool"


async def test_sensitive_conversation_is_not_persisted():
    m = manager()
    await m.async_load()
    await ConversationEngine(m).async_run(
        user("我的密码是supersecret"), Client([reply("不保存密码。")]), "deepseek", []
    )
    assert "supersecret" not in str(m.store.data)


async def test_restarted_engine_can_resume_recent_context():
    m = manager()
    await m.async_load()
    await ConversationEngine(m).async_run(
        user("明天提醒交电费"), Client([reply("几点？")]), "deepseek", []
    )
    restored = manager(m.store.data)
    await restored.async_load()
    client = Client([reply("好的")])
    await ConversationEngine(restored).async_run(user("上午九点"), client, "deepseek", [])
    assert any(x.get("content") == "明天提醒交电费" for x in client.requests[0]["messages"])


async def test_summary_failure_leaves_bounded_valid_history():
    m = manager()
    await m.async_load()
    key = models.session_key("a", None)
    m.data["sessions"][key] = {
        "updated_at": models.now().isoformat(),
        "summary": "",
        "turns": [{"user": "问" + str(i), "assistant": "答", "actions": []} for i in range(12)],
    }
    client = Client([reply("好的")])
    await ConversationEngine(m).async_run(user("接着说"), client, "deepseek", [])
    assert len(m.data["sessions"][key]["turns"]) <= 12


async def test_expired_session_does_not_execute_stale_reference():
    m = manager()
    await m.async_load()
    key = models.session_key("a", None)
    m.data["sessions"][key] = {
        "updated_at": (models.now() - timedelta(hours=1)).isoformat(),
        "summary": "",
        "turns": [{"user": "客厅灯", "assistant": "好", "actions": []}],
    }
    client = Client([reply("哪个设备？")])
    await ConversationEngine(m).async_run(user("把它关掉"), client, "deepseek", [])
    assert all(x.get("content") != "客厅灯" for x in client.requests[0]["messages"])
