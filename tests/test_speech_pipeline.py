import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.test_manager import Store


async def setup():
    from custom_components.ha_ai_tools.speech.pipeline import PipelineManager, SpeechStore

    backing = Store()
    store = SpeechStore(None, "root", store=backing)
    await store.async_load()
    pipelines = {"p": {"id": "p", "name": "旧助手", "conversation_engine": "conversation.keep",
                        "stt_engine": "stt.old", "stt_language": "zh-CN", "tts_engine": "tts.piper",
                        "tts_language": "zh_CN", "tts_voice": "old", "prefer_local_intents": True}}
    writes = []
    manager = PipelineManager(None, store)
    manager._read = lambda identifier: deepcopy(pipelines[identifier])
    async def write(identifier, fields):
        writes.append((identifier, fields))
        pipelines[identifier].update(fields)
    manager._write = write
    return manager, store, pipelines, writes, backing


async def preview(manager):
    return await manager.async_preview({"pipelines": [{"id": "p", "fields": {"tts_engine": "tts.local", "tts_voice": "Serena"}}]})


async def test_save_does_not_apply_pipeline():
    m, s, p, writes, _ = await setup()
    before = deepcopy(p)
    point = await preview(m)
    assert point["id"] in s.data["restore_points"]
    assert p == before and writes == []


async def test_stale_preview_is_rejected():
    m, _, p, writes, _ = await setup()
    point = await preview(m)
    p["p"]["tts_voice"] = "changed_elsewhere"
    with pytest.raises(ValueError):
        await m.async_apply(point["id"], point["fingerprint"])
    assert writes == []


async def test_restore_preserves_external_changes():
    m, _, p, _, _ = await setup()
    point = await preview(m)
    assert (await m.async_apply(point["id"], point["fingerprint"]))["applied"]
    p["p"]["tts_voice"] = "manual_voice"
    result = await m.async_restore(point["id"])
    assert result["status"] == "conflict"
    assert p["p"]["tts_voice"] == "manual_voice"
    assert p["p"]["tts_engine"] == "tts.piper"
    assert p["p"]["conversation_engine"] == "conversation.keep"


async def test_restart_recovers_interrupted_apply():
    from custom_components.ha_ai_tools.speech.pipeline import PipelineManager, SpeechStore

    m, store, p, _, backing = await setup()
    point = await preview(m)
    await m.async_apply(point["id"], point["fingerprint"])
    await store.async_update(lambda d: d["applications"][point["id"]].update(status="writing"))
    reloaded = SpeechStore(None, "root", store=backing)
    await reloaded.async_load()
    restarted = PipelineManager(None, reloaded)
    restarted._read, restarted._write = m._read, m._write
    assert (await restarted.async_recover_pending())[0]["status"] == "restored"
    assert p["p"]["tts_engine"] == "tts.piper"


async def test_snapshot_failure_never_changes_pipeline():
    m, _, _, writes, backing = await setup()
    async def fail(*args):
        raise OSError("disk full")
    backing.async_save = fail
    with pytest.raises(OSError):
        await preview(m)
    assert writes == []


async def test_partial_write_failure_compensates_and_preserves_unrelated_fields():
    m, _, p, writes, _ = await setup()
    point = await preview(m)
    original = m._write
    async def broken(identifier, fields):
        await original(identifier, fields)
        p[identifier]["prefer_local_intents"] = False
        if fields.get("tts_engine") == "tts.local":
            raise OSError("lost connection")
    m._write = broken
    result = await m.async_apply(point["id"], point["fingerprint"])
    assert result["applied"] is False
    assert p["p"]["tts_engine"] == "tts.piper"
    assert p["p"]["prefer_local_intents"] is False


async def test_unapproved_fields_and_secret_fields_are_rejected():
    m, _, _, writes, _ = await setup()
    for changes in [{"pipelines": [{"id": "p", "fields": {"conversation_engine": "other"}}]},
                    {"device_voices": {"assist_satellite.voice": {"api_key": "PRIVATE_SENTINEL"}}}]:
        with pytest.raises(ValueError):
            await m.async_preview(changes)
    assert writes == []


