from types import SimpleNamespace

import pytest

from tests.test_manager import manager


async def test_device_voice_uses_media_and_default_uses_message(monkeypatch):
    from custom_components.ha_ai_tools.speech import broadcast
    from tests.test_speech_pipeline import setup

    m = manager()
    _, store, _, _, _ = await setup()
    store.data["device_voices"]["assist_satellite.voice"] = {"tts_entity_id": "tts.local", "language": "zh", "voice": "Serena", "speed": 1.0}
    m.speech = SimpleNamespace(store=store)
    calls = []
    async def call(*args, **kwargs):
        calls.append(args)
    m.hass.services.async_call = call
    monkeypatch.setattr(broadcast, "make_media", lambda *args: "media-source://tts/test")
    assert await m.async_broadcast("你好", ["assist_satellite.voice"]) == {"assist_satellite.voice": "delivered"}
    assert calls[0][2]["media_id"] == "media-source://tts/test"
    assert "message" not in calls[0][2]
    store.data["device_voices"].clear()
    await m.async_broadcast("你好", ["assist_satellite.voice"])
    assert calls[1][2]["message"] == "你好"


async def test_policy_rejects_whole_broadcast_before_any_service(monkeypatch):
    from tests.test_speech_testing import policy

    p, store, run = await policy(monkeypatch)
    m = manager()
    m.speech = SimpleNamespace(store=store, testing=p)
    calls = []
    async def call(*args, **kwargs):
        calls.append(args)
    m.hass.services.async_call = call
    with pytest.raises(ValueError):
        await m.async_broadcast("测试", ["assist_satellite.voice", "text.speaker_play_text"], test_id=run["id"])
    assert calls == []


async def test_existing_text_protocol_still_works_with_simulated_services():
    m = manager()
    calls = []
    async def call(*args, **kwargs):
        calls.append(args)
    m.hass.services.async_call = call
    assert (await m.async_broadcast("好" * 450, ["text.speaker_play_text"]))["text.speaker_play_text"] == "delivered"
    assert [len(c[2]["value"]) for c in calls] == [200, 200, 50]
