from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_ai_tools import models
from tests.test_speech_pipeline import setup


async def policy(monkeypatch):
    from homeassistant.helpers import device_registry, entity_registry

    from custom_components.ha_ai_tools.speech.testing import TestPolicy

    _, store, _, _, _ = await setup()
    entities = {"assist_satellite.voice": SimpleNamespace(device_id="nabu"),
                "assist_satellite.imposter": SimpleNamespace(device_id="other")}
    devices = {"nabu": SimpleNamespace(manufacturer="Nabu Casa", model="Home Assistant Voice"),
               "other": SimpleNamespace(manufacturer="Xiaomi", model="Speaker")}
    monkeypatch.setattr(entity_registry, "async_get", lambda _: SimpleNamespace(async_get=entities.get))
    monkeypatch.setattr(device_registry, "async_get", lambda _: SimpleNamespace(async_get=devices.get))
    p = TestPolicy(SimpleNamespace(), store)
    run = await p.async_create_run("assist_satellite.voice")
    return p, store, run


async def test_mixed_targets_fail_before_any_send(monkeypatch):
    p, _, run = await policy(monkeypatch)
    with pytest.raises(ValueError):
        p.validate_targets(run["id"], ["assist_satellite.voice", "assist_satellite.imposter"])
    assert p.validate_targets(run["id"], ["assist_satellite.voice"]) == ["assist_satellite.voice"]


async def test_renamed_non_nabu_is_rejected(monkeypatch):
    p, _, _ = await policy(monkeypatch)
    with pytest.raises(ValueError):
        await p.async_create_run("assist_satellite.imposter")


async def test_delayed_test_reminder_keeps_policy_after_restart(monkeypatch):
    from custom_components.ha_ai_tools.speech.testing import TestPolicy

    p, store, run = await policy(monkeypatch)
    restarted = TestPolicy(p.hass, store)
    assert restarted.validate_targets(run["id"], ["assist_satellite.voice"])
    await restarted.async_finish_run(run["id"])
    with pytest.raises(ValueError):
        restarted.validate_targets(run["id"], ["assist_satellite.voice"])
    with pytest.raises(ValueError):
        restarted.validate_targets("unknown", ["assist_satellite.voice"])


async def test_expired_test_does_not_become_production_voice_request(monkeypatch):
    p, store, run = await policy(monkeypatch)
    await store.async_update(lambda d: d["test_runs"][run["id"]].update(expires_at=(models.now() - timedelta(seconds=1)).isoformat()))
    with pytest.raises(ValueError):
        p.test_id_for_device("nabu")


async def test_voice_created_test_reminder_inherits_trusted_test_id(monkeypatch):
    from custom_components.ha_ai_tools.tools import async_tool
    from tests.test_manager import manager

    p, store, run = await policy(monkeypatch)
    m = manager()
    await m.async_load()
    m.speech = SimpleNamespace(testing=p, store=store)
    u = SimpleNamespace(context=SimpleNamespace(user_id=None), device_id="nabu")
    r = await async_tool(m, "create_reminder", {"message": "测试喝水", "in_minutes": 1, "test_id": "forged"}, u, "req")
    assert r["test_id"] == run["id"]
    assert r["outputs"] == ["assist_satellite.voice"]


async def test_two_reminders_keep_their_outputs():
    from tests.test_manager import manager

    m = manager()
    await m.async_load()
    a = await m.async_add_reminder({"message": "甲", "in_minutes": 1, "outputs": ["assist_satellite.voice"]}, "a")
    b = await m.async_add_reminder({"message": "乙", "in_minutes": 2, "outputs": ["text.speaker_play_text"]}, "b")
    assert a["outputs"] != b["outputs"]


async def test_legacy_test_broadcast_requires_explicit_nabu_session():
    from custom_components.ha_ai_tools.api import async_action
    from tests.test_manager import manager

    m = manager()
    with pytest.raises(ValueError):
        await async_action(m, "test_broadcast", {})


async def test_speech_test_rejects_other_model_mutations_and_isolates_context(monkeypatch):
    from custom_components.ha_ai_tools.engine import ConversationEngine
    from tests.test_engine import Client, reply, user
    from tests.test_manager import manager

    p, store, run = await policy(monkeypatch)
    m = manager()
    await m.async_load()
    m.speech = SimpleNamespace(testing=p, store=store)
    calls = [{"id": "c", "type": "function", "function": {"name": "remember", "arguments": '{"key":"style","text":"喜欢简短回答","evidence":"我喜欢简短回答"}'}}]
    await ConversationEngine(m).async_run(user("我喜欢简短回答", device="nabu"), Client([reply(None, calls), reply("测试中不修改原有记忆")]), "fake", [])
    assert m.data["memories"] == []
    assert models.session_key("nabu", None) not in m.data["sessions"]
    assert all(s.get("test_id") == run["id"] for s in m.data["sessions"].values())
