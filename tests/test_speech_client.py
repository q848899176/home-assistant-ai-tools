import asyncio
import io
import wave

import httpx
import pytest


def wave_bytes():
    b = io.BytesIO()
    with wave.open(b, "wb") as f:
        f.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        f.writeframes(b"\0\0" * 240)
    return b.getvalue()


async def test_protocol_paths_auth_and_final_text():
    from custom_components.ha_ai_tools.speech.client import SpeechClient
    from tests.test_speech_config import config

    requests = []

    def serve(r):
        requests.append(r)
        if r.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "asr"}, {"id": "tts", "streaming": False}]})
        if r.url.path == "/health":
            return httpx.Response(200, json={"state": "ready"})
        if r.url.path.endswith("/transcriptions"):
            return httpx.Response(200, json={"text": "下午三点喝水"})
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wave_bytes())

    c = SpeechClient(config(), httpx.AsyncClient(transport=httpx.MockTransport(serve)))
    assert (await c.async_probe())["streaming"] is False
    assert await c.async_transcribe(b"audio", language="zh-CN") == "下午三点喝水"
    extension, audio = await c.async_synthesize("你好", language="zh", options={"voice": "Serena"})
    assert extension == "wav" and audio == wave_bytes()
    assert all(r.url.path.count("/v1") <= 1 for r in requests)
    assert all(r.headers["Authorization"] == "Bearer PRIVATE_SENTINEL" for r in requests)
    await c.async_close()


@pytest.mark.parametrize("response", [httpx.Response(401), httpx.Response(200, json={"text": ""}), httpx.Response(302, headers={"Location": "http://other.invalid/"})])
async def test_failure_never_returns_empty_text_or_follows_redirect(response):
    from custom_components.ha_ai_tools.speech.client import SpeechClient
    from tests.test_speech_config import config

    c = SpeechClient(config(), httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)))
    with pytest.raises(ValueError) as exc:
        await c.async_transcribe(b"audio", language="zh")
    assert "PRIVATE_SENTINEL" not in str(exc.value)
    await c.async_close()


async def test_invalid_content_type_and_truncated_wav_are_rejected():
    from custom_components.ha_ai_tools.speech.client import SpeechClient
    from tests.test_speech_config import config

    for content_type, body in [("application/json", b"{}"), ("audio/wav", wave_bytes()[:44])]:
        c = SpeechClient(config(), httpx.AsyncClient(transport=httpx.MockTransport(lambda _, ct=content_type, data=body: httpx.Response(200, headers={"Content-Type": ct}, content=data))))
        with pytest.raises(ValueError):
            await c.async_synthesize("你好", language="zh", options={"voice": "Serena"})
        await c.async_close()


async def test_timeout_and_cancellation_do_not_call_fallback():
    from custom_components.ha_ai_tools.speech.client import SpeechClient
    from tests.test_speech_config import config

    calls = []

    async def serve(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout")

    c = SpeechClient(config(), httpx.AsyncClient(transport=httpx.MockTransport(serve)))
    with pytest.raises(ValueError):
        await c.async_transcribe(b"audio", language="zh")
    assert len(calls) == 1
    await c.async_close()

    async def slow(request):
        await asyncio.sleep(99)

    c = SpeechClient(config(), httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    task = asyncio.create_task(c.async_transcribe(b"audio", language="zh"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await c.async_close()


async def test_stream_refuses_non_wav_audio_before_yielding():
    from custom_components.ha_ai_tools.speech.client import SpeechClient
    from tests.test_speech_config import config

    value = config() | {"verified": {"streaming": True}}
    client = SpeechClient(value, httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=b"mp3")
    )))
    try:
        _, stream = await client.async_stream_synthesize("你好", language="zh", options={"voice": "Serena"})
        with pytest.raises(ValueError):
            _ = [chunk async for chunk in stream]
    finally:
        await client.async_close()