async def test_deleted_pipeline_returns_conflict_without_recreating():
    m, _, p, _, _ = await setup()
    point = await preview(m)
    await m.async_apply(point["id"], point["fingerprint"])
    del p["p"]
    assert (await m.async_restore(point["id"]))["status"] == "conflict"
    assert p == {}


async def test_duplicate_apply_is_idempotent_and_detects_later_changes():
    m, _, p, writes, _ = await setup()
    point = await preview(m)
    await m.async_apply(point["id"], point["fingerprint"])
    count = len(writes)
    assert (await m.async_apply(point["id"], point["fingerprint"]))["applied"]
    assert len(writes) == count
    p["p"]["tts_voice"] = "manual"
    with pytest.raises(ValueError):
        await m.async_apply(point["id"], point["fingerprint"])


async def test_never_applied_preview_cannot_restore_other_changes():
    m, _, p, writes, _ = await setup()
    point = await preview(m)
    p["p"]["tts_engine"] = "tts.local"
    with pytest.raises(ValueError):
        await m.async_restore(point["id"])
    assert writes == []


async def test_cancelled_apply_restores_written_fields():
    m, _, p, _, _ = await setup()
    point = await preview(m)
    original = m._write
    async def cancelled(identifier, fields):
        await original(identifier, fields)
        if fields.get("tts_engine") == "tts.local":
            raise asyncio.CancelledError
    m._write = cancelled
    with pytest.raises(asyncio.CancelledError):
        await m.async_apply(point["id"], point["fingerprint"])
    assert p["p"]["tts_engine"] == "tts.piper"


async def prepare_test_fixture(monkeypatch, *, manual_change=False, cancel_binding=False):
    from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
    from homeassistant.helpers import entity_registry

    manager, store, pipelines, _, _ = await setup()
    states = {"select.nabu": SimpleNamespace(state="原绑定", attributes={"options": []})}
    writes = []
    await store.async_update(lambda d: d["test_runs"].__setitem__("run", {"status": "active"}))

    async def create(fields):
        states["select.nabu"].attributes["options"].append(fields["name"])
        if manual_change:
            states["select.nabu"].state = "人工新绑定"
        return SimpleNamespace(id="test_pipeline")

    async def select(domain, service, data, **kwargs):
        writes.append(data["option"])
        states["select.nabu"].state = data["option"]
        if cancel_binding and data["option"].startswith("AI Tools Nabu"):
            raise asyncio.CancelledError

    registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(device_id="nabu"), async_get_entity_id=lambda *args: "select.nabu")
    monkeypatch.setattr(entity_registry, "async_get", lambda _: registry)
    manager.hass = SimpleNamespace(states=SimpleNamespace(get=states.get), services=SimpleNamespace(async_call=select), data={
        KEY_ASSIST_PIPELINE: SimpleNamespace(pipeline_devices={"nabu": SimpleNamespace(domain="esphome", unique_id_prefix="nabu")},
                                            pipeline_store=SimpleNamespace(async_create_item=create))})
    return manager, store, states, writes


async def test_test_binding_cancellation_immediately_restores_original(monkeypatch):
    manager, store, states, writes = await prepare_test_fixture(monkeypatch, cancel_binding=True)
    with pytest.raises(asyncio.CancelledError):
        await manager.async_prepare_test("run", "p", "assist_satellite.nabu")
    assert states["select.nabu"].state == "原绑定"
    assert store.data["applications"]["run"]["status"] == "restored"
    assert writes[-1] == "原绑定"


async def test_test_binding_keeps_manual_change_while_creating_pipeline(monkeypatch):
    manager, store, states, writes = await prepare_test_fixture(monkeypatch, manual_change=True)
    with pytest.raises(ValueError):
        await manager.async_prepare_test("run", "p", "assist_satellite.nabu")
    assert states["select.nabu"].state == "人工新绑定"
    assert writes == []
    assert store.data["applications"]["run"]["status"] == "conflict"
