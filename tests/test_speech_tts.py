import asyncio
import io
import wave
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from tests.test_speech_client import wave_bytes
from tests.test_speech_config import config


def entity(**changes):
    from custom_components.ha_ai_tools.tts import AIToolsTTSEntity

    calls = []

    async def synth(text, **kwargs):
        calls.append((text, kwargs))
        return "wav", wave_bytes()

    client = SimpleNamespace(async_synthesize=synth)
    runtime = SimpleNamespace(entry=SimpleNamespace(entry_id="root"), configs={"local": config(**{"config_revision": "v1", "verified": {"streaming": False}, **changes})}, get_client=lambda _: client)
    return AIToolsTTSEntity("local", runtime), calls, client


def test_qwen_empty_voice_list_uses_verified_presets():
    e, _, _ = entity()
    assert {v.voice_id for v in e.async_get_supported_voices("zh")} >= {"Vivian", "Serena", "Uncle_Fu", "Dylan"}
    assert e.async_get_supported_voices("en") == []


def test_voice_revision_changes_cache_key():
    e, _, _ = entity()
    first = e.default_options.copy()
    e2, _, _ = entity(config_revision="v2")
    assert first != e2.default_options
    assert "voice" in e.supported_options


def test_no_fake_streaming_from_headers():
    e, _, _ = entity()
    assert e.async_supports_streaming_input() is False


def test_long_sentence_is_bounded():
    from custom_components.ha_ai_tools.speech.audio import split_text

    text = "这是一句很长的话" * 500 + "。第二句。"
    chunks = split_text(text)
    assert all(0 < len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == text


async def test_text_chunks_preserve_sentence_order():
    e, calls, _ = entity()
    text = "好" * 1001 + "。再见。"
    ext, data = await e.async_get_tts_audio(text, "zh", {"voice": "Vivian"})
    assert ext == "wav" and "".join(t for t, _ in calls) == text
    with wave.open(io.BytesIO(data), "rb") as f:
        assert f.getnframes() == 240 * len(calls)
    assert data.count(b"RIFF") == 1


async def test_partial_audio_never_replays():
    e, calls, client = entity()

    async def fail(text, **kwargs):
        calls.append(text)
        raise ValueError("partial audio")

    client.async_synthesize = fail
    with pytest.raises(HomeAssistantError):
        await e.async_get_tts_audio("你好", "zh", {})
    assert len(calls) == 1


async def test_stream_cancel_stops_upstream():
    e, _, client = entity()

    async def slow(text, **kwargs):
        await asyncio.sleep(99)

    client.async_synthesize = slow
    task = asyncio.create_task(e.async_get_tts_audio("你好", "zh", {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_disk_cache_disabled_by_default():
    from custom_components.ha_ai_tools.tts import media_options

    assert media_options()["cache"] is False


async def test_unknown_voice_and_unsupported_speed_rejected():
    e, calls, _ = entity()
    for options in [{"voice": "unknown"}, {"speed": 1.5}]:
        with pytest.raises(HomeAssistantError):
            await e.async_get_tts_audio("你好", "zh", options)
    assert calls == []


async def test_verified_stream_close_closes_upstream():
    from homeassistant.components.tts.entity import TTSAudioRequest

    e, _, client = entity(verified={"streaming": True})
    closed = []

    async def upstream():
        try:
            yield wave_bytes()
            await asyncio.sleep(99)
        finally:
            closed.append(True)

    async def synth(*args, **kwargs):
        return "wav", upstream()

    async def text():
        yield "你好"

    client.async_stream_synthesize = synth
    response = await e.async_stream_tts_audio(TTSAudioRequest("zh", {}, text()))
    assert await anext(response.data_gen) == wave_bytes()
    await response.data_gen.aclose()
    assert closed == [True]
