import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

from tests.test_speech_client import wave_bytes
from tests.test_speech_pipeline import setup


def request(user):
    return {"hass_user": user}


def test_non_admin_cannot_manage_or_fetch_test_audio():
    from custom_components.ha_ai_tools.speech.api import require_speech_admin

    for user in (None, SimpleNamespace(is_admin=False, id="u")):
        with pytest.raises(web.HTTPForbidden):
            require_speech_admin(request(user))
    assert require_speech_admin(request(SimpleNamespace(is_admin=True, id="admin"))) == "admin"


async def test_save_is_independent_from_apply_and_play(monkeypatch):
    from custom_components.ha_ai_tools.speech import api

    writes = []
    async def save(manager, payload):
        writes.append(payload)
        return {"saved": True}
    monkeypatch.setattr(api, "async_save_speech", save)
    m = SimpleNamespace(speech=SimpleNamespace(error=""))
    assert (await api.async_action(m, "save_speech", {"name": "连接"}, actor="admin"))["saved"]
    assert writes == [{"name": "连接"}]


def test_media_reference_contains_no_long_lived_secret_and_is_owner_bound():
    from custom_components.ha_ai_tools.speech.api import PreviewCache

    cache = PreviewCache()
    identifier = cache.add("admin1", wave_bytes())
    assert "PRIVATE_SENTINEL" not in identifier and "/" not in identifier
    assert cache.get(identifier, "admin1") == wave_bytes()
    with pytest.raises(web.HTTPForbidden):
        cache.get(identifier, "admin2")
    with pytest.raises(web.HTTPNotFound):
        cache.get("missing", "admin1")


def test_preview_expiry_releases_audio(monkeypatch):
    from custom_components.ha_ai_tools.speech import api

    clock = [100]
    monkeypatch.setattr(api, "monotonic", lambda: clock[0])
    cache = api.PreviewCache()
    identifier = cache.add("admin", wave_bytes())
    clock[0] = 161
    with pytest.raises(web.HTTPNotFound):
        cache.get(identifier, "admin")
    assert cache.items == {}


async def test_upload_cleans_temp_on_cancel():
    from custom_components.ha_ai_tools.speech.api import read_upload

    async def slow():
        await asyncio.sleep(99)
    part = SimpleNamespace(name="file", filename="audio.wav", read_chunk=slow)
    class Multipart:
        def __aiter__(self):
            async def parts():
                yield part
            return parts()
    r = SimpleNamespace(multipart=lambda: asyncio.sleep(0, result=Multipart()))
    task = asyncio.create_task(read_upload(r))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_pipeline_action_passes_exact_preview_and_no_credentials():
    from custom_components.ha_ai_tools.speech.api import async_action

    pm, store, _, writes, _ = await setup()
    m = SimpleNamespace(speech=SimpleNamespace(pipelines=pm, store=store, error=""))
    r = await async_action(m, "preview_speech_apply", {"pipelines": [{"id": "p", "fields": {"tts_voice": "Serena"}}]}, actor="admin")
    assert writes == [] and "PRIVATE_SENTINEL" not in str(r)
    result = await async_action(m, "apply_speech", {"preview_id": r["id"], "fingerprint": r["fingerprint"]}, actor="admin")
    assert result["applied"]


async def test_root_action_dispatches_speech_with_trusted_actor(monkeypatch):
    from custom_components.ha_ai_tools import api as root
    from custom_components.ha_ai_tools.speech import api as speech

    received = []
    async def action(*args, **kwargs):
        received.append(kwargs)
        return {"ok": True}
    monkeypatch.setattr(speech, "async_action", action)
    assert await root.async_action(SimpleNamespace(), "preview_tts", {}, actor="admin") == {"ok": True}
    assert received == [{"actor": "admin"}]


async def test_upload_valid_wav_requires_exact_duration_and_channels():
    from custom_components.ha_ai_tools.speech.audio import async_prepare_upload, pcm_to_wav

    data = pcm_to_wav(b"\x01\0" * 16000, sample_rate=16000, sample_width=2, channels=1)
    assert await async_prepare_upload(None, data, "audio/wav") == data
    with pytest.raises(ValueError):
        await async_prepare_upload(None, data[:44], "audio/wav")


async def test_preview_audio_expires_without_another_request():
    from custom_components.ha_ai_tools.speech.api import PreviewCache

    cache = PreviewCache(ttl=.01)
    cache.add("admin", wave_bytes())
    await asyncio.sleep(.02)
    assert cache.items == {}


def test_safe_snapshot_includes_real_assigned_speech_entity_ids(monkeypatch):
    from custom_components.ha_ai_tools.speech import api

    monkeypatch.setattr(api, "list_speech", lambda _: [{"id": "s", "api_key_configured": True}])
    monkeypatch.setattr(api, "_pipeline_list", lambda _: [])
    m = SimpleNamespace(entry=object(), speech=SimpleNamespace(store=None, entities={("s", "tts"): SimpleNamespace(entity_id="tts.assigned_name")}))
    value = api.snapshot(m)["configs"][0]
    assert value["tts_entity_id"] == "tts.assigned_name"
    assert "api_key" not in value
