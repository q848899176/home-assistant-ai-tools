import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from custom_components.ha_ai_tools.engine import ConversationEngine
from custom_components.ha_ai_tools.functions import FunctionExecutor
from custom_components.ha_ai_tools.tools import async_tool
from tests.test_engine import Client, reply, user
from tests.test_manager import manager


@pytest.fixture
async def broadcast_runtime():
    m = manager()
    await m.async_load()
    m.data["settings"]["outputs"] = ["assist_satellite.voice"]
    actor = SimpleNamespace(is_active=True, permissions=SimpleNamespace(check_entity=lambda *args: True))

    async def get_user(identifier):
        return actor if identifier == "actor" else None

    m.hass.auth = SimpleNamespace(async_get_user=get_user)
    calls = []

    async def call(domain, service, data, **kwargs):
        calls.append((domain, service, data))

    m.hass.services.async_call = call
    return m, actor, calls


async def test_broadcast_defaults_to_nabu_without_creating_reminders(broadcast_runtime):
    m, _, calls = broadcast_runtime
    before = deepcopy(m.data)
    result = await async_tool(m, "broadcast_message", {"message": "即时播报测试。"}, user("请播报测试", user_id="actor"), "one")
    assert calls == [("assist_satellite", "announce", {"entity_id": "assist_satellite.voice", "message": "即时播报测试。"})]
    assert result["service_accepted"] is True
    assert result["delivery"] == {"assist_satellite.voice": "delivered"}
    assert m.data == before


async def test_explicit_targets_do_not_change_defaults(broadcast_runtime):
    m, _, calls = broadcast_runtime
    await async_tool(m, "broadcast_message", {"message": "测试", "outputs": ["text.speaker_play_text", "text.speaker_play_text"]}, user("请在指定设备播报", user_id="actor"), "one")
    assert calls == [("text", "set_value", {"entity_id": "text.speaker_play_text", "value": "测试"})]
    assert m.settings["outputs"] == ["assist_satellite.voice"]


@pytest.mark.parametrize("outputs", [[], ["media_player.unknown"], ["assist_satellite.voice", "text.unknown_play_text"]])
async def test_invalid_targets_reject_whole_request(broadcast_runtime, outputs):
    m, _, calls = broadcast_runtime
    with pytest.raises(ValueError):
        await async_tool(m, "broadcast_message", {"message": "测试", "outputs": outputs}, user("播报", user_id="actor"), "one")
    assert calls == []


async def test_control_permission_is_checked_for_all_targets_before_sending(broadcast_runtime):
    m, actor, calls = broadcast_runtime
    actor.permissions.check_entity = lambda entity, policy: entity == "assist_satellite.voice"
    with pytest.raises(ValueError, match="权限"):
        await async_tool(m, "broadcast_message", {"message": "测试", "outputs": ["assist_satellite.voice", "text.speaker_play_text"]}, user("播报", user_id="actor"), "one")
    assert calls == []


async def test_missing_actor_is_rejected(broadcast_runtime):
    m, _, calls = broadcast_runtime
    with pytest.raises(ValueError, match="身份"):
        await async_tool(m, "broadcast_message", {"message": "测试"}, user("播报", user_id="missing"), "one")
    assert calls == []


async def test_registered_voice_can_broadcast_without_user_identity(broadcast_runtime, monkeypatch):
    from homeassistant.helpers import device_registry

    m, _, calls = broadcast_runtime
    monkeypatch.setattr(device_registry, "async_get", lambda _: SimpleNamespace(async_get=lambda identifier: object() if identifier == "nabu" else None))
    result = await async_tool(m, "broadcast_message", {"message": "测试"}, user("播报", device="nabu"), "one")
    assert result["service_accepted"] is True and len(calls) == 1
    calls.clear()
    with pytest.raises(ValueError, match="登记"):
        await async_tool(m, "broadcast_message", {"message": "测试"}, user("播报", device="unknown"), "two")
    assert calls == []


@pytest.mark.parametrize("message", ["", "好" * 1001, "我的密码是FAKE_SECRET_VALUE"])
async def test_invalid_content_is_not_sent(broadcast_runtime, message):
    m, _, calls = broadcast_runtime
    with pytest.raises(ValueError):
        await async_tool(m, "broadcast_message", {"message": message}, user("播报", user_id="actor"), "one")
    assert calls == []


async def test_offline_target_is_reported_without_fallback(broadcast_runtime):
    m, _, calls = broadcast_runtime
    m.hass.states.get("assist_satellite.voice").state = "unavailable"
    result = await async_tool(m, "broadcast_message", {"message": "测试"}, user("播报", user_id="actor"), "one")
    assert result["service_accepted"] is False
    assert result["delivery"] == {"assist_satellite.voice": "failed"}
    assert calls == []


