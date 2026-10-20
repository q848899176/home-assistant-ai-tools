import asyncio
import io
import wave
from types import SimpleNamespace

import pytest
from homeassistant.components import stt


def metadata(**changes):
    values = dict(language="zh", format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
                  bit_rate=stt.AudioBitRates.BITRATE_16, sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
                  channel=stt.AudioChannels.CHANNEL_MONO)
    return stt.SpeechMetadata(**{**values, **changes})


async def chunks(value):
    yield value


def entity():
    from custom_components.ha_ai_tools.stt import AIToolsSTTEntity
    from tests.test_speech_config import config

    calls = []

    async def transcribe(wav, **kwargs):
        calls.append(wav)
        return "提醒喝水"

    client = SimpleNamespace(async_transcribe=transcribe)
    runtime = SimpleNamespace(entry=SimpleNamespace(entry_id="root"), configs={"local": config()}, get_client=lambda _: client)
    return AIToolsSTTEntity("local", runtime), calls, client


async def test_pcm_is_wrapped_as_valid_wav():
    e, calls, _ = entity()
    pcm = b"\x01\x00" * 16000
    result = await e.async_process_audio_stream(metadata(), chunks(pcm))
    assert result.result == stt.SpeechResultState.SUCCESS
    with wave.open(io.BytesIO(calls[0]), "rb") as f:
        assert (f.getframerate(), f.getsampwidth(), f.getnchannels()) == (16000, 2, 1)
        assert f.readframes(16000) == pcm


async def test_empty_audio_never_calls_model():
    e, calls, _ = entity()
    assert (await e.async_process_audio_stream(metadata(), chunks(b""))).result == stt.SpeechResultState.ERROR
    assert calls == []


async def test_stt_refuses_unsupported_metadata():
    e, calls, _ = entity()
    assert (await e.async_process_audio_stream(metadata(language="en"), chunks(b"\x01\0" * 10))).result == stt.SpeechResultState.ERROR
    assert calls == []


async def test_stt_duration_and_byte_limits():
    from custom_components.ha_ai_tools.speech.audio import async_collect_pcm

    assert len(await async_collect_pcm(chunks(b"\x01\0" * 16000 * 60), metadata())) == 16000 * 60 * 2
    for size in [16000 * 60 * 2 + 2, 20 * 1024 * 1024 + 2]:
        with pytest.raises(ValueError):
            await async_collect_pcm(chunks(b"\x01" * size), metadata())


async def test_stt_cancel_closes_request():
    e, _, client = entity()

    async def slow(*args, **kwargs):
        await asyncio.sleep(99)

    client.async_transcribe = slow
    task = asyncio.create_task(e.async_process_audio_stream(metadata(), chunks(b"\x01\0" * 100)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stt_rename_keeps_unique_id():
    e, _, _ = entity()
    original = e.unique_id
    e.runtime.configs["local"]["name"] = "改名"
    assert e.unique_id == original == "root:speech:local:stt"


async def test_stt_error_leaks_no_audio_or_secret():
    e, _, client = entity()

    async def fail(*args, **kwargs):
        raise ValueError("PRIVATE_SENTINEL")

    client.async_transcribe = fail
    result = await e.async_process_audio_stream(metadata(), chunks(b"\x01\0" * 10))
    assert result.result == stt.SpeechResultState.ERROR
    assert "PRIVATE_SENTINEL" not in repr(result)