@pytest.mark.parametrize("mixed", [False, True])
async def test_speech_testing_uses_trusted_nabu_targets(broadcast_runtime, monkeypatch, mixed):
    from tests.test_speech_testing import policy

    m, _, calls = broadcast_runtime
    p, store, run = await policy(monkeypatch)
    m.speech = SimpleNamespace(testing=p, store=store)
    m.data["settings"]["outputs"] = ["text.speaker_play_text"]
    args = {"message": "测试", "test_id": "forged"}
    if mixed:
        args["outputs"] = ["assist_satellite.voice", "text.speaker_play_text"]
        with pytest.raises(ValueError, match="Nabu"):
            await async_tool(m, "broadcast_message", args, user("播报", device="nabu", user_id="actor"), "one")
        assert calls == []
    else:
        result = await async_tool(m, "broadcast_message", args, user("播报", device="nabu", user_id="actor"), "one")
        assert result["delivery"] == {run["targets"][0]: "delivered"}
        assert calls[0][2]["entity_id"] == "assist_satellite.voice"


@pytest.mark.parametrize("legacy", [False, True])
async def test_both_tool_protocols_support_broadcast_and_do_not_repeat(broadcast_runtime, legacy):
    m, _, calls = broadcast_runtime
    function = {"name": "broadcast_message", "arguments": json.dumps({"message": "测试"})}
    if legacy:
        value = {"role": "assistant", "content": None, "function_call": function}
        first = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(model_dump=lambda **kw: value))])
    else:
        first = reply(None, [{"id": "one", "type": "function", "function": function}])
    client = Client([first, first, reply("已提交播报。")])
    await ConversationEngine(m).async_run(user("播报测试", user_id="actor"), client, "model", [], profile={"use_tools": not legacy})
    assert len(calls) == 1
    specs = client.requests[0]["functions"] if legacy else [t["function"] for t in client.requests[0]["tools"]]
    assert any(s["name"] == "broadcast_message" for s in specs)


async def test_speech_test_allows_broadcast_tool_through_engine(broadcast_runtime, monkeypatch):
    from tests.test_speech_testing import policy

    m, _, calls = broadcast_runtime
    p, store, _ = await policy(monkeypatch)
    m.speech = SimpleNamespace(testing=p, store=store)
    call = {"id": "one", "type": "function", "function": {"name": "broadcast_message", "arguments": '{"message":"测试"}'}}
    await ConversationEngine(m).async_run(user("播报测试", device="nabu", user_id="actor"), Client([reply(None, [call]), reply("已提交播报")]), "model", [])
    assert len(calls) == 1 and calls[0][2]["entity_id"] == "assist_satellite.voice"


async def test_unsupported_service_error_points_to_broadcast_tool(broadcast_runtime):
    m, _, calls = broadcast_runtime
    with pytest.raises(ValueError, match="broadcast_message"):
        await FunctionExecutor(m)._service_plan({"domain": "assist_satellite", "service": "announce", "service_data": {"entity_id": "assist_satellite.voice", "message": "测试"}}, user("播报", user_id="actor"))
    assert calls == []


async def test_current_satellite_speaks_exact_message_in_its_pipeline(broadcast_runtime):
    m, _, calls = broadcast_runtime
    speaker = user("请播报测试", user_id="actor")
    speaker.satellite_id = "assist_satellite.voice"
    call = {"id": "one", "type": "function", "function": {"name": "broadcast_message", "arguments": '{"message":"原样播报这句话。"}'}}
    client = Client([reply(None, [call]), reply("改写后的解释")])
    answer = await ConversationEngine(m).async_run(speaker, client, "model", [])
    assert answer == "原样播报这句话。"
    assert calls == []  # HA announce would cancel this satellite's active pipeline.
    assert len(client.requests) == 1


async def test_other_targets_still_receive_announcement_when_current_satellite_replies(broadcast_runtime):
    m, _, calls = broadcast_runtime
    speaker = user("请在这两台设备播报", user_id="actor")
    speaker.satellite_id = "assist_satellite.voice"
    result = await async_tool(m, "broadcast_message", {"message": "测试", "outputs": ["assist_satellite.voice", "text.speaker_play_text"]}, speaker, "one")
    assert calls == [("text", "set_value", {"entity_id": "text.speaker_play_text", "value": "测试"})]
    assert result["response_message"] == "测试"
    assert result["delivery"]["assist_satellite.voice"] == "via_response"


async def test_reply_reports_failure_of_other_target(broadcast_runtime):
    m, _, calls = broadcast_runtime
    m.hass.states.get("text.failed_play_text").state = "unavailable"
    speaker = user("请播报测试", user_id="actor")
    speaker.satellite_id = "assist_satellite.voice"
    call = {"id": "one", "type": "function", "function": {"name": "broadcast_message", "arguments": '{"message":"测试","outputs":["assist_satellite.voice","text.failed_play_text"]}'}}
    client = Client([reply(None, [call]), reply("都成功了")])
    answer = await ConversationEngine(m).async_run(speaker, client, "model", [])
    assert answer == "测试\n另有1台设备播报失败。"
    assert calls == []
